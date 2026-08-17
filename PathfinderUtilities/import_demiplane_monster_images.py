from __future__ import annotations

import argparse
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import pyodbc
import requests


GRAPHQL_URL = "https://apiv4.demiplane.com/v1/graphql"
IMAGE_BASE_URL = "https://images.demiplane.com/"

PLACEHOLDER_IMAGE_NAMES = {
    "aberration.jpg",
    "animal.jpg",
    "astral.jpg",
    "beast.jpg",
    "celestial.jpg",
    "construct.jpg",
    "dragon.jpg",
    "elemental.jpg",
    "ethereal.jpg",
    "fey.jpg",
    "fiend.jpg",
    "fungus.jpg",
    "giant.jpg",
    "humanoid.jpg",
    "monitor.jpg",
    "ooze.jpg",
    "plant.jpg",
    "spirit.jpg",
    "undead.jpg",
}

CREATURE_QUERY = """
query DemiplaneCreatures($limit: Int!, $offset: Int!, $name: String) {
  demiplane_element_display_version(
    limit: $limit
    offset: $offset
    distinct_on: element_display_id
    order_by: [{element_display_id: asc}, {version_number: desc}]
    where: {
      name: {_ilike: $name}
      elementDisplayByElementDisplayId: {
        category: {_eq: "creature"}
        nexus: {slug: {_eq: "pathfinder2e"}}
      }
    }
  ) {
    id
    name
    element_display_id
    element_image
    element_thumbnail
    level
    primary_source_name
    version_number
    elementDisplayByElementDisplayId {
      id
      slug
      category
    }
  }
}
"""


@dataclass(frozen=True)
class LocalMonster:
    monster_id: int
    name: str
    level: int | None
    aon_id: int | None
    source_book: str | None


@dataclass(frozen=True)
class DemiplaneCreature:
    name: str
    level: int | None
    slug: str
    image_url: str
    source_name: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Import Demiplane creature artwork into pf2/dbo MonsterImage for "
            "local monsters that match by name+level and do not already have art."
        )
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually insert images. Default is dry-run."
    )
    parser.add_argument(
        "--name",
        help="Only inspect Demiplane creatures whose name matches this SQL ILIKE pattern fragment."
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=250,
        help="Demiplane GraphQL page size. Default: 250."
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        help="Stop after this many Demiplane pages."
    )
    parser.add_argument(
        "--max-imports",
        type=int,
        help="Stop after this many matched local image inserts."
    )
    parser.add_argument(
        "--commit-batch-size",
        type=int,
        default=25,
        help="Commit after this many inserts. Default: 25."
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.15,
        help="Delay between image downloads in commit mode. Default: 0.15 seconds."
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100,
        help="Print progress every N Demiplane creatures. Default: 100."
    )
    parser.add_argument(
        "--allow-level-mismatch",
        action="store_true",
        help="Match by name only when no name+level local match exists."
    )
    parser.add_argument(
        "--maximum-image-size-bytes",
        type=int,
        default=20 * 1024 * 1024,
        help="Reject downloaded images larger than this. Default: 20 MB."
    )
    return parser.parse_args()


def normalize_name(value: str | None) -> str:
    value = value or ""
    value = re.sub(r"\s+", " ", value).strip().lower()
    return value


def connect() -> pyodbc.Connection:
    connection_strings = [
        (
            "DRIVER={ODBC Driver 17 for SQL Server};"
            "SERVER=localhost;"
            "DATABASE=PathfinderUtil;"
            "Trusted_Connection=yes;"
        ),
        (
            "DRIVER={ODBC Driver 17 for SQL Server};"
            "SERVER=localhost;"
            "DATABASE=PathfinderUtil;"
            "Trusted_Connection=yes;"
            "Encrypt=no;"
            "TrustServerCertificate=yes;"
        ),
        (
            "DRIVER={ODBC Driver 18 for SQL Server};"
            "SERVER=localhost;"
            "DATABASE=PathfinderUtil;"
            "Trusted_Connection=yes;"
            "TrustServerCertificate=yes;"
        ),
    ]

    last_error: Exception | None = None

    for connection_string in connection_strings:
        try:
            return pyodbc.connect(connection_string)
        except pyodbc.Error as exc:
            last_error = exc

    raise RuntimeError("Could not connect to PathfinderUtil SQL Server.") from last_error


