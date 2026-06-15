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
    "User-Agent": "PathfinderUtil spell importer / personal use",
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


def fetch_spell_batch(offset=0, size=500):
    payload = {
        "from": offset,
        "size": size,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"category": "spell"}}
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


def fetch_all_spells():
    all_items = []
    offset = 0
    size = 500

    while True:
        data = fetch_spell_batch(offset, size)

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
            clean(first_existing(src, "spell_type", "type")) or "",
            spell_rank(src) if spell_rank(src) is not None else 999,
            clean(first_existing(src, "name", "title")) or ""
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
        return f"https://2e.aonprd.com/Spells.aspx?ID={aon_id}"

    return None


def source_page_from_raw(src):
    raw = clean(first_existing(src, "primary_source_raw", "source_raw"))

    if not raw:
        return None

    m = re.search(r"\bpg\.?\s*(\d+)", raw, re.I)

    return int(m.group(1)) if m else None


def spell_rank(src):
    rank = to_int(first_existing(src, "rank", "spell_level", "level"))

    if rank is not None:
        return rank

    text = clean(first_existing(src, "rank_text", "level_text"))

    if text and text.lower() == "cantrip":
        return 0

    return None


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


def insert_spell_row(cur, values, cache):
    columns = [
        "AonId",
        "AonUrl",
        "Name",
        "Rank",
        "SpellType",
        "RarityId",
        "SourceBookId",
        "SourcePage",
        "Actions",
        "TriggerText",
        "Target",
        "RangeText",
        "Area",
        "Duration",
        "Defense",
        "Heighten",
        "Summary",
        "PFS",
        "Components",
        "School",
        "Bloodline",
        "DomainText",
        "RemasterId",
        "RemasterName",
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
        if has_column(cur, "pf2", "Spell", c, cache)
    ]

    if not has_column(cur, "pf2", "Spell", "SpellId", cache):
        raise RuntimeError("pf2.Spell must have a SpellId identity column.")

    placeholders = []
    params = []

    for column in insert_columns:
        if column in ("CreatedAt", "UpdatedAt", "LastScraped"):
            placeholders.append("SYSDATETIME()")
        else:
            placeholders.append("?")
            params.append(values.get(column))

    cur.execute(f"""
        INSERT INTO pf2.Spell
        (
            {", ".join(insert_columns)}
        )
        OUTPUT INSERTED.SpellId
        VALUES
        (
            {", ".join(placeholders)}
        )
    """, *params)

    return cur.fetchone()[0]


def existing_spell_id(cur, aon_id, aon_url, cache):
    if has_column(cur, "pf2", "Spell", "AonId", cache) and aon_id is not None:
        cur.execute("""
            SELECT TOP 1 SpellId
            FROM pf2.Spell
            WHERE AonId = ?
        """, aon_id)

        row = cur.fetchone()

        if row:
            return row[0]

    if has_column(cur, "pf2", "Spell", "AonUrl", cache) and aon_url:
        cur.execute("""
            SELECT TOP 1 SpellId
            FROM pf2.Spell
            WHERE AonUrl = ?
        """, aon_url)

        row = cur.fetchone()

        if row:
            return row[0]

    return None


def insert_source_link(cur, spell_id, source_id, source_page, cache):
    if not source_id:
        return

    if not has_column(cur, "pf2", "SpellSourceLink", "SpellId", cache):
        return

    page_column = "PageNumber"

    if not has_column(cur, "pf2", "SpellSourceLink", page_column, cache):
        page_column = None

    if page_column:
        cur.execute(f"""
            IF NOT EXISTS
            (
                SELECT 1
                FROM pf2.SpellSourceLink
                WHERE SpellId = ?
                  AND SourceBookId = ?
                  AND
                  (
                      ({page_column} = ?)
                      OR ({page_column} IS NULL AND ? IS NULL)
                  )
            )
            BEGIN
                INSERT INTO pf2.SpellSourceLink
                (
                    SpellId,
                    SourceBookId,
                    {page_column}
                )
                VALUES
                (?, ?, ?)
            END
        """,
            spell_id,
            source_id,
            source_page,
            source_page,
            spell_id,
            source_id,
            source_page
        )
    else:
        cur.execute("""
            IF NOT EXISTS
            (
                SELECT 1
                FROM pf2.SpellSourceLink
                WHERE SpellId = ?
                  AND SourceBookId = ?
            )
            BEGIN
                INSERT INTO pf2.SpellSourceLink
                (
                    SpellId,
                    SourceBookId
                )
                VALUES
                (?, ?)
            END
        """,
            spell_id,
            source_id,
            spell_id,
            source_id
        )


