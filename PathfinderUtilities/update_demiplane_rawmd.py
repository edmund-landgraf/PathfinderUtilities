"""Refresh pf2.Monster.RawMD from Demiplane for rows the stat card cannot read.

Preview only unless --commit is passed. Matches existing rows by name and level.
Also fills an empty RawHtml/RawText, a missing ImageUrl, and missing MonsterTrait
links from the same Demiplane payload. Existing RawMD that already has a
"Creature N" heading is left alone.
"""

from __future__ import annotations

import argparse
import logging

import requests

from import_demiplane_missing_creatures import (
    connect,
    fetch_demiplane_creatures,
    get_or_create_lookup,
    html_to_raw_md,
    html_to_text,
    normalize_name,
)


SCRAPE_VERSION = "demiplane-rawmd-update-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update empty pf2.Monster.RawMD from Demiplane stat blocks."
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
            SELECT MonsterId, Name, Level, ImageUrl
            FROM pf2.Monster
            WHERE RawMD IS NULL
               OR LEN(LTRIM(RTRIM(RawMD))) = 0
               OR RawMD NOT LIKE '%Creature %'
            """
        )
        missing: dict[tuple[str, int | None], list[tuple]] = {}
        for row in cursor.fetchall():
            key = (normalize_name(row.Name), int(row.Level) if row.Level is not None else None)
            missing.setdefault(key, []).append(row)

        logging.info("Local monsters needing RawMD: %d", sum(len(v) for v in missing.values()))
        updated = 0
        traits_added = 0
        seen: set[tuple[str, int | None]] = set()
        lookup_cache: dict[tuple[str, str], int] = {}

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
                raw_md = html_to_raw_md(
                    creature.stat_html,
                    creature.name,
                    creature.level,
                    creature.traits.creature_traits,
                )
                if not raw_md:
                    continue
                for row in rows:
                    logging.info(
                        "%s MonsterId=%s | %s | Level=%s | rawmd=%s chars | image=%s",
                        "UPDATING" if args.commit else "WOULD UPDATE",
                        row.MonsterId,
                        row.Name,
                        row.Level,
                        len(raw_md),
                        "fill" if (not row.ImageUrl and creature.image_url) else "keep",
                    )
                    if not args.commit:
                        updated += 1
                        continue
                    cursor.execute(
                        """
                        UPDATE pf2.Monster
                        SET RawMD = ?,
                            RawHtml = CASE
                                WHEN RawHtml IS NULL OR LEN(LTRIM(RTRIM(RawHtml))) = 0 THEN ?
                                ELSE RawHtml
                            END,
                            RawText = CASE
                                WHEN RawText IS NULL OR LEN(LTRIM(RTRIM(RawText))) = 0 THEN ?
                                ELSE RawText
                            END,
                            ImageUrl = CASE
                                WHEN (ImageUrl IS NULL OR LEN(LTRIM(RTRIM(ImageUrl))) = 0) AND ? IS NOT NULL THEN ?
                                ELSE ImageUrl
                            END,
                            UpdatedAt = SYSDATETIME(),
                            LastScraped = SYSDATETIME(),
                            ScrapeVersion = ?
                        WHERE MonsterId = ?
                          AND (
                            RawMD IS NULL
                            OR LEN(LTRIM(RTRIM(RawMD))) = 0
                            OR RawMD NOT LIKE '%Creature %'
                          )
                        """,
                        raw_md,
                        creature.stat_html,
                        raw_text,
                        creature.image_url,
                        creature.image_url,
                        SCRAPE_VERSION,
                        row.MonsterId,
                    )
                    updated += cursor.rowcount
                    traits_added += ensure_traits(cursor, int(row.MonsterId), creature.traits.creature_traits, lookup_cache)

        if args.commit:
            connection.commit()
            logging.info("Committed RawMD updates for %d rows. Traits added: %d.", updated, traits_added)
        else:
            connection.rollback()
            logging.info("Preview only. %d rows would update. Re-run with --commit to write.", updated)


def ensure_traits(cursor, monster_id: int, traits: list[str], lookup_cache: dict[tuple[str, str], int]) -> int:
    """Insert Demiplane creature traits that are not already linked."""

    cursor.execute(
        """
        SELECT t.Name
        FROM pf2.MonsterTrait mt
        INNER JOIN pf2.Trait t ON t.TraitId = mt.TraitId
        WHERE mt.MonsterId = ?
        """,
        monster_id,
    )
    existing = {normalize_name(row.Name) for row in cursor.fetchall()}
    added = 0
    for trait in traits:
        if normalize_name(trait) in existing:
            continue
        trait_id = get_or_create_lookup(cursor, "pf2.Trait", "TraitId", "Name", trait, lookup_cache)
        if trait_id is None:
            continue
        cursor.execute(
            """
            INSERT INTO pf2.MonsterTrait (MonsterId, TraitId)
            VALUES (?, ?)
            """,
            monster_id,
            trait_id,
        )
        existing.add(normalize_name(trait))
        added += 1
    return added


if __name__ == "__main__":
    main()