def find_monster_image_table(cursor: pyodbc.Cursor) -> str:
    for schema_name in ("pf2", "dbo"):
        cursor.execute("""
            SELECT 1
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = ?
              AND TABLE_NAME = 'MonsterImage'
        """, schema_name)

        if cursor.fetchone():
            return f"{schema_name}.MonsterImage"

    raise RuntimeError("Could not find pf2.MonsterImage or dbo.MonsterImage.")


def load_local_monsters_missing_art(
    cursor: pyodbc.Cursor,
    image_table: str,
) -> tuple[dict[tuple[str, int | None], list[LocalMonster]], dict[str, list[LocalMonster]]]:
    cursor.execute(f"""
        SELECT
            m.MonsterId,
            m.AonId,
            m.Name,
            m.Level,
            sb.Name AS SourceBook
        FROM pf2.Monster AS m
        LEFT JOIN pf2.SourceBook AS sb
          ON sb.SourceBookId = m.SourceBookId
        WHERE NOT EXISTS
        (
            SELECT 1
            FROM {image_table} AS mi
            WHERE mi.MonsterID = m.MonsterId
        )
        ORDER BY m.Name, m.Level, m.MonsterId
    """)

    by_name_level: dict[tuple[str, int | None], list[LocalMonster]] = {}
    by_name: dict[str, list[LocalMonster]] = {}

    for row in cursor.fetchall():
        monster = LocalMonster(
            monster_id=int(row.MonsterId),
            name=str(row.Name),
            level=int(row.Level) if row.Level is not None else None,
            aon_id=int(row.AonId) if row.AonId is not None else None,
            source_book=str(row.SourceBook) if row.SourceBook else None,
        )
        key = (normalize_name(monster.name), monster.level)
        by_name_level.setdefault(key, []).append(monster)
        by_name.setdefault(normalize_name(monster.name), []).append(monster)

    return by_name_level, by_name


