# pip install requests pyodbc

import json
import re
import time
from urllib.parse import parse_qs, urlparse

import pyodbc
import requests

ELASTIC_URL = "https://elasticsearch.aonprd.com/aon/_search"
VERBOSE = False

CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;"
    "DATABASE=PathfinderUtil;"
    "Trusted_Connection=yes;"
)

session = requests.Session()
session.headers.update({
    "User-Agent": "PathfinderUtil feat importer / personal use",
    "Content-Type": "application/json"
})


def clean(v):
    if v is None:
        return None

    if isinstance(v, list):
        return ", ".join(str(x) for x in v if x is not None).strip() or None

    return re.sub(r"\s+", " ", str(v)).strip() or None


def to_int(v):
    if v is None:
        return None

    if isinstance(v, int):
        return v

    m = re.search(r"-?\d+", str(v))
    return int(m.group(0)) if m else None


def first_existing(src, *names):
    for name in names:
        if name in src and src[name] not in (None, "", []):
            return src[name]

    lowered = {str(k).lower(): v for k, v in src.items()}

    for name in names:
        wanted = name.lower()

        for k, v in lowered.items():
            if wanted == k or wanted in k:
                if v not in (None, "", []):
                    return v

    return None


def split_values(value):
    if not value:
        return []

    if isinstance(value, list):
        return [clean(x) for x in value if clean(x)]

    text = str(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace(";", ",").replace("|", ",")

    return [clean(x) for x in text.split(",") if clean(x)]


def unique_values(values):
    unique = []
    seen = set()

    for value in values:
        cleaned = clean(value)

        if not cleaned:
            continue

        key = cleaned.lower()

        if key in seen:
            continue

        seen.add(key)
        unique.append(cleaned)

    return unique


def get_or_create_lookup(cur, table, id_col, name_col, name, cache):
    name = clean(name)

    if not name:
        return None

    key = (table, name.lower())

    if key in cache:
        return cache[key]

    cur.execute(f"SELECT TOP 1 {id_col} FROM {table} WHERE {name_col} = ?", name)
    row = cur.fetchone()

    if row:
        cache[key] = row[0]
        return row[0]

    cur.execute(f"""
        INSERT INTO {table}
        (
            {name_col}
        )
        OUTPUT INSERTED.{id_col}
        VALUES
        (?)
    """, name)

    new_id = cur.fetchone()[0]
    cache[key] = new_id

    return new_id


def fetch_feat_batch(offset=0, size=500):
    payload = {
        "from": offset,
        "size": size,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"category": "feat"}}
                ]
            }
        }
    }

    r = session.post(ELASTIC_URL, json=payload, timeout=60)

    if VERBOSE:
        print("HTTP:", r.status_code, "offset:", offset)
        print("URL:", r.url)

    if r.status_code != 200:
        print("Payload:")
        print(json.dumps(payload, indent=2))
        print(r.text[:2000])
        r.raise_for_status()

    return r.json()


def fetch_all_feats():
    all_items = []
    offset = 0
    size = 500

    while True:
        data = fetch_feat_batch(offset, size)

        hits = data.get("hits", {}).get("hits", [])
        total = data.get("hits", {}).get("total", {})

        total_value = total.get("value") if isinstance(total, dict) else total

        print(f"Fetched {len(hits)} at offset {offset}; total={total_value}")

        if not hits:
            break

        for h in hits:
            src = h.get("_source", {})
            src["_elastic_id"] = h.get("_id")
            all_items.append(src)

        offset += size

        if total_value is not None and offset >= total_value:
            break

        time.sleep(0.2)

    return sorted(
        all_items,
        key=lambda src: (
            clean(first_existing(src, "type")) or "",
            to_int(first_existing(src, "level")) if to_int(first_existing(src, "level")) is not None else 999,
            clean(first_existing(src, "name", "feat")) or ""
        )
    )


def aon_numeric_id(src):
    raw_id = first_existing(src, "id", "aonid", "aon_id")

    if not raw_id:
        return None

    raw_id = str(raw_id)

    if "-" in raw_id:
        raw_id = raw_id.split("-")[-1]

    return to_int(raw_id)


def aon_id_from_url(url):
    if not url:
        return None

    qs = parse_qs(urlparse(url).query)

    if "ID" not in qs:
        return None

    return to_int(qs["ID"][0])


def aon_url_from_src(src):
    url = first_existing(src, "url", "link", "href")

    if url:
        url = str(url)

        if url.startswith("/"):
            return "https://2e.aonprd.com" + url

        if url.startswith("http"):
            return url

    aon_id = aon_numeric_id(src)

    if aon_id:
        return f"https://2e.aonprd.com/Feats.aspx?ID={aon_id}"

    return None


