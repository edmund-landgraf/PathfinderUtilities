"""Find AoN creature art and store it in MonsterImage.

Disabled for now. Archives of Nethys creature pages do not include portraits,
so this import has nothing to download. The lookup code is left in place.
"""

from __future__ import annotations

import argparse
import logging
import re
import time

import requests

from import_demiplane_missing_creatures import (
    connect,
    download_image,
    find_monster_image_table,
    insert_monster_image,
)
from update_aon_rawmd import aon_id_from_url


ELASTIC_URL = "https://elasticsearch.aonprd.com/aon/_search"
AON_ORIGIN = "https://2e.aonprd.com"
IMAGE_RE = re.compile(r"(?:!\[[^\]]*\]\(|src=[\"'])([^)\"']+\.(?:png|jpe?g|webp|gif))", re.I)
SKIP_NAME_PARTS = ("logo", "flourish", "nethys", "mask")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download missing AoN creature art into MonsterImage."
    )
    parser.add_argument("--commit", action="store_true", help="Write image rows. Default is preview.")
    parser.add_argument(
        "--enable",
        action="store_true",
        help="Run the art lookup. Without this flag the script exits, because AoN has no creature portraits.",
    )
    parser.add_argument("--name", help="Only local monster names containing this fragment.")
    parser.add_argument("--max-imports", type=int)
    parser.add_argument("--commit-batch-size", type=int, default=25)
    parser.add_argument("--delay", type=float, default=0.15)
    parser.add_argument("--maximum-image-size-bytes", type=int, default=20 * 1024 * 1024)
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    args = parse_args()
    if not args.enable:
        logging.info("import_aon_monster_images is disabled. Re-run with --enable if AoN creature art becomes available.")
        return

    with connect() as connection:
        connection.autocommit = False
        cursor = connection.cursor()
        image_table = find_monster_image_table(cursor)
        if not image_table:
            raise RuntimeError("MonsterImage table was not found.")
        rows = load_missing(cursor, image_table, args.name)
        logging.info("Using image table: %s", image_table)
        logging.info("AoN monsters missing art: %d", len(rows))

        inserted = 0
        pending = 0
        with requests.Session() as session:
            session.headers.update({"User-Agent": "PathfinderUtil AoN image import"})
            for row in rows:
                aon_id = aon_id_from_url(row.AonUrl)
                if aon_id is None:
                    continue
                image_url = find_image_url(session, aon_id)
                if not image_url:
                    logging.info("NO ART MonsterId=%s | %s", row.MonsterId, row.Name)
                    continue
                logging.info(
                    "%s MonsterId=%s | %s | %s",
                    "INSERTING" if args.commit else "WOULD INSERT",
                    row.MonsterId,
                    row.Name,
                    image_url,
                )
                if not args.commit:
                    inserted += 1
                    if args.max_imports is not None and inserted >= args.max_imports:
                        break
                    continue
                image_data = download_image(session, image_url, args.maximum_image_size_bytes)
                if not image_data:
                    continue
                insert_monster_image(cursor, image_table, int(row.MonsterId), image_data)
                cursor.execute(
                    """
                    UPDATE pf2.Monster
                    SET ImageUrl = CASE
                            WHEN ImageUrl IS NULL OR LEN(LTRIM(RTRIM(ImageUrl))) = 0 THEN ?
                            ELSE ImageUrl
                        END,
                        UpdatedAt = SYSDATETIME()
                    WHERE MonsterId = ?
                    """,
                    image_url,
                    row.MonsterId,
                )
                inserted += 1
                pending += 1
                if pending >= args.commit_batch_size:
                    connection.commit()
                    pending = 0
                if args.max_imports is not None and inserted >= args.max_imports:
                    break
                time.sleep(args.delay)

        if args.commit:
            connection.commit()
            logging.info("Committed %d AoN images.", inserted)
        else:
            connection.rollback()
            logging.info("Preview only. %d images would be inserted. Re-run with --commit to write.", inserted)


def load_missing(cursor, image_table: str, name: str | None):
    sql = f"""
        SELECT m.MonsterId, m.Name, m.AonUrl
        FROM pf2.Monster AS m
        WHERE m.AonUrl LIKE '%aonprd.com%'
          AND NOT EXISTS (
              SELECT 1 FROM {image_table} AS mi WHERE mi.MonsterID = m.MonsterId
          )
    """
    params: list = []
    if name:
        sql += " AND m.Name LIKE ?"
        params.append(f"%{name}%")
    sql += " ORDER BY m.MonsterId"
    cursor.execute(sql, params)
    return cursor.fetchall()


def find_image_url(session: requests.Session, aon_id: int) -> str | None:
    payload = {
        "size": 1,
        "_source": ["image", "markdown"],
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
    source = hits[0].get("_source") or {}
    candidates = []
    image = source.get("image")
    if isinstance(image, list):
        candidates.extend(str(item) for item in image)
    elif image:
        candidates.append(str(image))
    candidates.extend(IMAGE_RE.findall(str(source.get("markdown") or "")))
    for candidate in candidates:
        if any(part in candidate.lower() for part in SKIP_NAME_PARTS):
            continue
        if candidate.startswith("http://") or candidate.startswith("https://"):
            return candidate
        if candidate.startswith("/"):
            return AON_ORIGIN + candidate
    return None


if __name__ == "__main__":
    main()
