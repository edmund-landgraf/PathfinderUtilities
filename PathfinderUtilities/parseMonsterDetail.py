# pip install requests beautifulsoup4 lxml

import argparse
import json
import os
import re
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

BASE_URL = "https://2e.aonprd.com/"

CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;"
    "DATABASE=PathfinderUtil;"
    "Trusted_Connection=yes;"
)

session = requests.Session()
session.headers.update({
    "User-Agent": "PathfinderUtil monster detail parser / personal use"
})


def clean(value):
    if value is None:
        return None

    text = re.sub(r"\s+", " ", str(value)).strip()
    text = text.replace(" ;", ";").replace(" ,", ",").replace(" .", ".")
    text = text.replace("( ", "(").replace(" )", ")")

    return text or None


def to_int(value):
    if value is None:
        return None

    m = re.search(r"-?\d+", str(value))
    return int(m.group(0)) if m else None


def text_of(node):
    if node is None:
        return None

    if isinstance(node, NavigableString):
        return clean(node)

    return clean(node.get_text(" ", strip=True))


def action_cost(node):
    action = node.select_one(".action") if isinstance(node, Tag) else None

    if not action:
        return None

    return clean(action.get("aria-label") or action.get("title") or action.get_text(" ", strip=True))


def load_html(source):
    if re.match(r"^https?://", source, re.I):
        response = session.get(source, timeout=60)
        response.raise_for_status()
        return response.text, response.url

    with open(source, "r", encoding="utf-8") as handle:
        return handle.read(), source


def aon_id_from_url(url):
    if not url:
        return None

    qs = parse_qs(urlparse(url).query)

    if "ID" not in qs:
        return None

    return to_int(qs["ID"][0])


def first_int_after(label, text):
    m = re.search(rf"\b{re.escape(label)}\b\s*([+-]?\d+)", text or "", re.I)
    return int(m.group(1)) if m else None


def field_after(label, text):
    pattern = rf"\b{re.escape(label)}\b\s+(.+?)(?=(?:\b[A-Z][A-Za-z ]+\b\s+[+-]?\d+)|$)"
    m = re.search(pattern, text or "")
    return clean(m.group(1)) if m else None


def parse_source(line):
    if not line:
        return None, None

    m = re.search(r"^Source\s+(.+?)(?:\s+pg\.?\s*(\d+))?$", line, re.I)

    if not m:
        return None, None

    return clean(m.group(1)), to_int(m.group(2))


def split_semicolon_fields(line):
    fields = {}

    for part in re.split(r";\s*", line or ""):
        m = re.match(r"^([A-Z][A-Za-z ]+)\s+(.+)$", part)

        if m:
            fields[clean(m.group(1))] = clean(m.group(2))

    return fields


def labeled_semicolon_value(line, label):
    labels = "Immunities|Resistances|Weaknesses"
    pattern = rf"\b{re.escape(label)}\b\s+(.+?)(?=;\s*(?:{labels})\b|$)"
    m = re.search(pattern, line or "")

    return clean(m.group(1)) if m else None


def statblock_nodes(monster_page, stat_title):
    for node in stat_title.next_siblings:
        if isinstance(node, NavigableString):
            yield node
            continue

        if not isinstance(node, Tag):
            continue

        if "monster-family" in node.get("class", []):
            break

        if "creature-spellbook-wrapper" in node.get("class", []):
            break

        yield node


def statblock_lines(monster_page, stat_title):
    lines = [text_of(stat_title)]
    current = []

    def append_current():
        text = clean(" ".join(x for x in current if x))

        if text:
            lines.append(text)

        current.clear()

    for node in statblock_nodes(monster_page, stat_title):
        if isinstance(node, NavigableString):
            value = text_of(node)

            if value:
                current.append(value)

            continue

        name = node.name.lower()

        if name == "br":
            append_current()
            continue

        if name == "hr":
            append_current()
            lines.append("---")
            continue

        if "hanging-indent" in node.get("class", []):
            append_current()
            value = text_of(node)

            if value:
                lines.append(value)

            continue

        value = text_of(node)

        if value:
            current.append(value)

    append_current()

    return lines


