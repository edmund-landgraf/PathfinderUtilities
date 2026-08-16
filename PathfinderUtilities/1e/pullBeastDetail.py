import time
import re
import pyodbc
import requests
import logging
from datetime import datetime
from bs4 import BeautifulSoup
from bs4.element import Tag

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;"
    "DATABASE=PathfinderUtil;"
    "Trusted_Connection=yes;"
)

WAIT_SECONDS = 5       # wait between individual beast requests
BATCH_SIZE = 2000        # pause and ask for Enter after this many processed

# Set up logging with UTF-8
logging.basicConfig(
    filename='beast_scraper.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    encoding='utf-8'
)
# Also print to console (ASCII safe)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logging.getLogger('').addHandler(console)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; BeastHTMLScraper/1.0; +https://www.example.com)"
})

def clean_monster_html(raw_html, url):
    """
    Extract the monster stat block and description from the full page HTML.
    Returns (cleaned_html, error_message) tuple.
    """
    try:
        soup = BeautifulSoup(raw_html, "html.parser")

        # Find the main article content
        article = soup.find("div", id="article-content")
        if not article:
            article = soup.find("div", class_="article-content")
            if not article:
                return None, f"No article-content div found for {url}"

        # Remove unwanted elements (ads, breadcrumbs, etc.)
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

        # Find the ECOLOGY divider and remove everything from there onward
        ecology = article.find("p", class_="divider", string=re.compile("ECOLOGY", re.I))
        if ecology:
            # Remove all following siblings, including the ecology tag itself
            for sibling in ecology.find_all_next():
                sibling.decompose()
            ecology.decompose()

        # Optionally remove the <p class="description"> (just the flavor text)
        desc = article.find("p", class_="description")
        if desc:
            desc.decompose()

        cleaned = str(article)
        # Clean whitespace (optional)
        cleaned = re.sub(r"\s+", " ", cleaned)
        cleaned = re.sub(r">\s+<", "><", cleaned)

        if len(cleaned) < 100:
            return None, f"Cleaned HTML too short ({len(cleaned)} chars) for {url}"

        return cleaned, None

    except Exception as e:
        return None, f"Cleaning error for {url}: {e}"

def fetch_and_clean(url):
    """Fetch page and return cleaned monster HTML block."""
    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        # Force UTF-8 decoding
        resp.encoding = 'utf-8'
        html = resp.text
        return clean_monster_html(html, url)
    except requests.exceptions.RequestException as e:
        return None, f"Request error: {e}"
    except Exception as e:
        return None, f"Unexpected fetch error: {e}"

def main():
    logging.info("=" * 80)
    logging.info("Beast HTML Block Scraper (UTF-8 fixed, stat-block only)")
    logging.info(f"Wait between beasts: {WAIT_SECONDS}s, batch size: {BATCH_SIZE}")
    logging.info("=" * 80)

    try:
        cn = pyodbc.connect(CONN_STR)
        cn.autocommit = False
        cursor = cn.cursor()
        logging.info("Connected to SQL Server.")
    except Exception as e:
        logging.error(f"SQL connection failed: {e}")
        return

    # Count beasts without HTML
    cursor.execute("SELECT COUNT(*) FROM pf1_Beast WHERE html_block IS NULL")
    total = cursor.fetchone()[0]
    logging.info(f"Found {total} beasts without HTML content.")

    if total == 0:
        logging.info("No work to do.")
        cursor.close()
        cn.close()
        return

    # Get the list
    cursor.execute("SELECT BeastId, Name, Link FROM pf1_Beast WHERE html_block IS NULL")
    rows = cursor.fetchall()

    updated = 0
    failed = 0
    skipped = 0
    processed_in_batch = 0

    for idx, (beast_id, name, link) in enumerate(rows, start=1):
        logging.info(f"[{idx}/{total}] Processing: {name} ({link})")
        cleaned_html, error = fetch_and_clean(link)

        if cleaned_html is None:
            failed += 1
            logging.warning(f"  FAILED: {error}")
            time.sleep(WAIT_SECONDS)
            processed_in_batch += 1
            # Even if failed, we count as processed for batch pause
            if processed_in_batch >= BATCH_SIZE:
                input("--- Batch pause: Press Enter to continue ---")
                processed_in_batch = 0
            continue

        # Attempt to update the database
        try:
            cursor.execute("""
                UPDATE pf1_Beast
                SET html_block = ?
                WHERE BeastId = ?
            """, cleaned_html, beast_id)
            if cursor.rowcount == 0:
                logging.warning(f"  No rows updated for BeastId {beast_id}")
                skipped += 1
            else:
                cn.commit()
                updated += 1
                logging.info(f"  OK - stored {len(cleaned_html)} chars (committed)")

        except Exception as e:
            logging.error(f"  DB update failed: {e}")
            failed += 1
            cn.rollback()

        processed_in_batch += 1
        if processed_in_batch >= BATCH_SIZE:
            input("--- Batch pause: Press Enter to continue ---")
            processed_in_batch = 0

        time.sleep(WAIT_SECONDS)

    logging.info("=" * 80)
    logging.info(f"Done. Updated: {updated}, Failed: {failed}, Skipped: {skipped}")
    logging.info("=" * 80)

    cursor.execute("SELECT COUNT(*) FROM pf1_Beast WHERE html_block IS NULL")
    remaining = cursor.fetchone()[0]
    logging.info(f"Beasts still without HTML: {remaining}")

    cursor.close()
    cn.close()

if __name__ == "__main__":
    main()