def fetch_demiplane_creatures(
    session: requests.Session,
    page_size: int,
    max_pages: int | None,
    name: str | None,
):
    offset = 0
    page = 0
    name_pattern = f"%{name}%" if name else "%%"

    while True:
        page += 1

        if max_pages is not None and page > max_pages:
            return

        response = session.post(
            GRAPHQL_URL,
            json={
                "query": CREATURE_QUERY,
                "variables": {
                    "limit": page_size,
                    "offset": offset,
                    "name": name_pattern,
                },
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()

        if payload.get("errors"):
            raise RuntimeError(payload["errors"])

        rows = payload.get("data", {}).get("demiplane_element_display_version", [])

        if not rows:
            return

        for row in rows:
            creature = demiplane_creature_from_row(row)

            if creature:
                yield creature

        if len(rows) < page_size:
            return

        offset += page_size


def demiplane_creature_from_row(row: dict[str, Any]) -> DemiplaneCreature | None:
    name = clean(row.get("name"))
    display = row.get("elementDisplayByElementDisplayId") or {}
    slug = clean(display.get("slug"))
    image_url = best_image_url(row)

    if not name or not slug or not image_url:
        return None

    level = row.get("level")

    return DemiplaneCreature(
        name=name,
        level=int(level) if level is not None else None,
        slug=slug,
        image_url=image_url,
        source_name=clean(row.get("primary_source_name")),
    )


def clean(value: Any) -> str | None:
    if value is None:
        return None

    value = re.sub(r"\s+", " ", str(value)).strip()
    return value or None


def best_image_url(row: dict[str, Any]) -> str | None:
    for key in ("element_image", "element_thumbnail"):
        raw = clean(row.get(key))

        if not raw or is_placeholder_image(raw):
            continue

        return absolute_image_url(raw)

    return None


def is_placeholder_image(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return name in PLACEHOLDER_IMAGE_NAMES


def absolute_image_url(path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path

    return IMAGE_BASE_URL + quote(path.lstrip("/"), safe="/:")


def matching_local_monsters(
    creature: DemiplaneCreature,
    by_name_level: dict[tuple[str, int | None], list[LocalMonster]],
    by_name: dict[str, list[LocalMonster]],
    allow_level_mismatch: bool,
) -> list[LocalMonster]:
    normalized = normalize_name(creature.name)
    exact = by_name_level.get((normalized, creature.level), [])

    if exact or not allow_level_mismatch:
        return exact

    return by_name.get(normalized, [])


def download_image(
    session: requests.Session,
    image_url: str,
    maximum_image_size_bytes: int,
) -> bytes | None:
    response = session.get(
        image_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/149.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
        timeout=60,
        allow_redirects=True,
    )
    response.raise_for_status()

    content_type = (response.headers.get("Content-Type") or "").lower()
    image_data = response.content

    if not image_data:
        return None

    if content_type and not content_type.startswith("image/") and "octet-stream" not in content_type:
        return None

    if len(image_data) > maximum_image_size_bytes:
        return None

    return image_data


def insert_monster_image(
    cursor: pyodbc.Cursor,
    image_table: str,
    monster_id: int,
    image_data: bytes,
) -> None:
    cursor.execute(f"""
        INSERT INTO {image_table}
        (
            MonsterID,
            MonsterImage
        )
        SELECT
            ?,
            ?
        WHERE NOT EXISTS
        (
            SELECT 1
            FROM {image_table}
            WHERE MonsterID = ?
        )
    """,
        monster_id,
        pyodbc.Binary(image_data),
        monster_id,
    )


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    scanned = 0
    matched = 0
    inserted = 0
    skipped_no_match = 0
    skipped_no_image = 0
    failed = 0
    pending_commits = 0

    with connect() as connection:
        cursor = connection.cursor()
        image_table = find_monster_image_table(cursor)
        by_name_level, by_name = load_local_monsters_missing_art(cursor, image_table)
        local_missing = sum(len(rows) for rows in by_name_level.values())

        logging.info("Using image table: %s", image_table)
        logging.info("Local monsters missing MonsterImage rows: %d", local_missing)

        with requests.Session() as session:
            for creature in fetch_demiplane_creatures(
                session=session,
                page_size=args.page_size,
                max_pages=args.max_pages,
                name=args.name,
            ):
                scanned += 1
                locals_to_update = matching_local_monsters(
                    creature=creature,
                    by_name_level=by_name_level,
                    by_name=by_name,
                    allow_level_mismatch=args.allow_level_mismatch,
                )

                if not locals_to_update:
                    skipped_no_match += 1
                else:
                    matched += len(locals_to_update)

                    for local_monster in locals_to_update:
                        logging.info(
                            "%sMATCH MonsterID %s | %s L%s | %s",
                            "DRY-RUN " if not args.commit else "",
                            local_monster.monster_id,
                            local_monster.name,
                            local_monster.level,
                            creature.image_url,
                        )

                        if not args.commit:
                            inserted += 1
                            continue

                        try:
                            image_data = download_image(
                                session,
                                creature.image_url,
                                args.maximum_image_size_bytes,
                            )

                            if image_data is None:
                                skipped_no_image += 1
                                continue

                            insert_monster_image(
                                cursor,
                                image_table,
                                local_monster.monster_id,
                                image_data,
                            )
                            inserted += 1
                            pending_commits += 1

                            if pending_commits >= args.commit_batch_size:
                                connection.commit()
                                pending_commits = 0
                                logging.info("Committed batch.")

                            if args.delay > 0:
                                time.sleep(args.delay)
                        except Exception:
                            connection.rollback()
                            pending_commits = 0
                            failed += 1
                            logging.exception(
                                "Failed MonsterID %s | %s | %s",
                                local_monster.monster_id,
                                local_monster.name,
                                creature.image_url,
                            )

                        if args.max_imports is not None and inserted >= args.max_imports:
                            break

                if args.progress_every and scanned % args.progress_every == 0:
                    logging.info(
                        "Scanned %d Demiplane creatures | matched local rows: %d | %s: %d",
                        scanned,
                        matched,
                        "inserted" if args.commit else "would insert",
                        inserted,
                    )

                if args.max_imports is not None and inserted >= args.max_imports:
                    break

        if args.commit and pending_commits:
            connection.commit()

    logging.info(
        "Done. Scanned: %d | matched local rows: %d | no local missing-art match: %d | "
        "%s: %d | skipped image: %d | failed: %d",
        scanned,
        matched,
        skipped_no_match,
        "inserted" if args.commit else "would insert",
        inserted,
        skipped_no_image,
        failed,
    )

    if not args.commit:
        logging.info("Dry run only. Re-run with --commit to insert images.")


if __name__ == "__main__":
    main()