def parse_header(monster_page, stat_title, source_url):
    title_link = stat_title.find("a")
    name = text_of(title_link)
    title_text = text_of(stat_title)
    level = None

    m = re.search(r"\bCreature\s+(-?\d+)\b", title_text or "", re.I)

    if m:
        level = int(m.group(1))

    size = None
    traits = []
    rarity = None
    alignment = None

    for node in statblock_nodes(monster_page, stat_title):
        if isinstance(node, Tag) and node.name.lower() == "br":
            break

        if not isinstance(node, Tag):
            continue

        classes = node.get("class", [])

        if "traitsize" in classes:
            size = text_of(node)
            continue

        if any(cls.startswith("trait") for cls in classes):
            value = text_of(node)

            if not value:
                continue

            low = value.lower()

            if low in ("common", "uncommon", "rare", "unique"):
                rarity = value
            elif low in (
                "lg", "ng", "cg", "ln", "n", "cn", "le", "ne", "ce",
                "lawful good", "neutral good", "chaotic good",
                "lawful neutral", "neutral", "chaotic neutral",
                "lawful evil", "neutral evil", "chaotic evil"
            ):
                alignment = value
            else:
                traits.append(value)

    return {
        "aon_id": aon_id_from_url(source_url),
        "aon_url": source_url if source_url and re.match(r"^https?://", source_url, re.I) else None,
        "name": name,
        "level": level,
        "size": size,
        "rarity": rarity,
        "alignment": alignment,
        "is_unique": rarity.lower() == "unique" if rarity else False,
        "traits": traits
    }


def parse_stats(lines):
    stats = {
        "perception": None,
        "senses": None,
        "languages": None,
        "skills": None,
        "items": None,
        "str_mod": None,
        "dex_mod": None,
        "con_mod": None,
        "int_mod": None,
        "wis_mod": None,
        "cha_mod": None,
        "ac": None,
        "fortitude": None,
        "reflex": None,
        "will": None,
        "hp": None,
        "immunities": None,
        "resistances": None,
        "weaknesses": None,
        "speed": None
    }

    for line in lines:
        if line.startswith("Perception "):
            stats["perception"] = first_int_after("Perception", line)
            stats["senses"] = clean(re.sub(r"^Perception\s+[+-]?\d+;?\s*", "", line))
        elif line.startswith("Languages "):
            stats["languages"] = clean(line.removeprefix("Languages "))
        elif line.startswith("Skills "):
            stats["skills"] = clean(line.removeprefix("Skills "))
        elif line.startswith("Items "):
            stats["items"] = clean(line.removeprefix("Items "))
        elif line.startswith("Str "):
            stats["str_mod"] = first_int_after("Str", line)
            stats["dex_mod"] = first_int_after("Dex", line)
            stats["con_mod"] = first_int_after("Con", line)
            stats["int_mod"] = first_int_after("Int", line)
            stats["wis_mod"] = first_int_after("Wis", line)
            stats["cha_mod"] = first_int_after("Cha", line)
        elif line.startswith("AC "):
            stats["ac"] = first_int_after("AC", line)
            stats["fortitude"] = first_int_after("Fort", line)
            stats["reflex"] = first_int_after("Ref", line)
            stats["will"] = first_int_after("Will", line)
        elif line.startswith("HP "):
            stats["hp"] = first_int_after("HP", line)
            stats["immunities"] = labeled_semicolon_value(line, "Immunities")
            stats["resistances"] = labeled_semicolon_value(line, "Resistances")
            stats["weaknesses"] = labeled_semicolon_value(line, "Weaknesses")
        elif line.startswith("Speed "):
            stats["speed"] = clean(line.removeprefix("Speed "))

    return stats


