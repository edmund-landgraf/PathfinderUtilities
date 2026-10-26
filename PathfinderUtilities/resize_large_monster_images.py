"""Shrink pf2.MonsterImage rows of 4 MB or more into the 2–4 MB range.

Aspect ratio is kept. Each image is re-encoded at high quality first
(JPEG 4:4:4, or WebP when the source has an alpha channel). Dimensions
are reduced with Lanczos only as far as needed to land at or under 4 MB,
preferring a result that is still at least 2 MB.

Dry-run by default. Pass --apply to copy each original onto D:\\ first,
then write MonsterImage and a matching 96px WebP MonsterThumbnail.
The database row is updated only after that file is on disk.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

import pyodbc
from PIL import Image, ImageOps, UnidentifiedImageError

from mssql_conn import sql_auth


SERVER = "localhost"
DATABASE = "PathfinderUtil"

MIB = 1024 * 1024
SOURCE_MIN_BYTES = 4 * MIB
TARGET_MIN_BYTES = 2 * MIB
TARGET_MAX_BYTES = 4 * MIB

# Highest visual quality first. 4:4:4 JPEG avoids a chroma downsample.
JPEG_QUALITIES = (97, 95, 92, 90, 85)
WEBP_QUALITIES = (95, 90, 85)
MIN_SCALE = 0.15
THUMBNAIL_SIZE = 96
THUMBNAIL_WEBP_QUALITY = 78
BACKUP_DIR = Path(r"D:\monster-image-backups\large")

CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    f"SERVER={SERVER};"
    f"DATABASE={DATABASE};"
    + sql_auth()
    + "TrustServerCertificate=yes;"
)


def get_connection():
    return pyodbc.connect(CONN_STR, autocommit=False)


def prepare_image(image_bytes: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    img.load()
    if "A" in img.getbands():
        return img.convert("RGBA")
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _resized(img: Image.Image, scale: float) -> Image.Image:
    if scale >= 0.999:
        return img
    width = max(1, round(img.width * scale))
    height = max(1, round(img.height * scale))
    return img.resize((width, height), Image.Resampling.LANCZOS)


def encode(img: Image.Image, scale: float, *, webp: bool, quality: int) -> bytes:
    resized = _resized(img, scale)
    buffer = io.BytesIO()
    if webp:
        resized.save(
            buffer,
            format="WEBP",
            quality=quality,
            method=6,
        )
    else:
        resized.save(
            buffer,
            format="JPEG",
            quality=quality,
            subsampling=0,
            optimize=True,
        )
    return buffer.getvalue()


def _qualities(webp: bool) -> tuple[int, ...]:
    return WEBP_QUALITIES if webp else JPEG_QUALITIES


def fit_to_range(img: Image.Image) -> tuple[bytes, float, int, str]:
    """Return bytes, scale, quality, and format inside the target band.

    Full resolution at the highest quality is kept when it already fits.
    Otherwise the largest scale whose file is still <= 4 MB is used.
    A result under 2 MB is kept only when every larger candidate exceeds 4 MB
    or the near-lossless full image is already under 2 MB.
    """
    webp = img.mode == "RGBA"
    fmt = "WEBP" if webp else "JPEG"
    qualities = _qualities(webp)

    full = encode(img, 1.0, webp=webp, quality=qualities[0])
    if TARGET_MIN_BYTES <= len(full) <= TARGET_MAX_BYTES:
        return full, 1.0, qualities[0], fmt
    if len(full) < TARGET_MIN_BYTES:
        return full, 1.0, qualities[0], fmt

    best: tuple[bytes, float, int] | None = None
    for quality in qualities:
        low = MIN_SCALE
        high = 1.0
        quality_best: tuple[bytes, float] | None = None
        for _ in range(12):
            scale = (low + high) / 2
            data = encode(img, scale, webp=webp, quality=quality)
            if len(data) > TARGET_MAX_BYTES:
                high = scale
                continue
            quality_best = (data, scale)
            if len(data) >= TARGET_MIN_BYTES:
                break
            low = scale
        if quality_best is None:
            continue
        data, scale = quality_best
        if best is None:
            best = (data, scale, quality)
            continue
        # Prefer a file inside the band, then higher quality, then larger scale.
        in_band = TARGET_MIN_BYTES <= len(data) <= TARGET_MAX_BYTES
        best_in_band = TARGET_MIN_BYTES <= len(best[0]) <= TARGET_MAX_BYTES
        if (in_band, quality, scale) > (best_in_band, best[2], best[1]):
            best = (data, scale, quality)

    if best is None:
        data = encode(img, MIN_SCALE, webp=webp, quality=qualities[-1])
        return data, MIN_SCALE, qualities[-1], fmt
    return best[0], best[1], best[2], fmt


def create_thumbnail(img: Image.Image) -> bytes:
    thumb = img.copy()
    thumb.thumbnail((THUMBNAIL_SIZE, THUMBNAIL_SIZE), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    thumb.save(buffer, format="WEBP", quality=THUMBNAIL_WEBP_QUALITY, method=6)
    return buffer.getvalue()


def list_large_images(conn) -> list[tuple]:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            mi.MonsterID,
            m.Name,
            DATALENGTH(mi.MonsterImage) AS ImageBytes
        FROM pf2.MonsterImage AS mi
        JOIN pf2.Monster AS m
            ON m.MonsterId = mi.MonsterID
        WHERE DATALENGTH(mi.MonsterImage) >= ?
        ORDER BY DATALENGTH(mi.MonsterImage) DESC
        """,
        SOURCE_MIN_BYTES,
    )
    rows = cursor.fetchall()
    cursor.close()
    return rows


