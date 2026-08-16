# pip install requests beautifulsoup4 lxml pyodbc

import json
import re
import time
from collections import defaultdict
from datetime import date
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://2e.aonprd.com/"
ELASTIC_URL = "https://elasticsearch.aonprd.com/aon/_search"

CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;"
    "DATABASE=PathfinderUtil;"
    "Trusted_Connection=yes;"
)

INCLUDED_SOURCE_CATEGORIES = ("Adventure Paths", "Lost Omens", "Rulebooks")
SUPPORTED_SECTIONS = ("Equipment", "Feats", "Spells", "Monsters", "NPCs")

SECTION_ROUTE = {
    "Equipment": "equipment",
    "Feats": "feat",
    "Spells": "spell",
    "Monsters": "monster",
    "NPCs": "monster",
}

session = requests.Session()
session.headers.update({
    "User-Agent": "PathfinderUtil AoN source update importer / personal use",
    "Content-Type": "application/json",
})


def clean(value):
    if value is None:
        return None

    if isinstance(value, list):
        return ", ".join(str(x) for x in value if x is not None).strip() or None

    return re.sub(r"\s+", " ", str(value)).strip() or None


def to_int(value):
    if value is None:
        return None

    if isinstance(value, int):
        return value

    m = re.search(r"-?\d+", str(value))
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


def post_elastic(payload, timeout=60):
    response = session.post(ELASTIC_URL, json=payload, timeout=timeout)

    if response.status_code != 200:
        print("Elasticsearch payload:")
        print(json.dumps(payload, indent=2))
        print(response.text[:2000])
        response.raise_for_status()

    return response.json()


def fetch_sources_by_release_date(from_date, to_date=None, categories=INCLUDED_SOURCE_CATEGORIES):
    to_date = to_date or date.today().isoformat()
    sources = []
    offset = 0
    size = 100

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
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"category": "source"}},
                        {"term": {"type": "Source"}},
                        {"terms": {"source_category": list(categories)}},
                        {"range": {"release_date": {"gte": from_date, "lte": to_date}}},
                    ]
                }
            },
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
            src = hit.get("_source", {})
            sources.append({
                "elastic_id": hit.get("_id"),
                "aon_id": to_int(src.get("id")),
                "name": clean(src.get("name")),
                "url": urljoin(BASE_URL, src.get("url") or ""),
                "release_date": clean(src.get("release_date")),
                "source_category": clean(src.get("source_category")),
                "source_group": clean(src.get("source_group")),
            })

        offset += size

    return sources


def fetch_source_by_id(source_id):
    source_url = f"/Sources.aspx?ID={source_id}"
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
                "query": f'url:"{source_url}"'
            }
        }
    }

    data = post_elastic(payload)
    hits = data.get("hits", {}).get("hits", [])

    if not hits:
        raise RuntimeError(f"No AoN source found for source-{source_id}.")

    src = hits[0].get("_source", {})

    return {
        "elastic_id": hits[0].get("_id"),
        "aon_id": to_int(src.get("id")),
        "name": clean(src.get("name")),
        "url": urljoin(BASE_URL, src.get("url") or f"/Sources.aspx?ID={source_id}"),
        "release_date": clean(src.get("release_date")),
        "source_category": clean(src.get("source_category")),
        "source_group": clean(src.get("source_group")),
    }