def parse_attack(line):
    attack_type = "Melee" if line.startswith("Melee ") else "Ranged" if line.startswith("Ranged ") else None

    if not attack_type:
        return None

    without_type = clean(line[len(attack_type):])
    action = None
    action_match = re.search(r"\[(one-action|two-actions|three-actions|free-action|reaction)\]", without_type or "")

    if action_match:
        action = action_match.group(1)
        without_type = clean(without_type.replace(action_match.group(0), " ", 1))

    damage = None

    if " Damage " in without_type:
        before_damage, damage = without_type.split(" Damage ", 1)
    else:
        before_damage = without_type

    before_damage = clean((before_damage or "").rstrip(" ,"))
    traits = None
    trait_match = re.search(r"\(([^()]*)\)\s*,?$", before_damage or "")

    if trait_match:
        traits = clean(trait_match.group(1))
        before_damage = clean(before_damage[:trait_match.start()].rstrip(" ,"))

    bonus = None
    name = before_damage
    bonus_match = re.search(r"(.+?)\s+([+-]\d+)(?:\s+\[\s*[+-]\d+[^\]]*\])?$", before_damage or "")

    if bonus_match:
        name = clean(bonus_match.group(1))
        bonus = int(bonus_match.group(2))

    effects = None

    if damage and " and " in damage:
        damage_text, effects = damage.split(" and ", 1)
        damage = clean(damage_text)
        effects = clean(effects)

    return {
        "attack_type": attack_type,
        "name": name,
        "action_cost": action,
        "attack_bonus": bonus,
        "traits": traits,
        "damage": clean(damage),
        "effects": effects,
        "raw_text": line
    }


def parse_ability(span, section):
    bold = span.find("b")
    full_text = text_of(span)

    if not bold or not full_text:
        return None

    name = text_of(bold)
    action = action_cost(span)

    if action:
        name = clean(re.sub(r"\[[^\]]+\]", "", name or ""))

    if name in ("Melee", "Ranged"):
        return None

    description = clean(full_text[len(name or ""):])

    if action:
        description = clean(re.sub(r"^\s*\[[^\]]+\]\s*", "", description or ""))

    traits = None
    trait_match = re.match(r"^\(([^()]*)\)\s*(.*)$", description or "")

    if trait_match:
        traits = clean(trait_match.group(1))
        description = clean(trait_match.group(2))

    return {
        "name": name,
        "action_cost": action,
        "ability_type": section,
        "traits": traits,
        "description": description,
        "raw_text": full_text
    }


def parse_spellcasting(line):
    if " Spells " not in line:
        return None

    m = re.match(r"^([A-Za-z]+)\s+(.+? Spells)\s+DC\s+(\d+)(?:,\s+attack\s+([+-]\d+))?;\s*(.+)$", line)

    if not m:
        return None

    spellcasting = {
        "tradition": m.group(1),
        "spellcasting_type": clean(m.group(2).replace(m.group(1), "", 1)),
        "dc": int(m.group(3)),
        "attack_bonus": to_int(m.group(4)),
        "notes": clean(m.group(5)),
        "spells": []
    }

    spell_text = m.group(5)
    rank_pattern = re.compile(r"\b((?:Cantrips)(?:\s+\(\d+(?:st|nd|rd|th)\))?|\d+(?:st|nd|rd|th))\b\s+")
    matches = list(rank_pattern.finditer(spell_text))

    for idx, rank_match in enumerate(matches):
        rank = rank_match.group(1)
        start = rank_match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(spell_text)
        names = []

        for part in re.split(r",\s*", spell_text[start:end].strip(" ;")):
            name = clean(re.sub(r"\([^)]*\)", "", part))

            if name:
                names.append(name)

        spellcasting["spells"].append({
            "rank": rank,
            "names": names
        })

    return spellcasting


def parse_hanging_blocks(monster_page, stat_title):
    attacks = []
    abilities = []
    section = "defense"
    hr_count = 0

    for node in statblock_nodes(monster_page, stat_title):
        if isinstance(node, Tag) and node.name.lower() == "hr":
            hr_count += 1
            section = "defense" if hr_count == 1 else "offense"
            continue

        if not isinstance(node, Tag) or "hanging-indent" not in node.get("class", []):
            continue

        line = text_of(node)
        attack = parse_attack(line)

        if attack:
            attacks.append(attack)
            continue

        ability = parse_ability(node, section)

        if ability:
            abilities.append(ability)

    return attacks, abilities


def parse_links(monster_page, stat_title):
    links = []

    for node in statblock_nodes(monster_page, stat_title):
        if not isinstance(node, Tag):
            continue

        for link in node.find_all("a"):
            href = link.get("href")
            label = text_of(link)

            if href and label:
                links.append({
                    "label": label,
                    "href": urljoin(BASE_URL, href)
                })

    return links


