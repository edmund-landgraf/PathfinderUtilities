# pip install requests beautifulsoup4 lxml pyodbc

import argparse
import re
from datetime import date
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

import aon_update_common
from aon_update_common import (
    BASE_URL,
    SUPPORTED_SECTIONS,
    build_source_preview,
    dedupe_entries,
    duplicate_entries,
    fetch_source_by_id,
    fetch_sources_by_listed_date,
    import_preview,
    is_detail_entry_href,
    is_source_collection_href,
    is_supported_entry_href,
    expand_source_collection_entries,
    parse_source_page,
    print_preview,
    relative_aon_url,
)

ADVENTURE_CATEGORIES = ("Adventures",)

session = requests.Session()
session.headers.update({
    "User-Agent": "PathfinderUtil AoN adventure source-group importer / personal use",
})


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Import PF2 AoN Adventure content by listed date. If an Adventure source page "
            "contains no supported content but links to a Setting source group, follow the "
            "group page and ingest its content instead."
        )
    )
    parser.add_argument(
        "--from-date",
        default="2026-06-07",
        help="Inclusive AoN homepage listed-date start, YYYY-MM-DD. Default: 2026-06-07.",
    )
    parser.add_argument(
        "--to-date",
        default=date.today().isoformat(),
        help="Inclusive AoN homepage listed-date end, YYYY-MM-DD. Default: today.",
    )
    parser.add_argument(
        "--source-id",
        action="append",
        type=int,
        help="Specific AoN source ID to process. Can be supplied more than once.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only; do not perform SQL inserts.",
    )
    parser.add_argument(
        "--smoke-insert",
        action="store_true",
        help="Execute SQL inserts inside a transaction and roll them back.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Do not pause before importing each source.",
    )
    parser.add_argument(
        "--show-existing",
        action="store_true",
        help="Show existing rows as well as new rows in the preview.",
    )
    parser.add_argument(
        "--sections",
        help=(
            "Comma-separated sections to process. "
            "Allowed: Equipment, Feats, Spells, Monsters, NPCs."
        ),
    )
    parser.add_argument(
        "--trusted",
        action="store_true",
        help="Use Windows Authentication instead of the SQL login in aon_update_common.py.",
    )
    parser.add_argument(
        "--server",
        default="PTOLEMY",
        help="SQL Server name for --trusted. Default: PTOLEMY.",
    )
    parser.add_argument(
        "--database",
        default="PathfinderUtil",
        help="Database name for --trusted. Default: PathfinderUtil.",
    )
    return parser.parse_args()


def configure_database(args):
    if not args.trusted:
        return

    aon_update_common.CONN_STR = (
        "DRIVER={ODBC Driver 17 for SQL Server};"
        f"SERVER={args.server};"
        f"DATABASE={args.database};"
        "Trusted_Connection=yes;"
        "TrustServerCertificate=yes;"
    )
    print(
        f"SQL authentication: Windows Trusted Connection | "
        f"server={args.server} | database={args.database}"
    )


def discover_sources(args):
    if args.source_id:
        sources = [fetch_source_by_id(source_id) for source_id in args.source_id]
        return [
            source for source in sources
            if "Adventures" in (source.get("source_category") or "")
        ]

    return fetch_sources_by_listed_date(
        args.from_date,
        args.to_date,
        categories=ADVENTURE_CATEGORIES,
    )


def section_entry_count(source_page):
    return sum(
        len(source_page.get("sections", {}).get(section_name, {}).get("entries", []))
        for section_name in SUPPORTED_SECTIONS
    )


def source_has_supported_content(source_page):
    return section_entry_count(source_page) > 0


def source_group_url_from_value(value):
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    match = re.search(r"Setting\.aspx\?Group=(\d+)", text, re.I)
    if match:
        return urljoin(BASE_URL, f"Setting.aspx?Group={match.group(1)}")

    if text.isdigit():
        return urljoin(BASE_URL, f"Setting.aspx?Group={text}")

    match = re.search(r"\bGroup\s*[=:]\s*(\d+)\b", text, re.I)
    if match:
        return urljoin(BASE_URL, f"Setting.aspx?Group={match.group(1)}")

    return None


def find_source_group_url(source):
    # First use the Elasticsearch source_group field if it already contains a group id/url.
    group_url = source_group_url_from_value(source.get("source_group"))
    if group_url:
        return group_url

    # Otherwise inspect the actual source page for a Setting.aspx?Group=N link.
    response = session.get(source["url"], timeout=60)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")
    main = soup.select_one("#main") or soup

    for link in main.find_all("a", href=True):
        href = link.get("href") or ""
        match = re.search(r"Setting\.aspx\?Group=(\d+)", href, re.I)
        if match:
            return urljoin(BASE_URL, f"Setting.aspx?Group={match.group(1)}")

    return None


