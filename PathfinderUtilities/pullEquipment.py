# pip install requests pyodbc

import json
import re
import time
from urllib.parse import parse_qs, urlparse

import pyodbc
import requests

ELASTIC_URL = "https://elasticsearch.aonprd.com/aon/_search"
EQUIPMENT_CATEGORIES = ["equipment", "armor", "weapon", "shield"]
VERBOSE = False

CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;"
    "DATABASE=PathfinderUtil;"
    "Trusted_Connection=yes;"
)

session = requests.Session()
session.headers.update({
    "User-Agent": "PathfinderUtil equipment importer / personal use",
    "Content-Type": "application/json"
})


def clean(v):
    if v is None:
        return None

    if isinstance(v, list):
        return ", ".join(str(x) for x in v if x is not None).strip() or None

    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)

    return re.sub(r"\s+", " ", str(v)).strip() or None


def to_int(v):
    if v is None:
        return None

    if isinstance(v, int):
        return v

    m = re.search(r"-?\d+", str(v))
    return int(m.group(0)) if m else None


def to_decimal(v):
    if v is None:
        return None

    if isinstance(v, (int, float)):
        return v

    text = str(v).strip()

    if text.upper() == "L":
        return 0.1

    if text in ("-", "—", ""):
        return None

    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group(0)) if m else None


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


def fetch_equipment_batch(category, offset=0, size=500):
    payload = {
        "from": offset,
        "size": size,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"category": category}}
                ]
            }
        }
    }

    r = session.post(ELASTIC_URL, json=payload, timeout=60)

    if VERBOSE:
        print("HTTP:", r.status_code, "category:", category, "offset:", offset)
        print("URL:", r.url)

    if r.status_code != 200:
        print("Payload:")
        print(json.dumps(payload, indent=2))
        print(r.text[:2000])
        r.raise_for_status()

    return r.json()


