# pip install requests pyodbc

import json
import time
import requests
import pyodbc

ELASTIC_URL = "https://elasticsearch.aonprd.com/aon/_search"

CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;"
    "DATABASE=PathfinderUtil;"
    "Trusted_Connection=yes;"
)

session = requests.Session()
session.headers.update({
    "User-Agent": "PathfinderUtil monster importer / personal use",
    "Content-Type": "application/json"
})


def clean(v):
    if v is None:
        return None
    if isinstance(v, list):
        return ", ".join(str(x) for x in v if x is not None)
    return str(v).strip() or None


def to_int(v):
    try:
        return int(v)
    except Exception:
        return None


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

    cur.execute(f"INSERT INTO {table} ({name_col}) VALUES (?)", name)
    cur.execute("SELECT CONVERT(INT, SCOPE_IDENTITY())")
    new_id = cur.fetchone()[0]
    cache[key] = new_id
    return new_id


def fetch_monster_batch(offset=0, size=500):
    payload = {
        "from": offset,
        "size": size,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"category": "creature"}}
                ]
            }
        },
        "sort": [
            {"name.keyword": {"order": "asc"}}
        ]
    }

    r = session.post(ELASTIC_URL, json=payload, timeout=60)

    print("HTTP:", r.status_code, "offset:", offset)
    print("URL:", r.url)

    if r.status_code != 200:
        print(r.text[:2000])
        r.raise_for_status()

    return r.json()


def fetch_all_monsters():
    all_items = []
    offset = 0
    size = 500

    while True:
        data = fetch_monster_batch(offset, size)

        hits = data.get("hits", {}).get("hits", [])
        total = data.get("hits", {}).get("total", {})

        if isinstance(total, dict):
            total_value = total.get("value")
        else:
            total_value = total

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

    return all_items


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


def split_traits(value):
    value = first_existing(value, "trait", "traits") if isinstance(value, dict) else value

    if not value:
        return []

    if isinstance(value, list):
        return [clean(x) for x in value if clean(x)]

    text = str(value).replace(";", ",")
    return [clean(x) for x in text.split(",") if clean(x)]


def aon_url_from_src(src):
    url = first_existing(src, "url", "link", "href")
    if url:
        url = str(url)
        if url.startswith("/"):
            return "https://2e.aonprd.com" + url
        if url.startswith("http"):
            return url

    aon_id = first_existing(src, "id", "aonid", "aon_id")
    if aon_id:
        return f"https://2e.aonprd.com/Monsters.aspx?ID={aon_id}"

    return None


