# pip install pyodbc

import argparse
import csv
from datetime import datetime
from pathlib import Path

import pyodbc


CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;"
    "DATABASE=PathfinderUtil;"
    "Trusted_Connection=yes;"
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Report monsters in PathfinderUtil that do not have ImageUrl artwork."
    )
    parser.add_argument(
        "--report-dir",
        default="reports/monster-art",
        help="Directory for CSV and Markdown reports."
    )
    parser.add_argument(
        "--preview",
        type=int,
        default=40,
        help="Number of detail rows to print to the console. Default: 40."
    )
    return parser.parse_args()


def connect():
    try:
        return pyodbc.connect(CONN_STR)
    except pyodbc.Error:
        return pyodbc.connect(CONN_STR + "Encrypt=no;TrustServerCertificate=yes;")


def fetch_rows(cur):
    cur.execute("""
        SELECT
            MonsterId,
            AonId,
            Name,
            Level,
            COALESCE(Rarity, '') AS Rarity,
            COALESCE(Size, '') AS Size,
            COALESCE(SourceBook, '') AS SourceBook,
            COALESCE(Family, '') AS Family,
            IsNPC,
            AonUrl
        FROM pf2.vwMonsterFull
        WHERE ImageUrl IS NULL
           OR LTRIM(RTRIM(ImageUrl)) = ''
        ORDER BY
            COALESCE(SourceBook, ''),
            Level,
            Name,
            MonsterId
    """)

    columns = [column[0] for column in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def fetch_summary(cur):
    cur.execute("""
        SELECT
            COALESCE(SourceBook, '(unknown)') AS SourceBook,
            COUNT_BIG(1) AS MissingArtCount
        FROM pf2.vwMonsterFull
        WHERE ImageUrl IS NULL
           OR LTRIM(RTRIM(ImageUrl)) = ''
        GROUP BY COALESCE(SourceBook, '(unknown)')
        ORDER BY MissingArtCount DESC, SourceBook
    """)

    return [{"SourceBook": row[0], "MissingArtCount": int(row[1])} for row in cur.fetchall()]


def write_reports(rows, summary, report_dir):
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = report_dir / f"{stamp}_missing_monster_art.csv"
    md_path = report_dir / f"{stamp}_missing_monster_art.md"

    columns = [
        "MonsterId",
        "AonId",
        "Name",
        "Level",
        "Rarity",
        "Size",
        "SourceBook",
        "Family",
        "IsNPC",
        "AonUrl",
    ]

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    with md_path.open("w", encoding="utf-8") as f:
        f.write("# Monsters Missing Artwork\n\n")
        f.write(f"Generated: {datetime.now().isoformat(timespec='seconds')}\n\n")
        f.write(f"Total missing ImageUrl: {len(rows)}\n\n")
        f.write("## Summary By Source\n\n")
        f.write("| SourceBook | Missing Art |\n")
        f.write("| --- | ---: |\n")

        for item in summary:
            f.write(f"| {escape_md(item['SourceBook'])} | {item['MissingArtCount']} |\n")

        f.write("\n## Detail\n\n")
        f.write("| MonsterId | AonId | Name | Level | SourceBook | NPC | AoN |\n")
        f.write("| ---: | ---: | --- | ---: | --- | --- | --- |\n")

        for row in rows:
            f.write(
                f"| {row['MonsterId']} "
                f"| {row['AonId'] or ''} "
                f"| {escape_md(row['Name'])} "
                f"| {row['Level'] if row['Level'] is not None else ''} "
                f"| {escape_md(row['SourceBook'])} "
                f"| {'Y' if row['IsNPC'] else ''} "
                f"| {escape_md(row['AonUrl'] or '')} |\n"
            )

    return csv_path, md_path


def escape_md(value):
    return str(value or "").replace("|", "\\|")


def print_table(title, rows, columns):
    print()
    print(title)
    print("-" * len(title))
    print("| " + " | ".join(columns) + " |")
    print("| " + " | ".join("---" for _ in columns) + " |")

    for row in rows:
        print("| " + " | ".join(str(row.get(column, "")) for column in columns) + " |")


def main():
    args = parse_args()
    cn = connect()
    cur = cn.cursor()

    try:
        rows = fetch_rows(cur)
        summary = fetch_summary(cur)
    finally:
        cur.close()
        cn.close()

    csv_path, md_path = write_reports(rows, summary, Path(args.report_dir))

    print(f"Total monsters missing ImageUrl: {len(rows)}")
    print(f"CSV report: {csv_path}")
    print(f"Markdown report: {md_path}")

    print_table(
        "Top Sources Missing Monster Art",
        summary[:20],
        ["SourceBook", "MissingArtCount"],
    )

    preview_rows = []

    for row in rows[:args.preview]:
        preview_rows.append({
            "MonsterId": row["MonsterId"],
            "AonId": row["AonId"] or "",
            "Name": row["Name"],
            "Level": row["Level"] if row["Level"] is not None else "",
            "SourceBook": row["SourceBook"],
            "NPC": "Y" if row["IsNPC"] else "",
        })

    print_table(
        f"First {len(preview_rows)} Monsters Missing Art",
        preview_rows,
        ["MonsterId", "AonId", "Name", "Level", "SourceBook", "NPC"],
    )


if __name__ == "__main__":
    main()
