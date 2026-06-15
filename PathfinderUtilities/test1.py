# pip install requests

import re
import requests

session = requests.Session()
session.headers.update({
    "User-Agent": "PathfinderUtil image test / personal use"
})


def fetch_image_url(aon_url):
    if not aon_url:
        return None

    print("=" * 100)
    print("Fetching:", aon_url)

    r = session.get(aon_url, timeout=30)
    print("HTTP:", r.status_code)
    print("Final URL:", r.url)
    print("HTML length:", len(r.text))

    html = r.text

    all_webps = re.findall(
        r'["\'](/Images/[^"\']+?\.webp)["\']',
        html,
        re.IGNORECASE
    )

    print("All .webp matches:")
    for x in all_webps[:20]:
        print(" ", x)

    monster_matches = re.findall(
        r'["\'](/Images/Monsters/[^"\']+?\.webp)["\']',
        html,
        re.IGNORECASE
    )

    print("Monster .webp matches:")
    for x in monster_matches[:20]:
        print(" ", x)

    if monster_matches:
        return "https://2e.aonprd.com" + monster_matches[0]

    for path in all_webps:
        low = path.lower()

        if (
            "logo" not in low
            and "icon" not in low
            and "starfinder" not in low
            and "placeholder" not in low
        ):
            return "https://2e.aonprd.com" + path

    return None


def main():
    test_urls = [
        "https://2e.aonprd.com/Monsters.aspx?ID=343",
        "https://2e.aonprd.com/Monsters.aspx?ID=Draconic_Fumecrux",
        "https://2e.aonprd.com/Monsters.aspx?ID=719",
        "https://2e.aonprd.com/NPCs.aspx?ID=1966",
    ]

    for url in test_urls:
        image_url = fetch_image_url(url)
        print("SELECTED IMAGE:", image_url)
        print()

    input("Press any key to continue . . .")


if __name__ == "__main__":
    main()