def insert_monster_from_elastic(cur, src, cache):
    name = clean(first_existing(src, "name", "title"))
    if not name:
        raise ValueError("Monster has no name")

    aon_id = to_int(first_existing(src, "id", "aonid", "aon_id"))
    aon_url = aon_url_from_src(src)

    family = first_existing(src, "creature_family", "family", "creatureFamily")
    source = first_existing(src, "source", "source_book", "book")
    rarity = first_existing(src, "rarity")
    size = first_existing(src, "size")
    level = to_int(first_existing(src, "level"))

    hp = to_int(first_existing(src, "hp", "hit_points"))
    ac = to_int(first_existing(src, "ac", "armor_class"))
    fort = to_int(first_existing(src, "fortitude", "fort"))
    reflex = to_int(first_existing(src, "reflex", "ref"))
    will = to_int(first_existing(src, "will"))
    perception = to_int(first_existing(src, "perception"))

    senses = clean(first_existing(src, "sense", "senses"))
    speed = clean(first_existing(src, "speed", "speeds"))

    family_id = get_or_create_lookup(cur, "pf2.MonsterFamily", "FamilyId", "Name", family, cache)
    source_id = get_or_create_lookup(cur, "pf2.SourceBook", "SourceBookId", "Name", source, cache)
    rarity_id = get_or_create_lookup(cur, "pf2.Rarity", "RarityId", "Name", rarity, cache)
    size_id = get_or_create_lookup(cur, "pf2.SizeCategory", "SizeId", "Name", size, cache)

    raw_json = json.dumps(src, ensure_ascii=False)

    cur.execute("""
        INSERT INTO pf2.Monster
        (
            AonId, AonUrl, Name, Level,
            RarityId, SizeId, AlignmentId, FamilyId,
            SourceBookId, SourcePage,
            IsUnique, IsNPC,
            RawHtml, RawText, RawJson,
            CreatedAt, UpdatedAt,
            LastScraped, ScrapeVersion, ImageUrl
        )
        VALUES
        (?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL,
         ?, 0,
         NULL, NULL, ?,
         SYSDATETIME(), SYSDATETIME(),
         SYSDATETIME(), ?, NULL)
    """,
        aon_id,
        aon_url,
        name,
        level,
        rarity_id,
        size_id,
        family_id,
        source_id,
        1 if clean(rarity) and clean(rarity).lower() == "unique" else 0,
        raw_json,
        "aon-elastic-v1"
    )

    cur.execute("SELECT CONVERT(INT, SCOPE_IDENTITY())")
    monster_id = cur.fetchone()[0]

    cur.execute("""
        INSERT INTO pf2.MonsterStats
        (
            MonsterId, Perception, Senses, Languages, Skills, Items,
            StrMod, DexMod, ConMod, IntMod, WisMod, ChaMod,
            AC, Fortitude, Reflex, Will, HP,
            Immunities, Resistances, Weaknesses, Speed
        )
        VALUES
        (?, ?, ?, NULL, NULL, NULL,
         NULL, NULL, NULL, NULL, NULL, NULL,
         ?, ?, ?, ?, ?,
         NULL, NULL, NULL, ?)
    """,
        monster_id,
        perception,
        senses,
        ac,
        fort,
        reflex,
        will,
        hp,
        speed
    )

    if source_id:
        cur.execute("""
            INSERT INTO pf2.MonsterSourceLink
            (MonsterId, SourceBookId, PageNumber)
            VALUES (?, ?, NULL)
        """, monster_id, source_id)

    traits = first_existing(src, "trait", "traits")

    for trait in split_traits(traits):
        trait_id = get_or_create_lookup(cur, "pf2.Trait", "TraitId", "Name", trait, cache)
        cur.execute("""
            INSERT INTO pf2.MonsterTrait
            (MonsterId, TraitId)
            VALUES (?, ?)
        """, monster_id, trait_id)

    return monster_id


def main():
    print("=" * 100)
    print("PF2 AoN Monster Import - ELASTICSEARCH MODE")
    print("=" * 100)

    monsters = fetch_all_monsters()

    print(f"\nFetched {len(monsters)} creature records.")

    if not monsters:
        print("No records returned.")
        return

    print("\nFirst record keys:")
    print(sorted(monsters[0].keys()))

    print("\nFirst record sample:")
    print(json.dumps(monsters[0], indent=2, ensure_ascii=False)[:5000])

    input("\nPress Enter to import into SQL Server, or Ctrl+C to stop...")

    cn = pyodbc.connect(CONN_STR)
    cn.autocommit = False
    cur = cn.cursor()

    cache = {}
    imported = 0
    failed = 0

    for idx, src in enumerate(monsters, start=1):
        name = clean(first_existing(src, "name", "title"))

        try:
            monster_id = insert_monster_from_elastic(cur, src, cache)

            cur.execute("""
                INSERT INTO pf2.MonsterImportLog
                (AonUrl, ImportedAt, Success, Message)
                VALUES (?, SYSDATETIME(), 1, ?)
            """, aon_url_from_src(src), f"Imported {name} as MonsterId {monster_id}")

            imported += 1

            if imported % 100 == 0:
                cn.commit()
                print(f"Committed {imported}/{len(monsters)} monsters...")

        except Exception as ex:
            failed += 1
            cn.rollback()
            cur = cn.cursor()

            print(f"FAILED [{idx}] {name}: {ex}")
            print(json.dumps(src, indent=2, ensure_ascii=False)[:3000])

            try:
                cur.execute("""
                    INSERT INTO pf2.MonsterImportLog
                    (AonUrl, ImportedAt, Success, Message)
                    VALUES (?, SYSDATETIME(), 0, ?)
                """, aon_url_from_src(src), str(ex)[:4000])
                cn.commit()
            except Exception:
                pass

        time.sleep(0.02)

    cn.commit()
    cur.close()
    cn.close()

    print(f"\nDone. Imported={imported}, Failed={failed}")


if __name__ == "__main__":
    main()