def parse_monster_detail(html, source_url=None):
    soup = BeautifulSoup(html, "lxml")
    monster_page = soup.select_one(".monster-page")

    if not monster_page:
        raise ValueError("Could not find .monster-page in AoN HTML.")

    stat_title = monster_page.select_one(".monster-statblock-name")

    if not stat_title:
        raise ValueError("Could not find .monster-statblock-name in AoN HTML.")

    lines = statblock_lines(monster_page, stat_title)
    monster = parse_header(monster_page, stat_title, source_url)

    source_book = None
    source_page = None

    for line in lines:
        if line.startswith("Source "):
            source_book, source_page = parse_source(line)
            break

    monster["source_book"] = source_book
    monster["source_page"] = source_page

    attacks, abilities = parse_hanging_blocks(monster_page, stat_title)
    spellcasting = [
        parsed for parsed in (parse_spellcasting(line) for line in lines)
        if parsed
    ]

    return {
        "monster": monster,
        "stats": parse_stats(lines),
        "attacks": attacks,
        "abilities": abilities,
        "spellcasting": spellcasting,
        "links": parse_links(monster_page, stat_title),
        "raw_text": "\n".join(lines),
        "raw_html": str(monster_page),
        "scrape_version": "aon-detail-monster-v1"
    }


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

    cur.execute(f"""
        INSERT INTO {table}
        (
            {name_col}
        )
        OUTPUT INSERTED.{id_col}
        VALUES
        (?)
    """, name)

    new_id = cur.fetchone()[0]
    cache[key] = new_id

    return new_id


def existing_monster_id(cur, monster):
    if monster.get("aon_id") is not None:
        cur.execute("""
            SELECT TOP 1 MonsterId
            FROM pf2.Monster
            WHERE AonId = ?
        """, monster["aon_id"])
        row = cur.fetchone()

        if row:
            return row[0]

    if monster.get("aon_url"):
        cur.execute("""
            SELECT TOP 1 MonsterId
            FROM pf2.Monster
            WHERE AonUrl = ?
        """, monster["aon_url"])
        row = cur.fetchone()

        if row:
            return row[0]

    return None


def insert_monster_shell(cur, parsed, cache):
    monster = parsed["monster"]

    source_id = get_or_create_lookup(
        cur,
        "pf2.SourceBook",
        "SourceBookId",
        "Name",
        monster.get("source_book"),
        cache
    )
    rarity_id = get_or_create_lookup(cur, "pf2.Rarity", "RarityId", "Name", monster.get("rarity"), cache)
    size_id = get_or_create_lookup(cur, "pf2.SizeCategory", "SizeId", "Name", monster.get("size"), cache)
    alignment_id = get_or_create_lookup(cur, "pf2.Alignment", "AlignmentId", "Name", monster.get("alignment"), cache)

    cur.execute("""
        INSERT INTO pf2.Monster
        (
            AonId,
            AonUrl,
            Name,
            Level,
            RarityId,
            SizeId,
            AlignmentId,
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
            ScrapeVersion
        )
        OUTPUT INSERTED.MonsterId
        VALUES
        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
         SYSDATETIME(), SYSDATETIME(), SYSDATETIME(), ?)
    """,
        monster.get("aon_id"),
        monster.get("aon_url"),
        monster.get("name"),
        monster.get("level"),
        rarity_id,
        size_id,
        alignment_id,
        source_id,
        monster.get("source_page"),
        1 if monster.get("is_unique") else 0,
        1 if monster.get("is_npc") else 0,
        parsed.get("raw_html"),
        parsed.get("raw_text"),
        json.dumps(parsed, ensure_ascii=False),
        parsed.get("scrape_version")
    )

    return cur.fetchone()[0]


