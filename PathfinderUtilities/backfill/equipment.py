"""Backfill empty pf2.Equipment fields from RawJson.

Fills RawMD, SourcePage, Summary, and missing trait links. Existing values
are left in place. Preview unless --commit is passed.
"""

from __future__ import annotations

import argparse

from common import (
    connect,
    empty,
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
    return parser.parse_args()


def main():
    args = parse_args()
    cn = connect()
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
    counts = {"rows scanned": len(rows), "RawMD": 0, "SourcePage": 0, "Summary": 0, "traits": 0}

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
        if row[6] == 0:
            for trait in split_values(first_existing(src, "trait", "traits", "trait_raw")):
                counts["traits"] += 1
                if args.commit:
                    ensure_trait(cur, "Equipment", "EquipmentId", row.EquipmentId, trait, cache)

    print_plan(
        "Equipment backfill (" + ("COMMIT" if args.commit else "PREVIEW") + ")",
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
