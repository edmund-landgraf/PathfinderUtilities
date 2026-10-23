"""Backfill empty pf2.Monster fields from RawJson.

Fills empty RawMD, SourcePage, and SourceBookId. Rewrites Languages that
still contain AoN <%...%> tags, and rewrites Resistances and Weaknesses
that are stored as JSON into plain text. Existing RawMD is never cleared.
Preview unless --commit is passed.
"""

from __future__ import annotations

import argparse

from common import (
    connect,
    empty,
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
    return parser.parse_args()


def looks_like_json(value):
    if value is None:
        return False
    text = str(value).lstrip()
    return text[:1] in "[{"


def main():
    args = parse_args()
    cn = connect()
    cur = cn.cursor()
    cur.execute(
        """
        SELECT
            MonsterId,
            Name,
            RawMD,
            SourcePage,
            SourceBookId,
            Languages,
            Resistances,
            Weaknesses,
            RawJson
        FROM pf2.Monster
        WHERE RawJson IS NOT NULL
          AND (
                RawMD IS NULL OR LEN(LTRIM(RTRIM(RawMD))) = 0
             OR SourcePage IS NULL
             OR SourceBookId IS NULL
             OR Languages LIKE '%<%'
             OR LTRIM(Resistances) LIKE '[[]%'
             OR LTRIM(Resistances) LIKE '{%'
             OR LTRIM(Weaknesses) LIKE '[[]%'
             OR LTRIM(Weaknesses) LIKE '{%'
          )
        """
    )
    rows = cur.fetchall()
    cache = {}
    counts = {
        "rows scanned": len(rows),
        "RawMD": 0,
        "SourcePage": 0,
        "SourceBookId": 0,
        "Languages": 0,
        "Resistances": 0,
        "Weaknesses": 0,
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

        if src and row.SourceBookId is None:
            source_name = source_name_from_src(src)
            if source_name:
                counts["SourceBookId"] += 1
                if args.commit:
                    source_id = get_or_create_id(
                        cur, "pf2.SourceBook", "SourceBookId", "Name", source_name, cache
                    )
                    sets.append("SourceBookId = ?")
                    params.append(source_id)

        languages = row.Languages
        if languages and "<%" in str(languages):
            cleaned = strip_aon_tags(languages)
            if cleaned and cleaned != str(languages).strip():
                sets.append("Languages = ?")
                params.append(cleaned)
                counts["Languages"] += 1

        for column, current in (("Resistances", row.Resistances), ("Weaknesses", row.Weaknesses)):
            if not looks_like_json(current):
                continue
            formatted = format_defense(current)
            if formatted and formatted != str(current).strip():
                sets.append(f"{column} = ?")
                params.append(formatted)
                counts[column] += 1

        if sets and args.commit:
            params.append(row.MonsterId)
            cur.execute(
                f"""
                UPDATE pf2.Monster
                SET {", ".join(sets)}, UpdatedAt = SYSDATETIME()
                WHERE MonsterId = ?
                """,
                params,
            )

    print_plan(
        "Monster backfill (" + ("COMMIT" if args.commit else "PREVIEW") + ")",
        counts,
    )
    if args.commit:
        cn.commit()
        print("Committed.")
    else:
        cn.rollback()
        print("Preview only. Re-run with --commit to write.")
    cur.close()
    cn.close()


if __name__ == "__main__":
    main()