def update_monster_core(cur, monster_id, parsed, cache):
    monster = parsed["monster"]

    source_id = get_or_create_lookup(
        cur,
        "pf2.SourceBook",
        "SourceBookId",
        "Name",
        monster.get("source_book"),
        cache
    )
    rarity_id = get_or_create_lookup(cur, "pf2.Rarity", "RarityId", "Name", monster.get("rarity"), cache)
    size_id = get_or_create_lookup(cur, "pf2.SizeCategory", "SizeId", "Name", monster.get("size"), cache)
    alignment_id = get_or_create_lookup(cur, "pf2.Alignment", "AlignmentId", "Name", monster.get("alignment"), cache)

    cur.execute("""
        UPDATE pf2.Monster
        SET
            AonId = COALESCE(?, AonId),
            AonUrl = COALESCE(?, AonUrl),
            Name = COALESCE(?, Name),
            Level = COALESCE(?, Level),
            RarityId = COALESCE(?, RarityId),
            SizeId = COALESCE(?, SizeId),
            AlignmentId = COALESCE(?, AlignmentId),
            SourceBookId = COALESCE(?, SourceBookId),
            SourcePage = COALESCE(?, SourcePage),
            IsUnique = ?,
            RawHtml = ?,
            RawText = ?,
            RawJson = ?,
            UpdatedAt = SYSDATETIME(),
            LastScraped = SYSDATETIME(),
            ScrapeVersion = ?
        WHERE MonsterId = ?
    """,
        monster.get("aon_id"),
        monster.get("aon_url"),
        monster.get("name"),
        monster.get("level"),
        rarity_id,
        size_id,
        alignment_id,
        source_id,
        monster.get("source_page"),
        1 if monster.get("is_unique") else 0,
        parsed.get("raw_html"),
        parsed.get("raw_text"),
        json.dumps(parsed, ensure_ascii=False),
        parsed.get("scrape_version"),
        monster_id
    )