def source_page_from_raw(src):
    raw = clean(first_existing(src, "primary_source_raw", "source_raw"))

    if not raw:
        return None

    m = re.search(r"\bpg\.?\s*(\d+)", raw, re.I)

    return int(m.group(1)) if m else None


def has_column(cur, table_schema, table_name, column_name, cache):
    key = ("column", table_schema, table_name, column_name)

    if key not in cache:
        cur.execute("""
            SELECT 1
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = ?
              AND TABLE_NAME = ?
              AND COLUMN_NAME = ?
        """, table_schema, table_name, column_name)

        cache[key] = cur.fetchone() is not None

    return cache[key]


def insert_feat_row(cur, values, cache):
    columns = [
        "AonId",
        "AonUrl",
        "Name",
        "Level",
        "FeatType",
        "RarityId",
        "SourceBookId",
        "SourcePage",
        "PFS",
        "IsStandardAncestryFeat",
        "Summary",
        "RemasterId",
        "RawHtml",
        "RawText",
        "RawJson",
        "CreatedAt",
        "UpdatedAt",
        "LastScraped",
        "ScrapeVersion"
    ]

    insert_columns = [
        c for c in columns
        if has_column(cur, "pf2", "Feat", c, cache)
    ]

    if not has_column(cur, "pf2", "Feat", "FeatId", cache):
        raise RuntimeError("pf2.Feat must have a FeatId identity column.")

    placeholders = []
    params = []

    for column in insert_columns:
        if column in ("CreatedAt", "UpdatedAt", "LastScraped"):
            placeholders.append("SYSDATETIME()")
        else:
            placeholders.append("?")
            params.append(values.get(column))

    cur.execute(f"""
        INSERT INTO pf2.Feat
        (
            {", ".join(insert_columns)}
        )
        OUTPUT INSERTED.FeatId
        VALUES
        (
            {", ".join(placeholders)}
        )
    """, *params)

    return cur.fetchone()[0]


def existing_feat_id(cur, aon_id, aon_url, cache):
    if has_column(cur, "pf2", "Feat", "AonId", cache) and aon_id is not None:
        cur.execute("""
            SELECT TOP 1 FeatId
            FROM pf2.Feat
            WHERE AonId = ?
        """, aon_id)

        row = cur.fetchone()

        if row:
            return row[0]

    if has_column(cur, "pf2", "Feat", "AonUrl", cache) and aon_url:
        cur.execute("""
            SELECT TOP 1 FeatId
            FROM pf2.Feat
            WHERE AonUrl = ?
        """, aon_url)

        row = cur.fetchone()

        if row:
            return row[0]

    return None


def insert_source_link(cur, feat_id, source_id, source_page, cache):
    if not source_id:
        return

    if not has_column(cur, "pf2", "FeatSourceLink", "FeatId", cache):
        return

    page_column = "PageNumber"

    if not has_column(cur, "pf2", "FeatSourceLink", page_column, cache):
        page_column = None

    if page_column:
        cur.execute(f"""
            IF NOT EXISTS
            (
                SELECT 1
                FROM pf2.FeatSourceLink
                WHERE FeatId = ?
                  AND SourceBookId = ?
                  AND
                  (
                      ({page_column} = ?)
                      OR ({page_column} IS NULL AND ? IS NULL)
                  )
            )
            BEGIN
                INSERT INTO pf2.FeatSourceLink
                (
                    FeatId,
                    SourceBookId,
                    {page_column}
                )
                VALUES
                (?, ?, ?)
            END
        """,
            feat_id,
            source_id,
            source_page,
            source_page,
            feat_id,
            source_id,
            source_page
        )
    else:
        cur.execute("""
            IF NOT EXISTS
            (
                SELECT 1
                FROM pf2.FeatSourceLink
                WHERE FeatId = ?
                  AND SourceBookId = ?
            )
            BEGIN
                INSERT INTO pf2.FeatSourceLink
                (
                    FeatId,
                    SourceBookId
                )
                VALUES
                (?, ?)
            END
        """,
            feat_id,
            source_id,
            feat_id,
            source_id
        )


def insert_feat_trait(cur, feat_id, trait_id, cache):
    if not trait_id:
        return

    if not has_column(cur, "pf2", "FeatTrait", "FeatId", cache):
        return

    cur.execute("""
        IF NOT EXISTS
        (
            SELECT 1
            FROM pf2.FeatTrait
            WHERE FeatId = ?
              AND TraitId = ?
        )
        BEGIN
            INSERT INTO pf2.FeatTrait
            (
                FeatId,
                TraitId
            )
            VALUES
            (?, ?)
        END
    """,
        feat_id,
        trait_id,
        feat_id,
        trait_id
    )