def parse_source_page(source):
    response = session.get(source["url"], timeout=60)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")
    main = soup.select_one("#main") or soup

    text = main.get_text(" ", strip=True)
    release_date = source.get("release_date") or field_from_text(text, "Release Date")
    product_line = field_from_text(text, "Product Line")
    latest_errata = field_from_text(text, "Latest Errata")

    sections = {
        name: {
            "expected_count": 0,
            "source_link_count": 0,
            "entries": [],
            "duplicate_entries": [],
        }
        for name in SUPPORTED_SECTIONS
    }

    for heading in main.find_all("h2", class_="title"):
        title = clean(heading.get_text(" ", strip=True))

        if not title:
            continue

        match = re.match(r"^(.*?)\s*\[(\d+)\]$", title)

        if not match:
            continue

        section_name = clean(match.group(1))

        if section_name not in sections:
            continue

        expected_count = int(match.group(2))
        entries = []

        for sibling in heading.next_siblings:
            if getattr(sibling, "name", None) == "h2":
                break

            if not hasattr(sibling, "find_all"):
                continue

            for link in sibling.find_all("a", href=True):
                href = link.get("href")

                if not is_supported_entry_href(href):
                    continue

                entries.append({
                    "name": clean(link.get_text(" ", strip=True)),
                    "url": urljoin(BASE_URL, href),
                    "relative_url": relative_aon_url(href),
                    "section": section_name,
                })

        deduped_entries = dedupe_entries(entries)
        sections[section_name] = {
            "expected_count": expected_count,
            "source_link_count": len(entries),
            "entries": deduped_entries,
            "duplicate_entries": duplicate_entries(entries),
        }

    return {
        **source,
        "release_date": release_date,
        "product_line": product_line,
        "latest_errata": latest_errata,
        "sections": sections,
    }


def field_from_text(text, label):
    match = re.search(
        rf"\b{re.escape(label)}\b\s+(.+?)(?=\s+(?:Product Page|Latest Errata|Release Date|Product Line|These entries|Ancestry|Equipment|Feats|Spells|Monsters|NPCs)\b|$)",
        text or "",
        re.I,
    )

    return clean(match.group(1)) if match else None


def is_supported_entry_href(href):
    if not href:
        return False

    path = urlparse(urljoin(BASE_URL, href)).path.lower()

    return path.endswith((
        "/equipment.aspx",
        "/armor.aspx",
        "/weapons.aspx",
        "/shields.aspx",
        "/feats.aspx",
        "/spells.aspx",
        "/monsters.aspx",
        "/npcs.aspx",
    ))


def relative_aon_url(href):
    parsed = urlparse(urljoin(BASE_URL, href))
    return parsed.path.lstrip("/") + (f"?{parsed.query}" if parsed.query else "")


def dedupe_entries(entries):
    deduped = []
    seen = set()

    for entry in entries:
        key = (
            entry["section"].lower(),
            (entry["name"] or "").lower(),
            entry["relative_url"].lower(),
        )

        if key in seen:
            continue

        seen.add(key)
        deduped.append(entry)

    return deduped


def duplicate_entries(entries):
    counts = defaultdict(int)

    for entry in entries:
        key = (
            entry["section"].lower(),
            (entry["name"] or "").lower(),
            entry["relative_url"].lower(),
        )
        counts[key] += 1

    duplicates = []
    seen = set()

    for entry in entries:
        key = (
            entry["section"].lower(),
            (entry["name"] or "").lower(),
            entry["relative_url"].lower(),
        )

        if counts[key] > 1 and key not in seen:
            seen.add(key)
            duplicates.append(entry)

    return duplicates


def entry_kind(entry):
    path = urlparse(entry["url"]).path.lower()

    if path.endswith(("/equipment.aspx", "/armor.aspx", "/weapons.aspx", "/shields.aspx")):
        return "equipment"

    if path.endswith("/feats.aspx"):
        return "feat"

    if path.endswith("/spells.aspx"):
        return "spell"

    if path.endswith(("/monsters.aspx", "/npcs.aspx")):
        return "monster"

    return None


