import os
import io
import sys
import time

import pyodbc
from PIL import Image, ImageOps, UnidentifiedImageError


# ============================================================
# CONFIGURATION
# ============================================================

SERVER = "localhost"
DATABASE = "PathfinderUtil"

SCHEMA = "pf2"
TABLE = "MonsterImage"

THUMBNAIL_SIZE = 96
WEBP_QUALITY = 78

# Read/process/commit this many thumbnails at a time.
BATCH_SIZE = 10

CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    f"SERVER={SERVER};"
    f"DATABASE={DATABASE};"
    f"UID={os.environ.get('MSSQL_USER', 'sa')};"
    f"PWD={os.environ.get('MSSQL_PASSWORD', '')};"
    "TrustServerCertificate=yes;"
)


# ============================================================
# SQL SERVER CONNECTION
# ============================================================

def get_connection():
    return pyodbc.connect(
        CONN_STR,
        autocommit=False
    )


# ============================================================
# CONNECTION / PREFLIGHT
# ============================================================

def verify_connection(conn):
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            @@SERVERNAME AS ServerName,
            SERVERPROPERTY('InstanceName') AS InstanceName,
            DB_NAME() AS DatabaseName,
            SUSER_SNAME() AS LoginName
    """)

    row = cursor.fetchone()

    print()
    print("SQL Server connection successful")
    print("--------------------------------")
    print(f"Server:   {row.ServerName}")
    print(f"Instance: {row.InstanceName or '(default)'}")
    print(f"Database: {row.DatabaseName}")
    print(f"Login:    {row.LoginName}")
    print()

    # Verify table exists.
    cursor.execute("""
        SELECT OBJECT_ID('pf2.MonsterImage', 'U')
    """)

    object_id = cursor.fetchone()[0]

    if object_id is None:
        cursor.close()
        raise RuntimeError(
            "Table PathfinderUtil.pf2.MonsterImage was not found."
        )

    # Legacy IMAGE cannot be used directly with COUNT(column).
    cursor.execute("""
        SELECT
            COUNT(*) AS TotalRows,
            SUM(
                CASE
                    WHEN MonsterImage IS NOT NULL THEN 1
                    ELSE 0
                END
            ) AS Images
        FROM pf2.MonsterImage
    """)

    row = cursor.fetchone()

    print("MonsterImage table found")
    print("--------------------------------")
    print(f"Rows:   {row.TotalRows:,}")
    print(f"Images: {row.Images or 0:,}")
    print()

    cursor.close()


# ============================================================
# ADD THUMBNAIL COLUMN IF NEEDED
# ============================================================

def ensure_thumbnail_column(conn):
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            TYPE_NAME(system_type_id)
        FROM sys.columns
        WHERE
            object_id = OBJECT_ID('pf2.MonsterImage')
            AND name = 'MonsterThumbnail'
    """)

    row = cursor.fetchone()

    if row:
        print(
            f"MonsterThumbnail already exists "
            f"(SQL type: {row[0]})."
        )
        print()
        cursor.close()
        return

    print("Adding MonsterThumbnail column...")

    cursor.execute("""
        ALTER TABLE pf2.MonsterImage
        ADD MonsterThumbnail image NULL
    """)

    conn.commit()
    cursor.close()

    print("MonsterThumbnail column added.")
    print()


# ============================================================
# CREATE ONE THUMBNAIL
# ============================================================

def create_thumbnail(image_bytes):
    with Image.open(io.BytesIO(image_bytes)) as img:
        # Honor EXIF orientation.
        img = ImageOps.exif_transpose(img)

        img.load()

        # Preserve transparency where present.
        if "A" in img.getbands():
            img = img.convert("RGBA")
        else:
            img = img.convert("RGB")

        # Fit image inside 96x96 while maintaining aspect ratio.
        img.thumbnail(
            (THUMBNAIL_SIZE, THUMBNAIL_SIZE),
            Image.Resampling.LANCZOS
        )

        output = io.BytesIO()

        img.save(
            output,
            format="WEBP",
            quality=WEBP_QUALITY,
            method=6
        )

        return output.getvalue()


# ============================================================
# GENERATE ALL MISSING THUMBNAILS
# ============================================================

