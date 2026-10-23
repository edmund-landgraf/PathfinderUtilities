"""Shared helpers for empty-field backfills from stored RawJson.

Preview unless the caller passes commit=True. Updates never replace a
non-empty column.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pyodbc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mssql_conn import sql_auth

AON_TAG_RE = re.compile(r"<%([^%>]*)%>")
PAGE_RE = re.compile(r"\bpg\.?\s*(\d+)", re.I)
LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def add_sql_login_arg(parser):
    parser.add_argument(
        "--sql-login",
        action="store_true",
        help=(
            "Use MSSQL_USER and MSSQL_PASSWORD instead of Windows trusted connection. "
            "Required for SQL Server in Docker on Linux."
        ),
    )


CHUNK_SIZE = 100


def note_write(connection, pending, commit):
    """Count one written row and commit every CHUNK_SIZE rows."""
    if not commit:
        return pending
    pending += 1
    if pending % CHUNK_SIZE == 0:
        connection.commit()
        print(f"Committed {pending} rows.")
    return pending


def finish_writes(connection, pending, commit):
    if not commit:
        connection.rollback()
        print("Preview only. Re-run with --commit to write.")
        return
    if pending % CHUNK_SIZE:
        connection.commit()
        print(f"Committed {pending} rows.")
    elif pending == 0:
        print("Nothing to commit.")


def connect(sql_login: bool = False):
    conn_str = (
        "DRIVER={ODBC Driver 17 for SQL Server};"
        "SERVER=localhost;"
        "DATABASE=PathfinderUtil;"
        + sql_auth(sql_login)
        + "TrustServerCertificate=yes;"
    )
    cn = pyodbc.connect(conn_str)
    cn.autocommit = False
    return cn


def load_json(raw):
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def first_existing(src, *names):
    if not src:
        return None
    lowered = {str(k).lower(): v for k, v in src.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, "", []):
            return value
    return None


def clean(value):
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return None
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    return text or None


def strip_aon_tags(value):
    text = clean(value)
    if not text:
        return None
    text = AON_TAG_RE.sub(lambda m: m.group(1).strip(), text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+,", ",", text)
    return text.strip(" ,;") or None


def page_from_src(src):
    raw = clean(first_existing(src, "primary_source_raw", "source_raw"))
    if not raw:
        return None
    match = PAGE_RE.search(raw)
    return int(match.group(1)) if match else None


def source_name_from_src(src):
    return clean(first_existing(src, "primary_source", "source"))


def markdown_from_src(src):
    return clean(first_existing(src, "markdown"))


def summary_from_src(src):
    text = clean(first_existing(src, "summary"))
    if not text:
        return None
    text = LINK_RE.sub(r"\1", text)
    text = strip_aon_tags(text)
    return text


def split_values(value):
    if not value:
        return []
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                name = clean(first_existing(item, "name", "trait", "value"))
                if name:
                    parts.append(name)
            else:
                name = clean(item)
                if name:
                    parts.append(name)
        return unique(parts)
    text = strip_aon_tags(value) or ""
    text = text.replace(";", ",").replace("|", ",")
    return unique(part.strip() for part in text.split(","))


def unique(values):
    seen = set()
    out = []
    for value in values:
        text = clean(value)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def format_defense(value):
    """Turn a resistance or weakness into readable text.

    AoN rows store these as JSON arrays or objects. Plain text is kept
    after AoN tags are removed.
    """
    if value is None:
        return None
    parsed = value
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in "[{":
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return strip_aon_tags(text)
        else:
            return strip_aon_tags(text)

    parts = []
    if isinstance(parsed, dict):
        items = parsed.items()
        for name, amount in items:
            label = strip_aon_tags(name)
            if not label:
                continue
            amount_text = clean(amount)
            parts.append(f"{label} {amount_text}" if amount_text else label)
    elif isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                name = strip_aon_tags(
                    first_existing(item, "name", "type", "trait", "value")
                )
                amount = clean(
                    first_existing(item, "value", "amount", "modifier", "bonus")
                )
                if name and amount and amount.lower() != name.lower():
                    parts.append(f"{name} {amount}")
                elif name:
                    parts.append(name)
            else:
                text = strip_aon_tags(item)
                if text:
                    parts.append(text)
    else:
        return strip_aon_tags(parsed)

    joined = ", ".join(unique(parts))
    return joined or None


def empty(value):
    if value is None:
        return True
    return len(str(value).strip()) == 0


def get_or_create_id(cur, table, id_column, name_column, name, cache):
    key = (table, name.lower())
    if key in cache:
        return cache[key]
    cur.execute(
        f"SELECT {id_column} FROM {table} WHERE {name_column} = ?",
        name,
    )
    row = cur.fetchone()
    if row:
        cache[key] = row[0]
        return row[0]
    cur.execute(
        f"INSERT INTO {table} ({name_column}) OUTPUT INSERTED.{id_column} VALUES (?)",
        name,
    )
    new_id = cur.fetchone()[0]
    cache[key] = new_id
    return new_id


def ensure_trait(cur, owner_table, owner_id_column, owner_id, trait, cache):
    trait_id = get_or_create_id(cur, "pf2.Trait", "TraitId", "Name", trait, cache)
    link = f"pf2.{owner_table}Trait"
    cur.execute(
        f"""
        IF NOT EXISTS (
            SELECT 1 FROM {link}
            WHERE {owner_id_column} = ? AND TraitId = ?
        )
        INSERT INTO {link} ({owner_id_column}, TraitId) VALUES (?, ?)
        """,
        owner_id,
        trait_id,
        owner_id,
        trait_id,
    )
    return cur.rowcount


def column_exists(cur, table, column):
    cur.execute(
        """
        SELECT 1
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = 'pf2'
          AND TABLE_NAME = ?
          AND COLUMN_NAME = ?
        """,
        table,
        column,
    )
    return cur.fetchone() is not None


def ensure_column(cur, table, column, definition):
    if column_exists(cur, table, column):
        return False
    cur.execute(f"ALTER TABLE pf2.{table} ADD {column} {definition}")
    return True


def print_plan(title, counts):
    print("=" * 80)
    print(title)
    print("=" * 80)
    for label, count in counts.items():
        print(f"  {label}: {count}")
