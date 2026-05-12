from __future__ import annotations

import csv
import io
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen

SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vTpkJ7xVDrzE_U1aHkUaiNsE52JSZi4Y-oOuDA7-B_hcQw3SRSdSp6ortBagy2lNE2fZjga1O9T9ZBV"
    "/pub?gid=606390196&single=true&output=csv"
)

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR if (SCRIPT_DIR / "sql").exists() else SCRIPT_DIR.parent
DEFAULT_SQLITE = ROOT / "planner.db"
STAGING_TABLE = "setup_time_staging"

# Final columns stored in the staging table
COLUMNS = [
    "timestamp",
    "programmer_name",
    "program_file",
    "tool_list_flag",
    "tool_list_file",
    "setup_time",
    "cycle_time",
    "ps_no",
    "date",
    "machine_no",
    "op_no",
    "part_no",
]


# ── Environment ────────────────────────────────────────────────────────────────

def load_env_file() -> str:
    """Load .env from the project root and return SECRET_KEY_GOOGLE (unused for public CSV)."""
    env_path = ROOT / ".env"
    if env_path.exists():
        with env_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    return os.environ.get("SECRET_KEY_GOOGLE", "")


# ── Fetch ──────────────────────────────────────────────────────────────────────

def fetch_rows() -> list[dict]:
    """
    Fetch the published Google Sheet CSV.
    The sheet has TWO header rows:
      Row 0 — blank / numeric section labels (skip)
      Row 1 — actual column names ("Timestamp", "Set-up Time", …)
      Row 2+ — form response data
    SECRET_KEY_GOOGLE is available for Google Sheets API v4 if the sheet
    ever becomes private; the public CSV endpoint requires no authentication.
    """
    with urlopen(SHEET_URL, timeout=30) as resp:
        raw = resp.read().decode("cp1252")

    reader = csv.reader(io.StringIO(raw))
    all_rows = list(reader)

    if len(all_rows) < 3:
        return []

    headers = all_rows[1]           # second row = real column names
    data_rows = all_rows[2:]
    return [
        dict(zip(headers, row))
        for row in data_rows
        if any(c.strip() for c in row)
    ]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _int(val) -> int | None:
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return None


def _float(val) -> float | None:
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return None


_TS_FORMATS = (
    "%m/%d/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%m/%d/%Y",
    "%d/%m/%Y",
)


def _parse_dt(val: str) -> str | None:
    val = (val or "").strip()
    if not val:
        return None
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(val, fmt).isoformat()
        except ValueError:
            continue
    return None


# ── Transform ──────────────────────────────────────────────────────────────────

def transform(raw: list[dict], ps_lookup: dict[str, str]) -> list[dict]:
    """
    Applies the Power Query M transformations:
      1. RemoveRowsWithErrors on Timestamp
      2. Compute Machine No. and Op No. based on Operation Type
         (Milling → CNC Machine No._2 / Operation No._3, else CNC Machine No. / Operation No.)
      3. RemoveBlankRows
      4. Left-join with process_sheet to resolve part_no from P/S No.
      5. Sort by Timestamp descending, deduplicate on (part_no, op_no)
    """
    rows: list[dict] = []

    for r in raw:
        ts = _parse_dt(r.get("Timestamp", ""))
        if ts is None:                          # RemoveRowsWithErrors on Timestamp
            continue

        op_type = (r.get("Operation Type") or "").strip()
        if op_type == "Milling":
            machine_no = _int(r.get("CNC Machine No._2"))
            op_no      = _int(r.get("Operation No._3"))
        else:
            machine_no = _int(r.get("CNC Machine No."))
            op_no      = _int(r.get("Operation No."))

        # RemoveBlankRows: skip rows where every cell is blank/null
        if not any(str(v).strip() for v in r.values()):
            continue

        ps_no   = (r.get("P/S No.") or "").strip()
        part_no = ps_lookup.get(ps_no)

        rows.append({
            "timestamp":      ts,
            "programmer_name": r.get("Programmer Name "),
            "program_file":   r.get("Please upload the Program file. "),
            "tool_list_flag": r.get("Do you have completed Tool List files?"),
            "tool_list_file": r.get("Please upload the Tool List files."),
            "setup_time":     _int(r.get("Set-up Time")),
            "cycle_time":     _float(r.get("Cycle Time")),
            "ps_no":          ps_no,
            "date":           _parse_dt(r.get("Date", "")),
            "machine_no":     machine_no,
            "op_no":          op_no,
            "part_no":        part_no,
        })

    # Sort newest-first, then keep only the first row per (part_no, op_no)
    rows.sort(key=lambda x: x["timestamp"] or "", reverse=True)
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for row in rows:
        key = (row["part_no"], row["op_no"])
        if key not in seen:
            seen.add(key)
            deduped.append(row)

    return deduped


# ── SQLite lookup ──────────────────────────────────────────────────────────────

def load_ps_lookup(sqlite_path: Path) -> dict[str, str]:
    """Returns {ps_id: inv_code} from the local process_sheet table."""
    with sqlite3.connect(sqlite_path) as con:
        rows = con.execute("SELECT ps_id, inv_code FROM process_sheet").fetchall()
    return {ps_id: (inv_code or "") for ps_id, inv_code in rows}


# ── Write staging ──────────────────────────────────────────────────────────────

def write_staging(sqlite_path: Path, rows: list[dict]) -> str:
    synced_at = datetime.now().isoformat(timespec="seconds")
    col_defs  = '"_synced_at" TEXT NOT NULL, ' + ", ".join(f'"{c}" TEXT' for c in COLUMNS)
    col_list  = ", ".join(f'"{c}"' for c in ["_synced_at"] + COLUMNS)
    ph        = ", ".join("?" for _ in range(len(COLUMNS) + 1))
    insert_sql = f'INSERT INTO "{STAGING_TABLE}" ({col_list}) VALUES ({ph})'

    with sqlite3.connect(sqlite_path) as con:
        con.execute(f'DROP TABLE IF EXISTS "{STAGING_TABLE}"')
        con.execute(f'CREATE TABLE "{STAGING_TABLE}" ({col_defs})')
        payload = [
            [synced_at] + [
                str(row.get(c)) if row.get(c) is not None else ""
                for c in COLUMNS
            ]
            for row in rows
        ]
        con.executemany(insert_sql, payload)
        con.commit()

    return synced_at


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    _api_key = load_env_file()      # loads SECRET_KEY_GOOGLE into os.environ

    sqlite_path = DEFAULT_SQLITE

    print("Fetching Google Sheet...")
    raw = fetch_rows()
    print(f"  {len(raw)} raw rows")

    print("Loading process sheet lookup from SQLite...")
    ps_lookup = load_ps_lookup(sqlite_path)
    print(f"  {len(ps_lookup)} entries")

    print("Transforming rows...")
    rows = transform(raw, ps_lookup)
    print(f"  {len(rows)} rows after transform + dedup on (part_no, op_no)")

    print("Writing to SQLite staging...")
    synced_at = write_staging(sqlite_path, rows)
    print(f"  Table   : {STAGING_TABLE}")
    print(f"  SQLite  : {sqlite_path}")
    print(f"  Synced  : {synced_at}")


if __name__ == "__main__":
    main()
