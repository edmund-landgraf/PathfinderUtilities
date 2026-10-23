"""Backfill empty pf2.Equipment fields from RawJson.

Fills RawMD, SourcePage, Summary, and missing trait links. Existing values
are left in place. Preview unless --commit is passed.
"""

from __future__ import annotations

import argparse

from common import (
    add_sql_login_arg,
    connect,
    empty,
    finish_writes,
    note_write,
    ensure_trait,
    first_existing,
    load_json,
    markdown_from_src,
    page_from_src,
    print_plan,
    split_values,
    summary_from_src,
)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="Write updates.")
    add_sql_login_arg(parser)
    return parser.parse_args()


def main():
    args = parse_args()
    cn = connect(sql_login=args.sql_login)
    cur = cn.cursor()
    cur.execute(
        """
        SELECT
            e.EquipmentId,
            e.Name,
            e.RawMD,
            e.SourcePage,
            e.Summary,
            e.RawJson,
            CASE WHEN EXISTS (
                SELECT 1 FROM pf2.EquipmentTrait t WHERE t.EquipmentId = e.EquipmentId
            ) THEN 1 ELSE 0 END
        FROM pf2.Equipment e
        WHERE e.RawJson IS NOT NULL
          AND (
                e.RawMD IS NULL OR LEN(LTRIM(RTRIM(e.RawMD))) = 0
             OR e.SourcePage IS NULL
             OR e.Summary IS NULL OR LEN(LTRIM(RTRIM(e.Summary))) = 0
             OR NOT EXISTS (
                    SELECT 1 FROM pf2.EquipmentTrait t WHERE t.EquipmentId = e.EquipmentId
                )
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
        "Summary": 0,
        "traits": 0,
        "rows scanned": len(rows),
    }

    for row in rows:
        src = load_json(row.RawJson)
        if not src:
            continue
        sets = []
        params = []
        if empty(row.RawMD):
            markdown = markdown_from_src(src)
            if markdown:
                sets.append("RawMD = ?")
                params.append(markdown)
                counts["RawMD"] += 1
        if row.SourcePage is None:
            page = page_from_src(src)
            if page is not None:
                sets.append("SourcePage = ?")
                params.append(page)
                counts["SourcePage"] += 1
        if empty(row.Summary):
            summary = summary_from_src(src)
            if summary:
                sets.append("Summary = ?")
                params.append(summary)
                counts["Summary"] += 1
        trait_names = []
        if row[6] == 0:
            trait_names = split_values(first_existing(src, "trait", "traits", "trait_raw"))
        if sets or trait_names:
            counts["rows to update"] += 1
        if sets and args.commit:
            params.append(row.EquipmentId)
            cur.execute(
                f"""
                UPDATE pf2.Equipment
                SET {", ".join(sets)}, UpdatedAt = SYSDATETIME()
                WHERE EquipmentId = ?
                """,
                params,
            )
        wrote = False
        for trait in trait_names:
            counts["traits"] += 1
            if args.commit:
                ensure_trait(cur, "Equipment", "EquipmentId", row.EquipmentId, trait, cache)
                wrote = True
        if args.commit and (sets or wrote):
            pending = note_write(cn, pending, True)

    print_plan(
        "Equipment backfill (" + ("COMMIT" if args.commit else "PREVIEW") + ")",
        counts,
    )
    finish_writes(cn, pending, args.commit)
    cur.close()
    cn.close()


if __name__ == "__main__":
    main()