def load_image(conn, monster_id: int) -> bytes:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT MonsterImage FROM pf2.MonsterImage WHERE MonsterID = ?",
        monster_id,
    )
    row = cursor.fetchone()
    cursor.close()
    if row is None or row[0] is None:
        raise LookupError(f"MonsterID {monster_id} has no image")
    return bytes(row[0])


def save_image(conn, monster_id: int, image_bytes: bytes, thumb_bytes: bytes) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE pf2.MonsterImage
        SET
            MonsterImage = ?,
            MonsterThumbnail = ?
        WHERE MonsterID = ?
        """,
        pyodbc.Binary(image_bytes),
        pyodbc.Binary(thumb_bytes),
        monster_id,
    )
    cursor.close()


def original_extension(image_bytes: bytes) -> str:
    with Image.open(io.BytesIO(image_bytes)) as img:
        fmt = (img.format or "bin").lower()
    return {
        "jpeg": "jpg",
        "png": "png",
        "webp": "webp",
        "gif": "gif",
        "bmp": "bmp",
        "tiff": "tiff",
    }.get(fmt, "bin")


def backup_original(monster_id: int, name: str, image_bytes: bytes, directory: Path) -> Path:
    """Write the database bytes unchanged. Never overwrite an existing backup."""
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "monster"
    path = directory / f"{monster_id}_{safe_name[:80]}.{original_extension(image_bytes)}"
    if path.exists():
        return path
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_bytes(image_bytes)
    temporary.replace(path)
    return path


def mb(size: int) -> str:
    return f"{size / MIB:.2f} MB"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write resized images. Without this flag the script only reports.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most this many rows (0 means all).",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=BACKUP_DIR,
        help=f"Folder for original files before replace (default: {BACKUP_DIR})",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Update the database without writing the original file to disk.",
    )
    args = parser.parse_args()

    print(f"Server:   {SERVER}")
    print(f"Database: {DATABASE}")
    print(f"Source:   >= {mb(SOURCE_MIN_BYTES)}")
    print(f"Target:   {mb(TARGET_MIN_BYTES)} – {mb(TARGET_MAX_BYTES)}")
    print("Backup:   off" if args.no_backup else f"Backup:   {args.backup_dir}")
    print("Mode:     apply" if args.apply else "Mode:     dry-run")
    print()

    conn = get_connection()
    rows = list_large_images(conn)
    if args.limit:
        rows = rows[: args.limit]
    print(f"Images to process: {len(rows)}")
    print()

    changed = 0
    skipped = 0
    failed = 0
    old_total = 0
    new_total = 0

    total = len(rows)
    for index, (monster_id, name, old_size) in enumerate(rows, start=1):
        old_size = int(old_size)
        print(f"[{index}/{total}] {name} ({monster_id}) {mb(old_size)}", flush=True)
        try:
            original = load_image(conn, monster_id)
            img = prepare_image(original)
            new_bytes, scale, quality, fmt = fit_to_range(img)
            new_size = len(new_bytes)
        except (UnidentifiedImageError, OSError, LookupError) as exc:
            failed += 1
            print(f"FAIL  {name} ({monster_id}): {exc}")
            continue

        if new_size >= old_size:
            skipped += 1
            print(
                f"SKIP  {name}: {mb(old_size)} -> {mb(new_size)} "
                f"({fmt} q{quality} scale {scale:.3f}) not smaller"
            )
            continue

        old_total += old_size
        new_total += new_size
        changed += 1
        print(
            f"{'WRITE' if args.apply else 'PLAN '} {name}: "
            f"{mb(old_size)} -> {mb(new_size)}  "
            f"{img.width}x{img.height} -> "
            f"{max(1, round(img.width * scale))}x{max(1, round(img.height * scale))}  "
            f"{fmt} q{quality}"
        )

        if args.apply:
            if not args.no_backup:
                try:
                    backup_path = backup_original(monster_id, name, original, args.backup_dir)
                except OSError as exc:
                    failed += 1
                    changed -= 1
                    old_total -= old_size
                    new_total -= new_size
                    print(f"FAIL  {name}: backup not written ({exc}); database left unchanged")
                    continue
                print(f"      saved {backup_path}")
            thumb = create_thumbnail(img if scale >= 0.999 else _resized(img, scale))
            save_image(conn, monster_id, new_bytes, thumb)
            conn.commit()

    print()
    print(f"Changed: {changed}")
    print(f"Skipped: {skipped}")
    print(f"Failed:  {failed}")
    if changed:
        print(f"Bytes:   {mb(old_total)} -> {mb(new_total)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
