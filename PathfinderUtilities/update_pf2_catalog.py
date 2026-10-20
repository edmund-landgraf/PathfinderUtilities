"""Shared AoN Elasticsearch updater for PF2 equipment, feats, and spells.

Preview by default. Pass --commit to insert missing rows and refresh existing ones.
"""
from __future__ import annotations

import argparse
import time

import pyodbc

import pullEquipment
import pullFeats
import pullSpells
from mssql_conn import sql_auth

CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;"
    "DATABASE=PathfinderUtil;"
    + sql_auth()
    + "TrustServerCertificate=yes;"
)

CATALOGS = {
    "equipment": {
        "label": "Equipment",
        "fetch": pullEquipment.fetch_all_equipment,
        "name": lambda src: pullEquipment.clean(
            pullEquipment.first_existing(src, "name", "title")
        ),
        "url": pullEquipment.aon_url_from_src,
        "existing_id": lambda cur, src, cache: pullEquipment.existing_equipment_id(
            cur,
            pullEquipment.aon_key_from_src(src),
            pullEquipment.aon_url_from_src(src),
            pullEquipment.clean(pullEquipment.first_existing(src, "name", "title")),
            cache,
        ),
        "upsert": pullEquipment.insert_equipment_from_elastic,
        "log": pullEquipment.write_import_log,
        "id_name": "EquipmentId",
    },
    "feat": {
        "label": "Feats",
        "fetch": pullFeats.fetch_all_feats,
        "name": lambda src: pullFeats.clean(
            pullFeats.first_existing(src, "name", "feat", "title")
        ),
        "url": pullFeats.aon_url_from_src,
        "existing_id": lambda cur, src, cache: pullFeats.existing_feat_id(
            cur,
            pullFeats.stored_feat_aon_id(src, pullFeats.aon_url_from_src(src)),
            pullFeats.aon_url_from_src(src),
            pullFeats.clean(pullFeats.first_existing(src, "name", "feat", "title")),
            cache,
        ),
        "upsert": pullFeats.insert_feat_from_elastic,
        "log": pullFeats.write_import_log,
        "id_name": "FeatId",
    },
    "spell": {
        "label": "Spells",
        "fetch": pullSpells.fetch_all_spells,
        "name": lambda src: pullSpells.clean(
            pullSpells.first_existing(src, "name", "title")
        ),
        "url": pullSpells.aon_url_from_src,
        "existing_id": lambda cur, src, cache: pullSpells.existing_spell_id(
            cur,
            pullSpells.stored_spell_aon_id(src, pullSpells.aon_url_from_src(src)),
            pullSpells.aon_url_from_src(src),
            pullSpells.clean(pullSpells.first_existing(src, "name", "title")),
            cache,
        ),
        "upsert": pullSpells.insert_spell_from_elastic,
        "log": pullSpells.write_import_log,
        "id_name": "SpellId",
    },
}


def parse_args(kind: str):
    spec = CATALOGS[kind]
    parser = argparse.ArgumentParser(
        description=(
            f"Insert missing PF2 {spec['label'].lower()} from AoN Elasticsearch "
            "and refresh rows that already exist."
        )
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Write inserts and updates. Default is preview only.",
    )
    parser.add_argument(
        "--new-only",
        action="store_true",
        help="Insert missing rows only; do not refresh existing rows.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Process only the first N AoN records (after fetch/sort).",
    )
    parser.add_argument(
        "--show-existing",
        type=int,
        default=20,
        help="How many existing names to print in preview. Default: 20.",
    )
    return parser.parse_args()


def main(kind: str):
    spec = CATALOGS[kind]
    args = parse_args(kind)

    print("=" * 100)
    print(f"PF2 AoN {spec['label']} update")
    print("Source: https://elasticsearch.aonprd.com/aon/_search")
    print("Mode:", "COMMIT" if args.commit else "PREVIEW")
    if args.new_only:
        print("Scope: new rows only")
    else:
        print("Scope: insert missing and refresh existing")
    print("=" * 100)

    records = spec["fetch"]()
    if args.limit is not None:
        records = records[: args.limit]

    print(f"\nFetched {len(records)} {spec['label'].lower()} records.")
    if not records:
        return

    cn = pyodbc.connect(CONN_STR)
    cn.autocommit = False
    cur = cn.cursor()
    cache = {}

    new_rows = []
    existing_rows = []
    unnamed = 0

    for src in records:
        name = spec["name"](src)
        url = spec["url"](src)
        if not name:
            unnamed += 1
            continue
        existing_id = spec["existing_id"](cur, src, cache)
        item = (name, url, src, existing_id)
        if existing_id:
            existing_rows.append(item)
        else:
            new_rows.append(item)

    print(f"Local matches: {len(existing_rows)}")
    print(f"Missing locally: {len(new_rows)}")
    if unnamed:
        print(f"Skipped unnamed Elasticsearch hits: {unnamed}")

    print("\nNew records:")
    if not new_rows:
        print("  (none)")
    else:
        for name, url, _src, _existing in new_rows[:50]:
            print(f"  {name}  {url or '(no url)'}")
        if len(new_rows) > 50:
            print(f"  ... {len(new_rows) - 50} more")

    if existing_rows:
        print(f"\nExisting records (first {args.show_existing}):")
        for name, url, _src, existing_id in existing_rows[: args.show_existing]:
            print(f"  [{spec['id_name']}={existing_id}] {name}  {url or '(no url)'}")

    if not args.commit:
        print("\nPreview only. Re-run with --commit to write.")
        cur.close()
        cn.close()
        return

    work = list(new_rows)
    if not args.new_only:
        work.extend(existing_rows)

    inserted = 0
    updated = 0
    failed = 0

    for idx, (name, url, src, existing_id) in enumerate(work, start=1):
        update_existing = existing_id is not None and not args.new_only
        try:
            row_id = spec["upsert"](cur, src, cache, update_existing=update_existing)
            action = "Updated" if existing_id else "Inserted"
            spec["log"](cur, url, True, f"{action} {name} as {spec['id_name']} {row_id}", cache)
            cn.commit()
            if existing_id:
                updated += 1
            else:
                inserted += 1
            if (inserted + updated) % 100 == 0:
                print(f"Wrote {inserted + updated}/{len(work)}...")
        except Exception as ex:
            failed += 1
            cn.rollback()
            cur = cn.cursor()
            cache = {}
            print(f"FAILED [{idx}] {name}: {ex}")
            print(f"AoN URL: {url or '(unknown)'}")
            try:
                spec["log"](cur, url, False, str(ex), cache)
                cn.commit()
            except Exception:
                cn.rollback()
                cur = cn.cursor()
                cache = {}
        time.sleep(0.02)

    cur.close()
    cn.close()
    print(f"\nDone. Inserted={inserted}, Updated={updated}, Failed={failed}")
