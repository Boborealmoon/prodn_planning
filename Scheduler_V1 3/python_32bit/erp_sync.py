from __future__ import annotations

import argparse
import csv
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable

try:
    import pyodbc
except ImportError as exc:  # pragma: no cover
    raise SystemExit("pyodbc is required for ERP sync. Install it in the 32-bit Python environment.") from exc


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR if (SCRIPT_DIR / "sql").exists() else SCRIPT_DIR.parent
DEFAULT_DSN = os.environ.get("ERP_ODBC_DSN", "PostgreSQL30")
DEFAULT_DATABASE = os.environ.get("ERP_DATABASE", "COMAIN")
DEFAULT_SQLITE = ROOT / "planner.db"
DEFAULT_QUERY_FILE = ROOT / "sql" / "erp_extract.sql"
DEFAULT_CSV = ROOT / "exports" / "erp_extract.csv"
DEFAULT_TABLE = "erp_sync_staging"


def parse_args():
    parser = argparse.ArgumentParser(description="Extract ERP data through a 32-bit ODBC DSN into planner staging.")
    parser.add_argument("--dsn", default=DEFAULT_DSN, help="ODBC DSN name, default: %(default)s")
    parser.add_argument("--database", default=DEFAULT_DATABASE, help="Database name, default: %(default)s")
    parser.add_argument("--uid", default=os.environ.get("ERP_UID", ""), help="Optional ERP username")
    parser.add_argument("--pwd", default=os.environ.get("ERP_PWD", ""), help="Optional ERP password")
    parser.add_argument("--query-file", default=str(DEFAULT_QUERY_FILE), help="SQL file to run against ERP")
    parser.add_argument("--sqlite", default=str(DEFAULT_SQLITE), help="SQLite file to load staging data into")
    parser.add_argument("--table", default=DEFAULT_TABLE, help="SQLite staging table name")
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="CSV export path")
    parser.add_argument("--limit", type=int, default=0, help="Optional LIMIT appended to the ERP query")
    return parser.parse_args()


def read_query(query_file: Path, limit: int) -> str:
    query = query_file.read_text(encoding="utf-8").strip()
    if not query:
        raise SystemExit(f"Query file is empty: {query_file}")
    if limit > 0:
        query = f"SELECT * FROM ({query}) erp_sync_query LIMIT {limit}"
    return query


def fetch_erp_rows(dsn: str, database: str, query: str, uid: str = "", pwd: str = ""):
    parts = [f"DSN={dsn}"]
    if database:
        parts.append(f"DATABASE={database}")
    if uid:
        parts.append(f"UID={uid}")
    if pwd:
        parts.append(f"PWD={pwd}")
    conn_str = ";".join(parts) + ";"

    with pyodbc.connect(conn_str, timeout=30) as con:
        cur = con.cursor()
        cur.execute(query)
        columns = [col[0] for col in cur.description]
        rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    return columns, rows


def sanitize_columns(columns: Iterable[str]) -> list[str]:
    seen: dict[str, int] = {}
    sanitized = []
    for idx, col in enumerate(columns):
        base = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in (col or f"col_{idx+1}")).strip("_")
        base = base or f"col_{idx+1}"
        key = base.lower()
        seen[key] = seen.get(key, 0) + 1
        sanitized.append(base if seen[key] == 1 else f"{base}_{seen[key]}")
    return sanitized


def load_into_sqlite(sqlite_path: Path, table: str, columns: list[str], rows: list[dict]):
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    safe_columns = sanitize_columns(columns)
    synced_at = datetime.now().isoformat(timespec="seconds")

    with sqlite3.connect(sqlite_path) as con:
        quoted_cols = ", ".join(f'"{name}" TEXT' for name in safe_columns)
        con.execute(f'CREATE TABLE IF NOT EXISTS "{table}" (_synced_at TEXT NOT NULL, {quoted_cols})')
        existing_info = con.execute(f'PRAGMA table_info("{table}")').fetchall()
        existing_cols = [row[1] for row in existing_info]
        wanted_cols = ["_synced_at", *safe_columns]
        if existing_cols != wanted_cols:
            con.execute(f'DROP TABLE IF EXISTS "{table}"')
            con.execute(f'CREATE TABLE "{table}" (_synced_at TEXT NOT NULL, {quoted_cols})')
        else:
            con.execute(f'DELETE FROM "{table}"')

        if rows:
            placeholders = ", ".join("?" for _ in wanted_cols)
            insert_sql = f'INSERT INTO "{table}" ({", ".join(f"""\"{c}\"""" for c in wanted_cols)}) VALUES ({placeholders})'
            payload = []
            for row in rows:
                payload.append([synced_at, *["" if row.get(src) is None else str(row.get(src)) for src in columns]])
            con.executemany(insert_sql, payload)
        con.commit()

    return safe_columns, synced_at


def write_csv(csv_path: Path, columns: list[str], rows: list[dict]):
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def main():
    args = parse_args()
    query_file = Path(args.query_file)
    sqlite_path = Path(args.sqlite)
    csv_path = Path(args.csv)
    query = read_query(query_file, args.limit)
    columns, rows = fetch_erp_rows(args.dsn, args.database, query, args.uid, args.pwd)
    safe_columns, synced_at = load_into_sqlite(sqlite_path, args.table, columns, rows)
    write_csv(csv_path, columns, rows)

    print(f"ERP sync complete: {len(rows)} rows")
    print(f"DSN: {args.dsn}")
    print(f"Database: {args.database}")
    print(f"SQLite staging table: {args.table}")
    print(f"SQLite file: {sqlite_path}")
    print(f"CSV export: {csv_path}")
    print(f"Columns: {', '.join(safe_columns)}")
    print(f"Synced at: {synced_at}")


if __name__ == "__main__":
    main()
