# pip install pyodbc

"""Fill missing monster speed, attacks, and abilities from stored RawText / RawMD.

Only writes a child set when that set is empty, unless --force-replace is set.
"""

from __future__ import annotations

from mssql_conn import sql_auth
import argparse
import re

import pyodbc

from backfill_from_rawjson import map_action_cost, parse_attacks_from_statblock, strip_md
from parseMonsterDetail import replace_monster_abilities, replace_monster_attacks

CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;"
    "DATABASE=PathfinderUtil;"
    + sql_auth()
    + "TrustServerCertificate=yes;"
)

BATCH_SIZE = 100

SKIP_HEADINGS = {
    "source",
    "perception",
    "languages",
    "skills",
    "str",
    "dex",
    "con",
    "int",
    "wis",
    "cha",
    "items",
    "ac",
    "fort",
    "ref",
    "will",
    "hp",
    "speed",
    "melee",
    "ranged",
    "damage",
}

HEADING_RE = re.compile(
    r"^\*\*(?:\[([^\]]+)\]\([^)]*\)|([^*\n]+))\*\*\s*(.*)$"
)
ACTION_PREFIX_RE = re.compile(
    r"^(Single Action|Two Actions|Three Actions|Free Action|Reaction|one-action|two-actions|three-actions|free-action|reaction)\b\s*",
    re.I,
)


def clean(value):
    if value is None:
        return None

    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def statblock_text(raw_text, raw_md):
    if raw_md and str(raw_md).strip():
        return str(raw_md).replace("\r\n", "\n").replace("\r", "\n")

    if raw_text and str(raw_text).strip():
        return str(raw_text).replace("\r\n", "\n").replace("\r", "\n")

    return ""


def heading_blocks(text):
    lines = text.splitlines()
    blocks = []
    current = None

    for line in lines:
        match = HEADING_RE.match(line.strip())

        if match:
            if current:
                blocks.append(current)

            name = clean(match.group(1) or match.group(2))
            current = {"name": name, "rest": match.group(3) or "", "body": []}
            continue

        if current is not None and line.strip() != "---":
            current["body"].append(line)

    if current:
        blocks.append(current)

    return blocks


def parse_speed(text):
    for block in heading_blocks(text):
        if (block["name"] or "").lower() == "speed":
            return clean(strip_md(block["rest"]) or strip_md(" ".join(block["body"])))

    for line in text.splitlines():
        line = clean(line)

        if line and line.startswith("Speed "):
            return clean(line.removeprefix("Speed "))

    return None


def parse_abilities(text):
    abilities = []
    seen_attack = False

    for block in heading_blocks(text):
        name = block["name"]
        key = (name or "").lower()

        if key in ("melee", "ranged"):
            seen_attack = True
            continue

        if not name or key in SKIP_HEADINGS or "lore" in key or key.startswith("recall knowledge"):
            continue

        if "spells" in key:
            continue

        remainder = clean(strip_md(block["rest"])) or ""
        action = None
        action_match = ACTION_PREFIX_RE.match(remainder)

        if action_match:
            action = map_action_cost(action_match.group(1))
            remainder = clean(remainder[action_match.end() :]) or ""

        traits = None
        trait_match = re.match(r"^\(([^)]+)\)\s*(.*)$", remainder)

        if trait_match:
            traits = clean(strip_md(trait_match.group(1)))
            remainder = clean(trait_match.group(2)) or ""

        description = clean(" ".join(part for part in (remainder, strip_md(" ".join(block["body"]))) if part))

        if len(name) > 120 or (not description and not action):
            continue

        abilities.append(
            {
                "name": name,
                "action_cost": action,
                "ability_type": "offense" if seen_attack else "defense",
                "traits": traits,
                "description": description,
            }
        )

    return abilities


INLINE_ATTACK_RE = re.compile(
    r"\*\*(Melee|Ranged)\*\*\s+"
    r"(?:(Single Action|Two Actions|Three Actions|Free Action|Reaction)\s+)?"
    r"(.+?)\s+([+-]\d+)(?:\s+\[[^\]]+\])?"
    r"(?:\s+\(([^)]*)\))?"
    r",?\s+Damage\s+(.+?)"
    r"(?=\n\*\*|\n##|\n---|\Z)",
    re.I | re.S,
)


