# pip install requests

import json
import re
import requests

ELASTIC_URL = "https://elasticsearch.aonprd.com/aon/_search"

session = requests.Session()
session.headers.update({
    "User-Agent": "PathfinderUtil image debug / personal use",
    "Content-Type": "application/json"
})


def pretty(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False)[:6000])


def test_page(url):
    print("=" * 100)
    print("PAGE TEST:", url)

    r = session.get(url, timeout=30)
    html = r.text

    print("HTTP:", r.status_code)
    print("Final URL:", r.url)
    print("HTML length:", len(html))
    print("Contains /Images/Monsters:", "/Images/Monsters" in html)
    print("Contains .webp:", ".webp" in html)

    matches = re.findall(r'/Images/[^"\'>\s)]+?\.(?:webp|png|jpg|jpeg)', html, re.I)

    print("Image-like matches:")
    for m in matches[:50]:
        print(" ", m)

    print()


def test_elastic_by_id(elastic_id):
    print("=" * 100)
    print("ELASTIC TEST:", elastic_id)

    payload = {
        "size": 1,
        "query": {
            "term": {
                "id.keyword": elastic_id
            }
        }
    }

    r = session.post(ELASTIC_URL, json=payload, timeout=30)
    print("HTTP:", r.status_code)

    data = r.json()
    hits = data.get("hits", {}).get("hits", [])

    if not hits:
        print("NO ELASTIC HIT")
        return

    src = hits[0].get("_source", {})

    print("Keys:")
    print(sorted(src.keys()))

    print("\nFields that look image-related:")
    for k, v in src.items():
        lk = k.lower()
        sv = str(v).lower()

        if (
            "image" in lk
            or "img" in lk
            or "picture" in lk
            or "portrait" in lk
            or "thumbnail" in lk
            or ".webp" in sv
            or "/images/" in sv
        ):
            print(k, "=", v)

    print("\nAny /Images or .webp in full JSON?")
    raw = json.dumps(src, ensure_ascii=False)
    print("/Images/" in raw, ".webp" in raw)

    matches = re.findall(r'/Images/[^"\'>\s)]+?\.(?:webp|png|jpg|jpeg)', raw, re.I)

    for m in matches[:50]:
        print(" ", m)

    print("\nSample:")
    pretty(src)


def main():
    urls = [
        "https://2e.aonprd.com/Monsters.aspx?ID=3159",
        "https://2e.aonprd.com/Monsters.aspx?ID=719",
        "https://2e.aonprd.com/NPCs.aspx?ID=1966",
    ]

    for url in urls:
        test_page(url)

    elastic_ids = [
        "creature-3159",
        "creature-719",
        "creature-1966",
    ]

    for elastic_id in elastic_ids:
        test_elastic_by_id(elastic_id)

    input("Press any key to continue . . .")


if __name__ == "__main__":
    main()