import re
import time
import logging
import pyodbc
import requests
from bs4 import BeautifulSoup
from bs4.element import Tag

CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;"
    "DATABASE=PathfinderUtil;"
    "Trusted_Connection=yes;"
)

WAIT_SECONDS = 2
LOG_FILE = "fix_animal_subcategory_links.log"

REPLACEMENTS = [
    "fish",
    "birds",
    "wolliped",
    "felines",
    "snake",
    "sloth",
    "marsupial",
    "canines",
    # add more here
]

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding="utf-8",
)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; BeastAnimalSubcategoryFixer/1.0)"
})


def clean_monster_html(raw_html, url):
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
        return None, f"Cleaned HTML too short: {len(cleaned)} chars"

    return cleaned, None


def build_candidate_urls(url):
    marker = "/bestiary/monster-listings/animals/"

    if not url or marker not in url:
        return []

    for word in REPLACEMENTS:
        if f"/animals/{word}/" in url:
            return []

    return [
        url.replace(marker, marker + word + "/", 1)
        for word in REPLACEMENTS
    ]


def fetch_cleaned_html(url):
    try:
        response = session.get(url, timeout=30)

        if response.status_code == 404:
            return None, "404"

        response.raise_for_status()
        response.encoding = "utf-8"

        return clean_monster_html(response.text, url)

    except Exception as e:
        return None, str(e)


def update_row(cur, cn, beast_id, link, cleaned_html):
    cur.execute("""
        UPDATE [PathfinderUtil].[dbo].[pf1_Beast]
        SET Link = ?, html_block = ?
        WHERE BeastId = ?
          AND html_block IS NULL
    """, link, cleaned_html, beast_id)

    if cur.rowcount:
        cn.commit()
        return True

    cn.rollback()
    return False


def main():
    cn = pyodbc.connect(CONN_STR)
    cn.autocommit = False
    cur = cn.cursor()

    cur.execute("""
        SELECT BeastId, Name, Link
        FROM [PathfinderUtil].[dbo].[pf1_Beast]
        WHERE html_block IS NULL
          AND Link LIKE '%/bestiary/monster-listings/animals/%'
        ORDER BY BeastId
    """)

    rows = cur.fetchall()

    print(f"Found {len(rows)} animal rows with html_block NULL.")
    print(f"Trying replacements: {', '.join(REPLACEMENTS)}")
    print("If replacements fail, paste a correct URL or press Enter to skip.\n")

    updated = 0
    not_found = 0
    failed = 0
    skipped = 0

    for i, row in enumerate(rows, start=1):
        beast_id, name, old_link = row
        candidates = build_candidate_urls(old_link)

        print("=" * 90)
        print(f"[{i}/{len(rows)}] BeastId={beast_id} | {name}")
        print(f"Old: {old_link}")

        matched = False

        for new_link in candidates:
            print(f"Trying: {new_link}")

            cleaned_html, error = fetch_cleaned_html(new_link)

            if cleaned_html is None:
                print(f"  no: {error}")
                logging.info(f"NO MATCH BeastId={beast_id} {name} {new_link} :: {error}")
                time.sleep(WAIT_SECONDS)
                continue

            try:
                if update_row(cur, cn, beast_id, new_link, cleaned_html):
                    updated += 1
                    matched = True
                    print(f"UPDATED: {new_link}")
                    print(f"Stored {len(cleaned_html)} chars")
                    logging.info(f"AUTO UPDATED BeastId={beast_id} {name} {new_link}")
                else:
                    failed += 1
                    print("No row updated.")
                    logging.warning(f"NO ROW UPDATED BeastId={beast_id} {name}")

            except Exception as e:
                cn.rollback()
                failed += 1
                print(f"DB failed: {e}")
                logging.error(f"DB FAILED BeastId={beast_id} {name}: {e}")

            break

        if matched:
            continue

        print("No replacement worked.")
        manual_url = input("Paste correct URL, or press Enter to skip: ").strip()

        if not manual_url:
            skipped += 1
            not_found += 1
            print("Skipped. Left html_block NULL.")
            continue

        cleaned_html, error = fetch_cleaned_html(manual_url)

        if cleaned_html is None:
            not_found += 1
            print(f"Manual URL failed: {error}")
            logging.warning(f"MANUAL FAILED BeastId={beast_id} {name} {manual_url} :: {error}")
            continue

        try:
            if update_row(cur, cn, beast_id, manual_url, cleaned_html):
                updated += 1
                print(f"MANUAL UPDATED: {manual_url}")
                print(f"Stored {len(cleaned_html)} chars")
                logging.info(f"MANUAL UPDATED BeastId={beast_id} {name} {manual_url}")
            else:
                failed += 1
                print("No row updated.")
                logging.warning(f"MANUAL NO ROW UPDATED BeastId={beast_id} {name}")

        except Exception as e:
            cn.rollback()
            failed += 1
            print(f"Manual DB failed: {e}")
            logging.error(f"MANUAL DB FAILED BeastId={beast_id} {name}: {e}")

    cur.close()
    cn.close()

    print("\nDone.")
    print(f"Updated: {updated}")
    print(f"Not found / left NULL: {not_found}")
    print(f"Skipped manually: {skipped}")
    print(f"Failed: {failed}")
    print(f"Log: {LOG_FILE}")


if __name__ == "__main__":
    main()