def insert_feat_from_elastic(cur, src, cache):
    name = clean(first_existing(src, "name", "feat", "title"))

    if not name:
        raise ValueError("Feat has no name")

    aon_url = aon_url_from_src(src)
    aon_id = aon_numeric_id(src) or aon_id_from_url(aon_url)

    source = clean(first_existing(src, "primary_source", "source"))
    source_page = source_page_from_raw(src)
    rarity = clean(first_existing(src, "rarity"))

    source_id = get_or_create_lookup(cur, "pf2.SourceBook", "SourceBookId", "Name", source, cache)
    rarity_id = get_or_create_lookup(cur, "pf2.Rarity", "RarityId", "Name", rarity, cache)

    raw_json = json.dumps(src, ensure_ascii=False)

    values = {
        "AonId": aon_id,
        "AonUrl": aon_url,
        "Name": name,
        "Level": to_int(first_existing(src, "level")),
        "FeatType": clean(first_existing(src, "type")),
        "RarityId": rarity_id,
        "SourceBookId": source_id,
        "SourcePage": source_page,
        "PFS": clean(first_existing(src, "pfs")),
        "IsStandardAncestryFeat": 1 if first_existing(src, "is_standard_ancestry_feat") is True else 0,
        "Summary": clean(first_existing(src, "summary")),
        "RemasterId": clean(first_existing(src, "remaster_id")),
        "RawHtml": clean(first_existing(src, "markdown")),
        "RawText": clean(first_existing(src, "text", "search_markdown")),
        "RawJson": raw_json,
        "ScrapeVersion": "aon-elastic-feats-v1"
    }

    feat_id = existing_feat_id(cur, aon_id, aon_url, cache)

    if not feat_id:
        feat_id = insert_feat_row(cur, values, cache)

    insert_source_link(cur, feat_id, source_id, source_page, cache)

    for trait in unique_values(split_values(first_existing(src, "trait", "traits", "trait_raw"))):
        trait_id = get_or_create_lookup(cur, "pf2.Trait", "TraitId", "Name", trait, cache)
        insert_feat_trait(cur, feat_id, trait_id, cache)

    return feat_id


def write_import_log(cur, aon_url, success, message, cache):
    if not has_column(cur, "pf2", "FeatImportLog", "AonUrl", cache):
        return

    cur.execute("""
        INSERT INTO pf2.FeatImportLog
        (
            AonUrl,
            ImportedAt,
            Success,
            Message
        )
        VALUES
        (?, SYSDATETIME(), ?, ?)
    """,
        aon_url,
        1 if success else 0,
        message[:4000] if message else None
    )


def main():
    print("=" * 100)
    print("PF2 AoN Feat Import - ELASTICSEARCH MODE")
    print("=" * 100)

    feats = fetch_all_feats()

    print(f"\nFetched {len(feats)} feat records.")

    if not feats:
        print("No records returned.")
        return

    first_name = clean(first_existing(feats[0], "name", "feat", "title")) or "(unknown)"
    print(f"First feat: {first_name}")

    input("\nPress Enter to import into SQL Server, or Ctrl+C to stop...")

    cn = pyodbc.connect(CONN_STR)
    cn.autocommit = False
    cur = cn.cursor()

    cache = {}
    imported = 0
    failed = 0

    for idx, src in enumerate(feats, start=1):
        name = clean(first_existing(src, "name", "feat", "title"))
        aon_url = aon_url_from_src(src)

        try:
            feat_id = insert_feat_from_elastic(cur, src, cache)

            write_import_log(
                cur,
                aon_url,
                True,
                f"Imported {name} as FeatId {feat_id}",
                cache
            )

            imported += 1
            cn.commit()

            if imported % 100 == 0:
                print(f"Imported {imported}/{len(feats)} feats...")

        except Exception as ex:
            failed += 1

            cn.rollback()
            cur = cn.cursor()
            cache = {}

            print(f"FAILED [{idx}] {name}: {ex}")
            print(f"AoN URL: {aon_url or '(unknown)'}")

            try:
                write_import_log(cur, aon_url, False, str(ex), cache)
                cn.commit()
            except Exception:
                cn.rollback()
                cur = cn.cursor()
                cache = {}
                pass

        time.sleep(0.02)

    cn.commit()
    cur.close()
    cn.close()

    print(f"\nDone. Imported={imported}, Failed={failed}")


if __name__ == "__main__":
    main()
