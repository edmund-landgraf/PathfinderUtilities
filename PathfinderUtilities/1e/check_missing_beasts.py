"""Compare d20pfsrd alphabetical bestiary listings to local pf1_Beast.

Preview only: does not insert or update rows.
"""
import csv
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pyodbc
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mssql_conn import sql_auth

CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;"
    "DATABASE=PathfinderUtil;"
    + sql_auth()
    + "TrustServerCertificate=yes;"
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
INDEX_WAIT_SECONDS = 1


def clean_text(text):
    return re.sub(r"\s+", " ", text).strip() if text else None


def canonicalize_link(url):
    if not url:
        return None
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower().replace("www.", "")
    path = parsed.path.rstrip("/").lower()
    return f"{host}{path}"


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
    match = re.search(r"/bestiary/monster-listings/([^/]+)(?:/|$)", url.lower())
    return match.group(1) if match else None


def get_beast_links_from_page(session, url):
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    links = []
    seen = set()

    for a in soup.find_all("a", href=True):
        name = clean_text(a.get_text())
        if not name:
            continue
        full_url = normalize_url(a["href"])
        if not full_url or full_url in seen:
            continue
        seen.add(full_url)
        links.append((name, full_url, extract_group_from_url(full_url)))
    return links


def main():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; BeastMissingCheck/1.0)"
    })

    cn = pyodbc.connect(CONN_STR)
    cursor = cn.cursor()
    cursor.execute("SELECT BeastId, Name, Link FROM pf1_Beast")
    db_rows = cursor.fetchall()
    cursor.close()
    cn.close()

    db_by_canon = {}
    for beast_id, name, link in db_rows:
        key = canonicalize_link(link)
        if key:
            db_by_canon.setdefault(key, []).append((beast_id, name, link))

    remote = []
    remote_seen = set()
    for label, slug in PAGES:
        index_url = f"{BASE_URL}/bestiary/bestiary-alphabetical/{slug}/"
        print(f"Fetching {label}: {index_url}")
        beasts = get_beast_links_from_page(session, index_url)
        print(f"  {len(beasts)} creature links")
        for name, link, group in beasts:
            key = canonicalize_link(link)
            if not key or key in remote_seen:
                continue
            remote_seen.add(key)
            remote.append((label, name, link, group, key))
        time.sleep(INDEX_WAIT_SECONDS)

    missing = [row for row in remote if row[4] not in db_by_canon]
    matched = len(remote) - len(missing)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(__file__).resolve().parents[2] / "reports" / "1e-missing-beasts"
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{stamp}_missing_1e_beasts.csv"
    md_path = out_dir / f"{stamp}_missing_1e_beasts.md"

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["IndexPage", "Name", "Group", "Link"])
        for label, name, link, group, _key in missing:
            writer.writerow([label, name, group or "", link])

    lines = [
        "# Missing PF1 creatures (d20pfsrd vs pf1_Beast)",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Source: {BASE_URL}/bestiary/",
        "",
        f"- d20pfsrd unique monster-listing links: {len(remote)}",
        f"- Local pf1_Beast rows: {len(db_rows)}",
        f"- Matched by canonical URL: {matched}",
        f"- Missing locally: {len(missing)}",
        "",
    ]
    if missing:
        lines.extend([
            "| Index | Name | Group | Link |",
            "|---|---|---|---|",
        ])
        for label, name, link, group, _key in missing:
            lines.append(f"| {label} | {name} | {group or ''} | {link} |")
    else:
        lines.append("No missing creatures found.")

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("=" * 80)
    print(f"Remote unique listings: {len(remote)}")
    print(f"Local pf1_Beast rows: {len(db_rows)}")
    print(f"Matched: {matched}")
    print(f"Missing: {len(missing)}")
    print(f"CSV: {csv_path}")
    print(f"MD: {md_path}")
    if missing:
        print("First 30 missing:")
        for row in missing[:30]:
            print(f"  [{row[0]}] {row[1]}  {row[2]}")


if __name__ == "__main__":
    main()
