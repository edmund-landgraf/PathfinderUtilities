from __future__ import annotations

import argparse
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import pyodbc
import requests
from bs4 import BeautifulSoup


# Demiplane is the source of truth for this script. SQL Server remains the
# destination, and preview mode is the default so a scan cannot write by
# accident.
GRAPHQL_URL = "https://apiv4.demiplane.com/v1/graphql"
IMAGE_BASE_URL = "https://images.demiplane.com/"
DEMIPLANE_CREATURE_URL = "https://app.demiplane.com/nexus/pathfinder2e/creature/{slug}"
SCRAPE_VERSION = "demiplane-graphql-v1"

# Demiplane sends these values as pipe/comma-delimited trait labels. We split
# them into local lookup fields first, then keep the rest as monster traits.
RARITY_LABELS = {"common", "uncommon", "rare", "unique"}
SIZE_LABELS = {"tiny", "small", "medium", "large", "huge", "gargantuan"}
ALIGNMENT_LABELS = {
    "lg": "LG",
    "ng": "NG",
    "cg": "CG",
    "ln": "LN",
    "n": "N",
    "cn": "CN",
    "le": "LE",
    "ne": "NE",
    "ce": "CE",
}

# Family icons are useful on Demiplane, but they are not creature art. Exclude
# them so MonsterImage only receives specific artwork or thumbnails.
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

