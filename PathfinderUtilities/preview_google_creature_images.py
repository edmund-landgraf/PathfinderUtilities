"""Preview the first Google Image result for creatures that have no art.

Does not write to the database. Picks a random sample of monsters with no
MonsterImage row, searches for "pathfinder 2e {name}", and prints the first
image URL.

Google blocks this server with a captcha, so the script uses the Custom Search
JSON API when GOOGLE_API_KEY and GOOGLE_CSE_ID are set. Without those keys it
opens the public image search and reports the block instead of guessing.
"""

from __future__ import annotations

import argparse
import os
import random

import requests

from import_demiplane_missing_creatures import connect, find_monster_image_table


SEARCH_URL = "https://www.google.com/search"
CSE_URL = "https://www.googleapis.com/customsearch/v1"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/149.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview Google image matches for creatures without art. Never writes."
    )
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--seed", type=int)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.seed is not None:
        random.seed(args.seed)

    with connect() as connection:
        cursor = connection.cursor()
        image_table = find_monster_image_table(cursor)
        if not image_table:
            raise RuntimeError("MonsterImage table was not found.")
        monsters = load_without_images(cursor, image_table)

    sample = random.sample(monsters, min(args.count, len(monsters)))
    rows = [lookup(monster) for monster in sample]
    print_table(rows)


def load_without_images(cursor, image_table: str) -> list[tuple[int, str]]:
    cursor.execute(
        f"""
        SELECT m.MonsterId, m.Name
        FROM pf2.Monster AS m
        WHERE NOT EXISTS (
            SELECT 1 FROM {image_table} AS mi WHERE mi.MonsterID = m.MonsterId
        )
        """
    )
    return [(int(row.MonsterId), str(row.Name)) for row in cursor.fetchall()]


def lookup(monster: tuple[int, str]) -> dict[str, str]:
    monster_id, name = monster
    query = f"pathfinder 2e {name}"
    search_url = f"{SEARCH_URL}?udm=2&hl=en&q={requests.utils.quote(query)}"
    image_url, note = first_image(query)
    return {
        "id": str(monster_id),
        "name": name,
        "query": query,
        "image": image_url or "",
        "note": note,
        "search": search_url,
    }


def first_image(query: str) -> tuple[str | None, str]:
    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    cse_id = os.environ.get("GOOGLE_CSE_ID", "").strip()
    if api_key and cse_id:
        response = requests.get(
            CSE_URL,
            params={
                "key": api_key,
                "cx": cse_id,
                "q": query,
                "searchType": "image",
                "num": 1,
                "safe": "active",
            },
            timeout=30,
        )
        if response.status_code != 200:
            return None, f"Custom Search HTTP {response.status_code}"
        items = response.json().get("items") or []
        if not items:
            return None, "Custom Search returned no image"
        return str(items[0].get("link") or ""), "Custom Search API"

    response = requests.get(
        SEARCH_URL,
        params={"udm": "2", "hl": "en", "q": query},
        headers=HEADERS,
        timeout=30,
    )
    text = response.text.lower()
    if "unusual traffic" in text or "/sorry/" in response.url or "enablejs" in text:
        return None, "Google blocked the request. Set GOOGLE_API_KEY and GOOGLE_CSE_ID."
    return None, f"No image parsed from HTTP {response.status_code}"


def print_table(rows: list[dict[str, str]]) -> None:
    headers = ["MonsterId", "Name", "First image", "Note"]
    values = [[row["id"], row["name"], row["image"] or row["search"], row["note"]] for row in rows]
    widths = [len(header) for header in headers]
    for value in values:
        for index, cell in enumerate(value):
            widths[index] = max(widths[index], min(len(cell), 88))

    def line(cells: list[str]) -> str:
        shown = [cell if len(cell) <= 88 else cell[:85] + "..." for cell in cells]
        return "| " + " | ".join(cell.ljust(widths[index]) for index, cell in enumerate(shown)) + " |"

    print(line(headers))
    print("| " + " | ".join("-" * width for width in widths) + " |")
    for value in values:
        print(line(value))
    print()
    print("No database rows were written.")


if __name__ == "__main__":
    main()
