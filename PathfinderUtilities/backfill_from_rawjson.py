# pip install pyodbc

"""Backfill empty pf2 child tables from parent RawJson / RawMD. No AoN scrape."""

from __future__ import annotations

from mssql_conn import sql_auth
import argparse
import json
import re

import pyodbc

from parseMonsterDetail import replace_monster_attacks

CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;"
    "DATABASE=PathfinderUtil;"
    + sql_auth()
    + "TrustServerCertificate=yes;"
)

BATCH_SIZE = 100

ACTION_MAP = {
    "single action": "one-action",
    "one-action": "one-action",
    "two actions": "two-actions",
    "two-actions": "two-actions",
    "three actions": "three-actions",
    "three-actions": "three-actions",
    "free action": "free-action",
    "free-action": "free-action",
    "reaction": "reaction",
}

ATTACK_BLOCK_RE = re.compile(
    r"\*\*(Melee|Ranged)\*\*\s*"
    r"(?:(Single Action|Two Actions|Three Actions|Free Action|Reaction|one-action|two-actions|three-actions|free-action)\s+)?"
    r"(.*?)\s*"
    r"\*\*Damage\*\*\s*(.+?)"
    r"(?=\n\*\*(?!Damage)|\n---|\Z)",
    re.I | re.S,
)

SPELL_HEADER_RE = re.compile(
    r"^\*\*([^*\n]+Spells)\*\*\s*"
    r"DC\s+(\d+)"
    r"(?:\s*,\s*attack\s+([+-]?\d+))?"
    r"(?:\s*,\s*(.+?))?\s*$",
    re.I | re.M,
)

RANK_RE = re.compile(
    r"^-?\s*\*\*((?:Cantrips)(?:\s+\([^)]+\))?|\d+(?:st|nd|rd|th))\*\*\s*$",
    re.I,
)

SPELL_LINK_RE = re.compile(
    r"\[([^\]]+)\]\(/Spells\.aspx\?ID=\d+\)(?:\s*\(([^)]*)\))?",
    re.I,
)

LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def clean(value):
    if value is None:
        return None

    if isinstance(value, list):
        return ", ".join(str(x) for x in value if x is not None).strip() or None

    return re.sub(r"\s+", " ", str(value)).strip() or None


def to_int(value):
    if value is None:
        return None

    if isinstance(value, int):
        return value

    match = re.search(r"-?\d+", str(value))
    return int(match.group(0)) if match else None


def first_existing(src, *names):
    for name in names:
        if name in src and src[name] not in (None, "", []):
            return src[name]

    lowered = {str(k).lower(): v for k, v in src.items()}

    for name in names:
        wanted = name.lower()

        for key, value in lowered.items():
            if wanted == key or wanted in key:
                if value not in (None, "", []):
                    return value

    return None


def split_values(value):
    if not value:
        return []

    if isinstance(value, list):
        return [clean(x) for x in value if clean(x)]

    text = re.sub(r"<[^>]+>", " ", str(value))
    text = text.replace(";", ",").replace("|", ",")
    return [clean(x) for x in text.split(",") if clean(x)]


def unique_values(values):
    unique = []
    seen = set()

    for value in values:
        cleaned = clean(value)

        if not cleaned:
            continue

        key = cleaned.lower()

        if key in seen:
            continue

        seen.add(key)
        unique.append(cleaned)

    return unique


