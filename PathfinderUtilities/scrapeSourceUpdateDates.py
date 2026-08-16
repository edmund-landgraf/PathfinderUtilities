# pip install requests beautifulsoup4 lxml pyodbc

import argparse
import csv
import json
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from aon_update_common import (
    BASE_URL,
    CONN_STR,
    ELASTIC_URL,
    INCLUDED_SOURCE_CATEGORIES,
    clean,
    post_elastic,
    session,
    to_int,
)


DATE_PATTERN = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})\b")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Scrape AoN source release/errata dates and flag sources that may need updates."
    )
    parser.add_argument(
        "--since",
        default="2026-06-09",
        help="Last trusted local scrape/import date, YYYY-MM-DD. Default: 2026-06-09."
    )
    parser.add_argument(
        "--to-date",
        default=date.today().isoformat(),
        help="Ignore source dates after this date, YYYY-MM-DD. Default: today."
    )
    parser.add_argument(
        "--source-id",
        action="append",
        type=int,
        help="Specific AoN source ID to inspect. Can be supplied more than once."
    )
    parser.add_argument(
        "--categories",
        default=",".join(INCLUDED_SOURCE_CATEGORIES),
        help="Comma-separated AoN source categories. Default: Adventure Paths, Lost Omens, Rulebooks."
    )
    parser.add_argument(
        "--include-all-categories",
        action="store_true",
        help="Inspect all source categories from AoN Elasticsearch."
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit number of sources to inspect, useful for testing."
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="Do not connect to SQL Server for existing source/content counts."
    )
    parser.add_argument(
        "--report-dir",
        default="reports/aon-errata",
        help="Directory for Markdown/CSV/JSON reports."
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=25,
        help="Print progress every N sources. Default: 25."
    )
    return parser.parse_args()


def parse_date(value):
    if not value:
        return None

    value = clean(value)
    match = DATE_PATTERN.search(value or "")

    if not match:
        return None

    raw = match.group(1)

    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass

    return None


def iso(value):
    return value.isoformat() if value else None


def max_date(*values):
    parsed = [value for value in values if value is not None]
    return max(parsed) if parsed else None


def category_list(args):
    if args.include_all_categories:
        return None

    return [clean(value) for value in args.categories.split(",") if clean(value)]


def fetch_sources(categories=None, source_ids=None):
    if source_ids:
        return [fetch_source_by_id(source_id) for source_id in source_ids]

    sources = []
    offset = 0
    size = 100

    filters = [
        {"term": {"category": "source"}},
        {"term": {"type": "Source"}},
    ]

    if categories:
        filters.append({"terms": {"source_category": categories}})

    while True:
        payload = {
            "from": offset,
            "size": size,
            "_source": [
                "id",
                "name",
                "url",
                "release_date",
                "source_category",
                "source_group",
                "type",
                "category",
            ],
            "query": {"bool": {"filter": filters}},
            "sort": [
                {"release_date": {"order": "asc"}},
                {"name.keyword": {"order": "asc"}},
            ],
        }

        data = post_elastic(payload)
        hits = data.get("hits", {}).get("hits", [])

        if not hits:
            break

        for hit in hits:
            sources.append(source_from_hit(hit))

        offset += size

    return sources


def fetch_source_by_id(source_id):
    payload = {
        "size": 1,
        "_source": [
            "id",
            "name",
            "url",
            "release_date",
            "source_category",
            "source_group",
            "type",
            "category",
        ],
        "query": {
            "query_string": {
                "query": f'url:"/Sources.aspx?ID={source_id}"'
            }
        },
    }

    data = post_elastic(payload)
    hits = data.get("hits", {}).get("hits", [])

    if not hits:
        raise RuntimeError(f"No AoN source found for source ID {source_id}.")

    return source_from_hit(hits[0])


def source_from_hit(hit):
    src = hit.get("_source", {})

    return {
        "elastic_id": hit.get("_id"),
        "aon_id": source_id_value(src.get("id"), src.get("url")),
        "name": clean(src.get("name")),
        "url": urljoin(BASE_URL, src.get("url") or ""),
        "elastic_release_date": clean(src.get("release_date")),
        "source_category": clean(src.get("source_category")),
        "source_group": clean(src.get("source_group")),
    }


def source_id_value(raw_id, raw_url=None):
    parsed = to_int(raw_id)

    if parsed is not None:
        return abs(parsed)

    parsed = to_int(raw_url)

    if parsed is not None:
        return abs(parsed)

    return None