def canonical_detail_href(href):
    """
    Normalize AoN detail links so presentation-only query parameters such as
    NoRedirect=1 do not create duplicate records or break Elasticsearch hydration.
    """
    parsed = urlparse(urljoin(BASE_URL, href))
    query = parse_qs(parsed.query)

    # Preserve the AoN record identity and drop presentation-only parameters.
    canonical_query = {}
    if "ID" in query and query["ID"]:
        canonical_query["ID"] = query["ID"][0]

    normalized = parsed._replace(query=urlencode(canonical_query), fragment="")
    return urlunparse(normalized)


def entry_identity(entry):
    return (
        (entry.get("section") or "").lower(),
        (entry.get("relative_url") or "").lower(),
    )


def prefer_group_entry(current, candidate):
    """
    Prefer the canonical single-creature/NPC label over encounter-count labels such as
    'Ratfolk Ambushers (4)' when both point at the same AoN ID.
    """
    if current is None:
        return candidate

    current_name = (current.get("name") or "").strip()
    candidate_name = (candidate.get("name") or "").strip()

    current_counted = bool(re.search(r"\s*\(\d+\)\s*$", current_name))
    candidate_counted = bool(re.search(r"\s*\(\d+\)\s*$", candidate_name))

    if current_counted and not candidate_counted:
        return candidate
    if candidate_counted and not current_counted:
        return current

    # Otherwise keep the shorter/cleaner display label.
    if candidate_name and (not current_name or len(candidate_name) < len(current_name)):
        return candidate

    return current


def dedupe_group_entries(entries):
    by_identity = {}
    duplicates = []

    for entry in entries:
        key = entry_identity(entry)
        if key in by_identity:
            duplicates.append(entry)
            by_identity[key] = prefer_group_entry(by_identity[key], entry)
        else:
            by_identity[key] = entry

    return list(by_identity.values()), duplicates


def normalize_group_sections(source_page):
    """
    Canonicalize all supported detail URLs and dedupe by AoN identity (section + URL),
    not by display text. Setting pages often contain encounter labels like '(4)' that
    point to the exact same AoN record as the normal single-creature link.
    """
    for section_name in SUPPORTED_SECTIONS:
        section = source_page.get("sections", {}).get(section_name)
        if not section:
            continue

        normalized = []
        for entry in section.get("entries", []):
            canonical_href = canonical_detail_href(entry.get("url") or entry.get("relative_url") or "")
            normalized.append({
                **entry,
                "url": canonical_href,
                "relative_url": relative_aon_url(canonical_href),
                "section": section_name,
            })

        deduped, duplicates = dedupe_group_entries(normalized)
        section["entries"] = deduped
        section["duplicate_entries"] = list(section.get("duplicate_entries", [])) + duplicates
        section["expected_count"] = len(deduped)

    return source_page


def classify_section_from_href(href):
    path = urlparse(urljoin(BASE_URL, href)).path.lower()

    if path.endswith(("/equipment.aspx", "/armor.aspx", "/weapons.aspx", "/shields.aspx")):
        return "Equipment"
    if path.endswith("/feats.aspx"):
        return "Feats"
    if path.endswith("/spells.aspx"):
        return "Spells"
    if path.endswith("/monsters.aspx"):
        return "Monsters"
    if path.endswith("/npcs.aspx"):
        return "NPCs"

    return None