def strip_md(text):
    if not text:
        return None

    text = LINK_RE.sub(r"\1", text)
    text = re.sub(r"[_*`]", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ,;")
    return text or None


def normalize_statblock(text):
    if not text:
        return ""

    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(
        r"<actions[^>]*string=\"([^\"]+)\"[^>]*/?>",
        r"\1",
        text,
        flags=re.I,
    )
    return text


def map_action_cost(value):
    if not value:
        return None

    return ACTION_MAP.get(value.strip().lower(), clean(value))


def spell_rank_to_int(rank):
    if not rank:
        return None

    if str(rank).lower().startswith("cantrip"):
        return 0

    return to_int(rank)


def source_page_from_raw(src):
    raw = clean(first_existing(src, "primary_source_raw", "source_raw"))

    if not raw:
        return None

    match = re.search(r"\bpg\.?\s*(\d+)", raw, re.I)
    return int(match.group(1)) if match else None


def parse_attacks_from_statblock(text):
    text = normalize_statblock(text)
    attacks = []

    for match in ATTACK_BLOCK_RE.finditer(text):
        attack_type = match.group(1).title()
        action_cost = map_action_cost(match.group(2))
        weapon_line = strip_md(match.group(3))
        damage_line = strip_md(match.group(4))

        traits = None
        trait_match = re.search(r"\(([^()]*)\)\s*,?$", weapon_line or "")

        if trait_match:
            traits = clean(trait_match.group(1))
            weapon_line = clean((weapon_line or "")[: trait_match.start()].rstrip(" ,"))

        bonus = None
        name = weapon_line
        bonus_match = re.search(
            r"(.+?)\s+([+-]\d+)(?:\s+\[[^\]]+\])?$",
            weapon_line or "",
        )

        if bonus_match:
            name = clean(bonus_match.group(1))
            bonus = int(bonus_match.group(2))

        effects = None
        damage = damage_line

        if damage and " plus " in damage.lower():
            idx = damage.lower().find(" plus ")
            effects = clean(damage[idx + 6 :])
            damage = clean(damage[:idx])

        if not name:
            continue

        if len(name) > 200 or "Single Action" in name:
            first = clean(name.split(",")[0])
            if first and re.search(r"[+-]\d+\s*$", first) and len(first) <= 200:
                name = first
            else:
                continue

        attacks.append(
            {
                "attack_type": attack_type,
                "name": name,
                "action_cost": action_cost,
                "attack_bonus": bonus,
                "traits": traits,
                "damage": damage,
                "effects": effects,
            }
        )

    return attacks


def parse_spellcasting_from_statblock(text):
    text = normalize_statblock(text)
    blocks = []

    headers = list(SPELL_HEADER_RE.finditer(text))

    for index, header in enumerate(headers):
        title = clean(header.group(1))
        dc = to_int(header.group(2))
        attack_bonus = to_int(header.group(3))
        notes = clean(header.group(4))
        start = header.end()
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        body = text[start:end]
        ability_cut = re.search(
            r"^\*\*(?:\[[^\]]+\]|[A-Za-z][^*]{0,80})\*\*\s{2,}",
            body,
            re.M,
        )

        if ability_cut:
            body = body[: ability_cut.start()]

        tradition = None
        spellcasting_type = title
        title_match = re.match(r"^([A-Za-z]+)\s+(.+)$", title or "")

        if title_match:
            tradition = title_match.group(1)
            spellcasting_type = clean(title_match.group(2))

        spells = []
        current_rank = None

        for line in body.splitlines():
            line = line.strip()

            if not line:
                continue

            rank_match = RANK_RE.match(line)

            if rank_match:
                current_rank = rank_match.group(1)
                continue

            names = []

            for link in SPELL_LINK_RE.finditer(line):
                spell_name = clean(link.group(1))
                extra = clean(link.group(2))
                uses = None
                leftover_notes = None

                if extra:
                    if extra.lower().startswith("x") or re.match(r"^\d+$", extra):
                        uses = extra
                    elif extra.lower() == "at will":
                        uses = extra
                    else:
                        leftover_notes = extra

                if spell_name and len(spell_name) <= 250:
                    names.append(
                        {
                            "name": spell_name[:250],
                            "uses": uses,
                            "notes": leftover_notes,
                        }
                    )

            if names:
                spells.append({"rank": current_rank, "entries": names})

        if tradition or spells:
            blocks.append(
                {
                    "tradition": tradition,
                    "spellcasting_type": spellcasting_type,
                    "dc": dc,
                    "attack_bonus": attack_bonus,
                    "notes": notes,
                    "spells": [
                        {
                            "rank": group["rank"],
                            "names": [entry["name"] for entry in group["entries"]],
                            "entries": group["entries"],
                        }
                        for group in spells
                    ],
                }
            )

    return blocks


def replace_monster_spellcasting_with_uses(cur, monster_id, spellcasting_blocks):
    cur.execute(
        """
        SELECT SpellcastingId
        FROM pf2.MonsterSpellcasting
        WHERE MonsterId = ?
        """,
        monster_id,
    )
    spellcasting_ids = [row[0] for row in cur.fetchall()]

    for spellcasting_id in spellcasting_ids:
        cur.execute(
            "DELETE FROM pf2.MonsterSpell WHERE SpellcastingId = ?",
            spellcasting_id,
        )

    cur.execute("DELETE FROM pf2.MonsterSpellcasting WHERE MonsterId = ?", monster_id)

    for block in spellcasting_blocks:
        cur.execute(
            """
            INSERT INTO pf2.MonsterSpellcasting
            (
                MonsterId,
                Tradition,
                SpellcastingType,
                DC,
                AttackBonus,
                Notes
            )
            OUTPUT INSERTED.SpellcastingId
            VALUES
            (?, ?, ?, ?, ?, ?)
            """,
            monster_id,
            block.get("tradition"),
            block.get("spellcasting_type"),
            block.get("dc"),
            block.get("attack_bonus"),
            block.get("notes"),
        )
        spellcasting_id = cur.fetchone()[0]

        for rank in block.get("spells", []):
            entries = rank.get("entries")

            if entries:
                iterable = entries
            else:
                iterable = [
                    {"name": name, "uses": None, "notes": None}
                    for name in rank.get("names", [])
                ]

            for entry in iterable:
                cur.execute(
                    """
                    INSERT INTO pf2.MonsterSpell
                    (
                        SpellcastingId,
                        SpellLevel,
                        SpellName,
                        Uses,
                        Notes
                    )
                    VALUES
                    (?, ?, ?, ?, ?)
                    """,
                    spellcasting_id,
                    spell_rank_to_int(rank.get("rank")),
                    entry.get("name"),
                    entry.get("uses"),
                    entry.get("notes"),
                )


def get_or_create_lookup(cur, table, id_col, name_col, name, cache):
    name = clean(name)

    if not name:
        return None

    key = (table, name.lower())

    if key in cache:
        return cache[key]

    cur.execute(f"SELECT TOP 1 {id_col} FROM {table} WHERE {name_col} = ?", name)
    row = cur.fetchone()

    if row:
        cache[key] = row[0]
        return row[0]

    cur.execute(
        f"""
        INSERT INTO {table} ({name_col})
        OUTPUT INSERTED.{id_col}
        VALUES (?)
        """,
        name,
    )
    new_id = cur.fetchone()[0]
    cache[key] = new_id
    return new_id


def table_count(cur, table):
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return cur.fetchone()[0]


def load_json(raw):
    if not raw:
        return None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None

    return parsed if isinstance(parsed, dict) else None


def insert_source_link(cur, table, id_col, parent_id, source_id, source_page):
    if not source_id:
        return False

    cur.execute(
        f"""
        IF NOT EXISTS
        (
            SELECT 1
            FROM {table}
            WHERE {id_col} = ?
              AND SourceBookId = ?
              AND
              (
                  (PageNumber = ?)
                  OR (PageNumber IS NULL AND ? IS NULL)
              )
        )
        BEGIN
            INSERT INTO {table} ({id_col}, SourceBookId, PageNumber)
            VALUES (?, ?, ?)
        END
        """,
        parent_id,
        source_id,
        source_page,
        source_page,
        parent_id,
        source_id,
        source_page,
    )
    return True


def insert_trait_link(cur, table, id_col, parent_id, trait_id):
    if not trait_id:
        return

    cur.execute(
        f"""
        IF NOT EXISTS
        (
            SELECT 1 FROM {table}
            WHERE {id_col} = ? AND TraitId = ?
        )
        BEGIN
            INSERT INTO {table} ({id_col}, TraitId)
            VALUES (?, ?)
        END
        """,
        parent_id,
        trait_id,
        parent_id,
        trait_id,
    )


def insert_spell_tradition(cur, spell_id, tradition_id):
    if not tradition_id:
        return

    cur.execute(
        """
        IF NOT EXISTS
        (
            SELECT 1 FROM pf2.SpellTradition
            WHERE SpellId = ? AND TraditionId = ?
        )
        BEGIN
            INSERT INTO pf2.SpellTradition (SpellId, TraditionId)
            VALUES (?, ?)
        END
        """,
        spell_id,
        tradition_id,
        spell_id,
        tradition_id,
    )


def backfill_simple_entity(
    cur,
    cache,
    parent_table,
    id_col,
    source_table,
    trait_table,
    tradition_table=None,
    limit=None,
):
    extra = ""

    if tradition_table:
        extra = f"""
            OR NOT EXISTS (
                SELECT 1 FROM {tradition_table} x WHERE x.{id_col} = p.{id_col}
            )
        """

    sql = f"""
        SELECT p.{id_col}, p.RawJson
        FROM {parent_table} p
        WHERE ISJSON(p.RawJson) = 1
          AND
          (
              NOT EXISTS (SELECT 1 FROM {source_table} s WHERE s.{id_col} = p.{id_col})
              OR NOT EXISTS (SELECT 1 FROM {trait_table} t WHERE t.{id_col} = p.{id_col})
              {extra}
          )
    """

    if limit:
        sql = f"SELECT TOP ({int(limit)}) q.{id_col}, q.RawJson FROM ({sql}) q"

    cur.execute(sql)
    rows = cur.fetchall()
    inserted = 0

    for parent_id, raw in rows:
        src = load_json(raw)

        if not src:
            continue

        source = clean(first_existing(src, "primary_source", "source"))
        source_page = source_page_from_raw(src)
        source_id = get_or_create_lookup(
            cur, "pf2.SourceBook", "SourceBookId", "Name", source, cache
        )
        insert_source_link(cur, source_table, id_col, parent_id, source_id, source_page)

        for trait in unique_values(
            split_values(first_existing(src, "trait", "traits", "trait_raw"))
        ):
            trait_id = get_or_create_lookup(
                cur, "pf2.Trait", "TraitId", "Name", trait, cache
            )
            insert_trait_link(cur, trait_table, id_col, parent_id, trait_id)

        if tradition_table:
            for tradition in unique_values(
                split_values(first_existing(src, "tradition", "traditions"))
            ):
                tradition_id = get_or_create_lookup(
                    cur, "pf2.Tradition", "TraditionId", "Name", tradition, cache
                )
                insert_spell_tradition(cur, parent_id, tradition_id)

        inserted += 1

    return len(rows), inserted


def monster_statblock(raw_md, raw_html, raw_json):
    if raw_md and raw_md.strip():
        return raw_md

    src = load_json(raw_json)

    if src:
        markdown = src.get("markdown") or src.get("search_markdown") or ""

        if markdown:
            return markdown

    return raw_html or ""


def backfill_monsters(cur, cache, force_replace, limit, dry_run=False):
    top = f"TOP ({int(limit)}) " if limit else ""
    sql = f"""
        SELECT {top}
            m.MonsterId,
            m.RawMD,
            m.RawHtml,
            m.RawJson
        FROM pf2.Monster m
        WHERE
            (
                NOT EXISTS (
                    SELECT 1 FROM pf2.MonsterAttack a WHERE a.MonsterId = m.MonsterId
                )
                OR NOT EXISTS (
                    SELECT 1 FROM pf2.MonsterSpellcasting sc
                    WHERE sc.MonsterId = m.MonsterId
                )
                OR NOT EXISTS (
                    SELECT 1 FROM pf2.MonsterSourceLink l
                    WHERE l.MonsterId = m.MonsterId
                )
                OR ? = 1
            )
        ORDER BY m.MonsterId
    """

    cur.execute(sql, 1 if force_replace else 0)
    rows = cur.fetchall()

    stats = {
        "scanned": 0,
        "attacks": 0,
        "spell_blocks": 0,
        "spells": 0,
        "source_links": 0,
        "json_invalid": 0,
        "skipped_detail": 0,
    }

    for index, (monster_id, raw_md, raw_html, raw_json) in enumerate(rows, start=1):
        stats["scanned"] += 1

        try:
            src = load_json(raw_json)

            if raw_json and not src:
                stats["json_invalid"] += 1

            if src:
                source = clean(first_existing(src, "primary_source", "source"))
                source_page = source_page_from_raw(src)
                source_id = get_or_create_lookup(
                    cur, "pf2.SourceBook", "SourceBookId", "Name", source, cache
                )

                cur.execute(
                    "SELECT 1 FROM pf2.MonsterSourceLink WHERE MonsterId = ?",
                    monster_id,
                )

                if source_id and (force_replace or not cur.fetchone()):
                    insert_source_link(
                        cur,
                        "pf2.MonsterSourceLink",
                        "MonsterId",
                        monster_id,
                        source_id,
                        source_page,
                    )
                    stats["source_links"] += 1

            cur.execute(
                "SELECT 1 FROM pf2.MonsterAttack WHERE MonsterId = ?",
                monster_id,
            )
            has_attacks = cur.fetchone() is not None
            cur.execute(
                "SELECT 1 FROM pf2.MonsterSpellcasting WHERE MonsterId = ?",
                monster_id,
            )
            has_spells = cur.fetchone() is not None

            if has_attacks and has_spells and not force_replace:
                stats["skipped_detail"] += 1
            else:
                statblock = monster_statblock(raw_md, raw_html, raw_json)
                attacks = parse_attacks_from_statblock(statblock)
                spell_blocks = parse_spellcasting_from_statblock(statblock)

                if force_replace or not has_attacks:
                    replace_monster_attacks(cur, monster_id, attacks)
                    stats["attacks"] += len(attacks)

                if force_replace or not has_spells:
                    replace_monster_spellcasting_with_uses(cur, monster_id, spell_blocks)
                    stats["spell_blocks"] += len(spell_blocks)
                    stats["spells"] += sum(
                        len(rank.get("entries") or rank.get("names") or [])
                        for block in spell_blocks
                        for rank in block.get("spells", [])
                    )
        except Exception as exc:
            cur.rollback()
            cache.clear()
            print(f"FAILED MonsterId {monster_id}: {exc}")
            stats["json_invalid"] += 1

        if index % BATCH_SIZE == 0:
            if not dry_run:
                cur.commit()
            print(f"  monsters {index}/{len(rows)}...")

    if not dry_run:
        cur.commit()
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Populate empty pf2 child tables from embedded RawJson/RawMD."
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force-replace", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cn = pyodbc.connect(CONN_STR)
    cn.autocommit = False
    cur = cn.cursor()
    cache = {}

    try:
        print("Feat/spell/equipment junctions for parents with no children...")
        feat_rows, feat_done = backfill_simple_entity(
            cur,
            cache,
            "pf2.Feat",
            "FeatId",
            "pf2.FeatSourceLink",
            "pf2.FeatTrait",
            limit=args.limit,
        )
        spell_rows, spell_done = backfill_simple_entity(
            cur,
            cache,
            "pf2.Spell",
            "SpellId",
            "pf2.SpellSourceLink",
            "pf2.SpellTrait",
            tradition_table="pf2.SpellTradition",
            limit=args.limit,
        )
        eq_rows, eq_done = backfill_simple_entity(
            cur,
            cache,
            "pf2.Equipment",
            "EquipmentId",
            "pf2.EquipmentSourceLink",
            "pf2.EquipmentTrait",
            limit=args.limit,
        )
        print(
            f"  feats scanned={feat_rows} processed={feat_done}; "
            f"spells scanned={spell_rows} processed={spell_done}; "
            f"equipment scanned={eq_rows} processed={eq_done}"
        )
        if not args.dry_run:
            cur.commit()

        print("Monster attacks, spellcasting, and missing source links...")
        monster_stats = backfill_monsters(
            cur, cache, args.force_replace, args.limit, args.dry_run
        )
        print(monster_stats)

        if args.dry_run:
            cn.rollback()
            print("Dry run: rolled back.")
        else:
            cn.commit()
            print("Committed.")
    except Exception:
        cn.rollback()
        raise
    finally:
        cur.close()
        cn.close()


if __name__ == "__main__":
    main()
