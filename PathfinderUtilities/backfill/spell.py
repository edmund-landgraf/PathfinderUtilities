"""Backfill empty pf2.Spell fields from RawJson.

Adds pf2.Spell.Requirements when that column is missing, then fills it,
RawMD, SourcePage, Summary, and missing trait links. Existing values are
left in place. Preview unless --commit is passed. Adding the column is part
of the commit.
"""

from __future__ import annotations

import argparse

from common import (
    clean,
    column_exists,
    connect,
    empty,
    ensure_column,
    ensure_trait,
    first_existing,
    load_json,
    markdown_from_src,
    page_from_src,
    print_plan,
    split_values,
    strip_aon_tags,
    summary_from_src,
)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="Write updates.")
    return parser.parse_args()


def requirements_from_src(src):
    return strip_aon_tags(
        clean(first_existing(src, "requirements", "requirement", "requirement_markdown"))
    )


def main():
    args = parse_args()
    cn = connect()
    cur = cn.cursor()
    has_requirements = column_exists(cur, "Spell", "Requirements")
    if args.commit and not has_requirements:
        ensure_column(cur, "Spell", "Requirements", "NVARCHAR(MAX) NULL")
        cn.commit()
        has_requirements = True

    requirement_sql = "s.Requirements" if has_requirements else "CAST(NULL AS NVARCHAR(MAX))"
    requirement_filter = (
        """
             OR (
                    (s.Requirements IS NULL OR LEN(LTRIM(RTRIM(s.Requirements))) = 0)
                    AND (
                        NULLIF(LTRIM(RTRIM(JSON_VALUE(s.RawJson, '$.requirements'))), '') IS NOT NULL
                        OR NULLIF(LTRIM(RTRIM(JSON_VALUE(s.RawJson, '$.requirement'))), '') IS NOT NULL
                    )
                )
        """
        if has_requirements
        else """
             OR NULLIF(LTRIM(RTRIM(JSON_VALUE(s.RawJson, '$.requirements'))), '') IS NOT NULL
             OR NULLIF(LTRIM(RTRIM(JSON_VALUE(s.RawJson, '$.requirement'))), '') IS NOT NULL
        """
    )
    cur.execute(
        f"""
        SELECT
            s.SpellId,
            s.Name,
            s.RawMD,
            s.SourcePage,
            s.Summary,
            s.RawJson,
            {requirement_sql},
            CASE WHEN EXISTS (
                SELECT 1 FROM pf2.SpellTrait t WHERE t.SpellId = s.SpellId
            ) THEN 1 ELSE 0 END
        FROM pf2.Spell s
        WHERE s.RawJson IS NOT NULL
          AND (
                s.RawMD IS NULL OR LEN(LTRIM(RTRIM(s.RawMD))) = 0
             OR s.SourcePage IS NULL
             OR s.Summary IS NULL OR LEN(LTRIM(RTRIM(s.Summary))) = 0
             OR NOT EXISTS (
                    SELECT 1 FROM pf2.SpellTrait t WHERE t.SpellId = s.SpellId
                )
             {requirement_filter}
          )
        """
    )
    rows = cur.fetchall()
    cache = {}
    counts = {
        "rows scanned": len(rows),
        "Requirements column added": int(args.commit and not column_exists(cur, "Spell", "Requirements")),
        "Requirements": 0,
        "RawMD": 0,
        "SourcePage": 0,
        "Summary": 0,
        "traits": 0,
    }
    if not has_requirements and not args.commit:
        counts["Requirements column added"] = 1

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
        if has_requirements and empty(row[6]):
            requirements = requirements_from_src(src)
            if requirements:
                sets.append("Requirements = ?")
                params.append(requirements)
                counts["Requirements"] += 1
        elif not has_requirements and requirements_from_src(src):
            counts["Requirements"] += 1
        if sets and args.commit:
            params.append(row.SpellId)
            cur.execute(
                f"""
                UPDATE pf2.Spell
                SET {", ".join(sets)}, UpdatedAt = SYSDATETIME()
                WHERE SpellId = ?
                """,
                params,
            )
        if row[7] == 0:
            for trait in split_values(first_existing(src, "trait", "traits", "trait_raw")):
                counts["traits"] += 1
                if args.commit:
                    ensure_trait(cur, "Spell", "SpellId", row.SpellId, trait, cache)

    print_plan(
        "Spell backfill (" + ("COMMIT" if args.commit else "PREVIEW") + ")",
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