def hydrate_entry_from_elastic(entry):
    relative_url = "/" + entry["relative_url"].lstrip("/")
    payload = {
        "size": 20,
        "query": {
            "query_string": {
                "query": f'url:"{relative_url}"'
            }
        },
    }

    data = post_elastic(payload)
    candidates = []

    for hit in data.get("hits", {}).get("hits", []):
        src = hit.get("_source", {})

        if src.get("url") == relative_url:
            src["_elastic_id"] = hit.get("_id")
            candidates.append(src)

    if not candidates:
        return None

    candidates.sort(key=lambda src: (
        name_match_penalty(src, entry),
        variant_penalty(src),
        len(clean(src.get("id")) or ""),
    ))
    return candidates[0]


def name_match_penalty(src, entry):
    src_name = (clean(src.get("name")) or "").lower()
    entry_name = (clean(entry.get("name")) or "").lower()

    return 0 if src_name == entry_name else 1


def variant_penalty(src):
    raw_id = clean(src.get("id")) or ""
    parts = raw_id.split("-")

    if len(parts) <= 2:
        return 0

    return 1


def aon_id_from_url(url):
    query = parse_qs(urlparse(url).query)

    if "ID" not in query:
        return None

    return to_int(query["ID"][0])


def aon_numeric_id(src, fallback_url=None):
    raw_id = first_existing(src, "id", "aonid", "aon_id")

    if raw_id:
        raw_id = str(raw_id)

        if "-" in raw_id:
            raw_id = raw_id.split("-")[-1]

        parsed = to_int(raw_id)

        if parsed is not None:
            return parsed

    return aon_id_from_url(fallback_url)


def local_url(entry):
    return "/" + entry["relative_url"].lstrip("/")


def existing_row_id(cur, kind, entry, src):
    url = local_url(entry)
    aon_id = aon_numeric_id(src or {}, entry["url"])

    if kind == "equipment":
        aon_key = clean(first_existing(src or {}, "id", "_elastic_id"))

        if aon_key:
            cur.execute("""
                SELECT TOP 1 EquipmentId
                FROM pf2.Equipment
                WHERE AonKey = ?
            """, aon_key)
            row = cur.fetchone()

            if row:
                return row[0]

            return None

        cur.execute("""
            SELECT TOP 1 EquipmentId
            FROM pf2.Equipment
            WHERE AonUrl IN (?, ?)
        """, url, entry["url"])
        row = cur.fetchone()
        return row[0] if row else None

    table_map = {
        "feat": ("pf2.Feat", "FeatId"),
        "spell": ("pf2.Spell", "SpellId"),
        "monster": ("pf2.Monster", "MonsterId"),
    }
    table_name, id_column = table_map[kind]

    if aon_id is not None:
        cur.execute(f"""
            SELECT TOP 1 {id_column}
            FROM {table_name}
            WHERE AonId = ?
        """, aon_id)
        row = cur.fetchone()

        if row:
            return row[0]

    cur.execute(f"""
        SELECT TOP 1 {id_column}
        FROM {table_name}
        WHERE AonUrl IN (?, ?)
    """, url, entry["url"])
    row = cur.fetchone()
    return row[0] if row else None


