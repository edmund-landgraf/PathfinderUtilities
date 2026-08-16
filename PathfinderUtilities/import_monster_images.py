from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import pyodbc
import requests


@dataclass(frozen=True)
class Config:
    sql_server: str = r"localhost"
    database: str = "PathfinderUtil"

    # Use Windows authentication.
    connection_string: str = (
        r"DRIVER={ODBC Driver 18 for SQL Server};"
        r"SERVER=localhost;"
        r"DATABASE=PathfinderUtil;"
        r"Trusted_Connection=yes;"
        r"TrustServerCertificate=yes;"
    )

    request_timeout_seconds: int = 30
    request_delay_seconds: float = 0.25
    commit_batch_size: int = 25
    maximum_image_size_bytes: int = 20 * 1024 * 1024
    overwrite_existing: bool = False


CONFIG = Config()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/149.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
}


def get_connection() -> pyodbc.Connection:
    return pyodbc.connect(CONFIG.connection_string)


def get_monsters(cursor: pyodbc.Cursor) -> list[tuple[int, str, str]]:
    """
    Return MonsterId, Name, and ImageUrl for monsters having an image URL.

    When overwrite_existing is False, monsters already represented in
    dbo.MonsterImage are excluded.
    """
    if CONFIG.overwrite_existing:
        sql = """
            SELECT
                m.MonsterId,
                m.Name,
                LTRIM(RTRIM(m.ImageUrl)) AS ImageUrl
            FROM pf2.Monster AS m
            WHERE NULLIF(LTRIM(RTRIM(m.ImageUrl)), '') IS NOT NULL
            ORDER BY m.MonsterId;
        """
    else:
        sql = """
            SELECT
                m.MonsterId,
                m.Name,
                LTRIM(RTRIM(m.ImageUrl)) AS ImageUrl
            FROM pf2.Monster AS m
            WHERE NULLIF(LTRIM(RTRIM(m.ImageUrl)), '') IS NOT NULL
              AND NOT EXISTS
              (
                  SELECT 1
                  FROM dbo.MonsterImage AS mi
                  WHERE mi.MonsterID = m.MonsterId
              )
            ORDER BY m.MonsterId;
        """

    cursor.execute(sql)
    return [
        (int(row.MonsterId), str(row.Name), str(row.ImageUrl))
        for row in cursor.fetchall()
    ]


def download_image(
    session: requests.Session,
    image_url: str,
) -> bytes | None:
    try:
        response = session.get(
            image_url,
            headers=HEADERS,
            timeout=CONFIG.request_timeout_seconds,
            allow_redirects=True,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logging.warning("Download failed: %s | %s", image_url, exc)
        return None

    content_type = response.headers.get("Content-Type", "").lower()
    image_data = response.content

    if not image_data:
        logging.warning("Empty response: %s", image_url)
        return None

    # Some servers return application/octet-stream for valid images,
    # so reject only obvious HTML/text responses.
    if (
        content_type
        and not content_type.startswith("image/")
        and "octet-stream" not in content_type
    ):
        logging.warning(
            "URL did not return an image: %s | Content-Type: %s",
            image_url,
            content_type,
        )
        return None

    if len(image_data) > CONFIG.maximum_image_size_bytes:
        logging.warning(
            "Image exceeds maximum size: %s | %.2f MB",
            image_url,
            len(image_data) / 1024 / 1024,
        )
        return None

    return image_data


def save_monster_image(
    cursor: pyodbc.Cursor,
    monster_id: int,
    image_data: bytes,
) -> None:
    if CONFIG.overwrite_existing:
        sql = """
            DELETE FROM dbo.MonsterImage
            WHERE MonsterID = ?;

            INSERT INTO dbo.MonsterImage
            (
                MonsterID,
                MonsterImage
            )
            VALUES
            (
                ?,
                ?
            );
        """

        cursor.execute(
            sql,
            monster_id,
            monster_id,
            pyodbc.Binary(image_data),
        )
    else:
        sql = """
            INSERT INTO dbo.MonsterImage
            (
                MonsterID,
                MonsterImage
            )
            SELECT
                ?,
                ?
            WHERE NOT EXISTS
            (
                SELECT 1
                FROM dbo.MonsterImage
                WHERE MonsterID = ?
            );
        """

        cursor.execute(
            sql,
            monster_id,
            pyodbc.Binary(image_data),
            monster_id,
        )


def main() -> None:
    downloaded = 0
    failed = 0
    pending_commits = 0

    with get_connection() as connection:
        cursor = connection.cursor()
        monsters = get_monsters(cursor)

        logging.info(
            "Found %d monsters with image URLs requiring processing.",
            len(monsters),
        )

        with requests.Session() as session:
            for position, (monster_id, name, image_url) in enumerate(
                monsters,
                start=1,
            ):
                logging.info(
                    "[%d/%d] MonsterID %d: %s",
                    position,
                    len(monsters),
                    monster_id,
                    name,
                )

                image_data = download_image(session, image_url)

                if image_data is None:
                    failed += 1
                    continue

                try:
                    save_monster_image(
                        cursor=cursor,
                        monster_id=monster_id,
                        image_data=image_data,
                    )
                except pyodbc.Error:
                    logging.exception(
                        "Database insert failed for MonsterID %d: %s",
                        monster_id,
                        name,
                    )
                    connection.rollback()
                    pending_commits = 0
                    failed += 1
                    continue

                downloaded += 1
                pending_commits += 1

                logging.info(
                    "Saved MonsterID %d: %.1f KB",
                    monster_id,
                    len(image_data) / 1024,
                )

                if pending_commits >= CONFIG.commit_batch_size:
                    connection.commit()
                    pending_commits = 0
                    logging.info("Committed batch.")

                if CONFIG.request_delay_seconds > 0:
                    time.sleep(CONFIG.request_delay_seconds)

        if pending_commits:
            connection.commit()

    logging.info(
        "Finished. Saved: %d | Failed or skipped: %d",
        downloaded,
        failed,
    )


if __name__ == "__main__":
    main()