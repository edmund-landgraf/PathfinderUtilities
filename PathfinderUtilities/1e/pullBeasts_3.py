import re
import csv
import time
import logging
import pyodbc
import requests
from datetime import datetime
from bs4 import BeautifulSoup
from bs4.element import Tag
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

WAIT_SECONDS = 5
INDEX_WAIT_SECONDS = 1
BATCH_SIZE = 2000

MAIN_LOG_FILE = "beast_scraper.log"
FAILURE_LOG_FILE = "beast_failures.csv"

# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------
logging.basicConfig(
    filename=MAIN_LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding="utf-8"
)

console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logging.getLogger("").addHandler(console)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; BeastScraper/3.0; +https://www.example.com)"
})

# ------------------------------------------------------------
# Failure logging
# ------------------------------------------------------------
def init_failure_log():
    try:
        with open(FAILURE_LOG_FILE, "x", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "LoggedAt",
                "BeastId",
                "Name",
                "Link",
                "Stage",
                "Error"
            ])
    except FileExistsError:
        pass


def log_failure(beast_id, name, link, stage, error):
    with open(FAILURE_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now().isoformat(timespec="seconds"),
            beast_id or "",
            name or "",
            link or "",
            stage,
            str(error)
        ])

# ------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------
def clean_text(text):
    return re.sub(r"\s+", " ", text).strip() if text else None


def normalize_url(href):
    if not href:
        return None

    href = href.strip()

    if href.startswith("#"):
        return None

    full_url = urljoin(BASE_URL, href)
    parsed = urlparse(full_url)

    if "d20pfsrd.com" not in parsed.netloc.lower():
        return None

    path = parsed.path

    if "/bestiary/monster-listings/" not in path.lower():
        return None

    parts = [p for p in path.split("/") if p]

    try:
        idx = parts.index("monster-listings")
    except ValueError:
        return None

    after = parts[idx + 1:]

    if len(after) < 2:
        return None

    clean_path = "/" + "/".join(parts) + "/"
    return f"{parsed.scheme}://{parsed.netloc}{clean_path}"


def extract_group_from_url(url):
    pattern = r"/bestiary/monster-listings/([^/]+)(?:/|$)"
    match = re.search(pattern, url.lower())
    return match.group(1) if match else None


def get_beast_links_from_page(url):
    logging.info(f"Fetching index page: {url}")

    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        logging.error(f"Failed to fetch index page {url}: {e}")
        log_failure("", "", url, "INDEX_FETCH", e)
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
# HTML cleaning
# ------------------------------------------------------------
def clean_monster_html(raw_html, url):
    try:
        soup = BeautifulSoup(raw_html, "html.parser")

        article = soup.find("div", id="article-content")
        if not article:
            article = soup.find("div", class_="article-content")
            if not article:
                return None, f"No article-content div found for {url}"

        for bad in article.find_all(["div", "p"]):
            if not isinstance(bad, Tag):
                continue

            cls = bad.get("class")
            if cls and "breadcrumbs" in cls:
                bad.decompose()
                continue

            if bad.get("id") and bad.get("id").startswith("attachment_"):
                bad.decompose()
                continue

            style = bad.get("style")
            if style and "text-align:center" in style:
                bad.decompose()
                continue

            if bad.name == "p" and "Subscribe to the Open Gaming Network" in bad.get_text():
                bad.decompose()
                continue

        ecology = article.find("p", class_="divider", string=re.compile("ECOLOGY", re.I))
        if ecology:
            for sibling in ecology.find_all_next():
                sibling.decompose()
            ecology.decompose()

        desc = article.find("p", class_="description")
        if desc:
            desc.decompose()

        cleaned = str(article)
        cleaned = re.sub(r"\s+", " ", cleaned)
        cleaned = re.sub(r">\s+<", "><", cleaned)

        if len(cleaned) < 100:
            return None, f"Cleaned HTML too short ({len(cleaned)} chars) for {url}"

        return cleaned, None

    except Exception as e:
        return None, f"Cleaning error for {url}: {e}"