def parse_inline_attacks(text):
    attacks = []

    for match in INLINE_ATTACK_RE.finditer(text):
        name = clean(strip_md(match.group(3)))
        damage = clean(strip_md(match.group(6)))

        if not name or len(name) > 200:
            continue

        attacks.append(
            {
                "attack_type": match.group(1).title(),
                "name": name,
                "action_cost": map_action_cost(match.group(2)) or "one-action",
                "attack_bonus": int(match.group(4)),
                "traits": clean(strip_md(match.group(5))),
                "damage": damage,
                "effects": None,
            }
        )

    return attacks


def parse_attacks(text):
    attacks = parse_attacks_from_statblock(text) or parse_inline_attacks(text)

    for attack in attacks:
        damage = attack.get("damage") or ""
        plus_at = damage.lower().find(" plus ")

        if plus_at >= 0 and not attack.get("effects"):
            attack["effects"] = clean(damage[plus_at + 6 :])
            attack["damage"] = clean(damage[:plus_at])

    return attacks


def delete_flat_text_abilities(cur):
    cur.execute(
        """
        DELETE FROM pf2.MonsterAbility
        WHERE Description LIKE '%Perception +%'
          AND LEN(Description) > 400
        """
    )
    return cur.rowcount


def backfill(cur, force_replace, limit, dry_run):
    top = f"TOP ({int(limit)}) " if limit else ""
    cur.execute(
        f"""
        SELECT {top}
            m.MonsterId,
            m.RawText,
            m.RawMD,
            s.Speed,
            CASE WHEN EXISTS (
                SELECT 1 FROM pf2.MonsterAttack a WHERE a.MonsterId = m.MonsterId
            ) THEN 1 ELSE 0 END,
            CASE WHEN EXISTS (
                SELECT 1 FROM pf2.MonsterAbility b WHERE b.MonsterId = m.MonsterId
            ) THEN 1 ELSE 0 END
        FROM pf2.Monster m
        LEFT JOIN pf2.MonsterStats s ON s.MonsterId = m.MonsterId
        WHERE
            ? = 1
            OR s.MonsterId IS NULL
            OR s.Speed IS NULL
            OR NOT EXISTS (
                SELECT 1 FROM pf2.MonsterAttack a WHERE a.MonsterId = m.MonsterId
            )
            OR NOT EXISTS (
                SELECT 1 FROM pf2.MonsterAbility b WHERE b.MonsterId = m.MonsterId
            )
        ORDER BY m.MonsterId
        """,
        1 if force_replace else 0,
    )
    rows = cur.fetchall()
    stats = {
        "scanned": 0,
        "speed": 0,
        "attacks": 0,
        "abilities": 0,
        "unchanged": 0,
    }

    for index, (monster_id, raw_text, raw_md, speed, has_attacks, has_abilities) in enumerate(
        rows, start=1
    ):
        stats["scanned"] += 1
        text = statblock_text(raw_text, raw_md)
        wrote = False

        if text and (force_replace or not speed):
            parsed_speed = parse_speed(text)

            if parsed_speed:
                cur.execute("SELECT 1 FROM pf2.MonsterStats WHERE MonsterId = ?", monster_id)

                if cur.fetchone():
                    cur.execute(
                        "UPDATE pf2.MonsterStats SET Speed = ? WHERE MonsterId = ?",
                        parsed_speed,
                        monster_id,
                    )
                else:
                    cur.execute(
                        "INSERT INTO pf2.MonsterStats (MonsterId, Speed) VALUES (?, ?)",
                        monster_id,
                        parsed_speed,
                    )

                stats["speed"] += 1
                wrote = True

        if text and (force_replace or not has_attacks):
            attacks = parse_attacks(text)

            if attacks:
                replace_monster_attacks(cur, monster_id, attacks)
                stats["attacks"] += len(attacks)
                wrote = True

        if text and (force_replace or not has_abilities):
            abilities = parse_abilities(text)

            if abilities:
                replace_monster_abilities(cur, monster_id, abilities)
                stats["abilities"] += len(abilities)
                wrote = True

        if not wrote:
            stats["unchanged"] += 1

        if index % BATCH_SIZE == 0:
            if not dry_run:
                cur.commit()
            print(f"  monsters {index}/{len(rows)}...")

    if not dry_run:
        cur.commit()

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Update monsters missing speed, attacks, or abilities from RawText."
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force-replace", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cn = pyodbc.connect(CONN_STR)
    cn.autocommit = False
    cur = cn.cursor()

    try:
        removed = delete_flat_text_abilities(cur)
        print(f"Removed flat-text ability rows: {removed}")
        result = backfill(cur, args.force_replace, args.limit, args.dry_run)
        print(result)

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
