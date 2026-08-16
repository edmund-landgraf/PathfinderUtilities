import re
import pyodbc

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;"
    "DATABASE=PathfinderUtil;"
    "Trusted_Connection=yes;"
)

def extract_group_from_url(url):
    """Extract the group from a monster detail URL."""
    pattern = r"/bestiary/monster-listings/([^/]+)(?:/|$)"
    match = re.search(pattern, url.lower())
    return match.group(1) if match else None

def main():
    print("=" * 80)
    print("Updating beast groups (Python version)")
    print("=" * 80)

    try:
        cn = pyodbc.connect(CONN_STR)
        cursor = cn.cursor()
        print("Connected to SQL Server.")
    except Exception as e:
        print(f"SQL connection failed: {e}")
        return

    # Get all beasts with NULL GroupId
    cursor.execute("SELECT BeastId, Name, Link FROM pf1_Beast WHERE GroupId IS NULL")
    rows = cursor.fetchall()
    print(f"Found {len(rows)} beasts without a group.")

    if not rows:
        print("No updates needed.")
        cursor.close()
        cn.close()
        return

    # Get all existing groups
    cursor.execute("SELECT GroupName, GroupId FROM pf1_BeastGroup")
    existing_groups = {row[0]: row[1] for row in cursor.fetchall()}
    print(f"Found {len(existing_groups)} existing groups.")

    updated = 0
    failed = 0

    for beast_id, name, link in rows:
        group = extract_group_from_url(link)
        if not group:
            print(f"  Could not extract group from: {link}")
            failed += 1
            continue

        group_id = existing_groups.get(group)
        if not group_id:
            print(f"  No matching group for: {group} (beast: {name})")
            failed += 1
            continue

        try:
            cursor.execute("UPDATE pf1_Beast SET GroupId = ? WHERE BeastId = ?", group_id, beast_id)
            updated += 1
            if updated % 100 == 0:
                print(f"  Processed {updated} beasts...")
        except Exception as e:
            print(f"  Failed to update {name} (ID {beast_id}): {e}")
            failed += 1

    cn.commit()
    print(f"\nDone. Updated {updated} beasts, failed {failed}.")

    # Verify
    cursor.execute("SELECT COUNT(*) FROM pf1_Beast WHERE GroupId IS NULL")
    still_null = cursor.fetchone()[0]
    print(f"Beasts still without a group: {still_null}")

    cursor.close()
    cn.close()

if __name__ == "__main__":
    main()