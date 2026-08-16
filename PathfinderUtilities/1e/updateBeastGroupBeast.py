import re
import pyodbc

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;"
    "DATABASE=PathfinderUtil;"          # adjust if needed
    "Trusted_Connection=yes;"
)

def extract_group_from_url(url):
    pattern = r"/bestiary/monster-listings/([^/]+)(?:/|$)"
    match = re.search(pattern, url.lower())
    return match.group(1) if match else None

def get_or_create_group(cursor, group_name):
    if not group_name:
        return None

    cursor.execute("SELECT GroupId FROM pf1_BeastGroup WHERE GroupName = ?", group_name)
    row = cursor.fetchone()
    if row:
        return row[0]

    cursor.execute("""
        INSERT INTO pf1_BeastGroup (GroupName)
        OUTPUT INSERTED.GroupId
        VALUES (?)
    """, group_name)
    return cursor.fetchone()[0]

def main():
    print("=" * 80)
    print("Updating beast groups from URLs (fixed regex)")
    print("=" * 80)

    try:
        cn = pyodbc.connect(CONN_STR)
        cursor = cn.cursor()
        print("Connected to SQL Server.")
    except Exception as e:
        print(f"SQL connection failed: {e}")
        return

    cursor.execute("""
        SELECT BeastId, Name, Link
        FROM pf1_Beast
        WHERE GroupId IS NULL
    """)
    rows = cursor.fetchall()
    print(f"Found {len(rows)} beasts without a group.")

    if not rows:
        print("No updates needed.")
        cursor.close()
        cn.close()
        return

    updated = 0
    failed = 0
    group_cache = {}

    for beast_id, name, link in rows:
        group = extract_group_from_url(link)
        if not group:
            print(f"  Could not extract group from: {link}")
            failed += 1
            continue

        try:
            if group in group_cache:
                group_id = group_cache[group]
            else:
                group_id = get_or_create_group(cursor, group)
                group_cache[group] = group_id

            cursor.execute("""
                UPDATE pf1_Beast
                SET GroupId = ?
                WHERE BeastId = ?
            """, group_id, beast_id)

            updated += 1
            if updated % 100 == 0:
                print(f"  Processed {updated} beasts...")

        except Exception as e:
            print(f"  Failed to update {name} (ID {beast_id}): {e}")
            failed += 1
            cn.rollback()

    cn.commit()
    print(f"\nDone. Updated {updated} beasts, failed {failed}.")

    cursor.close()
    cn.close()

if __name__ == "__main__":
    main()