def insert_spell_trait(cur, spell_id, trait_id, cache):
    if not trait_id:
        return

    if not has_column(cur, "pf2", "SpellTrait", "SpellId", cache):
        return

    cur.execute("""
        IF NOT EXISTS
        (
            SELECT 1
            FROM pf2.SpellTrait
            WHERE SpellId = ?
              AND TraitId = ?
        )
        BEGIN
            INSERT INTO pf2.SpellTrait
            (
                SpellId,
                TraitId
            )
            VALUES
            (?, ?)
        END
    """,
        spell_id,
        trait_id,
        spell_id,
        trait_id
    )


def insert_spell_tradition(cur, spell_id, tradition_id, cache):
    if not tradition_id:
        return

    if not has_column(cur, "pf2", "SpellTradition", "SpellId", cache):
        return

    cur.execute("""
        IF NOT EXISTS
        (
            SELECT 1
            FROM pf2.SpellTradition
            WHERE SpellId = ?
              AND TraditionId = ?
        )
        BEGIN
            INSERT INTO pf2.SpellTradition
            (
                SpellId,
                TraditionId
            )
            VALUES
            (?, ?)
        END
    """,
        spell_id,
        tradition_id,
        spell_id,
        tradition_id
    )


def insert_spell_from_elastic(cur, src, cache):
    name = clean(first_existing(src, "name", "title"))

    if not name:
        raise ValueError("Spell has no name")

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
        "Rank": spell_rank(src),
        "SpellType": clean(first_existing(src, "spell_type", "type")),
        "RarityId": rarity_id,
        "SourceBookId": source_id,
        "SourcePage": source_page,
        "Actions": clean(first_existing(src, "actions", "action", "cast")),
        "TriggerText": clean(first_existing(src, "trigger")),
        "Target": clean(first_existing(src, "target", "targets")),
        "RangeText": clean(first_existing(src, "range_raw", "range")),
        "Area": clean(first_existing(src, "area")),
        "Duration": clean(first_existing(src, "duration")),
        "Defense": clean(first_existing(src, "defense", "saving_throw", "saving_throw_markdown", "save")),
        "Heighten": clean(first_existing(src, "heighten", "heightened", "heighten_group", "heighten_level")),
        "Summary": clean(first_existing(src, "summary")),
        "PFS": clean(first_existing(src, "pfs")),
        "Components": clean(first_existing(src, "component", "components")),
        "School": clean(first_existing(src, "school")),
        "Bloodline": clean(first_existing(src, "bloodline", "bloodline_markdown")),
        "DomainText": clean(first_existing(src, "domain", "domain_markdown")),
        "RemasterId": clean(first_existing(src, "remaster_id")),
        "RemasterName": clean(first_existing(src, "remaster_name")),
        "RawHtml": clean(first_existing(src, "markdown")),
        "RawText": clean(first_existing(src, "text", "search_markdown")),
        "RawJson": raw_json,
        "ScrapeVersion": "aon-elastic-spells-v1"
    }

    spell_id = existing_spell_id(cur, aon_id, aon_url, cache)

    if not spell_id:
        spell_id = insert_spell_row(cur, values, cache)

    insert_source_link(cur, spell_id, source_id, source_page, cache)

    for trait in unique_values(split_values(first_existing(src, "trait", "traits", "trait_raw"))):
        trait_id = get_or_create_lookup(cur, "pf2.Trait", "TraitId", "Name", trait, cache)
        insert_spell_trait(cur, spell_id, trait_id, cache)

    for tradition in unique_values(split_values(first_existing(src, "tradition", "traditions"))):
        tradition_id = get_or_create_lookup(cur, "pf2.Tradition", "TraditionId", "Name", tradition, cache)
        insert_spell_tradition(cur, spell_id, tradition_id, cache)

    return spell_id


def write_import_log(cur, aon_url, success, message, cache):
    if not has_column(cur, "pf2", "SpellImportLog", "AonUrl", cache):
        return

    cur.execute("""
        INSERT INTO pf2.SpellImportLog
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
    print("PF2 AoN Spell Import - ELASTICSEARCH MODE")
    print("=" * 100)

    spells = fetch_all_spells()

    print(f"\nFetched {len(spells)} spell records.")

    if not spells:
        print("No records returned.")
        return

    first_name = clean(first_existing(spells[0], "name", "title")) or "(unknown)"
    print(f"First spell: {first_name}")

    input("\nPress Enter to import into SQL Server, or Ctrl+C to stop...")

    cn = pyodbc.connect(CONN_STR)
    cn.autocommit = False
    cur = cn.cursor()

    cache = {}
    imported = 0
    failed = 0

    for idx, src in enumerate(spells, start=1):
        name = clean(first_existing(src, "name", "title"))
        aon_url = aon_url_from_src(src)

        try:
            spell_id = insert_spell_from_elastic(cur, src, cache)

            write_import_log(
                cur,
                aon_url,
                True,
                f"Imported {name} as SpellId {spell_id}",
                cache
            )

            imported += 1
            cn.commit()

            if imported % 100 == 0:
                print(f"Imported {imported}/{len(spells)} spells...")

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
