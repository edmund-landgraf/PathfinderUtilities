# pip install requests pyodbc

import json
import re
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
        return ", ".join(str(x) for x in v if x is not None).strip() or None

    return str(v).strip() or None


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

    return all_items


def split_values(value):
    if not value:
        return []

    if isinstance(value, list):
        return [clean(x) for x in value if clean(x)]

    text = str(value).replace(";", ",")
    return [clean(x) for x in text.split(",") if clean(x)]


def aon_numeric_id(src):
    raw_id = first_existing(src, "id", "aonid", "aon_id")

    if not raw_id:
        return None

    raw_id = str(raw_id)

    if "-" in raw_id:
        raw_id = raw_id.split("-")[-1]

    return to_int(raw_id)


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
        if src.get("npc") is True:
            return f"https://2e.aonprd.com/NPCs.aspx?ID={aon_id}"

        return f"https://2e.aonprd.com/Monsters.aspx?ID={aon_id}"

    return None


def source_page_from_raw(src):
    raw = clean(first_existing(src, "primary_source_raw", "source_raw"))

    if not raw:
        return None

    m = re.search(r"\bpg\.?\s*(\d+)", raw, re.I)

    return int(m.group(1)) if m else None


def insert_monster_from_elastic(cur, src, cache):
    name = clean(first_existing(src, "name", "title"))

    if not name:
        raise ValueError("Monster has no name")

    aon_id = aon_numeric_id(src)
    aon_url = aon_url_from_src(src)

    family = clean(first_existing(src, "creature_family", "creature_family_markdown", "family"))
    source = clean(first_existing(src, "primary_source", "source"))
    rarity = clean(first_existing(src, "rarity"))
    size = clean(first_existing(src, "size"))
    alignment = clean(first_existing(src, "alignment"))

    level = to_int(first_existing(src, "level"))
    source_page = source_page_from_raw(src)

    hp = to_int(first_existing(src, "hp"))
    ac = to_int(first_existing(src, "ac"))

    fort = to_int(first_existing(src, "fortitude_save", "fortitude", "fort"))
    reflex = to_int(first_existing(src, "reflex_save", "reflex", "ref"))
    will = to_int(first_existing(src, "will_save", "will"))

    perception = to_int(first_existing(src, "perception"))

    senses = clean(first_existing(src, "sense", "senses"))
    speed = clean(first_existing(src, "speed_raw", "speed", "speed_markdown"))

    languages = clean(first_existing(src, "language", "language_markdown"))
    skills = clean(first_existing(src, "skill", "skill_markdown"))
    items = clean(first_existing(src, "item"))

    str_mod = to_int(first_existing(src, "strength"))
    dex_mod = to_int(first_existing(src, "dexterity"))
    con_mod = to_int(first_existing(src, "constitution"))
    int_mod = to_int(first_existing(src, "intelligence"))
    wis_mod = to_int(first_existing(src, "wisdom"))
    cha_mod = to_int(first_existing(src, "charisma"))

    weakness = clean(first_existing(src, "weakness"))
    resistance = clean(first_existing(src, "resistance"))

    raw_json = json.dumps(src, ensure_ascii=False)
    markdown = clean(first_existing(src, "markdown"))
    text = clean(first_existing(src, "text", "search_markdown"))

    family_id = get_or_create_lookup(cur, "pf2.MonsterFamily", "FamilyId", "Name", family, cache)
    source_id = get_or_create_lookup(cur, "pf2.SourceBook", "SourceBookId", "Name", source, cache)
    rarity_id = get_or_create_lookup(cur, "pf2.Rarity", "RarityId", "Name", rarity, cache)
    size_id = get_or_create_lookup(cur, "pf2.SizeCategory", "SizeId", "Name", size, cache)
    alignment_id = get_or_create_lookup(cur, "pf2.Alignment", "AlignmentId", "Name", alignment, cache)

    cur.execute("""
        INSERT INTO pf2.Monster
        (
            AonId,
            AonUrl,
            Name,
            Level,
            RarityId,
            SizeId,
            AlignmentId,
            FamilyId,
            SourceBookId,
            SourcePage,
            IsUnique,
            IsNPC,
            RawHtml,
            RawText,
            RawJson,
            CreatedAt,
            UpdatedAt,
            LastScraped,
            ScrapeVersion,
            ImageUrl
        )
        OUTPUT INSERTED.MonsterId
        VALUES
        (?, ?, ?, ?,
         ?, ?, ?, ?,
         ?, ?,
         ?, ?,
         ?, ?, ?,
         SYSDATETIME(),
         SYSDATETIME(),
         SYSDATETIME(),
         ?,
         NULL)
    """,
        aon_id,
        aon_url,
        name,
        level,
        rarity_id,
        size_id,
        alignment_id,
        family_id,
        source_id,
        source_page,
        1 if rarity and rarity.lower() == "unique" else 0,
        1 if src.get("npc") is True else 0,
        markdown,
        text,
        raw_json,
        "aon-elastic-v2"
    )
    monster_id = cur.fetchone()[0]

    cur.execute("""
        INSERT INTO pf2.MonsterStats
        (
            MonsterId,
            Perception,
            Senses,
            Languages,
            Skills,
            Items,
            StrMod,
            DexMod,
            ConMod,
            IntMod,
            WisMod,
            ChaMod,
            AC,
            Fortitude,
            Reflex,
            Will,
            HP,
            Immunities,
            Resistances,
            Weaknesses,
            Speed
        )
        VALUES
        (?, ?, ?, ?, ?, ?,
         ?, ?, ?, ?, ?, ?,
         ?, ?, ?, ?, ?,
         NULL, ?, ?, ?)
    """,
        monster_id,
        perception,
        senses,
        languages,
        skills,
        items,
        str_mod,
        dex_mod,
        con_mod,
        int_mod,
        wis_mod,
        cha_mod,
        ac,
        fort,
        reflex,
        will,
        hp,
        resistance,
        weakness,
        speed
    )

    if source_id:
        cur.execute("""
            INSERT INTO pf2.MonsterSourceLink
            (
                MonsterId,
                SourceBookId,
                PageNumber
            )
            VALUES
            (?, ?, ?)
        """,
            monster_id,
            source_id,
            source_page
        )

    for trait in split_values(first_existing(src, "trait", "trait_raw")):
        trait_id = get_or_create_lookup(cur, "pf2.Trait", "TraitId", "Name", trait, cache)

        cur.execute("""
            INSERT INTO pf2.MonsterTrait
            (
                MonsterId,
                TraitId
            )
            VALUES
            (?, ?)
        """,
            monster_id,
            trait_id
        )

    for ability_name in split_values(first_existing(src, "creature_ability")):
        cur.execute("""
            INSERT INTO pf2.MonsterAbility
            (
                MonsterId,
                Name,
                ActionCost,
                AbilityType,
                Traits,
                Description
            )
            VALUES
            (?, ?, NULL, NULL, NULL, NULL)
        """,
            monster_id,
            ability_name
        )

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
                (
                    AonUrl,
                    ImportedAt,
                    Success,
                    Message
                )
                VALUES
                (?, SYSDATETIME(), 1, ?)
            """,
                aon_url_from_src(src),
                f"Imported {name} as MonsterId {monster_id}"
            )

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
                    (
                        AonUrl,
                        ImportedAt,
                        Success,
                        Message
                    )
                    VALUES
                    (?, SYSDATETIME(), 0, ?)
                """,
                    aon_url_from_src(src),
                    str(ex)[:4000]
                )

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
