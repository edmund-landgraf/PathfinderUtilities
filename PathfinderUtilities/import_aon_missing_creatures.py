"""Insert Archives of Nethys creatures that are not already in pf2.Monster.

Preview only unless --commit is passed. A creature is already present when its
AoN id is stored, or when the same name and level already exist. New rows get
the AoN markdown in RawMD, which the full catalog pull leaves empty.
"""

from __future__ import annotations

import argparse
import logging
import time

import requests

from import_demiplane_missing_creatures import connect, normalize_name
from pullMonsters_3 import (
    aon_numeric_id,
    clean,
    fetch_monster_batch,
    first_existing,
    insert_monster_from_elastic,
    to_int,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import AoN PF2 creatures missing from local pf2.Monster."
    )
    parser.add_argument("--commit", action="store_true", help="Insert rows. Default is preview.")
    parser.add_argument("--name", help="Only inspect AoN creature names containing this fragment.")
    parser.add_argument("--page-size", type=int, default=250)
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--max-imports", type=int)
    parser.add_argument("--commit-batch-size", type=int, default=25)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    with connect() as connection:
        connection.autocommit = False
        cursor = connection.cursor()
        existing_ids, existing_name_level = load_existing(cursor)
        logging.info("Local AoN ids: %d. Local name/level keys: %d.", len(existing_ids), len(existing_name_level))

        imported = 0
        preview_new = 0
        pending = 0
        cache: dict = {}
        page = 0
        offset = 0

        while True:
            page += 1
            if args.max_pages is not None and page > args.max_pages:
                break
            data = fetch_monster_batch(offset, args.page_size)
            hits = data.get("hits", {}).get("hits", [])
            if not hits:
                break
            if args.name:
                hits = [hit for hit in hits if args.name.lower() in str((hit.get("_source") or {}).get("name") or "").lower()]

            for hit in hits:
                src = hit.get("_source") or {}
                src["_elastic_id"] = hit.get("_id")
                name = clean(first_existing(src, "name", "title"))
                level = to_int(first_existing(src, "level"))
                aon_id = aon_numeric_id(src)
                if not name or aon_id is None:
                    continue
                key = (normalize_name(name), level)
                if aon_id in existing_ids or key in existing_name_level:
                    continue
                markdown = clean(first_existing(src, "markdown"))
                logging.info(
                    "%s AonId=%s | %s | Level=%s",
                    "INSERTING" if args.commit else "WOULD INSERT",
                    aon_id,
                    name,
                    level,
                )
                existing_ids.add(aon_id)
                existing_name_level.add(key)
                if not args.commit:
                    preview_new += 1
                    if args.max_imports is not None and preview_new >= args.max_imports:
                        break
                    continue
                monster_id = insert_monster_from_elastic(cursor, src, cache)
                cursor.execute(
                    """
                    UPDATE pf2.Monster
                    SET RawMD = CASE
                            WHEN RawMD IS NULL OR LEN(LTRIM(RTRIM(RawMD))) = 0 THEN ?
                            ELSE RawMD
                        END
                    WHERE MonsterId = ?
                    """,
                    markdown,
                    monster_id,
                )
                imported += 1
                pending += 1
                if pending >= args.commit_batch_size:
                    connection.commit()
                    pending = 0
                if args.max_imports is not None and imported >= args.max_imports:
                    break
                time.sleep(0.02)

            if args.max_imports is not None and (imported >= args.max_imports or preview_new >= args.max_imports):
                break
            total = data.get("hits", {}).get("total", {})
            total_value = total.get("value") if isinstance(total, dict) else total
            offset += args.page_size
            if not data.get("hits", {}).get("hits") or (total_value is not None and offset >= total_value):
                break
            time.sleep(0.2)

        if args.commit:
            connection.commit()
            logging.info("Committed %d new AoN creatures.", imported)
        else:
            connection.rollback()
            logging.info("Preview only. %d creatures would be inserted. Re-run with --commit to write.", preview_new)


def load_existing(cursor) -> tuple[set[int], set[tuple[str, int | None]]]:
    cursor.execute("SELECT AonId, Name, Level FROM pf2.Monster")
    ids: set[int] = set()
    keys: set[tuple[str, int | None]] = set()
    for row in cursor.fetchall():
        if row.AonId is not None:
            ids.add(int(row.AonId))
        keys.add((normalize_name(row.Name), int(row.Level) if row.Level is not None else None))
    return ids, keys


if __name__ == "__main__":
    main()