def build_thumbnails(conn):
    count_cursor = conn.cursor()

    count_cursor.execute("""
        SELECT COUNT(*)
        FROM pf2.MonsterImage
        WHERE
            MonsterImage IS NOT NULL
            AND MonsterThumbnail IS NULL
    """)

    total_remaining = count_cursor.fetchone()[0]
    count_cursor.close()

    print(f"Total thumbnails still needed: {total_remaining:,}")
    print(f"Commit batch size: {BATCH_SIZE}")
    print()

    if total_remaining == 0:
        print("Nothing to process.")
        return

    print("Starting unattended thumbnail processing...")
    print("==========================================")
    print()

    processed = 0
    failed = 0
    attempted = 0

    original_bytes_total = 0
    thumbnail_bytes_total = 0

    # Keyset cursor lets us move forward without repeatedly loading
    # the already-processed rows.
    last_monster_id = -1

    started = time.time()

    while True:
        # ----------------------------------------------------
        # LOAD NEXT SMALL BATCH
        # ----------------------------------------------------
        read_cursor = conn.cursor()

        read_cursor.execute(f"""
            SELECT TOP ({BATCH_SIZE})
                MonsterID,
                MonsterImage
            FROM pf2.MonsterImage
            WHERE
                MonsterImage IS NOT NULL
                AND MonsterThumbnail IS NULL
                AND MonsterID > ?
            ORDER BY MonsterID
        """, last_monster_id)

        rows = read_cursor.fetchall()
        read_cursor.close()

        if not rows:
            break

        write_cursor = conn.cursor()

        # ----------------------------------------------------
        # PROCESS BATCH
        # ----------------------------------------------------
        for row in rows:
            monster_id = row.MonsterID

            # Always move forward for this run, even if one image fails.
            # A later run can retry failed/null rows.
            last_monster_id = monster_id
            attempted += 1

            try:
                original = bytes(row.MonsterImage)
                thumbnail = create_thumbnail(original)

                write_cursor.execute("""
                    UPDATE pf2.MonsterImage
                    SET MonsterThumbnail = ?
                    WHERE MonsterID = ?
                """,
                    pyodbc.Binary(thumbnail),
                    monster_id
                )

                processed += 1
                original_bytes_total += len(original)
                thumbnail_bytes_total += len(thumbnail)

            except (
                UnidentifiedImageError,
                OSError,
                ValueError
            ) as exc:
                failed += 1

                print(
                    f"FAILED MonsterID {monster_id}: {exc}",
                    file=sys.stderr
                )

            except Exception as exc:
                failed += 1

                print(
                    f"ERROR MonsterID {monster_id}: {exc}",
                    file=sys.stderr
                )

        # ----------------------------------------------------
        # COMMIT EVERY BATCH (10 ROWS, EXCEPT FINAL PARTIAL)
        # ----------------------------------------------------
        conn.commit()
        write_cursor.close()

        elapsed = time.time() - started

        rate = attempted / elapsed if elapsed > 0 else 0

        avg_thumb_kb = (
            thumbnail_bytes_total / processed / 1024
            if processed else 0
        )

        remaining_estimate = max(
            0,
            total_remaining - attempted
        )

        eta_minutes = (
            (remaining_estimate / rate) / 60
            if rate > 0 else 0
        )

        print(f"Processing {attempted:,} / {total_remaining:,}...")
        print(
            f"  committed"
            f" | successful: {processed:,}"
            f" | failed: {failed:,}"
            f" | remaining: {remaining_estimate:,}"
            f" | avg thumbnail: {avg_thumb_kb:.1f} KB"
            f" | ETA: {eta_minutes:.1f} min"
        )
        print()

    # ========================================================
    # FINISHED
    # ========================================================

    elapsed = time.time() - started

    print()
    print("================================")
    print("ALL PROCESSING COMPLETE")
    print("================================")
    print(f"Attempted:  {attempted:,}")
    print(f"Successful: {processed:,}")
    print(f"Failed:     {failed:,}")
    print(f"Elapsed:    {elapsed / 60:.1f} minutes")

    if processed:
        original_mb = original_bytes_total / 1024 / 1024
        thumbnail_mb = thumbnail_bytes_total / 1024 / 1024
        avg_kb = thumbnail_bytes_total / processed / 1024

        print()
        print(f"Original data read: {original_mb:,.1f} MB")
        print(f"Thumbnail data:     {thumbnail_mb:,.1f} MB")
        print(f"Average thumbnail:  {avg_kb:.1f} KB")

    # Verify actual database state instead of relying only on counters.
    verify_cursor = conn.cursor()

    verify_cursor.execute("""
        SELECT COUNT(*)
        FROM pf2.MonsterImage
        WHERE
            MonsterImage IS NOT NULL
            AND MonsterThumbnail IS NULL
    """)

    actually_remaining = verify_cursor.fetchone()[0]
    verify_cursor.close()

    print()
    print(
        f"Database rows still needing thumbnails: "
        f"{actually_remaining:,}"
    )

    if actually_remaining == 0:
        print("All monster thumbnails are populated.")
    elif failed > 0:
        print(
            "Some rows remain without thumbnails. "
            "Run the script again to retry them."
        )


# ============================================================
# MAIN
# ============================================================

def main():
    print()
    print("Pathfinder Monster Thumbnail Generator")
    print("======================================")
    print()
    print(f"Server:   {SERVER}")
    print(f"Database: {DATABASE}")
    print()

    print("Connecting to SQL Server...")

    conn = get_connection()

    try:
        # Verify connection and table before doing anything else.
        verify_connection(conn)

        # Existing column is detected and left alone.
        ensure_thumbnail_column(conn)

        # Process every remaining thumbnail unattended.
        build_thumbnails(conn)

    except KeyboardInterrupt:
        print()
        print()
        print("Interrupted by user.")

        try:
            conn.rollback()
        except Exception:
            pass

        print("Previously committed batches are preserved.")

    except Exception as exc:
        print()
        print("ERROR:")
        print(exc)

        try:
            conn.rollback()
        except Exception:
            pass

        raise

    finally:
        conn.close()

        print()
        print("SQL Server connection closed.")


if __name__ == "__main__":
    main()
