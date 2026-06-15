
# pip install requests beautifulsoup4 pyodbc lxml

import re
import time
import json
import requests
import pyodbc
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs

BASE_URL = "https://2e.aonprd.com/"
MONSTER_TABLE_URL = (
    "https://2e.aonprd.com/Monsters.aspx?"
    "Letter=&columns=creature_family+source+rarity+size+trait+level+hp+ac+"
    "fortitude+reflex+will+perception+sense+speed&display=table&sort=name-asc"
)

CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;"
    "DATABASE=PathfinderUtil;"
    "Trusted_Connection=yes;"
)

session = requests.Session()
session.headers.update({
    "User-Agent": "PathfinderUtil monster importer / personal use"
})


def clean(value):
    if value is None:
        return None
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def to_int(value):
    if value is None:
        return None
    m = re.search(r"-?\d+", value)
    return int(m.group(0)) if m else None


def aon_id_from_href(href):
    if not href:
        return None
    qs = parse_qs(urlparse(href).query)
    if "ID" in qs:
        try:
            return int(qs["ID"][0])
        except Exception:
            return None
    return None


def fetch_html(url):
    r = session.get(url, timeout=60)
    r.raise_for_status()
    return r.text


def find_monster_table(soup):
    tables = soup.find_all("table")
    best = None
    best_score = -1

    for table in tables:
        text = table.get_text(" ", strip=True).lower()
        score = 0
        for word in ["creature", "level", "rarity", "size", "trait", "hp", "ac", "fortitude"]:
            if word in text:
                score += 1
        rows = table.find_all("tr")
        score += min(len(rows), 10)

        if score > best_score:
            best = table
            best_score = score

    if not best:
        raise RuntimeError("Could not find monster table on page.")

    return best


def parse_table(html):
    soup = BeautifulSoup(html, "lxml")
    table = find_monster_table(soup)

    rows = table.find_all("tr")
    header_cells = rows[0].find_all(["th", "td"])
    headers = [clean(c.get_text(" ", strip=True)) for c in header_cells]

    monsters = []

    for tr in rows[1:]:
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue

        values = {}
        for i, cell in enumerate(cells):
            key = headers[i] if i < len(headers) else f"col_{i}"
            values[key] = clean(cell.get_text(" ", strip=True))

        first_link = tr.find("a", href=re.compile(r"Monsters\.aspx\?ID=", re.I))
        if not first_link:
            continue

        href = first_link.get("href")
        aon_url = urljoin(BASE_URL, href)
        aon_id = aon_id_from_href(href)

        name = clean(first_link.get_text(" ", strip=True))

        monster = {
            "AonId": aon_id,
            "AonUrl": aon_url,
            "Name": name,
            "RawJson": json.dumps(values, ensure_ascii=False),
            "raw": values
        }

        monsters.append(monster)

    return monsters


def pick(row, *names):
    raw = row["raw"]
    lowered = {k.lower(): v for k, v in raw.items() if k}
    for name in names:
        n = name.lower()
        for k, v in lowered.items():
            if k == n or n in k:
                return v
    return None


def split_traits(value):
    if not value:
        return []
    value = value.replace(";", ",")
    parts = [clean(x) for x in value.split(",")]
    return [x for x in parts if x]


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