def build_source_preview(source_page, include_hydration=True, progress_every=100):
    import pyodbc

    cn = pyodbc.connect(CONN_STR)
    cur = cn.cursor()

    try:
        preview = {
            "source": source_page,
            "groups": {},
            "totals": {
                "expected": 0,
                "deduped": 0,
                "already_present": 0,
                "missing": 0,
                "missing_hydration": 0,
            }
        }
        total_entries = sum(
            len(source_page["sections"].get(section_name, {}).get("entries", []))
            for section_name in SUPPORTED_SECTIONS
        )
        processed_entries = 0

        if total_entries:
            print(f"Preparing preview: 0/{total_entries} rows", flush=True)

        for section_name in SUPPORTED_SECTIONS:
            section = source_page["sections"].get(
                section_name,
                {
                    "expected_count": 0,
                    "source_link_count": 0,
                    "entries": [],
                    "duplicate_entries": [],
                }
            )
            rows = []

            for entry in section["entries"]:
                kind = entry_kind(entry)
                src = hydrate_entry_from_elastic(entry) if include_hydration else None
                existing_id = existing_row_id(cur, kind, entry, src) if kind else None
                processed_entries += 1

                rows.append({
                    **entry,
                    "kind": kind,
                    "hydrated": src is not None,
                    "source": src,
                    "existing_id": existing_id,
                    "is_missing": existing_id is None,
                })

                if progress_every and processed_entries % progress_every == 0:
                    print(f"Preparing preview: {processed_entries}/{total_entries} rows", flush=True)

                time.sleep(0.01)

            already_present = sum(1 for row in rows if row["existing_id"] is not None)
            missing = sum(1 for row in rows if row["existing_id"] is None)
            missing_hydration = sum(1 for row in rows if row["existing_id"] is None and not row["hydrated"])

            preview["groups"][section_name] = {
                "expected_count": section["expected_count"],
                "source_link_count": section["source_link_count"],
                "deduped_count": len(rows),
                "already_present": already_present,
                "missing": missing,
                "missing_hydration": missing_hydration,
                "duplicate_entries": section["duplicate_entries"],
                "rows": rows,
            }

            preview["totals"]["expected"] += section["expected_count"]
            preview["totals"]["deduped"] += len(rows)
            preview["totals"]["already_present"] += already_present
            preview["totals"]["missing"] += missing
            preview["totals"]["missing_hydration"] += missing_hydration

        if total_entries and progress_every and processed_entries % progress_every != 0:
            print(f"Preparing preview: {processed_entries}/{total_entries} rows", flush=True)

        return preview

    finally:
        cur.close()
        cn.close()


def print_preview(preview, show_all=False):
    source = preview["source"]

    print()
    print("=" * 100)
    print(f"{source.get('name')} | {source.get('url')}")
    print(f"Release Date: {source.get('release_date') or '(unknown)'}")
    print(f"Product Line: {source.get('product_line') or source.get('source_category') or '(unknown)'}")

    if source.get("latest_errata"):
        print(f"Latest Errata: {source['latest_errata']}")

    print("-" * 100)

    for section_name in SUPPORTED_SECTIONS:
        group = preview["groups"][section_name]
        print(
            f"{section_name}: expected={group['expected_count']} "
            f"source_links={group['source_link_count']} "
            f"unique={group['deduped_count']} "
            f"existing={group['already_present']} "
            f"new={group['missing']} "
            f"missing_elastic={group['missing_hydration']}"
        )

        for duplicate in group.get("duplicate_entries", []):
            print(f"  [DUPLICATE SOURCE LINK] {duplicate['name']} - {duplicate['relative_url']}")

        rows_to_show = group["rows"] if show_all else [row for row in group["rows"] if row["is_missing"]]

        for row in rows_to_show:
            marker = "NEW" if row["is_missing"] else f"EXISTS:{row['existing_id']}"
            hydrated = "" if row["hydrated"] else " [NO ELASTIC]"
            print(f"  [{marker}] {row['name']} - {row['relative_url']}{hydrated}")

    print("-" * 100)
    print(
        f"Totals: expected={preview['totals']['expected']} "
        f"deduped={preview['totals']['deduped']} "
        f"existing={preview['totals']['already_present']} "
        f"new={preview['totals']['missing']} "
        f"missing_elastic={preview['totals']['missing_hydration']}"
    )


