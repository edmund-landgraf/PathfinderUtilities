# AoN Source Update SQL Report Plan

## Goal

For every AoN source ingest, save a report file that documents the SQL tables touched and the row counts before and after the attempted import. In smoke mode, the report must also prove that the transaction was rolled back.

## Report Location

Create one report per source import:

```text
reports/aon-update/{yyyyMMdd-HHmmss}_{source-id}_{source-name}.md
reports/aon-update/{yyyyMMdd-HHmmss}_{source-id}_{source-name}.json
```

The Markdown file is for human review. The JSON file is for later comparison or automation.

## Report Header

Capture:

- Source name, AoN source id, AoN URL
- Release date and product line
- Run mode: `dry-run`, `smoke-insert`, or `commit`
- Started/finished timestamps
- Connection target: server and database, without credentials
- Git commit or working tree marker if available

## Table Scope

Only report tables that can be touched by the supported import types.

Equipment:

- `pf2.Equipment`
- `pf2.EquipmentSourceLink`
- `pf2.EquipmentTrait`
- `pf2.EquipmentImportLog`
- `pf2.EquipmentType`
- `pf2.Rarity`
- `pf2.SizeCategory`
- `pf2.SourceBook`

Feats:

- `pf2.Feat`
- `pf2.FeatSourceLink`
- `pf2.FeatTrait`
- `pf2.FeatImportLog`
- `pf2.Rarity`
- `pf2.SourceBook`

Spells:

- `pf2.Spell`
- `pf2.SpellSourceLink`
- `pf2.SpellTrait`
- `pf2.SpellTradition`
- `pf2.SpellImportLog`
- `pf2.Rarity`
- `pf2.SourceBook`

Monsters and NPCs:

- `pf2.Monster`
- `pf2.MonsterStats`
- `pf2.MonsterSourceLink`
- `pf2.MonsterTrait`
- `pf2.MonsterAbility`
- `pf2.MonsterAttack`
- `pf2.MonsterSpellCasting`
- `pf2.MonsterImportLog`
- `pf2.MonsterFamily`
- `pf2.Alignment`
- `pf2.Rarity`
- `pf2.SizeCategory`
- `pf2.SourceBook`

## Count Snapshots

For each table in scope, capture:

- `before_count`
- `after_attempt_count`
- `delta_attempt`
- `after_rollback_count`, smoke mode only
- `rollback_delta`, smoke mode only

Use `COUNT_BIG(*)` to avoid count overflow:

```sql
SELECT COUNT_BIG(*) AS RowCount
FROM pf2.Equipment;
```

For source-specific link tables, also capture rows for the current source when a `SourceBookId` can be resolved:

```sql
SELECT COUNT_BIG(*) AS SourceRows
FROM pf2.EquipmentSourceLink
WHERE SourceBookId = ?;
```

## Import Outcome Section

Reuse the existing preview/import summary:

- Expected AoN source count
- Source links found
- Unique entries after duplicate removal
- Existing rows before import
- New rows attempted
- Missing Elasticsearch payloads
- Imported/skipped/failed counts
- Duplicate source links, if any
- Failed entries with exception text

## Smoke Mode Semantics

For `--smoke-insert`, record counts in this order:

1. Open one SQL transaction.
2. Capture `before_count`.
3. Run all insert logic.
4. Capture `after_attempt_count` inside the same transaction.
5. Roll back.
6. Capture `after_rollback_count` using a fresh cursor/transaction.

The smoke report passes when:

- Failed insert count is `0`.
- `after_attempt_count >= before_count` for expected write tables.
- `after_rollback_count == before_count` for every reported table.

## Commit Mode Semantics

For a real import, record:

1. `before_count`
2. Import execution
3. `after_attempt_count` after commit

The commit report passes when:

- Failed insert count is `0`.
- `delta_attempt` matches the actual committed row changes.
- Existing rows and skipped rows explain any source entries that did not create primary rows.

## Impossible Magic Smoke Baseline

Current smoke test expectations for `Sources.aspx?ID=355`:

```text
Equipment: expected=314, new=314
Feats:     expected=374, unique=373, existing=9, new=364
Spells:    expected=301, new=301
Monsters:  expected=0
NPCs:      expected=0
```

Known source-page duplicate:

```text
Steady Spellcasting - Feats.aspx?ID=4602
```

## Implementation Steps

1. Add a report builder module, probably `aon_update_report.py`.
2. Add `--report-dir`, defaulting to `reports/aon-update`.
3. Add `capture_table_counts(cur, table_names, source_book_id=None)`.
4. Resolve the source book id before import using AoN source id, name, or URL.
5. Capture `before` before calling the import loop.
6. Capture `after_attempt` before commit or rollback.
7. In smoke mode, roll back and capture `after_rollback`.
8. Write Markdown and JSON reports at the end of each source.
9. Print the report paths in the console summary.

## Follow-Up Fix Before Relying On Equipment Deltas

The current smoke path reuses the older equipment importer. That importer can collapse some named equipment variants by shared `AonUrl`, even when the source page lists separate named entries. Before treating equipment row deltas as authoritative, replace the reused helper with a new equipment insert path that prefers `AonKey` and only uses `AonUrl` as a fallback when no key exists.