def scrape_source_dates(source):
    response = session.get(source["url"], timeout=60)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")
    main = soup.select_one("#main") or soup
    text = main.get_text(" ", strip=True)

    release_raw = field_from_text(text, "Release Date") or source.get("elastic_release_date")
    product_line = field_from_text(text, "Product Line") or source.get("source_category")
    latest_errata_raw = field_from_text(text, "Latest Errata")
    update_notice_raw = update_notice_from_text(text)

    release_date = parse_date(release_raw or source.get("elastic_release_date"))
    elastic_release_date = parse_date(source.get("elastic_release_date"))
    latest_errata_date = parse_date(latest_errata_raw)
    update_notice_date = parse_date(update_notice_raw)
    relevant_update_date = max_date(
        latest_errata_date,
        update_notice_date,
        release_date,
        elastic_release_date,
    )

    return {
        **source,
        "release_date": iso(release_date),
        "elastic_release_date": iso(elastic_release_date),
        "product_line": product_line,
        "latest_errata_raw": latest_errata_raw,
        "latest_errata_date": iso(latest_errata_date),
        "update_notice_raw": update_notice_raw,
        "update_notice_date": iso(update_notice_date),
        "relevant_update_date": iso(relevant_update_date),
    }


def field_from_text(text, label):
    match = re.search(
        rf"\b{re.escape(label)}\b\s+(.+?)(?=\s+(?:Product Page|Latest Errata|Release Date|Product Line|These entries|Ancestry|Equipment|Feats|Spells|Monsters|NPCs)\b|$)",
        text or "",
        re.I,
    )

    return clean(match.group(1)) if match else None


def update_notice_from_text(text):
    match = re.search(
        r"(These entries have been updated with errata released on\s+\d{1,2}/\d{1,2}/\d{4})",
        text or "",
        re.I,
    )

    return clean(match.group(1)) if match else None


def load_db_source_counts():
    import pyodbc

    cn = pyodbc.connect(CONN_STR)
    cur = cn.cursor()

    try:
        tables = {
            "equipment_rows": "pf2.Equipment",
            "feat_rows": "pf2.Feat",
            "spell_rows": "pf2.Spell",
            "monster_rows": "pf2.Monster",
        }
        counts = {}

        cur.execute("""
            SELECT SourceBookId, Name
            FROM pf2.SourceBook
        """)

        for source_book_id, name in cur.fetchall():
            counts[clean(name).lower()] = {
                "db_source_book_id": source_book_id,
                "db_source_book_name": clean(name),
                "equipment_rows": 0,
                "feat_rows": 0,
                "spell_rows": 0,
                "monster_rows": 0,
            }

        for key, table in tables.items():
            if not table_exists(cur, table):
                continue

            cur.execute(f"""
                SELECT s.SourceBookId, s.Name, COUNT_BIG(*) AS RowCount
                FROM {table} t
                JOIN pf2.SourceBook s ON s.SourceBookId = t.SourceBookId
                GROUP BY s.SourceBookId, s.Name
            """)

            for source_book_id, name, row_count in cur.fetchall():
                normalized_name = clean(name).lower()

                if normalized_name not in counts:
                    counts[normalized_name] = {
                        "db_source_book_id": source_book_id,
                        "db_source_book_name": clean(name),
                        "equipment_rows": 0,
                        "feat_rows": 0,
                        "spell_rows": 0,
                        "monster_rows": 0,
                    }

                counts[normalized_name][key] = int(row_count)

        return counts

    finally:
        cur.close()
        cn.close()


def table_exists(cur, full_table_name):
    schema_name, table_name = full_table_name.split(".", 1)
    cur.execute("""
        SELECT 1
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = ?
          AND TABLE_NAME = ?
    """, schema_name, table_name)

    return cur.fetchone() is not None


