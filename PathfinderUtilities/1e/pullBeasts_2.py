import re
import time
import pyodbc
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;"
    "DATABASE=PathfinderUtil;"
    "Trusted_Connection=yes;"
)

BASE_URL = "https://www.d20pfsrd.com"

PAGES = [
    ("A-B", "bestiary-a-b"),
    ("C-D", "bestiary-c-d"),
    ("E-F", "bestiary-e-f"),
    ("G-H", "bestiary-g-h"),
    ("I-J", "bestiary-i-j"),
    ("K-L", "bestiary-k-l"),
    ("M-N", "bestiary-m-n"),
    ("O-P", "bestiary-o-p"),
    ("Q-R", "bestiary-q-r"),
    ("S-T", "bestiary-s-t"),
    ("U-V", "bestiary-u-v"),
    ("W-X", "bestiary-w-x"),
    ("Y-Z", "bestiary-y-z"),
]

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; BeastScraper/2.0; +https://www.example.com)"
})

# ------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------
def clean_text(text):
    return re.sub(r"\s+", " ", text).strip() if text else None


def normalize_url(href):
    """
    Build a full absolute URL and preserve the full monster path.

    Important:
    Do NOT shorten:
      /animals/birds/vulture/
    into:
      /animals/vulture

    The deeper path is the real d20PFSRD canonical location.
    """
    if not href:
        return None

    href = href.strip()

    if href.startswith("#"):
        return None

    full_url = urljoin(BASE_URL, href)

    parsed = urlparse(full_url)

    if "d20pfsrd.com" not in parsed.netloc.lower():
        return None

    # Drop fragments and query strings but keep full path depth.
    path = parsed.path

    if "/bestiary/monster-listings/" not in path.lower():
        return None

    # Skip category landing pages that are probably not monster detail pages.
    parts = [p for p in path.split("/") if p]
    try:
        idx = parts.index("monster-listings")
    except ValueError:
        return None

    after = parts[idx + 1:]

    # Need at least group + monster slug.
    # Good:
    #   animals/birds/vulture
    #   dragons/chromatic/black/black-dragon
    #
    # Bad/category-ish:
    #   animals
    if len(after) < 2:
        return None

    # Restore clean URL with trailing slash.
    clean_path = "/" + "/".join(parts) + "/"
    return f"{parsed.scheme}://{parsed.netloc}{clean_path}"


def extract_group_from_url(url):
    """
    Extract first group after monster-listings.

    Example:
    /bestiary/monster-listings/animals/birds/vulture/
    -> animals
    """
    pattern = r"/bestiary/monster-listings/([^/]+)(?:/|$)"
    match = re.search(pattern, url.lower())
    return match.group(1) if match else None


def get_beast_links_from_page(url):
    print(f"Fetching index page: {url} ...")

    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"  Failed to fetch: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    links = []
    seen = set()

    for a in soup.find_all("a", href=True):
        name = clean_text(a.get_text())
        if not name:
            continue

        full_url = normalize_url(a["href"])
        if not full_url:
            continue

        if full_url in seen:
            continue
        seen.add(full_url)

        group = extract_group_from_url(full_url)
        links.append((name, full_url, group))

    return links


# ------------------------------------------------------------
# Database functions
# ------------------------------------------------------------
def get_existing_links(cursor):
    cursor.execute("SELECT Link FROM pf1_Beast")
    return {row[0] for row in cursor.fetchall()}


def get_or_create_group(cursor, group_name):
    if not group_name:
        return None

    cursor.execute("SELECT GroupId FROM pf1_BeastGroup WHERE GroupName = ?", group_name)
    row = cursor.fetchone()
    if row:
        return row[0]

    cursor.execute("""
        INSERT INTO pf1_BeastGroup (GroupName)
        OUTPUT INSERTED.GroupId
        VALUES (?)
    """, group_name)

    return cursor.fetchone()[0]


def insert_beast(cursor, name, link, group_id):
    cursor.execute("""
        INSERT INTO pf1_Beast (Name, Link, GroupId)
        SELECT ?, ?, ?
        WHERE NOT EXISTS (
            SELECT 1 FROM pf1_Beast WHERE Link = ?
        )
    """, name, link, group_id, link)


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
def main():
    print("=" * 80)
    print("d20PFSRD Beast Scraper v2")
    print("=" * 80)
    print("Preserves full monster URL paths.")
    print("Example: /animals/birds/vulture/ stays /animals/birds/vulture/")
    print("=" * 80)

    try:
        cn = pyodbc.connect(CONN_STR)
        cursor = cn.cursor()
        print("Connected to SQL Server.")
    except Exception as e:
        print(f"SQL connection failed: {e}")
        return

    existing_links = get_existing_links(cursor)
    print(f"Already have {len(existing_links)} beasts in database.")

    total_new = 0
    group_cache = {}

    for label, slug in PAGES:
        url = f"{BASE_URL}/bestiary/bestiary-alphabetical/{slug}/"
        beasts = get_beast_links_from_page(url)

        if not beasts:
            print(f"  No beasts found for {label}.")
            continue

        print(f"  Found {len(beasts)} potential entries for {label}.")
        new_count = 0

        for name, link, group in beasts:
            if link in existing_links:
                continue

            try:
                group_id = None

                if group:
                    if group in group_cache:
                        group_id = group_cache[group]
                    else:
                        group_id = get_or_create_group(cursor, group)
                        group_cache[group] = group_id

                insert_beast(cursor, name, link, group_id)

                new_count += 1
                existing_links.add(link)

            except Exception as e:
                print(f"    Failed to insert {name} ({link}): {e}")
                cn.rollback()

        if new_count:
            cn.commit()
            total_new += new_count
            print(f"    Inserted {new_count} new beasts for {label}.")
        else:
            print(f"    No new beasts for {label}.")

        time.sleep(1)

    print(f"\nDone. Inserted {total_new} new beasts in total.")

    cursor.close()
    cn.close()


if __name__ == "__main__":
    main()