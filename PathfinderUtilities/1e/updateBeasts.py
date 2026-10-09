import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mssql_conn import sql_auth
import argparse
import re
import time
import pyodbc
import requests
from urllib.parse import urljoin
from bs4 import BeautifulSoup

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;"
    "DATABASE=PathfinderUtil;"
    + sql_auth()
    + "TrustServerCertificate=yes;"
)

BASE_URL = "https://www.aonprd.com/"
LETTERS = [chr(code) for code in range(ord("A"), ord("Z") + 1)]

session = requests.Session()
session.headers.update({
    "User-Agent": "PathfinderUtil 1e beast updater / personal use"
})


def clean_text(text):
    return re.sub(r"\s+", " ", text).strip() if text else None


def name_words(name):
    text = clean_text(name) or ""
    text = text.lower().replace("'", "").replace("'", "")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    words = []
    for word in text.split():
        if len(word) > 4 and word.endswith("s"):
            word = word[:-1]
        words.append(word)
    return frozenset(words)


def candidate_names(display_name, link):
    names = [display_name]
    item = re.search(r"ItemName=([^&#]+)", link or "")
    if item:
        names.append(requests.utils.unquote(item.group(1)).replace("+", " "))
    for name in list(names):
        bare = re.sub(r"\([^)]*\)", " ", name)
        inner = re.findall(r"\(([^)]*)\)", name)
        names.append(bare)
        names.extend(inner)
    return names


def creature_group(creature_type):
    text = clean_text(creature_type)
    if not text:
        return None
    return re.split(r"[\s,(]", text, maxsplit=1)[0].lower()


def fetch_letter(letter):
    url = f"{BASE_URL}Monsters.aspx?Letter={letter}"
    print(f"Fetching {url}")
    resp = session.get(url, timeout=60)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    found = []
    seen = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if "MonsterDisplay.aspx" not in href:
            continue

        name = clean_text(anchor.get_text())
        if not name:
            continue

        link = urljoin(BASE_URL, href)
        if link in seen:
            continue
        seen.add(link)

        row = anchor.find_parent("tr")
        cells = [clean_text(td.get_text()) for td in row.find_all("td")] if row else []
        creature_type = cells[2] if len(cells) > 2 else None
        found.append((name, link, creature_group(creature_type)))

    print(f"  {len(found)} creatures")
    return found


def get_existing(cursor):
    cursor.execute("SELECT Name, Link FROM pf1_Beast")
    names = set()
    links = set()
    for name, link in cursor.fetchall():
        words = name_words(name)
        if words:
            names.add(words)
        if link:
            links.add(link.strip())
    return names, links


def get_or_create_group(cursor, group_name, cache):
    if not group_name:
        return None
    if group_name in cache:
        return cache[group_name]

    cursor.execute("SELECT GroupId FROM pf1_BeastGroup WHERE GroupName = ?", group_name)
    row = cursor.fetchone()
    if row:
        cache[group_name] = row[0]
        return row[0]

    cursor.execute("""
        INSERT INTO pf1_BeastGroup (GroupName)
        OUTPUT INSERTED.GroupId
        VALUES (?)
    """, group_name)
    group_id = cursor.fetchone()[0]
    cache[group_name] = group_id
    return group_id


def insert_beast(cursor, name, link, group_id):
    cursor.execute("""
        INSERT INTO pf1_Beast (Name, Link, GroupId)
        OUTPUT INSERTED.BeastId
        SELECT ?, ?, ?
        WHERE NOT EXISTS (
            SELECT 1 FROM pf1_Beast WHERE Link = ?
        )
    """, name, link, group_id, link)
    row = cursor.fetchone()
    return row[0] if row else None


def main():
    parser = argparse.ArgumentParser(
        description="Insert 1e creatures listed on Archives of Nethys that are not already in pf1_Beast."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Insert the new rows. Without this flag the script only prints them.",
    )
    args = parser.parse_args()

    print("=" * 80)
    print("PF1 AoN new-creature check")
    print("Lists Monsters.aspx A-Z and skips names or links already in pf1_Beast.")
    print("=" * 80)

    listed = []
    for letter in LETTERS:
        listed.extend(fetch_letter(letter))
        time.sleep(0.3)

    cn = pyodbc.connect(CONN_STR)
    cn.autocommit = False
    cursor = cn.cursor()
    existing_names, existing_links = get_existing(cursor)
    print(f"Database has {len(existing_links)} links and {len(existing_names)} distinct names.")

    new_rows = []
    seen_names = set()
    for name, link, group in listed:
        keys = [name_words(candidate) for candidate in candidate_names(name, link)]
        keys = [key for key in keys if key]
        if link in existing_links or any(key in existing_names for key in keys):
            continue
        identity = keys[0]
        if identity in seen_names:
            continue
        seen_names.add(identity)
        new_rows.append((name, link, group))

    print(f"\nNew creatures: {len(new_rows)}")
    for name, link, group in new_rows:
        print(f"  {name} [{group or 'no group'}] {link}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to insert these rows.")
        cursor.close()
        cn.close()
        return

    cache = {}
    inserted = 0
    for name, link, group in new_rows:
        group_id = get_or_create_group(cursor, group, cache)
        beast_id = insert_beast(cursor, name, link, group_id)
        if beast_id:
            inserted += 1
            print(f"Inserted BeastId={beast_id}: {name}")

    cn.commit()
    print(f"\nDone. Inserted {inserted}.")
    cursor.close()
    cn.close()


if __name__ == "__main__":
    main()
