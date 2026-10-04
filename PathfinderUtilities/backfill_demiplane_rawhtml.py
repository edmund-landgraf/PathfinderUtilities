"""Fill empty pf2.Monster.RawHtml from Demiplane stat-block HTML.

Preview only unless --commit is passed. Matches existing rows by name and level.
"""

from __future__ import annotations

import argparse
import logging

from import_demiplane_missing_creatures import (
    connect,
    fetch_demiplane_creatures,
    html_to_text,
    normalize_name,
)

import requests


SCRAPE_VERSION = "demiplane-statblock-backfill-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill pf2.Monster rows whose RawHtml stat block is empty."
    )
    parser.add_argument("--commit", action="store_true", help="Write updates. Default is preview.")
    parser.add_argument("--name", help="Only inspect Demiplane creature names matching this fragment.")
    parser.add_argument("--page-size", type=int, default=250)
    parser.add_argument("--max-pages", type=int)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    with connect() as connection:
        connection.autocommit = False
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT MonsterId, Name, Level
            FROM pf2.Monster
            WHERE RawHtml IS NULL OR LEN(LTRIM(RTRIM(RawHtml))) = 0
            """
        )
        missing: dict[tuple[str, int | None], list[tuple]] = {}
        for row in cursor.fetchall():
            key = (normalize_name(row.Name), int(row.Level) if row.Level is not None else None)
            missing.setdefault(key, []).append(row)

        logging.info("Local monsters missing RawHtml: %d", sum(len(v) for v in missing.values()))
        updated = 0
        seen: set[tuple[str, int | None]] = set()

        with requests.Session() as session:
            for creature in fetch_demiplane_creatures(
                session=session,
                page_size=args.page_size,
                max_pages=args.max_pages,
                name=args.name,
            ):
                if not creature.stat_html:
                    continue
                key = (normalize_name(creature.name), creature.level)
                rows = missing.get(key)
                if not rows or key in seen:
                    continue
                seen.add(key)
                raw_text = html_to_text(creature.stat_html)
                for row in rows:
                    logging.info(
                        "%s MonsterId=%s | %s | Level=%s | html=%s chars",
                        "UPDATING" if args.commit else "WOULD UPDATE",
                        row.MonsterId,
                        row.Name,
                        row.Level,
                        len(creature.stat_html),
                    )
                    if not args.commit:
                        updated += 1
                        continue
                    cursor.execute(
                        """
                        UPDATE pf2.Monster
                        SET RawHtml = ?,
                            RawText = ?,
                            RawMD = CASE
                                WHEN RawMD IS NULL OR LEN(LTRIM(RTRIM(RawMD))) = 0 THEN ?
                                ELSE RawMD
                            END,
                            UpdatedAt = SYSDATETIME(),
                            LastScraped = SYSDATETIME(),
                            ScrapeVersion = ?
                        WHERE MonsterId = ?
                          AND (RawHtml IS NULL OR LEN(LTRIM(RTRIM(RawHtml))) = 0)
                        """,
                        creature.stat_html,
                        raw_text,
                        creature.stat_html,
                        SCRAPE_VERSION,
                        row.MonsterId,
                    )
                    updated += cursor.rowcount

        if args.commit:
            connection.commit()
            logging.info("Committed stat-block backfill for %d rows.", updated)
        else:
            connection.rollback()
            logging.info("Preview only. %d rows would update. Re-run with --commit to write.", updated)


if __name__ == "__main__":
    main()