def fetch_all_equipment():
    all_items = []
    seen_keys = set()
    size = 500

    for category in EQUIPMENT_CATEGORIES:
        offset = 0

        while True:
            data = fetch_equipment_batch(category, offset, size)

            hits = data.get("hits", {}).get("hits", [])
            total = data.get("hits", {}).get("total", {})

            total_value = total.get("value") if isinstance(total, dict) else total

            print(f"Fetched {len(hits)} {category} records at offset {offset}; total={total_value}")

            if not hits:
                break

            for h in hits:
                src = h.get("_source", {})
                src["_elastic_id"] = h.get("_id")
                key = clean(first_existing(src, "id")) or h.get("_id")

                if key in seen_keys:
                    continue

                seen_keys.add(key)
                all_items.append(src)

            offset += size

            if total_value is not None and offset >= total_value:
                break

            time.sleep(0.2)

    return sorted(
        all_items,
        key=lambda src: (
            clean(first_existing(src, "category")) or "",
            clean(first_existing(src, "item_category")) or "",
            to_int(first_existing(src, "level")) if to_int(first_existing(src, "level")) is not None else 999,
            clean(first_existing(src, "name")) or ""
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


def aon_key_from_src(src):
    return clean(first_existing(src, "id", "_elastic_id"))


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
    category = clean(first_existing(src, "category"))

    if not aon_id:
        return None

    if category == "armor":
        return f"https://2e.aonprd.com/Armor.aspx?ID={aon_id}"

    if category == "weapon":
        return f"https://2e.aonprd.com/Weapons.aspx?ID={aon_id}"

    if category == "shield":
        return f"https://2e.aonprd.com/Shields.aspx?ID={aon_id}"

    return f"https://2e.aonprd.com/Equipment.aspx?ID={aon_id}"


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


def insert_equipment_row(cur, values, cache):
    columns = [
        "AonId",
        "AonKey",
        "AonUrl",
        "Name",
        "Level",
        "EquipmentType",
        "SearchCategory",
        "ItemCategory",
        "ItemSubcategory",
        "RarityId",
        "SourceBookId",
        "SourcePage",
        "PFS",
        "PriceCp",
        "PriceText",
        "BulkValue",
        "BulkText",
        "Summary",
        "RemasterId",
        "BaseItemText",
        "SpellText",
        "StageText",
        "WeaponCategory",
        "WeaponGroup",
        "WeaponType",
        "Damage",
        "DamageDie",
        "DamageType",
        "Hands",
        "AmmunitionText",
        "ArmorCategory",
        "ArmorGroupText",
        "AC",
        "Hardness",
        "HardnessText",
        "HP",
        "HPText",
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
        if has_column(cur, "pf2", "Equipment", c, cache)
    ]

    if not has_column(cur, "pf2", "Equipment", "EquipmentId", cache):
        raise RuntimeError("pf2.Equipment must have an EquipmentId identity column.")

    placeholders = []
    params = []

    for column in insert_columns:
        if column in ("CreatedAt", "UpdatedAt", "LastScraped"):
            placeholders.append("SYSDATETIME()")
        else:
            placeholders.append("?")
            params.append(values.get(column))

    cur.execute(f"""
        INSERT INTO pf2.Equipment
        (
            {", ".join(insert_columns)}
        )
        OUTPUT INSERTED.EquipmentId
        VALUES
        (
            {", ".join(placeholders)}
        )
    """, *params)

    return cur.fetchone()[0]


def existing_equipment_id(cur, aon_key, aon_url, name, cache):
    if has_column(cur, "pf2", "Equipment", "AonKey", cache) and aon_key:
        cur.execute("""
            SELECT TOP 1 EquipmentId
            FROM pf2.Equipment
            WHERE AonKey = ?
        """, aon_key)

        row = cur.fetchone()

        if row:
            return row[0]

        # Variants often share one Equipment.aspx?ID= URL. If we have a key,
        # do not collapse onto a sibling row that only matches AonUrl.
        return None

    if has_column(cur, "pf2", "Equipment", "AonUrl", cache) and aon_url and name:
        cur.execute("""
            SELECT TOP 1 EquipmentId
            FROM pf2.Equipment
            WHERE Name = ?
              AND AonUrl = ?
        """, name, aon_url)

        row = cur.fetchone()

        if row:
            return row[0]

        return None

    if has_column(cur, "pf2", "Equipment", "AonUrl", cache) and aon_url:
        cur.execute("""
            SELECT TOP 1 EquipmentId
            FROM pf2.Equipment
            WHERE AonUrl = ?
        """, aon_url)

        row = cur.fetchone()

        if row:
            return row[0]

    return None


def insert_source_link(cur, equipment_id, source_id, source_page, cache):
    if not source_id:
        return

    if not has_column(cur, "pf2", "EquipmentSourceLink", "EquipmentId", cache):
        return

    page_column = "PageNumber"

    if not has_column(cur, "pf2", "EquipmentSourceLink", page_column, cache):
        page_column = None

    if page_column:
        cur.execute(f"""
            IF NOT EXISTS
            (
                SELECT 1
                FROM pf2.EquipmentSourceLink
                WHERE EquipmentId = ?
                  AND SourceBookId = ?
                  AND
                  (
                      ({page_column} = ?)
                      OR ({page_column} IS NULL AND ? IS NULL)
                  )
            )
            BEGIN
                INSERT INTO pf2.EquipmentSourceLink
                (
                    EquipmentId,
                    SourceBookId,
                    {page_column}
                )
                VALUES
                (?, ?, ?)
            END
        """,
            equipment_id,
            source_id,
            source_page,
            source_page,
            equipment_id,
            source_id,
            source_page
        )
    else:
        cur.execute("""
            IF NOT EXISTS
            (
                SELECT 1
                FROM pf2.EquipmentSourceLink
                WHERE EquipmentId = ?
                  AND SourceBookId = ?
            )
            BEGIN
                INSERT INTO pf2.EquipmentSourceLink
                (
                    EquipmentId,
                    SourceBookId
                )
                VALUES
                (?, ?)
            END
        """,
            equipment_id,
            source_id,
            equipment_id,
            source_id
        )


def insert_equipment_trait(cur, equipment_id, trait_id, cache):
    if not trait_id:
        return

    if not has_column(cur, "pf2", "EquipmentTrait", "EquipmentId", cache):
        return

    cur.execute("""
        IF NOT EXISTS
        (
            SELECT 1
            FROM pf2.EquipmentTrait
            WHERE EquipmentId = ?
              AND TraitId = ?
        )
        BEGIN
            INSERT INTO pf2.EquipmentTrait
            (
                EquipmentId,
                TraitId
            )
            VALUES
            (?, ?)
        END
    """,
        equipment_id,
        trait_id,
        equipment_id,
        trait_id
    )


def insert_equipment_from_elastic(cur, src, cache):
    name = clean(first_existing(src, "name", "title"))

    if not name:
        raise ValueError("Equipment has no name")

    aon_key = aon_key_from_src(src)
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
        "AonKey": aon_key,
        "AonUrl": aon_url,
        "Name": name,
        "Level": to_int(first_existing(src, "level")),
        "EquipmentType": clean(first_existing(src, "type")),
        "SearchCategory": clean(first_existing(src, "category")),
        "ItemCategory": clean(first_existing(src, "item_category")),
        "ItemSubcategory": clean(first_existing(src, "item_subcategory")),
        "RarityId": rarity_id,
        "SourceBookId": source_id,
        "SourcePage": source_page,
        "PFS": clean(first_existing(src, "pfs")),
        "PriceCp": to_int(first_existing(src, "price")),
        "PriceText": clean(first_existing(src, "price_raw")),
        "BulkValue": to_decimal(first_existing(src, "bulk")),
        "BulkText": clean(first_existing(src, "bulk_raw", "bulk")),
        "Summary": clean(first_existing(src, "summary")),
        "RemasterId": clean(first_existing(src, "remaster_id")),
        "BaseItemText": clean(first_existing(src, "base_item_markdown")),
        "SpellText": clean(first_existing(src, "spell_markdown")),
        "StageText": clean(first_existing(src, "stage_markdown")),
        "WeaponCategory": clean(first_existing(src, "weapon_category")),
        "WeaponGroup": clean(first_existing(src, "weapon_group")),
        "WeaponType": clean(first_existing(src, "weapon_type")),
        "Damage": clean(first_existing(src, "damage")),
        "DamageDie": to_int(first_existing(src, "damage_die")),
        "DamageType": clean(first_existing(src, "damage_type")),
        "Hands": clean(first_existing(src, "hands")),
        "AmmunitionText": clean(first_existing(src, "ammunition_markdown")),
        "ArmorCategory": clean(first_existing(src, "armor_category")),
        "ArmorGroupText": clean(first_existing(src, "armor_group_markdown")),
        "AC": to_int(first_existing(src, "ac")),
        "Hardness": to_int(first_existing(src, "hardness")),
        "HardnessText": clean(first_existing(src, "hardness_raw", "hardness")),
        "HP": to_int(first_existing(src, "hp")),
        "HPText": clean(first_existing(src, "hp_raw", "hp")),
        "RawHtml": clean(first_existing(src, "markdown")),
        "RawText": clean(first_existing(src, "text", "search_markdown")),
        "RawJson": raw_json,
        "ScrapeVersion": "aon-elastic-equipment-v1"
    }

    equipment_id = existing_equipment_id(cur, aon_key, aon_url, name, cache)

    if not equipment_id:
        equipment_id = insert_equipment_row(cur, values, cache)

    insert_source_link(cur, equipment_id, source_id, source_page, cache)

    for trait in unique_values(split_values(first_existing(src, "trait", "traits", "trait_raw"))):
        trait_id = get_or_create_lookup(cur, "pf2.Trait", "TraitId", "Name", trait, cache)
        insert_equipment_trait(cur, equipment_id, trait_id, cache)

    return equipment_id


def write_import_log(cur, aon_url, success, message, cache):
    if not has_column(cur, "pf2", "EquipmentImportLog", "AonUrl", cache):
        return

    cur.execute("""
        INSERT INTO pf2.EquipmentImportLog
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
    print("PF2 AoN Equipment Import - ELASTICSEARCH MODE")
    print("=" * 100)

    equipment = fetch_all_equipment()

    print(f"\nFetched {len(equipment)} equipment records.")

    if not equipment:
        print("No records returned.")
        return

    first_name = clean(first_existing(equipment[0], "name", "title")) or "(unknown)"
    print(f"First equipment: {first_name}")

    input("\nPress Enter to import into SQL Server, or Ctrl+C to stop...")

    cn = pyodbc.connect(CONN_STR)
    cn.autocommit = False
    cur = cn.cursor()

    cache = {}
    imported = 0
    failed = 0

    for idx, src in enumerate(equipment, start=1):
        name = clean(first_existing(src, "name", "title"))
        aon_url = aon_url_from_src(src)

        try:
            equipment_id = insert_equipment_from_elastic(cur, src, cache)

            write_import_log(
                cur,
                aon_url,
                True,
                f"Imported {name} as EquipmentId {equipment_id}",
                cache
            )

            imported += 1
            cn.commit()

            if imported % 100 == 0:
                print(f"Imported {imported}/{len(equipment)} equipment records...")

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