def insert_monster(cur, m, cache):
    family = pick(m, "Family", "Creature Family")
    source = pick(m, "Source")
    rarity = pick(m, "Rarity")
    size = pick(m, "Size")
    traits = pick(m, "Trait", "Traits")

    level = to_int(pick(m, "Level"))
    hp = to_int(pick(m, "HP"))
    ac = to_int(pick(m, "AC"))
    fort = to_int(pick(m, "Fortitude", "Fort"))
    reflex = to_int(pick(m, "Reflex", "Ref"))
    will = to_int(pick(m, "Will"))
    perception = to_int(pick(m, "Perception"))

    senses = pick(m, "Sense", "Senses")
    speed = pick(m, "Speed")

    family_id = get_or_create_lookup(cur, "pf2.MonsterFamily", "FamilyId", "Name", family, cache)
    source_id = get_or_create_lookup(cur, "pf2.SourceBook", "SourceBookId", "Name", source, cache)
    rarity_id = get_or_create_lookup(cur, "pf2.Rarity", "RarityId", "Name", rarity, cache)
    size_id = get_or_create_lookup(cur, "pf2.SizeCategory", "SizeId", "Name", size, cache)

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
        (?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?, 0, NULL, NULL, ?, SYSDATETIME(), SYSDATETIME(), SYSDATETIME(), ?, NULL)
    """,
        m["AonId"],
        m["AonUrl"],
        m["Name"],
        level,
        rarity_id,
        size_id,
        family_id,
        source_id,
        1 if rarity and rarity.lower() == "unique" else 0,
        m["RawJson"],
        "aon-table-v1"
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
    print("PF2 AoN Monster Import - DEBUG MODE")
    print("=" * 100)

    print("\nMONSTER_TABLE_URL:")
    print(MONSTER_TABLE_URL)

    try:
        html = fetch_html(MONSTER_TABLE_URL)
    except Exception as ex:
        print("\nFETCH FAILED")
        print(type(ex).__name__)
        print(ex)
        return

    print("\nHTML DEBUG")
    print("-" * 100)
    print("HTML length:", len(html))
    print("Contains '<table':", "<table" in html.lower())
    print("Contains 'Monsters.aspx?ID=':", "Monsters.aspx?ID=" in html)
    print("Contains 'creature':", "creature" in html.lower())
    print("Contains 'Just a moment':", "just a moment" in html.lower())
    print("Contains 'Cloudflare':", "cloudflare" in html.lower())

    with open("aon_monsters_debug.html", "w", encoding="utf-8") as f:
        f.write(html)

    print("\nSaved full HTML to:")
    print("aon_monsters_debug.html")

    print("\nFIRST 3000 CHARS")
    print("-" * 100)
    print(html[:3000])

    soup = BeautifulSoup(html, "lxml")

    print("\nSOUP DEBUG")
    print("-" * 100)
    print("Title:", soup.title.get_text(" ", strip=True) if soup.title else "NO TITLE")
    print("Table count:", len(soup.find_all("table")))
    print("TR count:", len(soup.find_all("tr")))
    print("TD count:", len(soup.find_all("td")))
    print("A/link count:", len(soup.find_all("a")))

    all_links = soup.find_all("a")
    monster_links = []

    for a in all_links:
        href = a.get("href") or ""
        text = a.get_text(" ", strip=True)

        if "Monsters.aspx" in href:
            monster_links.append((href, text))

    print("\nMONSTER LINK DEBUG")
    print("-" * 100)
    print("Links containing Monsters.aspx:", len(monster_links))

    for href, text in monster_links[:50]:
        print(f"{href} => {text}")

    with open("aon_monsters_links_debug.txt", "w", encoding="utf-8") as f:
        for a in all_links:
            href = a.get("href")
            text = a.get_text(" ", strip=True)
            if href:
                f.write(f"{href}\t{text}\n")

    print("\nSaved all links to:")
    print("aon_monsters_links_debug.txt")

    print("\nTABLE DEBUG")
    print("-" * 100)

    tables = soup.find_all("table")

    for i, table in enumerate(tables):
        rows = table.find_all("tr")
        table_text = table.get_text(" ", strip=True)

        print("=" * 100)
        print(f"TABLE #{i}")
        print("Rows:", len(rows))
        print("Text length:", len(table_text))
        print("First 1500 chars:")
        print(table_text[:1500])

        if rows:
            print("\nFirst 5 rows:")
            for r_idx, tr in enumerate(rows[:5]):
                cells = tr.find_all(["th", "td"])
                values = [c.get_text(" ", strip=True) for c in cells]
                print(f"Row {r_idx}: {values}")

    try:
        monsters = parse_table(html)
    except Exception as ex:
        print("\nPARSE FAILED")
        print(type(ex).__name__)
        print(ex)
        return

    print("\nPARSE RESULT")
    print("-" * 100)
    print(f"Parsed {len(monsters)} monsters from AoN table.")

    if monsters:
        print("\nFirst 5 parsed monsters:")
        for m in monsters[:5]:
            print(json.dumps(m, indent=2, ensure_ascii=False)[:3000])
    else:
        print("\nNO MONSTERS PARSED.")
        print("Check aon_monsters_debug.html and aon_monsters_links_debug.txt")

        print("\nLikely causes:")
        print("1. AoN returned a script-rendered page instead of a static table.")
        print("2. AoN blocked the request.")
        print("3. Links do not use Monsters.aspx?ID= anymore.")
        print("4. Table headers differ from expected format.")
        return

    input("\nPress Enter to continue with database import, or Ctrl+C to stop...")

    print("\nCONNECTING TO SQL SERVER")
    print("-" * 100)

    try:
        cn = pyodbc.connect(CONN_STR)
    except Exception as ex:
        print("SQL CONNECTION FAILED")
        print(type(ex).__name__)
        print(ex)
        return

    cn.autocommit = False
    cur = cn.cursor()

    cache = {}
    imported = 0
    failed = 0

    print("\nSTARTING IMPORT")
    print("-" * 100)

    for idx, m in enumerate(monsters, start=1):
        print(f"\n[{idx}/{len(monsters)}] Importing: {m.get('Name')} | {m.get('AonUrl')}")

        try:
            monster_id = insert_monster(cur, m, cache)

            cur.execute("""
                INSERT INTO pf2.MonsterImportLog
                (AonUrl, ImportedAt, Success, Message)
                VALUES (?, SYSDATETIME(), 1, ?)
            """, m["AonUrl"], f"Imported {m['Name']} as MonsterId {monster_id}")

            imported += 1

            print(f"SUCCESS MonsterId={monster_id}")

            if imported % 100 == 0:
                cn.commit()
                print(f"COMMITTED {imported} monsters...")

        except Exception as ex:
            failed += 1

            print("FAILED")
            print("Monster object:")
            print(json.dumps(m, indent=2, ensure_ascii=False)[:4000])
            print("Exception type:", type(ex).__name__)
            print("Exception:", ex)

            cn.rollback()
            cur = cn.cursor()

            try:
                cur.execute("""
                    INSERT INTO pf2.MonsterImportLog
                    (AonUrl, ImportedAt, Success, Message)
                    VALUES (?, SYSDATETIME(), 0, ?)
                """, m.get("AonUrl"), str(ex)[:4000])
                cn.commit()
            except Exception as log_ex:
                print("ALSO FAILED TO WRITE IMPORT LOG")
                print(type(log_ex).__name__)
                print(log_ex)

        time.sleep(0.05)

    print("\nFINAL COMMIT")
    print("-" * 100)

    try:
        cn.commit()
    except Exception as ex:
        print("FINAL COMMIT FAILED")
        print(type(ex).__name__)
        print(ex)

    cur.close()
    cn.close()

    print("\nDONE")
    print("-" * 100)
    print(f"Imported={imported}, Failed={failed}")


if __name__ == "__main__":
    main()