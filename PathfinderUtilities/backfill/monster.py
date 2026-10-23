"""Backfill empty pf2.Monster fields from RawJson.

Fills empty RawMD, SourcePage, and SourceBookId. Rewrites Languages that
still contain AoN <%...%> tags, and rewrites Resistances and Weaknesses
that are stored as JSON into plain text. Existing RawMD is never cleared.
Preview unless --commit is passed.
"""

from __future__ import annotations

import argparse

from common import (
    add_sql_login_arg,
    connect,
    empty,
    finish_writes,
    note_write,
    first_existing,
    format_defense,
    get_or_create_id,
    load_json,
    markdown_from_src,
    page_from_src,
    print_plan,
    source_name_from_src,
    strip_aon_tags,
)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="Write updates.")
    add_sql_login_arg(parser)
    return parser.parse_args()


def looks_like_json(value):
    if value is None:
        return False
    text = str(value).lstrip()
    return text[:1] in "[{"


def main():
    args = parse_args()
    cn = connect(sql_login=args.sql_login)
    cur = cn.cursor()
    cur.execute(
        """
        SELECT
            m.MonsterId,
            m.Name,
            m.RawMD,
            m.SourcePage,
            m.SourceBookId,
            s.Languages,
            s.Resistances,
            s.Weaknesses,
            m.RawJson
        FROM pf2.Monster m
        LEFT JOIN pf2.MonsterStats s ON s.MonsterId = m.MonsterId
        WHERE m.RawJson IS NOT NULL
          AND (
                m.RawMD IS NULL OR LEN(LTRIM(RTRIM(m.RawMD))) = 0
             OR m.SourcePage IS NULL
             OR m.SourceBookId IS NULL
             OR s.Languages LIKE '%<%'
             OR LTRIM(s.Resistances) LIKE '[[]%'
             OR LTRIM(s.Resistances) LIKE '{%'
             OR LTRIM(s.Weaknesses) LIKE '[[]%'
             OR LTRIM(s.Weaknesses) LIKE '{%'
          )
        """
    )
    rows = cur.fetchall()
    cache = {}
    pending = 0
    counts = {
        "rows to update": 0,
        "RawMD": 0,
        "SourcePage": 0,
        "SourceBookId": 0,
        "Languages": 0,
        "Resistances": 0,
        "Weaknesses": 0,
        "rows scanned": len(rows),
    }

    for row in rows:
        src = load_json(row.RawJson)
        sets = []
        params = []

        if src and empty(row.RawMD):
            markdown = markdown_from_src(src)
            if markdown:
                sets.append("RawMD = ?")
                params.append(markdown)
                counts["RawMD"] += 1

        if src and row.SourcePage is None:
            page = page_from_src(src)
            if page is not None:
                sets.append("SourcePage = ?")
                params.append(page)
                counts["SourcePage"] += 1

        source_pending = False
        if src and row.SourceBookId is None:
            source_name = source_name_from_src(src)
            if source_name:
                counts["SourceBookId"] += 1
                source_pending = True
                if args.commit:
                    source_id = get_or_create_id(
                        cur, "pf2.SourceBook", "SourceBookId", "Name", source_name, cache
                    )
                    sets.append("SourceBookId = ?")
                    params.append(source_id)

        stat_sets = []
        stat_params = []
        languages = row.Languages
        if languages and "<%" in str(languages):
            cleaned = strip_aon_tags(languages)
            if cleaned and cleaned != str(languages).strip():
                stat_sets.append("Languages = ?")
                stat_params.append(cleaned)
                counts["Languages"] += 1

        for column, current in (("Resistances", row.Resistances), ("Weaknesses", row.Weaknesses)):
            if not looks_like_json(current):
                continue
            formatted = format_defense(current)
            if formatted and formatted != str(current).strip():
                stat_sets.append(f"{column} = ?")
                stat_params.append(formatted)
                counts[column] += 1

        if sets or source_pending or stat_sets:
            counts["rows to update"] += 1
        if args.commit and sets:
            params.append(row.MonsterId)
            cur.execute(
                f"""
                UPDATE pf2.Monster
                SET {", ".join(sets)}, UpdatedAt = SYSDATETIME()
                WHERE MonsterId = ?
                """,
                params,
            )
        if args.commit and stat_sets:
            stat_params.append(row.MonsterId)
            cur.execute(
                f"""
                UPDATE pf2.MonsterStats
                SET {", ".join(stat_sets)}
                WHERE MonsterId = ?
                """,
                stat_params,
            )
        if args.commit and (sets or source_pending or stat_sets):
            pending = note_write(cn, pending, True)

    print_plan(
        "Monster backfill (" + ("COMMIT" if args.commit else "PREVIEW") + ")",
        counts,
    )
    finish_writes(cn, pending, args.commit)
    cur.close()
    cn.close()


if __name__ == "__main__":
    main()