def fetch_and_clean(url):
    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return clean_monster_html(resp.text, url)

    except requests.exceptions.RequestException as e:
        return None, f"Request error: {e}"

    except Exception as e:
        return None, f"Unexpected fetch error: {e}"

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
        OUTPUT INSERTED.BeastId
        SELECT ?, ?, ?
        WHERE NOT EXISTS (
            SELECT 1 FROM pf1_Beast WHERE Link = ?
        )
    """, name, link, group_id, link)

    row = cursor.fetchone()
    return row[0] if row else None


def get_beasts_without_html(cursor):
    cursor.execute("""
        SELECT BeastId, Name, Link
        FROM pf1_Beast
        WHERE html_block IS NULL
        ORDER BY BeastId
    """)
    return cursor.fetchall()


def update_beast_html(cursor, beast_id, cleaned_html):
    cursor.execute("""
        UPDATE pf1_Beast
        SET html_block = ?
        WHERE BeastId = ?
    """, cleaned_html, beast_id)

    return cursor.rowcount

# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
def main():
    init_failure_log()

    logging.info("=" * 80)
    logging.info("d20PFSRD Beast Scraper v3")
    logging.info("Index scrape + insert + HTML scrape + failure CSV logging")
    logging.info(f"Failure log: {FAILURE_LOG_FILE}")
    logging.info("=" * 80)

    try:
        cn = pyodbc.connect(CONN_STR)
        cn.autocommit = False
        cursor = cn.cursor()
        logging.info("Connected to SQL Server.")
    except Exception as e:
        logging.error(f"SQL connection failed: {e}")
        log_failure("", "", "", "SQL_CONNECT", e)
        return

    existing_links = get_existing_links(cursor)
    logging.info(f"Already have {len(existing_links)} beasts in database.")

    total_new = 0
    group_cache = {}

    # --------------------------------------------------------
    # Phase 1: scrape index pages and insert missing beasts
    # --------------------------------------------------------
    for label, slug in PAGES:
        index_url = f"{BASE_URL}/bestiary/bestiary-alphabetical/{slug}/"
        beasts = get_beast_links_from_page(index_url)

        if not beasts:
            logging.warning(f"No beasts found for {label}.")
            continue

        logging.info(f"Found {len(beasts)} potential entries for {label}.")
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

                beast_id = insert_beast(cursor, name, link, group_id)

                if beast_id:
                    new_count += 1
                    existing_links.add(link)
                    logging.info(f"Inserted BeastId={beast_id}: {name} ({link})")

            except Exception as e:
                logging.error(f"Failed to insert {name} ({link}): {e}")
                log_failure("", name, link, "DB_INSERT", e)
                cn.rollback()

        if new_count:
            cn.commit()
            total_new += new_count
            logging.info(f"Inserted {new_count} new beasts for {label}.")
        else:
            logging.info(f"No new beasts for {label}.")

        time.sleep(INDEX_WAIT_SECONDS)

    logging.info(f"Index phase done. Inserted {total_new} new beasts.")

    # --------------------------------------------------------
    # Phase 2: fetch and store HTML for all beasts missing HTML
    # --------------------------------------------------------
    rows = get_beasts_without_html(cursor)
    total_html = len(rows)

    logging.info(f"Found {total_html} beasts without HTML content.")

    updated = 0
    failed = 0
    skipped = 0
    processed_in_batch = 0

    for idx, (beast_id, name, link) in enumerate(rows, start=1):
        logging.info(f"[{idx}/{total_html}] Processing BeastId={beast_id}: {name} ({link})")

        cleaned_html, error = fetch_and_clean(link)

        if cleaned_html is None:
            failed += 1
            logging.warning(f"FAILED BeastId={beast_id}: {error}")
            log_failure(beast_id, name, link, "FETCH_OR_CLEAN", error)

            time.sleep(WAIT_SECONDS)
            processed_in_batch += 1

            if processed_in_batch >= BATCH_SIZE:
                input("--- Batch pause: Press Enter to continue ---")
                processed_in_batch = 0

            continue

        try:
            rowcount = update_beast_html(cursor, beast_id, cleaned_html)

            if rowcount == 0:
                skipped += 1
                logging.warning(f"No rows updated for BeastId={beast_id}")
                log_failure(beast_id, name, link, "DB_UPDATE_NO_ROWS", "UPDATE affected 0 rows")
            else:
                cn.commit()
                updated += 1
                logging.info(f"OK BeastId={beast_id} - stored {len(cleaned_html)} chars")

        except Exception as e:
            failed += 1
            cn.rollback()
            logging.error(f"DB update failed for BeastId={beast_id}: {e}")
            log_failure(beast_id, name, link, "DB_UPDATE", e)

        processed_in_batch += 1

        if processed_in_batch >= BATCH_SIZE:
            input("--- Batch pause: Press Enter to continue ---")
            processed_in_batch = 0

        time.sleep(WAIT_SECONDS)

    cursor.execute("SELECT COUNT(*) FROM pf1_Beast WHERE html_block IS NULL")
    remaining = cursor.fetchone()[0]

    logging.info("=" * 80)
    logging.info(f"Done.")
    logging.info(f"New beasts inserted: {total_new}")
    logging.info(f"HTML updated: {updated}")
    logging.info(f"Failed: {failed}")
    logging.info(f"Skipped: {skipped}")
    logging.info(f"Beasts still without HTML: {remaining}")
    logging.info(f"Failure CSV: {FAILURE_LOG_FILE}")
    logging.info("=" * 80)

    cursor.close()
    cn.close()


if __name__ == "__main__":
    main()