def upsert_monster_stats(cur, monster_id, stats):
    cur.execute("SELECT 1 FROM pf2.MonsterStats WHERE MonsterId = ?", monster_id)

    params = (
        stats.get("perception"),
        stats.get("senses"),
        stats.get("languages"),
        stats.get("skills"),
        stats.get("items"),
        stats.get("str_mod"),
        stats.get("dex_mod"),
        stats.get("con_mod"),
        stats.get("int_mod"),
        stats.get("wis_mod"),
        stats.get("cha_mod"),
        stats.get("ac"),
        stats.get("fortitude"),
        stats.get("reflex"),
        stats.get("will"),
        stats.get("hp"),
        stats.get("immunities"),
        stats.get("resistances"),
        stats.get("weaknesses"),
        stats.get("speed")
    )

    if cur.fetchone():
        cur.execute("""
            UPDATE pf2.MonsterStats
            SET
                Perception = ?,
                Senses = ?,
                Languages = ?,
                Skills = ?,
                Items = ?,
                StrMod = ?,
                DexMod = ?,
                ConMod = ?,
                IntMod = ?,
                WisMod = ?,
                ChaMod = ?,
                AC = ?,
                Fortitude = ?,
                Reflex = ?,
                Will = ?,
                HP = ?,
                Immunities = ?,
                Resistances = ?,
                Weaknesses = ?,
                Speed = ?
            WHERE MonsterId = ?
        """, *params, monster_id)
    else:
        cur.execute("""
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
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, monster_id, *params)


def replace_monster_traits(cur, monster_id, traits, cache):
    cur.execute("DELETE FROM pf2.MonsterTrait WHERE MonsterId = ?", monster_id)

    for trait in traits:
        trait_id = get_or_create_lookup(cur, "pf2.Trait", "TraitId", "Name", trait, cache)

        if trait_id:
            cur.execute("""
                INSERT INTO pf2.MonsterTrait
                (
                    MonsterId,
                    TraitId
                )
                VALUES
                (?, ?)
            """, monster_id, trait_id)


def replace_monster_attacks(cur, monster_id, attacks):
    cur.execute("DELETE FROM pf2.MonsterAttack WHERE MonsterId = ?", monster_id)

    for attack in attacks:
        cur.execute("""
            INSERT INTO pf2.MonsterAttack
            (
                MonsterId,
                AttackType,
                Name,
                ActionCost,
                AttackBonus,
                Traits,
                Damage,
                Effects
            )
            VALUES
            (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            monster_id,
            attack.get("attack_type"),
            attack.get("name"),
            attack.get("action_cost"),
            attack.get("attack_bonus"),
            attack.get("traits"),
            attack.get("damage"),
            attack.get("effects")
        )


def replace_monster_abilities(cur, monster_id, abilities):
    cur.execute("DELETE FROM pf2.MonsterAbility WHERE MonsterId = ?", monster_id)

    for ability in abilities:
        cur.execute("""
            INSERT INTO pf2.MonsterAbility
            (
                MonsterId,
                Name,
                ActionCost,
                AbilityType,
                Traits,
                Description
            )
            VALUES
            (?, ?, ?, ?, ?, ?)
        """,
            monster_id,
            ability.get("name"),
            ability.get("action_cost"),
            ability.get("ability_type"),
            ability.get("traits"),
            ability.get("description")
        )


def spell_rank_to_int(rank):
    if not rank:
        return None

    if str(rank).lower().startswith("cantrip"):
        return 0

    return to_int(rank)


def replace_monster_spellcasting(cur, monster_id, spellcasting_blocks):
    cur.execute("""
        SELECT SpellcastingId
        FROM pf2.MonsterSpellcasting
        WHERE MonsterId = ?
    """, monster_id)
    spellcasting_ids = [row[0] for row in cur.fetchall()]

    for spellcasting_id in spellcasting_ids:
        cur.execute("DELETE FROM pf2.MonsterSpell WHERE SpellcastingId = ?", spellcasting_id)

    cur.execute("DELETE FROM pf2.MonsterSpellcasting WHERE MonsterId = ?", monster_id)

    for block in spellcasting_blocks:
        cur.execute("""
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
            block.get("notes")
        )
        spellcasting_id = cur.fetchone()[0]

        for rank in block.get("spells", []):
            for spell_name in rank.get("names", []):
                cur.execute("""
                    INSERT INTO pf2.MonsterSpell
                    (
                        SpellcastingId,
                        SpellLevel,
                        SpellName,
                        Uses,
                        Notes
                    )
                    VALUES
                    (?, ?, ?, NULL, NULL)
                """,
                    spellcasting_id,
                    spell_rank_to_int(rank.get("rank")),
                    spell_name
                )


def import_parsed_monster(parsed, create_missing=False, dry_run=False):
    import pyodbc

    cn = pyodbc.connect(CONN_STR)
    cn.autocommit = False
    cur = cn.cursor()
    cache = {}

    try:
        monster_id = existing_monster_id(cur, parsed["monster"])

        if not monster_id:
            if not create_missing:
                raise RuntimeError(
                    "Monster does not already exist in pf2.Monster. "
                    "Pass --create-missing to insert a new shell row."
                )

            monster_id = insert_monster_shell(cur, parsed, cache)

        else:
            update_monster_core(cur, monster_id, parsed, cache)

        upsert_monster_stats(cur, monster_id, parsed["stats"])
        replace_monster_traits(cur, monster_id, parsed["monster"].get("traits", []), cache)
        replace_monster_attacks(cur, monster_id, parsed.get("attacks", []))
        replace_monster_abilities(cur, monster_id, parsed.get("abilities", []))
        replace_monster_spellcasting(cur, monster_id, parsed.get("spellcasting", []))

        if dry_run:
            cn.rollback()
        else:
            cn.commit()

        return monster_id

    except Exception:
        cn.rollback()
        raise

    finally:
        cur.close()
        cn.close()


def main():
    parser = argparse.ArgumentParser(description="Parse an AoN PF2e monster detail page into JSON.")
    parser.add_argument(
        "source",
        nargs="?",
        default="https://2e.aonprd.com/Monsters.aspx?ID=2939",
        help="AoN monster URL or local HTML file."
    )
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON.")
    parser.add_argument("--import-sql", action="store_true", help="Import parsed detail into PathfinderUtil.")
    parser.add_argument("--create-missing", action="store_true", help="Create a pf2.Monster row if none exists.")
    parser.add_argument("--dry-run", action="store_true", help="Run SQL import and roll it back.")
    args = parser.parse_args()

    html, final_url = load_html(args.source)
    source_url = final_url if re.match(r"^https?://", final_url, re.I) else None
    parsed = parse_monster_detail(html, source_url=source_url)

    if not source_url and os.path.exists(args.source):
        parsed["monster"]["source_file"] = os.path.abspath(args.source)

    if args.import_sql:
        monster_id = import_parsed_monster(
            parsed,
            create_missing=args.create_missing,
            dry_run=args.dry_run
        )
        action = "Validated" if args.dry_run else "Imported"
        print(f"{action} {parsed['monster']['name']} as MonsterId {monster_id}.")
    else:
        print(json.dumps(parsed, ensure_ascii=False, indent=None if args.compact else 2))


if __name__ == "__main__":
    main()