def parse_group_page(source, group_url):
    """
    Try the normal source-page parser first. If the Setting group page does not use
    the exact 'Section [count]' heading format, fall back to scanning supported AoN
    detail links in #main and grouping them by URL type.
    """
    group_source = {
        **source,
        "url": group_url,
        "group_fallback_for_url": source.get("url"),
        "group_fallback_for_name": source.get("name"),
    }

    parsed = normalize_group_sections(parse_source_page(group_source))

    if source_has_supported_content(parsed):
        return parsed

    response = session.get(group_url, timeout=60)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")
    main = soup.select_one("#main") or soup

    sections = {
        name: {
            "expected_count": 0,
            "source_link_count": 0,
            "entries": [],
            "duplicate_entries": [],
        }
        for name in SUPPORTED_SECTIONS
    }

    raw_entries = {name: [] for name in SUPPORTED_SECTIONS}

    for link in main.find_all("a", href=True):
        href = link.get("href") or ""

        if not is_supported_entry_href(href):
            continue

        section_name = classify_section_from_href(href)
        if not section_name:
            continue

        sections[section_name]["source_link_count"] += 1

        if is_source_collection_href(href):
            raw_entries[section_name].extend(
                expand_source_collection_entries(href, section_name)
            )
            continue

        if not is_detail_entry_href(href):
            continue

        canonical_href = canonical_detail_href(href)

        raw_entries[section_name].append({
            "name": link.get_text(" ", strip=True) or None,
            "url": canonical_href,
            "relative_url": relative_aon_url(canonical_href),
            "section": section_name,
        })

    for section_name in SUPPORTED_SECTIONS:
        entries = raw_entries[section_name]
        deduped, duplicates = dedupe_group_entries(entries)
        sections[section_name] = {
            "expected_count": len(deduped),
            "source_link_count": sections[section_name]["source_link_count"],
            "entries": deduped,
            "duplicate_entries": duplicates,
        }

    parsed["sections"] = sections
    parsed["url"] = group_url
    return parsed


def apply_section_filter(source_page, section_filter):
    if not section_filter:
        return

    wanted = {
        section.strip().lower()
        for section in section_filter.split(",")
        if section.strip()
    }
    allowed = {section.lower(): section for section in SUPPORTED_SECTIONS}
    unknown = sorted(section for section in wanted if section not in allowed)

    if unknown:
        raise RuntimeError(
            "Unknown section(s): "
            + ", ".join(unknown)
            + f". Allowed: {', '.join(SUPPORTED_SECTIONS)}"
        )

    selected = {allowed[section] for section in wanted}

    for section_name in SUPPORTED_SECTIONS:
        if section_name in selected:
            continue

        source_page["sections"][section_name] = {
            "expected_count": 0,
            "source_link_count": 0,
            "entries": [],
            "duplicate_entries": [],
        }


def print_source_list(sources, args):
    print("=" * 100)
    print("AoN Adventure / Source Group Update")
    print("=" * 100)

    if args.source_id:
        print("Mode: explicit Adventure source ID")
    else:
        print(f"Mode: AoN listed date {args.from_date} through {args.to_date}")
        print("Included category: Adventures")

    print()
    print(f"Adventure sources to inspect: {len(sources)}")

    for idx, source in enumerate(sources, start=1):
        listed_date = source.get("listed_date") or "(unknown listed date)"
        release_date = source.get("release_date") or "(unknown pub date)"
        print(
            f"{idx}. {source.get('name')} | listed {listed_date} | "
            f"pub {release_date} | {source.get('url')}"
        )


def process_source(source, args):
    print()
    print("=" * 100)
    print(f"Checking Adventure source: {source.get('name')}")
    print(f"Source page: {source.get('url')}")

    source_page = parse_source_page(source)

    if not source_has_supported_content(source_page):
        print("No supported Equipment/Feats/Spells/Monsters/NPCs found on source page.")

        group_url = find_source_group_url(source)

        if not group_url:
            print("No Setting source-group link found; skipping.")
            return

        print(f"Following source group: {group_url}")
        source_page = parse_group_page(source, group_url)

        if not source_has_supported_content(source_page):
            print("Source group also contains no supported content; skipping.")
            return
    else:
        print(
            f"Source page contains {section_entry_count(source_page)} supported entries; "
            "using the source page directly."
        )

    apply_section_filter(source_page, args.sections)

    preview = build_source_preview(source_page, include_hydration=True)
    print_preview(preview, show_all=args.show_existing)

    if args.dry_run and not args.smoke_insert:
        print("Dry run: no SQL inserts will be performed for this source.")
        return

    if not args.yes:
        action = "smoke insert" if args.smoke_insert else "import"
        input(
            f"\nPress Enter to {action} {source.get('name')}, "
            "or Ctrl+C to stop before SQL insert..."
        )

    results = import_preview(preview, dry_run=args.smoke_insert)

    print()
    if args.smoke_insert:
        print(f"Smoke insert complete for {source.get('name')}; transaction rolled back:")
    else:
        print(f"Import complete for {source.get('name')}:")

    for section_name, result in results.items():
        print(
            f"  {section_name}: imported={result['imported']} "
            f"skipped={result['skipped']} failed={result['failed']}"
        )


def main():
    args = parse_args()
    configure_database(args)

    sources = discover_sources(args)
    print_source_list(sources, args)

    if not sources:
        print("No eligible Adventure sources found.")
        return

    for source in sources:
        process_source(source, args)

    print()
    print("Done.")


if __name__ == "__main__":
    main()
