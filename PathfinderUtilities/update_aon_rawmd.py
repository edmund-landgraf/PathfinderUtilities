"""Fill empty pf2.Monster.RawMD from Archives of Nethys.

Preview only unless --commit is passed. Selects local rows whose RawMD is empty
and whose AonUrl is an AoN monster or NPC page, then loads the creature markdown
from the AoN search index by creature id.
"""

from __future__ import annotations

import argparse
import logging
import re

import requests

from import_demiplane_missing_creatures import connect


ELASTIC_URL = "https://elasticsearch.aonprd.com/aon/_search"
SCRAPE_VERSION = "aon-rawmd-update-v1"
AON_ID_RE = re.compile(r"(?:Monsters|NPCs)\.aspx\?ID=(\d+)", re.I)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update empty pf2.Monster.RawMD from Archives of Nethys."
    )
    parser.add_argument("--commit", action="store_true", help="Write updates. Default is preview.")
    parser.add_argument("--name", help="Only update local names containing this fragment.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    with connect() as connection:
        connection.autocommit = False
        cursor = connection.cursor()
        sql = """
            SELECT MonsterId, Name, Level, AonUrl
            FROM pf2.Monster
            WHERE (RawMD IS NULL OR LEN(LTRIM(RTRIM(RawMD))) = 0)
              AND AonUrl LIKE '%aonprd.com%'
        """
        params: list = []
        if args.name:
            sql += " AND Name LIKE ?"
            params.append(f"%{args.name}%")
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        logging.info("Local AoN monsters missing RawMD: %d", len(rows))

        updated = 0
        with requests.Session() as session:
            session.headers.update({"User-Agent": "PathfinderUtil AoN RawMD update"})
            for row in rows:
                aon_id = aon_id_from_url(row.AonUrl)
                if aon_id is None:
                    logging.info("SKIP MonsterId=%s | %s | no AoN id in %s", row.MonsterId, row.Name, row.AonUrl)
                    continue
                markdown = fetch_markdown(session, aon_id)
                if not markdown:
                    logging.info("SKIP MonsterId=%s | %s | no AoN markdown for creature-%s", row.MonsterId, row.Name, aon_id)
                    continue
                logging.info(
                    "%s MonsterId=%s | %s | Level=%s | rawmd=%s chars",
                    "UPDATING" if args.commit else "WOULD UPDATE",
                    row.MonsterId,
                    row.Name,
                    row.Level,
                    len(markdown),
                )
                if not args.commit:
                    updated += 1
                    continue
                cursor.execute(
                    """
                    UPDATE pf2.Monster
                    SET RawMD = ?,
                        RawText = CASE
                            WHEN RawText IS NULL OR LEN(LTRIM(RTRIM(RawText))) = 0 THEN ?
                            ELSE RawText
                        END,
                        UpdatedAt = SYSDATETIME(),
                        LastScraped = SYSDATETIME(),
                        ScrapeVersion = ?
                    WHERE MonsterId = ?
                      AND (RawMD IS NULL OR LEN(LTRIM(RTRIM(RawMD))) = 0)
                    """,
                    markdown,
                    markdown,
                    SCRAPE_VERSION,
                    row.MonsterId,
                )
                updated += cursor.rowcount

        if args.commit:
            connection.commit()
            logging.info("Committed AoN RawMD updates for %d rows.", updated)
        else:
            connection.rollback()
            logging.info("Preview only. %d rows would update. Re-run with --commit to write.", updated)


def aon_id_from_url(url: str | None) -> int | None:
    match = AON_ID_RE.search(url or "")
    return int(match.group(1)) if match else None


def fetch_markdown(session: requests.Session, aon_id: int) -> str | None:
    payload = {
        "size": 1,
        "_source": ["markdown"],
        "query": {
            "bool": {
                "filter": [
                    {"term": {"category": "creature"}},
                    {"term": {"id.keyword": f"creature-{aon_id}"}},
                ]
            }
        },
    }
    response = session.post(ELASTIC_URL, json=payload, timeout=60)
    response.raise_for_status()
    hits = response.json().get("hits", {}).get("hits", [])
    if not hits:
        return None
    markdown = hits[0].get("_source", {}).get("markdown")
    text = str(markdown or "").strip()
    return text or None


if __name__ == "__main__":
    main()