def import_preview(preview, dry_run=False, progress_every=100):
    import pyodbc
    from pullEquipment import insert_equipment_from_elastic
    from pullFeats import insert_feat_from_elastic
    from pullMonsters_3 import insert_monster_from_elastic
    from pullSpells import insert_spell_from_elastic

    importer_map = {
        "equipment": insert_equipment_from_elastic,
        "feat": insert_feat_from_elastic,
        "spell": insert_spell_from_elastic,
        "monster": insert_monster_from_elastic,
    }

    cn = pyodbc.connect(CONN_STR)
    cn.autocommit = False
    cur = cn.cursor()
    cache = {}
    results = defaultdict(lambda: {"imported": 0, "skipped": 0, "failed": 0})
    total_rows = sum(len(preview["groups"][section_name]["rows"]) for section_name in SUPPORTED_SECTIONS)
    processed_rows = 0

    try:
        if total_rows:
            print(f"Processing import: 0/{total_rows} rows", flush=True)

        for section_name in SUPPORTED_SECTIONS:
            group = preview["groups"][section_name]

            for row in group["rows"]:
                kind = row["kind"]
                processed_rows += 1

                if not row["is_missing"]:
                    results[section_name]["skipped"] += 1

                    if progress_every and processed_rows % progress_every == 0:
                        print(f"Processing import: {processed_rows}/{total_rows} rows", flush=True)

                    continue

                if not row["hydrated"] or not row["source"]:
                    results[section_name]["failed"] += 1
                    print(f"FAILED {section_name}: {row['name']} has no Elasticsearch payload.")

                    if progress_every and processed_rows % progress_every == 0:
                        print(f"Processing import: {processed_rows}/{total_rows} rows", flush=True)

                    continue

                importer = importer_map.get(kind)

                if not importer:
                    results[section_name]["failed"] += 1
                    print(f"FAILED {section_name}: no importer for {row['relative_url']}")

                    if progress_every and processed_rows % progress_every == 0:
                        print(f"Processing import: {processed_rows}/{total_rows} rows", flush=True)

                    continue

                try:
                    imported_id = importer(cur, row["source"], cache)
                    write_import_log(cur, kind, row, True, f"Imported as {imported_id}", cache)

                    if not dry_run:
                        cn.commit()

                    results[section_name]["imported"] += 1
                    verb = "SMOKE INSERTED" if dry_run else "IMPORTED"
                    print(f"{verb} {section_name}: {row['name']} -> {imported_id}")
                except Exception as ex:
                    cn.rollback()
                    cur = cn.cursor()
                    cache = {}
                    results[section_name]["failed"] += 1
                    print(f"FAILED {section_name}: {row['name']} - {ex}")

                    try:
                        write_import_log(cur, kind, row, False, str(ex), cache)
                        cn.commit()
                    except Exception:
                        cn.rollback()
                        cur = cn.cursor()
                        cache = {}

                if progress_every and processed_rows % progress_every == 0:
                    print(f"Processing import: {processed_rows}/{total_rows} rows", flush=True)

        if total_rows and progress_every and processed_rows % progress_every != 0:
            print(f"Processing import: {processed_rows}/{total_rows} rows", flush=True)

        if dry_run:
            cn.rollback()

        return dict(results)

    finally:
        cur.close()
        cn.close()


def write_import_log(cur, kind, row, success, message, cache):
    table_map = {
        "equipment": "pf2.EquipmentImportLog",
        "feat": "pf2.FeatImportLog",
        "spell": "pf2.SpellImportLog",
        "monster": "pf2.MonsterImportLog",
    }
    table = table_map.get(kind)

    if not table or not table_exists(cur, table, cache):
        return

    cur.execute(f"""
        INSERT INTO {table}
        (
            AonUrl,
            ImportedAt,
            Success,
            Message
        )
        VALUES
        (?, SYSDATETIME(), ?, ?)
    """,
        row["url"],
        1 if success else 0,
        (message or "")[:4000],
    )


def table_exists(cur, full_table_name, cache):
    key = ("table", full_table_name)

    if key in cache:
        return cache[key]

    if "." in full_table_name:
        schema_name, table_name = full_table_name.split(".", 1)
    else:
        schema_name, table_name = "dbo", full_table_name

    cur.execute("""
        SELECT 1
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = ?
          AND TABLE_NAME = ?
    """, schema_name, table_name)

    cache[key] = cur.fetchone() is not None
    return cache[key]
