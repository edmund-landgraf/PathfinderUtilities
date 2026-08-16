import webbrowser
import pyodbc

CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;"
    "DATABASE=PathfinderUtil;"
    "Trusted_Connection=yes;"
)

QUERY = """
SELECT BeastId, Name, Link, CreatedAt, GroupId, html_block
FROM [PathfinderUtil].[dbo].[pf1_Beast]
WHERE html_block IS NULL
ORDER BY BeastId
"""


def main():
    cn = pyodbc.connect(CONN_STR)
    cn.autocommit = False
    cur = cn.cursor()

    cur.execute(QUERY)
    rows = cur.fetchall()

    print(f"Found {len(rows)} beasts with html_block NULL.")
    print("Browser opens current link. Paste correct URL, press Enter to skip, or type q to quit.\n")

    updated = 0
    skipped = 0

    for i, row in enumerate(rows, start=1):
        beast_id, name, bad_link, created_at, group_id, html_block = row

        print("=" * 90)
        print(f"[{i}/{len(rows)}] BeastId: {beast_id}")
        print(f"Name: {name}")
        print(f"Current Link: {bad_link}")

        if bad_link:
            webbrowser.open(bad_link)

        new_link = input("Correct Link: ").strip()

        if new_link.lower() == "q":
            print("Quitting.")
            break

        if not new_link:
            skipped += 1
            print("Skipped.")
            continue

        try:
            cur.execute("""
                UPDATE [PathfinderUtil].[dbo].[pf1_Beast]
                SET Link = ?
                WHERE BeastId = ?
            """, new_link, beast_id)

            cn.commit()
            updated += 1
            print(f"Updated BeastId={beast_id}")

        except Exception as e:
            cn.rollback()
            print(f"FAILED update BeastId={beast_id}: {e}")

    cur.close()
    cn.close()

    print("\nDone.")
    print(f"Updated: {updated}")
    print(f"Skipped: {skipped}")


if __name__ == "__main__":
    main()