def add_db_and_decision(row, db_counts, since_date, to_date):
    db_row = db_counts.get((row.get("name") or "").lower(), {}) if db_counts is not None else {}
    content_rows = sum(
        int(db_row.get(key) or 0)
        for key in ("equipment_rows", "feat_rows", "spell_rows", "monster_rows")
    )
    relevant_date = parse_date(row.get("relevant_update_date"))
    release_date = parse_date(row.get("release_date"))
    has_existing_content = content_rows > 0
    is_in_window = relevant_date is not None and since_date < relevant_date <= to_date
    is_new_release = release_date is not None and since_date < release_date <= to_date

    if not is_in_window:
        status = "current_or_outside_window"
    elif has_existing_content:
        status = "existing_source_update_available"
    elif is_new_release:
        status = "new_source_available"
    else:
        status = "metadata_update_available"

    return {
        **row,
        "db_source_book_id": db_row.get("db_source_book_id"),
        "db_source_book_name": db_row.get("db_source_book_name"),
        "db_has_supported_content": has_existing_content,
        "db_supported_content_rows": content_rows,
        "db_equipment_rows": int(db_row.get("equipment_rows") or 0),
        "db_feat_rows": int(db_row.get("feat_rows") or 0),
        "db_spell_rows": int(db_row.get("spell_rows") or 0),
        "db_monster_rows": int(db_row.get("monster_rows") or 0),
        "needs_review": is_in_window,
        "status": status,
    }


def write_reports(rows, args):
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = report_dir / f"{stamp}_aon_source_update_dates"
    json_path = base.with_suffix(".json")
    csv_path = base.with_suffix(".csv")
    md_path = base.with_suffix(".md")

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)

    columns = [
        "status",
        "needs_review",
        "aon_id",
        "name",
        "source_category",
        "release_date",
        "latest_errata_date",
        "update_notice_date",
        "relevant_update_date",
        "db_has_supported_content",
        "db_supported_content_rows",
        "db_equipment_rows",
        "db_feat_rows",
        "db_spell_rows",
        "db_monster_rows",
        "latest_errata_raw",
        "update_notice_raw",
        "url",
    ]

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    review_rows = [row for row in rows if row["needs_review"]]

    with md_path.open("w", encoding="utf-8") as f:
        f.write("# AoN Source Update Dates\n\n")
        f.write(f"Generated: {datetime.now().isoformat(timespec='seconds')}\n\n")
        f.write(f"Window: after `{args.since}` through `{args.to_date}`\n\n")
        f.write(f"Sources inspected: {len(rows)}\n\n")
        f.write(f"Sources needing review: {len(review_rows)}\n\n")
        f.write("| Status | Source | Release | Latest Errata | Update Notice | DB Rows |\n")
        f.write("| --- | --- | --- | --- | --- | ---: |\n")

        for row in review_rows:
            f.write(
                f"| {row['status']} "
                f"| [{row['name']}]({row['url']}) "
                f"| {row.get('release_date') or ''} "
                f"| {row.get('latest_errata_date') or ''} "
                f"| {row.get('update_notice_date') or ''} "
                f"| {row.get('db_supported_content_rows') or 0} |\n"
            )

    return md_path, csv_path, json_path


def main():
    args = parse_args()
    since_date = parse_date(args.since)
    to_date = parse_date(args.to_date)

    if since_date is None or to_date is None:
        raise RuntimeError("--since and --to-date must be valid dates.")

    categories = category_list(args)
    sources = fetch_sources(categories=categories, source_ids=args.source_id)

    if args.limit:
        sources = sources[:args.limit]

    print("=" * 100)
    print("AoN Source Date Scrape")
    print("=" * 100)
    print(f"Sources to inspect: {len(sources)}")
    print(f"Window: after {args.since} through {args.to_date}")

    if categories:
        print(f"Categories: {', '.join(categories)}")
    else:
        print("Categories: all")

    db_counts = None

    if not args.no_db:
        try:
            db_counts = load_db_source_counts()
            print(f"SQL comparison: loaded {len(db_counts)} SourceBook rows")
        except Exception as ex:
            print(f"SQL comparison unavailable: {ex}")

    rows = []

    for idx, source in enumerate(sources, start=1):
        scraped = scrape_source_dates(source)
        row = add_db_and_decision(scraped, db_counts, since_date, to_date)
        rows.append(row)

        if args.progress_every and (idx % args.progress_every == 0 or idx == len(sources)):
            print(f"Scraped {idx}/{len(sources)} sources", flush=True)

    rows.sort(key=lambda row: (
        row.get("needs_review") is not True,
        row.get("relevant_update_date") or "",
        row.get("name") or "",
    ))

    md_path, csv_path, json_path = write_reports(rows, args)
    review_count = sum(1 for row in rows if row["needs_review"])

    print()
    print(f"Sources needing review: {review_count}")
    print(f"Markdown report: {md_path}")
    print(f"CSV report: {csv_path}")
    print(f"JSON report: {json_path}")


if __name__ == "__main__":
    main()