# Distincting by element_display_id asks Demiplane for the newest display
# version of each creature. A later in-script pass still dedupes name/level
# because the same creature can appear through multiple source products.
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
    version_number
    level
    traits
    creature_family
    primary_source_name
    short_description
    long_description
    element_display
    element_image
    element_thumbnail
    elementDisplayByElementDisplayId {
      id
      slug
      category
    }
  }
}
"""


@dataclass(frozen=True)
class TraitParts:
    """Normalized trait data split into local lookup values and PF traits."""

    labels: list[str]
    rarity: str | None
    size: str | None
    alignment: str | None
    creature_traits: list[str]
    is_unique: bool


@dataclass(frozen=True)
class ParsedStats:
    """Subset of stat-block fields that map directly into pf2.MonsterStats."""

    perception: int | None = None
    senses: str | None = None
    languages: str | None = None
    skills: str | None = None
    items: str | None = None
    str_mod: int | None = None
    dex_mod: int | None = None
    con_mod: int | None = None
    int_mod: int | None = None
    wis_mod: int | None = None
    cha_mod: int | None = None
    ac: int | None = None
    fortitude: int | None = None
    reflex: int | None = None
    will: int | None = None
    hp: int | None = None
    immunities: str | None = None
    resistances: str | None = None
    weaknesses: str | None = None
    speed: str | None = None


@dataclass(frozen=True)
class DemiplaneCreature:
    """One Demiplane creature row after parsing, ready for preview or insert."""

    name: str
    level: int | None
    slug: str
    source_name: str | None
    family: str | None
    image_url: str | None
    stat_html: str | None
    description_html: str | None
    raw: dict[str, Any]
    traits: TraitParts
    stats: ParsedStats


def parse_args() -> argparse.Namespace:
    """Parse CLI switches. The script is preview-only unless --commit is set."""

    parser = argparse.ArgumentParser(
        description="Import Demiplane PF2 creatures missing from local pf2.Monster."
    )
    parser.add_argument("--commit", action="store_true", help="Insert rows. Default is preview only.")
    parser.add_argument("--name", help="Only inspect Demiplane creature names matching this fragment.")
    parser.add_argument("--page-size", type=int, default=250, help="Demiplane GraphQL page size.")
    parser.add_argument("--max-pages", type=int, help="Stop after this many Demiplane pages.")
    parser.add_argument("--max-imports", type=int, help="Stop after this many new local creature rows.")
    parser.add_argument("--commit-batch-size", type=int, default=25, help="Commit after this many inserts.")
    parser.add_argument("--delay", type=float, default=0.05, help="Delay between commit-mode rows.")
    parser.add_argument("--progress-every", type=int, default=100, help="Print progress every N creatures.")
    parser.add_argument(
        "--match-name-only",
        action="store_true",
        help="Treat any existing local monster with the same name as already present, ignoring level.",
    )
    parser.add_argument(
        "--skip-images",
        action="store_true",
        help="Do not download image bytes into MonsterImage during commit.",
    )
    parser.add_argument(
        "--maximum-image-size-bytes",
        type=int,
        default=20 * 1024 * 1024,
        help="Reject downloaded images larger than this.",
    )
    return parser.parse_args()


def connect() -> pyodbc.Connection:
    """Connect to local PathfinderUtil using the installed SQL Server driver."""

    # Different developer shells have had different ODBC versions/options.
    # Try the known-good variants before failing the run.
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


def normalize_name(value: str | None) -> str:
    """Normalize names for duplicate checks without changing stored values."""

    return re.sub(r"\s+", " ", value or "").strip().lower()


def clean(value: Any) -> str | None:
    """Collapse whitespace and convert blank values to None."""

    if value is None:
        return None

    value = re.sub(r"\s+", " ", str(value)).strip()
    return value or None


def to_int(value: Any) -> int | None:
    """Parse integer fields that may arrive as strings or signed text."""

    if value is None:
        return None

    if isinstance(value, int):
        return value

    text = str(value).replace("\u2013", "-").replace("\u2212", "-")
    match = re.search(r"[-+]?\d+", text)
    return int(match.group(0)) if match else None


def load_existing_monster_keys(
    cursor: pyodbc.Cursor,
) -> tuple[set[tuple[str, int | None]], set[str]]:
    """Load local monster keys for both normal and name-only matching."""

    cursor.execute("""
        SELECT Name, Level
        FROM pf2.Monster
    """)
    by_name_level: set[tuple[str, int | None]] = set()
    by_name: set[str] = set()

    for row in cursor.fetchall():
        normalized = normalize_name(row.Name)
        by_name.add(normalized)
        by_name_level.add((normalized, int(row.Level) if row.Level is not None else None))

    return by_name_level, by_name


def find_monster_image_table(cursor: pyodbc.Cursor) -> str | None:
    """Find the local MonsterImage table, regardless of schema placement."""

    for schema_name in ("pf2", "dbo"):
        cursor.execute("""
            SELECT 1
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = ?
              AND TABLE_NAME = 'MonsterImage'
        """, schema_name)

        if cursor.fetchone():
            return f"{schema_name}.MonsterImage"

    return None


def fetch_demiplane_creatures(
    session: requests.Session,
    page_size: int,
    max_pages: int | None,
    name: str | None,
):
    """Yield parsed creatures from the paged Demiplane GraphQL endpoint."""

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

        # Convert rows one at a time so bad/incomplete rows can be skipped
        # without losing the rest of the page.
        for row in rows:
            creature = demiplane_creature_from_row(row)

            if creature:
                yield creature

        if len(rows) < page_size:
            return

        offset += page_size


def demiplane_creature_from_row(row: dict[str, Any]) -> DemiplaneCreature | None:
    """Convert one GraphQL row into the local import DTO."""

    name = clean(row.get("name"))
    display = row.get("elementDisplayByElementDisplayId") or {}
    slug = clean(display.get("slug"))

    if not name or not slug:
        return None

    stat_html = clean(row.get("element_display"))
    description_html = clean(row.get("long_description") or row.get("short_description"))

    return DemiplaneCreature(
        name=name,
        level=to_int(row.get("level")),
        slug=slug,
        source_name=clean(row.get("primary_source_name")),
        family=family_name(row.get("creature_family")),
        image_url=best_image_url(row),
        stat_html=stat_html,
        description_html=description_html,
        raw=row,
        traits=parse_traits(row.get("traits")),
        stats=parse_stats(stat_html or ""),
    )


def family_name(value: Any) -> str | None:
    """Convert Demiplane family slugs into local display names."""

    value = clean(value)

    if not value:
        return None

    value = re.sub(r"-(rm|legacy)$", "", value, flags=re.I)
    return value.replace("-", " ").title()


def parse_traits(raw_traits: Any) -> TraitParts:
    """Split rarity, size, alignment, and remaining trait labels."""

    labels = []

    for part in str(raw_traits or "").split(","):
        label = clean(part.split("|", 1)[0])

        if label:
            labels.append(label)

    rarity = None
    size = None
    alignment = None
    creature_traits = []

    for label in labels:
        lower = label.lower()

        if lower in RARITY_LABELS:
            rarity = label.title()
            continue

        if lower in SIZE_LABELS:
            size = label.title()
            continue

        if lower in ALIGNMENT_LABELS:
            alignment = ALIGNMENT_LABELS[lower]
            continue

        creature_traits.append(label)

    if rarity is None:
        rarity = "Common"

    # Unique is both a rarity label and our signal that this missing creature
    # should land as an NPC in the local monster table.
    is_unique = any(label.lower() == "unique" for label in labels)

    return TraitParts(
        labels=labels,
        rarity=rarity,
        size=size,
        alignment=alignment,
        creature_traits=creature_traits,
        is_unique=is_unique,
    )


def parse_stats(html: str) -> ParsedStats:
    """Parse the stat-block fields we can reliably locate from Demiplane HTML."""

    lines = stat_lines(html)
    text = "\n".join(lines)
    abilities = parse_ability_mods(text)

    perception_line = first_line(lines, "Perception")
    perception = first_number_after_label(perception_line, "Perception")
    senses = after_semicolon(perception_line)

    saves_line = first_line(lines, "AC")

    return ParsedStats(
        perception=perception,
        senses=senses,
        languages=after_label(first_line(lines, "Languages"), "Languages"),
        skills=after_label(first_line(lines, "Skills"), "Skills"),
        items=after_label(first_line(lines, "Items"), "Items"),
        str_mod=abilities.get("str"),
        dex_mod=abilities.get("dex"),
        con_mod=abilities.get("con"),
        int_mod=abilities.get("int"),
        wis_mod=abilities.get("wis"),
        cha_mod=abilities.get("cha"),
        ac=first_number_after_label(saves_line, "AC"),
        fortitude=first_number_after_label(saves_line, "Fort"),
        reflex=first_number_after_label(saves_line, "Ref"),
        will=first_number_after_label(saves_line, "Will"),
        hp=first_number_after_label(first_line(lines, "HP"), "HP"),
        immunities=after_label(first_line(lines, "Immunities"), "Immunities"),
        resistances=after_label(first_line(lines, "Resistances"), "Resistances"),
        weaknesses=after_label(first_line(lines, "Weaknesses"), "Weaknesses"),
        speed=after_label(first_line(lines, "Speed"), "Speed"),
    )


def stat_lines(html: str) -> list[str]:
    """Flatten Demiplane stat HTML into searchable text lines."""

    soup = BeautifulSoup(html or "", "lxml")
    lines = []

    for node in soup.find_all(["p", "div"]):
        text = clean(node.get_text(" ", strip=True))

        if text:
            lines.append(text)

    if not lines:
        text = clean(soup.get_text(" ", strip=True))
        return [text] if text else []

    return lines


def first_line(lines: list[str], label: str) -> str | None:
    """Return the first flattened stat line containing a label."""

    pattern = re.compile(rf"\b{re.escape(label)}\b", re.I)

    for line in lines:
        if pattern.search(line):
            return line

    return None


def first_number_after_label(line: str | None, label: str) -> int | None:
    """Read the first signed number after a stat label."""

    if not line:
        return None

    match = re.search(
        rf"\b{re.escape(label)}\b\s*([+-]?\d+)",
        line.replace("\u2013", "-").replace("\u2212", "-"),
        re.I,
    )
    return int(match.group(1)) if match else None


def after_label(line: str | None, label: str) -> str | None:
    """Return free text that follows a label on a stat line."""

    if not line:
        return None

    match = re.search(rf"\b{re.escape(label)}\b\s*(.+)$", line, re.I)
    return clean(match.group(1)) if match else None


def after_semicolon(line: str | None) -> str | None:
    """Return text after a semicolon, used for Perception senses."""

    if not line or ";" not in line:
        return None

    return clean(line.split(";", 1)[1])


def parse_ability_mods(text: str) -> dict[str, int]:
    """Parse ability modifiers from combined stat text."""

    result = {}
    normalized = text.replace("\u2013", "-").replace("\u2212", "-")

    for ability in ("Str", "Dex", "Con", "Int", "Wis", "Cha"):
        match = re.search(rf"\b{ability}\b\s*([+-]?\d+)", normalized, re.I)

        if match:
            result[ability.lower()] = int(match.group(1))

    return result


def best_image_url(row: dict[str, Any]) -> str | None:
    """Prefer main art, fall back to thumbnail, and ignore placeholders."""

    for key in ("element_image", "element_thumbnail"):
        raw = clean(row.get(key))

        if not raw or is_placeholder_image(raw):
            continue

        return absolute_image_url(raw)

    return None


def is_placeholder_image(path: str) -> bool:
    """Identify generic family art that should not become MonsterImage data."""

    name = path.rsplit("/", 1)[-1].lower()
    return name in PLACEHOLDER_IMAGE_NAMES


def absolute_image_url(path: str) -> str:
    """Expand Demiplane image paths to absolute CDN URLs."""

    if path.startswith("http://") or path.startswith("https://"):
        return path

    return IMAGE_BASE_URL + quote(path.lstrip("/"), safe="/:")


def get_or_create_lookup(
    cursor: pyodbc.Cursor,
    table: str,
    id_column: str,
    name_column: str,
    value: str | None,
    cache: dict[tuple[str, str], int],
) -> int | None:
    """Resolve a lookup ID, creating the lookup row when Demiplane adds one."""

    value = clean(value)

    if not value:
        # Missing alignment, family, or source stays NULL in the destination.
        return None

    key = (table, value.lower())

    if key in cache:
        return cache[key]

    cursor.execute(f"""
        SELECT TOP 1 {id_column}
        FROM {table}
        WHERE {name_column} = ?
    """, value)
    row = cursor.fetchone()

    if row:
        cache[key] = int(row[0])
        return cache[key]

    cursor.execute(f"""
        INSERT INTO {table}
        (
            {name_column}
        )
        OUTPUT INSERTED.{id_column}
        VALUES
        (?)
    """, value)
    cache[key] = int(cursor.fetchone()[0])
    return cache[key]


def insert_creature(
    cursor: pyodbc.Cursor,
    creature: DemiplaneCreature,
    lookup_cache: dict[tuple[str, str], int],
) -> int:
    """Insert the creature and related rows, returning the new MonsterId."""

    rarity_id = get_or_create_lookup(
        cursor, "pf2.Rarity", "RarityId", "Name", creature.traits.rarity, lookup_cache
    )
    size_id = get_or_create_lookup(
        cursor, "pf2.SizeCategory", "SizeId", "Name", creature.traits.size, lookup_cache
    )
    alignment_id = get_or_create_lookup(
        cursor, "pf2.Alignment", "AlignmentId", "Name", creature.traits.alignment, lookup_cache
    )
    family_id = get_or_create_lookup(
        cursor, "pf2.MonsterFamily", "FamilyId", "Name", creature.family, lookup_cache
    )
    source_id = get_or_create_lookup(
        cursor, "pf2.SourceBook", "SourceBookId", "Name", creature.source_name, lookup_cache
    )
    demiplane_url = DEMIPLANE_CREATURE_URL.format(slug=creature.slug)
    raw_json = json.dumps(creature.raw, ensure_ascii=False)
    raw_text = html_to_text("\n".join(x for x in (creature.description_html, creature.stat_html) if x))

    # Aon* columns are reused as generic external-source columns here because
    # these creatures do not have Archives of Nethys IDs yet.
    cursor.execute("""
        INSERT INTO pf2.Monster
        (
            AonId,
            AonUrl,
            Name,
            Level,
            RarityId,
            SizeId,
            AlignmentId,
            FamilyId,
            SourceBookId,
            SourcePage,
            IsUnique,
            IsNPC,
            RawHtml,
            RawText,
            RawJson,
            CreatedAt,
            UpdatedAt,
            LastScraped,
            ScrapeVersion,
            ImageUrl,
            RawMD
        )
        OUTPUT INSERTED.MonsterId
        VALUES
        (
            NULL, ?, ?, ?,
            ?, ?, ?, ?,
            ?, NULL,
            ?, ?,
            ?, ?, ?,
            SYSDATETIME(),
            SYSDATETIME(),
            SYSDATETIME(),
            ?,
            ?,
            NULL
        )
    """,
        demiplane_url,
        creature.name,
        creature.level,
        rarity_id,
        size_id,
        alignment_id,
        family_id,
        source_id,
        1 if creature.traits.is_unique else 0,
        # Treat all Demiplane Unique creatures as NPCs for local browsing.
        1 if creature.traits.is_unique else 0,
        creature.stat_html,
        raw_text,
        raw_json,
        SCRAPE_VERSION,
        creature.image_url,
    )
    monster_id = int(cursor.fetchone()[0])

    insert_stats(cursor, monster_id, creature.stats)

    if source_id:
        # Demiplane does not expose page numbers in this query, so the link row
        # records source membership only.
        cursor.execute("""
            INSERT INTO pf2.MonsterSourceLink
            (
                MonsterId,
                SourceBookId,
                PageNumber
            )
            VALUES
            (?, ?, NULL)
        """, monster_id, source_id)

    for trait in creature.traits.creature_traits:
        trait_id = get_or_create_lookup(
            cursor, "pf2.Trait", "TraitId", "Name", trait, lookup_cache
        )

        if trait_id is None:
            continue

        cursor.execute("""
            INSERT INTO pf2.MonsterTrait
            (
                MonsterId,
                TraitId
            )
            VALUES
            (?, ?)
        """, monster_id, trait_id)

    return monster_id


def html_to_text(html: str | None) -> str | None:
    """Strip HTML to readable text for RawText."""

    if not html:
        return None

    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    return clean(text)


def insert_stats(cursor: pyodbc.Cursor, monster_id: int, stats: ParsedStats) -> None:
    """Insert parsed numeric/text stats for the new monster."""

    cursor.execute("""
        INSERT INTO pf2.MonsterStats
        (
            MonsterId,
            Perception,
            Senses,
            Languages,
            Skills,
            Items,
            StrMod,
            DexMod,
            ConMod,
            IntMod,
            WisMod,
            ChaMod,
            AC,
            Fortitude,
            Reflex,
            Will,
            HP,
            Immunities,
            Resistances,
            Weaknesses,
            Speed
        )
        VALUES
        (?, ?, ?, ?, ?, ?,
         ?, ?, ?, ?, ?, ?,
         ?, ?, ?, ?, ?,
         ?, ?, ?, ?)
    """,
        monster_id,
        stats.perception,
        stats.senses,
        stats.languages,
        stats.skills,
        stats.items,
        stats.str_mod,
        stats.dex_mod,
        stats.con_mod,
        stats.int_mod,
        stats.wis_mod,
        stats.cha_mod,
        stats.ac,
        stats.fortitude,
        stats.reflex,
        stats.will,
        stats.hp,
        stats.immunities,
        stats.resistances,
        stats.weaknesses,
        stats.speed,
    )


def download_image(
    session: requests.Session,
    image_url: str,
    maximum_image_size_bytes: int,
) -> bytes | None:
    """Download and lightly validate an image before inserting bytes."""

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

    # Keep oversized CDN surprises out of SQL Server.
    if len(image_data) > maximum_image_size_bytes:
        return None

    return image_data


def insert_monster_image(
    cursor: pyodbc.Cursor,
    image_table: str,
    monster_id: int,
    image_data: bytes,
) -> None:
    """Insert one MonsterImage row if another process has not already done so."""

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


def is_existing(
    creature: DemiplaneCreature,
    existing_name_level: set[tuple[str, int | None]],
    existing_name: set[str],
    match_name_only: bool,
) -> bool:
    """Check whether a Demiplane creature already exists locally."""

    normalized = normalize_name(creature.name)

    if match_name_only:
        return normalized in existing_name

    return (normalized, creature.level) in existing_name_level


def main() -> None:
    """Run the import workflow from scan, to preview, to optional commit."""

    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    scanned = 0
    existing = 0
    preview_new = 0
    imported = 0
    image_imported = 0
    failed = 0
    duplicate_source = 0
    pending_commits = 0
    lookup_cache: dict[tuple[str, str], int] = {}
    planned_name_level: set[tuple[str, int | None]] = set()
    planned_name: set[str] = set()

    with connect() as connection:
        # Explicit transaction control lets us commit in batches and roll back a
        # failed row without ending the whole import.
        connection.autocommit = False
        cursor = connection.cursor()
        existing_name_level, existing_name = load_existing_monster_keys(cursor)
        image_table = find_monster_image_table(cursor)

        logging.info("Local monsters loaded: %d", len(existing_name_level))

        if image_table:
            logging.info("Using image table: %s", image_table)
        else:
            logging.info("No MonsterImage table found; creature rows will still import.")

        with requests.Session() as session:
            for creature in fetch_demiplane_creatures(
                session=session,
                page_size=args.page_size,
                max_pages=args.max_pages,
                name=args.name,
            ):
                scanned += 1
                normalized_name = normalize_name(creature.name)
                name_level_key = (normalized_name, creature.level)

                # Normal mode dedupes by name+level. The optional name-only
                # mode is useful for hand review when level drift is suspected.
                if args.match_name_only:
                    already_existing = normalized_name in existing_name
                    already_planned = normalized_name in planned_name
                else:
                    already_existing = name_level_key in existing_name_level
                    already_planned = name_level_key in planned_name_level

                if already_existing:
                    existing += 1
                elif already_planned:
                    # Same-run duplicate, usually because the creature appears
                    # through more than one Demiplane source product.
                    duplicate_source += 1
                else:
                    preview_new += 1
                    unique_note = " -> NPC" if creature.traits.is_unique else ""
                    alignment_note = creature.traits.alignment or "no alignment"

                    if not args.commit:
                        # Preview mode does not modify existing_name*, so it
                        # needs its own planned sets to keep output deduped.
                        planned_name_level.add(name_level_key)
                        planned_name.add(normalized_name)

                    logging.info(
                        "%sIMPORT %s L%s [%s, %s, %s]%s | %s",
                        "WOULD " if not args.commit else "",
                        creature.name,
                        creature.level,
                        creature.traits.rarity or "",
                        creature.traits.size or "",
                        alignment_note,
                        unique_note,
                        creature.source_name or "",
                    )

                    if args.commit:
                        try:
                            monster_id = insert_creature(cursor, creature, lookup_cache)
                            # After a successful insert, later source-book
                            # duplicates in this same run become "existing."
                            existing_name_level.add(name_level_key)
                            existing_name.add(normalized_name)
                            imported += 1
                            pending_commits += 1

                            if (
                                image_table
                                and creature.image_url
                                and not args.skip_images
                            ):
                                image_data = download_image(
                                    session,
                                    creature.image_url,
                                    args.maximum_image_size_bytes,
                                )

                                if image_data:
                                    insert_monster_image(cursor, image_table, monster_id, image_data)
                                    image_imported += 1

                            if pending_commits >= args.commit_batch_size:
                                connection.commit()
                                pending_commits = 0
                                logging.info("Committed batch.")

                            if args.delay > 0:
                                time.sleep(args.delay)
                        except Exception:
                            # Roll back only the current batch. Rebuild the
                            # cursor/cache because lookup inserts may have been
                            # undone with the failed monster row.
                            connection.rollback()
                            cursor = connection.cursor()
                            lookup_cache = {}
                            pending_commits = 0
                            failed += 1
                            logging.exception("Failed to import %s", creature.name)

                    limit_count = imported if args.commit else preview_new

                    if args.max_imports is not None and limit_count >= args.max_imports:
                        break

                if args.progress_every and scanned % args.progress_every == 0:
                    logging.info(
                        "Scanned %d | existing %d | new %d | duplicate-source %d | imported %d | failed %d",
                        scanned,
                        existing,
                        preview_new,
                        duplicate_source,
                        imported,
                        failed,
                    )

        if args.commit and pending_commits:
            connection.commit()

    logging.info(
        "Done. Scanned=%d existing=%d new=%d duplicate_source=%d imported=%d images=%d failed=%d",
        scanned,
        existing,
        preview_new,
        duplicate_source,
        imported,
        image_imported,
        failed,
    )

    if not args.commit:
        logging.info("Preview only. Re-run with --commit to insert missing creatures.")


if __name__ == "__main__":
    main()
