# pip install requests beautifulsoup4 lxml pyodbc

import argparse
from datetime import date

from aon_update_common import (
    INCLUDED_SOURCE_CATEGORIES,
    build_source_preview,
    fetch_source_by_id,
    fetch_sources_by_release_date,
    import_preview,
    parse_source_page,
    print_preview,
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Import new PF2 AoN content from source books released in a date range."
    )
    parser.add_argument(
        "--from-date",
        default="2026-06-07",
        help="Inclusive AoN source release-date start, YYYY-MM-DD. Default: 2026-06-07."
    )
    parser.add_argument(
        "--to-date",
        default=date.today().isoformat(),
        help="Inclusive AoN source release-date end, YYYY-MM-DD. Default: today."
    )
    parser.add_argument(
        "--source-id",
        action="append",
        type=int,
        help="Specific AoN source ID to process. Can be supplied more than once."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only; do not perform SQL inserts."
    )
    parser.add_argument(
        "--smoke-insert",
        action="store_true",
        help="Execute SQL inserts inside a transaction and roll them back."
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Do not pause before importing each source."
    )
    parser.add_argument(
        "--show-existing",
        action="store_true",
        help="Show existing rows as well as new rows in the preview."
    )
    return parser.parse_args()


def discover_sources(args):
    if args.source_id:
        return [fetch_source_by_id(source_id) for source_id in args.source_id]

    return fetch_sources_by_release_date(
        args.from_date,
        args.to_date,
        categories=INCLUDED_SOURCE_CATEGORIES,
    )


def print_source_list(sources, args):
    print("=" * 100)
    print("AoN Source Update")
    print("=" * 100)

    if args.source_id:
        print("Mode: explicit source ID")
    else:
        print(f"Mode: release date {args.from_date} through {args.to_date}")
        print(f"Included categories: {', '.join(INCLUDED_SOURCE_CATEGORIES)}")

    print()
    print(f"Sources to inspect: {len(sources)}")

    for idx, source in enumerate(sources, start=1):
        category = source.get("source_category") or "(unknown category)"
        release_date = source.get("release_date") or "(unknown date)"
        print(f"{idx}. {source.get('name')} | {release_date} | {category} | {source.get('url')}")


def process_source(source, args):
    source_page = parse_source_page(source)
    preview = build_source_preview(source_page, include_hydration=True)
    print_preview(preview, show_all=args.show_existing)

    if args.dry_run and not args.smoke_insert:
        print("Dry run: no SQL inserts will be performed for this source.")
        return

    if not args.yes:
        action = "smoke insert" if args.smoke_insert else "import"
        input(
            f"\nPress Enter to {action} {source_page.get('name')}, "
            "or Ctrl+C to stop before SQL insert..."
        )

    results = import_preview(preview, dry_run=args.smoke_insert)

    print()
    if args.smoke_insert:
        print(f"Smoke insert complete for {source_page.get('name')}; transaction rolled back:")
    else:
        print(f"Import complete for {source_page.get('name')}:")

    for section_name, result in results.items():
        print(
            f"  {section_name}: imported={result['imported']} "
            f"skipped={result['skipped']} failed={result['failed']}"
        )


def main():
    args = parse_args()
    sources = discover_sources(args)
    print_source_list(sources, args)

    if not sources:
        print("No eligible sources found.")
        return

    for source in sources:
        process_source(source, args)

    print()
    print("Done.")


if __name__ == "__main__":
    main()
