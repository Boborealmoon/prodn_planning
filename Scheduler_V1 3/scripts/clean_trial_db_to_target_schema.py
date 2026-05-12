from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1] if "__file__" in globals() else Path.cwd()
DEFAULT_DB_PATH = ROOT / "planner.db"


TARGET_DDL = r"""
PRAGMA foreign_keys = OFF;

CREATE TABLE calendar_days (
  work_date TEXT PRIMARY KEY,
  is_working_day INTEGER NOT NULL DEFAULT 1,
  note TEXT DEFAULT ''
);

CREATE TABLE data_import_log (
  log_id INTEGER PRIMARY KEY AUTOINCREMENT,
  import_type TEXT NOT NULL,
  workbook_name TEXT DEFAULT '',
  active_sheet_name TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'STARTED',
  message TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT DEFAULT ''
);

CREATE TABLE machines (
  machine_id INTEGER PRIMARY KEY AUTOINCREMENT,
  machine_code TEXT NOT NULL,
  machine_category TEXT NOT NULL,
  shift_profile TEXT NOT NULL DEFAULT 'STANDARD',
  active INTEGER NOT NULL DEFAULT 1,
  notes TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE parts (
  part_id INTEGER PRIMARY KEY AUTOINCREMENT,
  part_no TEXT NOT NULL,
  part_desc TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE bom_variation (
  bom_id INTEGER PRIMARY KEY AUTOINCREMENT,
  part_id INTEGER NOT NULL REFERENCES parts(part_id) ON DELETE CASCADE,
  bom_code TEXT NOT NULL,
  bom_desc TEXT DEFAULT '',
  is_default INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE operation_seq (
  op_seq_id INTEGER PRIMARY KEY AUTOINCREMENT,
  bom_id INTEGER NOT NULL REFERENCES bom_variation(bom_id) ON DELETE CASCADE,
  seq_no INTEGER NOT NULL,
  op_no TEXT NOT NULL,
  op_type TEXT NOT NULL,
  machine_category TEXT NOT NULL,
  cycle_time REAL NOT NULL DEFAULT 1,
  setup_time REAL NOT NULL DEFAULT 0,
  preferred_machine TEXT DEFAULT '',
  is_last_op INTEGER DEFAULT 0
);

CREATE TABLE process_sheet (
  ps_id TEXT PRIMARY KEY,
  part_id INTEGER REFERENCES parts(part_id),
  part_no TEXT DEFAULT '',
  part_desc TEXT DEFAULT '',
  order_date TEXT DEFAULT '',
  due_date TEXT DEFAULT '',
  total_qty REAL NOT NULL DEFAULT 0,
  planned_qty REAL NOT NULL DEFAULT 0,
  finished_qty REAL NOT NULL DEFAULT 0,
  selected_bom_id INTEGER REFERENCES bom_variation(bom_id),
  planner_status TEXT NOT NULL DEFAULT 'UNPLANNED',
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  source_ps_id TEXT DEFAULT '',
  pp_partial_no TEXT DEFAULT '1'
);

CREATE TABLE process_sheet_material (
  mat_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ps_id TEXT NOT NULL REFERENCES process_sheet(ps_id) ON DELETE CASCADE,
  material_name TEXT DEFAULT '',
  material_ready INTEGER DEFAULT 0,
  material_ready_qty REAL DEFAULT 0,
  need_by_date TEXT DEFAULT '',
  order_status TEXT DEFAULT 'TO_ORDER',
  planner_note TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE process_sheet_material_order_log (
  log_id INTEGER PRIMARY KEY AUTOINCREMENT,
  mat_id INTEGER NOT NULL REFERENCES process_sheet_material(mat_id) ON DELETE CASCADE,
  ps_id TEXT NOT NULL REFERENCES process_sheet(ps_id) ON DELETE CASCADE,
  ordered_qty REAL DEFAULT 0,
  received_qty REAL DEFAULT 0,
  order_date TEXT DEFAULT '',
  expected_date TEXT DEFAULT '',
  received_date TEXT DEFAULT '',
  log_status TEXT DEFAULT 'PENDING',
  note TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE staff (
  staff_id INTEGER PRIMARY KEY AUTOINCREMENT,
  staff_name TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'MACHINIST',
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE bom_material (
  bom_material_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_inventory_code TEXT NOT NULL DEFAULT '',
  bom_code TEXT NOT NULL DEFAULT '',
  material_inventory_code TEXT NOT NULL DEFAULT '',
  material_description TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE capacity_profile (
  profile_id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_name TEXT NOT NULL,
  capacity_minutes INTEGER NOT NULL DEFAULT 0,
  start_minute INTEGER NOT NULL DEFAULT 510,
  note TEXT DEFAULT ''
);

CREATE TABLE machine_capacity_day (
  day_id INTEGER PRIMARY KEY AUTOINCREMENT,
  machine_id INTEGER NOT NULL REFERENCES machines(machine_id) ON DELETE CASCADE,
  work_date TEXT NOT NULL,
  profile_id INTEGER NOT NULL REFERENCES capacity_profile(profile_id),
  capacity_minutes INTEGER NOT NULL DEFAULT 0,
  start_minute INTEGER NOT NULL DEFAULT 510,
  note TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE material_requirement (
  requirement_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ps_id TEXT NOT NULL REFERENCES process_sheet(ps_id) ON DELETE CASCADE,
  source_inventory_code TEXT NOT NULL DEFAULT '',
  bom_code TEXT NOT NULL DEFAULT '',
  material_inventory_code TEXT NOT NULL DEFAULT '',
  material_description TEXT DEFAULT '',
  material_qty_needed REAL NOT NULL DEFAULT 0,
  material_uom TEXT DEFAULT '',
  supply_status TEXT NOT NULL DEFAULT 'PENDING_CONFIRMATION',
  expected_ready_date TEXT DEFAULT '',
  supplier_ref TEXT DEFAULT '',
  remarks TEXT DEFAULT '',
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE operation (
  operation_id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_no TEXT NOT NULL,
  operation_name TEXT NOT NULL,
  total_qty REAL NOT NULL DEFAULT 0,
  setup_minutes REAL NOT NULL DEFAULT 0,
  cycle_minutes_per_qty REAL NOT NULL DEFAULT 0,
  compatible_machine_group TEXT DEFAULT '',
  source_ps_id TEXT DEFAULT '',
  source_op_seq_id INTEGER NOT NULL DEFAULT 0,
  source_op_no TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  remarks TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE run_block_group (
  group_id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_label TEXT NOT NULL DEFAULT '',
  group_type TEXT NOT NULL DEFAULT 'COMBINED',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE run_block (
  block_id INTEGER PRIMARY KEY AUTOINCREMENT,
  operation_id INTEGER NOT NULL REFERENCES operation(operation_id) ON DELETE CASCADE,
  machine_id INTEGER NOT NULL REFERENCES machines(machine_id),
  queue_position INTEGER NOT NULL DEFAULT 0,
  scheduled_qty REAL NOT NULL DEFAULT 0,
  include_setup INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'PLANNED',
  anchor_datetime TEXT DEFAULT '',
  calculated_start_datetime TEXT DEFAULT '',
  calculated_end_datetime TEXT DEFAULT '',
  actual_good_qty REAL NOT NULL DEFAULT 0,
  actual_reject_qty REAL NOT NULL DEFAULT 0,
  remarks TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  block_type TEXT NOT NULL DEFAULT 'ORIGINAL',
  source_reject_block_id INTEGER,
  source_reject_segment_id INTEGER,
  planning_status TEXT NOT NULL DEFAULT 'UNPLANNED',
  execution_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
  anchor_status TEXT NOT NULL DEFAULT 'NONE',
  anchor_miss_minutes REAL NOT NULL DEFAULT 0,
  group_id INTEGER REFERENCES run_block_group(group_id)
);

CREATE TABLE planning_card (
  card_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ps_id TEXT NOT NULL REFERENCES process_sheet(ps_id) ON DELETE CASCADE,
  operation_label TEXT NOT NULL DEFAULT '',
  target_qty REAL NOT NULL DEFAULT 0,
  planning_status TEXT NOT NULL DEFAULT 'UNSCHEDULED',
  card_type TEXT NOT NULL DEFAULT 'NORMAL',
  machine_id INTEGER REFERENCES machines(machine_id),
  scheduled_block_group_id INTEGER REFERENCES run_block_group(group_id),
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE planning_card_operation (
  card_op_id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id INTEGER NOT NULL REFERENCES planning_card(card_id) ON DELETE CASCADE,
  source_ps_id TEXT NOT NULL DEFAULT '',
  source_op_seq_id INTEGER NOT NULL DEFAULT 0,
  source_op_no TEXT NOT NULL DEFAULT '',
  op_sequence INTEGER NOT NULL DEFAULT 0,
  setup_minutes REAL NOT NULL DEFAULT 0,
  cycle_minutes_per_qty REAL NOT NULL DEFAULT 0,
  target_qty REAL NOT NULL DEFAULT 0
);

CREATE TABLE run_block_segment (
  segment_id INTEGER PRIMARY KEY AUTOINCREMENT,
  block_id INTEGER NOT NULL REFERENCES run_block(block_id) ON DELETE CASCADE,
  machine_id INTEGER NOT NULL REFERENCES machines(machine_id),
  segment_date TEXT NOT NULL,
  segment_type TEXT NOT NULL,
  qty_done REAL NOT NULL DEFAULT 0,
  minutes_used REAL NOT NULL DEFAULT 0,
  start_datetime TEXT NOT NULL,
  end_datetime TEXT NOT NULL,
  is_actual INTEGER NOT NULL DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE production_actual (
  actual_id INTEGER PRIMARY KEY AUTOINCREMENT,
  block_id INTEGER NOT NULL REFERENCES run_block(block_id) ON DELETE CASCADE,
  report_date TEXT NOT NULL,
  remarks TEXT DEFAULT '',
  reported_at TEXT DEFAULT CURRENT_TIMESTAMP,
  segment_id INTEGER,
  output_qty REAL DEFAULT NULL,
  reject_qty REAL DEFAULT NULL,
  target_qty_at_report REAL DEFAULT NULL
);

CREATE TABLE public_holiday (
  holiday_date TEXT PRIMARY KEY,
  note TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_operation_source_ps ON operation(source_ps_id);
CREATE INDEX idx_run_block_operation ON run_block(operation_id);
CREATE INDEX idx_run_block_machine ON run_block(machine_id);
CREATE INDEX idx_run_block_segment_block ON run_block_segment(block_id);
CREATE INDEX idx_production_actual_block_date ON production_actual(block_id, report_date);
CREATE INDEX idx_material_requirement_ps ON material_requirement(ps_id);

PRAGMA foreign_keys = ON;
"""


LOGICAL_SOURCES = {
    "calendar_days": ["calendar_days"],
    "data_import_log": ["data_import_log"],
    "machines": ["machines"],
    "parts": ["parts"],
    "bom_variation": ["bom_variation", "part_flow_header"],
    "operation_seq": ["operation_seq", "part_flow_steps"],
    "process_sheet": ["process_sheet"],
    "process_sheet_material": ["process_sheet_material"],
    "process_sheet_material_order_log": ["process_sheet_material_order_log"],
    "staff": ["staff"],
    "bom_material": ["bom_material", "trial_bom_material"],
    "capacity_profile": ["capacity_profile", "capacity_profile"],
    "machine_capacity_day": ["machine_capacity_day", "machine_capacity_day"],
    "material_requirement": ["material_requirement", "material_requirement"],
    "operation": ["operation", "trial_operation"],
    "planning_card": ["planning_card", "trial_planning_card"],
    "planning_card_operation": ["planning_card_operation", "trial_planning_card_operation"],
    "production_actual": ["production_actual", "trial_production_actual"],
    "public_holiday": ["public_holiday", "trial_public_holiday"],
    "run_block": ["run_block", "trial_run_block"],
    "run_block_group": ["run_block_group", "trial_run_block_group"],
    "run_block_segment": ["run_block_segment", "trial_run_block_segment"],
}


def q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def scalar(row: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return default


def textv(row: dict[str, Any], *names: str, default: str = "") -> str:
    value = scalar(row, *names, default=default)
    if value is None:
        return default
    return str(value)


def intv(row: dict[str, Any], *names: str, default: int = 0) -> int:
    value = scalar(row, *names, default=default)
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def floatv(row: dict[str, Any], *names: str, default: float = 0.0) -> float:
    value = scalar(row, *names, default=default)
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def object_type(con: sqlite3.Connection, name: str) -> str | None:
    found = con.execute(
        "SELECT type FROM sqlite_master WHERE name = ? AND type IN ('table','view')",
        (name,),
    ).fetchone()
    return found["type"] if found else None


def columns(con: sqlite3.Connection, table: str) -> list[str]:
    if object_type(con, table) not in {"table", "view"}:
        return []
    return [row["name"] for row in con.execute(f"PRAGMA table_info({q(table)})")]


def row_count(con: sqlite3.Connection, table: str) -> int:
    if object_type(con, table) != "table":
        return 0
    try:
        return int(con.execute(f"SELECT COUNT(*) AS c FROM {q(table)}").fetchone()["c"] or 0)
    except sqlite3.Error:
        return 0


def choose_source(con: sqlite3.Connection, logical_name: str) -> str | None:
    candidates = LOGICAL_SOURCES.get(logical_name, [logical_name])
    existing = [(name, row_count(con, name)) for name in candidates if object_type(con, name) == "table"]
    if not existing:
        return None
    # Prefer the table with more rows. Ties prefer the first candidate, which is the new name.
    return sorted(existing, key=lambda item: item[1], reverse=True)[0][0]


def fetch_rows(con: sqlite3.Connection, table: str | None) -> list[dict[str, Any]]:
    if not table:
        return []
    return [dict(row) for row in con.execute(f"SELECT * FROM {q(table)}")]


def insert_many(con: sqlite3.Connection, table: str, cols: list[str], rows: list[tuple[Any, ...]]) -> int:
    if not rows:
        return 0
    placeholders = ",".join("?" for _ in cols)
    col_sql = ",".join(q(col) for col in cols)
    cur = con.executemany(
        f"INSERT OR IGNORE INTO {q(table)} ({col_sql}) VALUES ({placeholders})",
        rows,
    )
    return int(cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0)


def load_clean_schema(dst: sqlite3.Connection) -> None:
    dst.executescript(TARGET_DDL)


def copy_calendar(src: sqlite3.Connection, dst: sqlite3.Connection) -> int:
    rows = []
    for r in fetch_rows(src, choose_source(src, "calendar_days")):
        work_date = textv(r, "work_date")
        if not work_date:
            continue
        rows.append((work_date, intv(r, "is_working_day", default=1), textv(r, "note")))
    return insert_many(dst, "calendar_days", ["work_date", "is_working_day", "note"], rows)


def copy_data_import_log(src: sqlite3.Connection, dst: sqlite3.Connection) -> int:
    out = []
    for r in fetch_rows(src, choose_source(src, "data_import_log")):
        out.append((
            intv(r, "log_id"),
            textv(r, "import_type", default="legacy"),
            textv(r, "workbook_name"),
            textv(r, "active_sheet_name"),
            textv(r, "status", default="SUCCESS"),
            textv(r, "message"),
            textv(r, "created_at"),
            textv(r, "completed_at"),
        ))
    return insert_many(dst, "data_import_log", ["log_id","import_type","workbook_name","active_sheet_name","status","message","created_at","completed_at"], out)


def copy_machines(src: sqlite3.Connection, dst: sqlite3.Connection) -> set[int]:
    out = []
    for r in fetch_rows(src, choose_source(src, "machines")):
        machine_id = intv(r, "machine_id")
        code = textv(r, "machine_code")
        cat = textv(r, "machine_category")
        if not machine_id or not code or not cat:
            continue
        out.append((machine_id, code, cat, textv(r, "shift_profile", default="STANDARD"), intv(r, "active", default=1), textv(r, "notes"), textv(r, "created_at"), textv(r, "updated_at")))
    insert_many(dst, "machines", ["machine_id","machine_code","machine_category","shift_profile","active","notes","created_at","updated_at"], out)
    return {row["machine_id"] for row in dst.execute("SELECT machine_id FROM machines")}


def copy_parts(src: sqlite3.Connection, dst: sqlite3.Connection) -> set[int]:
    out = []
    for r in fetch_rows(src, choose_source(src, "parts")):
        part_id = intv(r, "part_id")
        part_no = textv(r, "part_no", "part_name")
        if not part_id:
            continue
        if not part_no:
            part_no = f"PART-{part_id}"
        out.append((part_id, part_no, textv(r, "part_desc"), textv(r, "created_at"), textv(r, "updated_at")))
    insert_many(dst, "parts", ["part_id","part_no","part_desc","created_at","updated_at"], out)
    return {row["part_id"] for row in dst.execute("SELECT part_id FROM parts")}


def copy_bom_variation(src: sqlite3.Connection, dst: sqlite3.Connection, part_ids: set[int]) -> set[int]:
    out = []
    for r in fetch_rows(src, choose_source(src, "bom_variation")):
        bom_id = intv(r, "bom_id", "flow_id")
        part_id = intv(r, "part_id")
        if not bom_id or part_id not in part_ids:
            continue
        out.append((bom_id, part_id, textv(r, "bom_code", "flow_code"), textv(r, "bom_desc", "flow_name"), intv(r, "is_default"), textv(r, "created_at"), textv(r, "updated_at")))
    insert_many(dst, "bom_variation", ["bom_id","part_id","bom_code","bom_desc","is_default","created_at","updated_at"], out)
    return {row["bom_id"] for row in dst.execute("SELECT bom_id FROM bom_variation")}


def copy_operation_seq(src: sqlite3.Connection, dst: sqlite3.Connection, bom_ids: set[int]) -> set[int]:
    out = []
    for r in fetch_rows(src, choose_source(src, "operation_seq")):
        op_seq_id = intv(r, "op_seq_id", "step_id")
        bom_id = intv(r, "bom_id", "flow_id")
        if not op_seq_id or bom_id not in bom_ids:
            continue
        out.append((
            op_seq_id,
            bom_id,
            intv(r, "seq_no", "seq", default=0),
            textv(r, "op_no"),
            textv(r, "op_type"),
            textv(r, "machine_category", default="UNKNOWN"),
            floatv(r, "cycle_time", default=1.0),
            floatv(r, "setup_time", default=0.0),
            textv(r, "preferred_machine"),
            intv(r, "is_last_op", default=0),
        ))
    insert_many(dst, "operation_seq", ["op_seq_id","bom_id","seq_no","op_no","op_type","machine_category","cycle_time","setup_time","preferred_machine","is_last_op"], out)
    return {row["op_seq_id"] for row in dst.execute("SELECT op_seq_id FROM operation_seq")}


def split_ps_key(ps_id: str) -> tuple[str, str]:
    if "::" in ps_id:
        base, partial = ps_id.rsplit("::", 1)
        return base or ps_id, partial or "1"
    return ps_id, "1"


def copy_process_sheet(src: sqlite3.Connection, dst: sqlite3.Connection, part_ids: set[int], bom_ids: set[int]) -> set[str]:
    out = []
    for r in fetch_rows(src, choose_source(src, "process_sheet")):
        ps_id = textv(r, "ps_id")
        if not ps_id:
            continue
        part_id = intv(r, "part_id")
        if not part_id or part_id not in part_ids:
            part_id = None
        selected_bom_id = intv(r, "selected_bom_id", "selected_flow_id", default=0)
        if not selected_bom_id or selected_bom_id not in bom_ids:
            selected_bom_id = None
        source_ps_id = textv(r, "source_ps_id")
        pp_partial_no = textv(r, "pp_partial_no")
        if not source_ps_id or not pp_partial_no:
            source_ps_id, fallback_partial = split_ps_key(ps_id)
            pp_partial_no = pp_partial_no or fallback_partial
        out.append((
            ps_id,
            part_id,
            textv(r, "part_no", "inv_code"),
            textv(r, "part_desc", "inv_desc"),
            textv(r, "order_date"),
            textv(r, "due_date"),
            floatv(r, "total_qty"),
            floatv(r, "planned_qty"),
            floatv(r, "finished_qty"),
            selected_bom_id,
            textv(r, "planner_status", default="UNPLANNED"),
            textv(r, "status", default="ACTIVE"),
            textv(r, "created_at"),
            textv(r, "updated_at"),
            source_ps_id,
            pp_partial_no or "1",
        ))
    insert_many(dst, "process_sheet", ["ps_id","part_id","part_no","part_desc","order_date","due_date","total_qty","planned_qty","finished_qty","selected_bom_id","planner_status","status","created_at","updated_at","source_ps_id","pp_partial_no"], out)
    return {row["ps_id"] for row in dst.execute("SELECT ps_id FROM process_sheet")}


def copy_process_sheet_material(src: sqlite3.Connection, dst: sqlite3.Connection, ps_ids: set[str]) -> set[int]:
    out = []
    for r in fetch_rows(src, choose_source(src, "process_sheet_material")):
        mat_id = intv(r, "mat_id")
        ps_id = textv(r, "ps_id")
        if not mat_id or ps_id not in ps_ids:
            continue
        out.append((mat_id, ps_id, textv(r,"material_name"), intv(r,"material_ready"), floatv(r,"material_ready_qty"), textv(r,"need_by_date"), textv(r,"order_status", default="TO_ORDER"), textv(r,"planner_note"), textv(r,"created_at"), textv(r,"updated_at")))
    insert_many(dst, "process_sheet_material", ["mat_id","ps_id","material_name","material_ready","material_ready_qty","need_by_date","order_status","planner_note","created_at","updated_at"], out)
    return {row["mat_id"] for row in dst.execute("SELECT mat_id FROM process_sheet_material")}


def copy_process_sheet_material_order_log(src: sqlite3.Connection, dst: sqlite3.Connection, ps_ids: set[str], mat_ids: set[int]) -> int:
    out = []
    for r in fetch_rows(src, choose_source(src, "process_sheet_material_order_log")):
        log_id = intv(r, "log_id")
        mat_id = intv(r, "mat_id")
        ps_id = textv(r, "ps_id")
        if not log_id or mat_id not in mat_ids or ps_id not in ps_ids:
            continue
        out.append((log_id, mat_id, ps_id, floatv(r,"ordered_qty"), floatv(r,"received_qty"), textv(r,"order_date"), textv(r,"expected_date"), textv(r,"received_date"), textv(r,"log_status", default="PENDING"), textv(r,"note"), textv(r,"created_at")))
    return insert_many(dst, "process_sheet_material_order_log", ["log_id","mat_id","ps_id","ordered_qty","received_qty","order_date","expected_date","received_date","log_status","note","created_at"], out)


def copy_staff(src: sqlite3.Connection, dst: sqlite3.Connection) -> int:
    out = []
    for r in fetch_rows(src, choose_source(src, "staff")):
        staff_id = intv(r, "staff_id")
        name = textv(r, "staff_name")
        if not staff_id or not name:
            continue
        out.append((staff_id, name, textv(r,"role", default="MACHINIST"), intv(r,"active", default=1), textv(r,"created_at"), textv(r,"updated_at")))
    return insert_many(dst, "staff", ["staff_id","staff_name","role","active","created_at","updated_at"], out)


def copy_bom_material(src: sqlite3.Connection, dst: sqlite3.Connection) -> int:
    out = []
    for r in fetch_rows(src, choose_source(src, "bom_material")):
        bom_material_id = intv(r, "bom_material_id")
        if not bom_material_id:
            continue
        out.append((bom_material_id, textv(r,"source_inventory_code"), textv(r,"bom_code"), textv(r,"material_inventory_code"), textv(r,"material_description"), textv(r,"created_at"), textv(r,"updated_at")))
    return insert_many(dst, "bom_material", ["bom_material_id","source_inventory_code","bom_code","material_inventory_code","material_description","created_at","updated_at"], out)


def copy_capacity_profile(src: sqlite3.Connection, dst: sqlite3.Connection) -> set[int]:
    out = []
    for r in fetch_rows(src, choose_source(src, "capacity_profile")):
        profile_id = intv(r, "profile_id")
        name = textv(r, "profile_name")
        if not profile_id or not name:
            continue
        out.append((profile_id, name, intv(r,"capacity_minutes"), intv(r,"start_minute", default=510), textv(r,"note")))
    insert_many(dst, "capacity_profile", ["profile_id","profile_name","capacity_minutes","start_minute","note"], out)
    return {row["profile_id"] for row in dst.execute("SELECT profile_id FROM capacity_profile")}


def copy_machine_capacity_day(src: sqlite3.Connection, dst: sqlite3.Connection, machine_ids: set[int], profile_ids: set[int]) -> int:
    out = []
    for r in fetch_rows(src, choose_source(src, "machine_capacity_day")):
        day_id = intv(r, "day_id")
        machine_id = intv(r, "machine_id")
        profile_id = intv(r, "profile_id")
        work_date = textv(r, "work_date")
        if not day_id or machine_id not in machine_ids or profile_id not in profile_ids or not work_date:
            continue
        out.append((day_id, machine_id, work_date, profile_id, intv(r,"capacity_minutes"), intv(r,"start_minute", default=510), textv(r,"note"), textv(r,"created_at"), textv(r,"updated_at")))
    return insert_many(dst, "machine_capacity_day", ["day_id","machine_id","work_date","profile_id","capacity_minutes","start_minute","note","created_at","updated_at"], out)


def copy_material_requirement(src: sqlite3.Connection, dst: sqlite3.Connection, ps_ids: set[str]) -> int:
    out = []
    for r in fetch_rows(src, choose_source(src, "material_requirement")):
        requirement_id = intv(r, "requirement_id")
        ps_id = textv(r, "ps_id")
        if not requirement_id or ps_id not in ps_ids:
            continue
        out.append((requirement_id, ps_id, textv(r,"source_inventory_code"), textv(r,"bom_code"), textv(r,"material_inventory_code"), textv(r,"material_description"), floatv(r,"material_qty_needed"), textv(r,"material_uom"), textv(r,"supply_status", default="PENDING_CONFIRMATION"), textv(r,"expected_ready_date"), textv(r,"supplier_ref"), textv(r,"remarks"), textv(r,"updated_at"), textv(r,"created_at")))
    return insert_many(dst, "material_requirement", ["requirement_id","ps_id","source_inventory_code","bom_code","material_inventory_code","material_description","material_qty_needed","material_uom","supply_status","expected_ready_date","supplier_ref","remarks","updated_at","created_at"], out)


def copy_operation(src: sqlite3.Connection, dst: sqlite3.Connection) -> set[int]:
    out = []
    for r in fetch_rows(src, choose_source(src, "operation")):
        operation_id = intv(r, "operation_id")
        if not operation_id:
            continue
        out.append((operation_id, textv(r,"job_no"), textv(r,"operation_name"), floatv(r,"total_qty"), floatv(r,"setup_minutes"), floatv(r,"cycle_minutes_per_qty"), textv(r,"compatible_machine_group"), textv(r,"source_ps_id"), intv(r,"source_op_seq_id","source_step_id"), textv(r,"source_op_no"), textv(r,"status", default="ACTIVE"), textv(r,"remarks"), textv(r,"created_at"), textv(r,"updated_at")))
    insert_many(dst, "operation", ["operation_id","job_no","operation_name","total_qty","setup_minutes","cycle_minutes_per_qty","compatible_machine_group","source_ps_id","source_op_seq_id","source_op_no","status","remarks","created_at","updated_at"], out)
    return {row["operation_id"] for row in dst.execute("SELECT operation_id FROM operation")}


def copy_run_block_group(src: sqlite3.Connection, dst: sqlite3.Connection) -> set[int]:
    out = []
    for r in fetch_rows(src, choose_source(src, "run_block_group")):
        group_id = intv(r, "group_id")
        if not group_id:
            continue
        out.append((group_id, textv(r,"group_label"), textv(r,"group_type", default="COMBINED"), textv(r,"created_at")))
    insert_many(dst, "run_block_group", ["group_id","group_label","group_type","created_at"], out)
    return {row["group_id"] for row in dst.execute("SELECT group_id FROM run_block_group")}


def copy_run_block(src: sqlite3.Connection, dst: sqlite3.Connection, operation_ids: set[int], machine_ids: set[int], group_ids: set[int]) -> set[int]:
    out = []
    for r in fetch_rows(src, choose_source(src, "run_block")):
        block_id = intv(r, "block_id")
        operation_id = intv(r, "operation_id")
        machine_id = intv(r, "machine_id")
        if not block_id or operation_id not in operation_ids or machine_id not in machine_ids:
            continue
        group_id = intv(r, "group_id", default=0)
        if not group_id or group_id not in group_ids:
            group_id = None
        out.append((block_id, operation_id, machine_id, intv(r,"queue_position"), floatv(r,"scheduled_qty"), intv(r,"include_setup", default=1), textv(r,"status", default="PLANNED"), textv(r,"anchor_datetime"), textv(r,"calculated_start_datetime"), textv(r,"calculated_end_datetime"), floatv(r,"actual_good_qty"), floatv(r,"actual_reject_qty"), textv(r,"remarks"), textv(r,"created_at"), textv(r,"updated_at"), textv(r,"block_type", default="ORIGINAL"), scalar(r,"source_reject_block_id"), scalar(r,"source_reject_segment_id"), textv(r,"planning_status", default="UNPLANNED"), textv(r,"execution_status", default="NOT_STARTED"), textv(r,"anchor_status", default="NONE"), floatv(r,"anchor_miss_minutes"), group_id))
    insert_many(dst, "run_block", ["block_id","operation_id","machine_id","queue_position","scheduled_qty","include_setup","status","anchor_datetime","calculated_start_datetime","calculated_end_datetime","actual_good_qty","actual_reject_qty","remarks","created_at","updated_at","block_type","source_reject_block_id","source_reject_segment_id","planning_status","execution_status","anchor_status","anchor_miss_minutes","group_id"], out)
    return {row["block_id"] for row in dst.execute("SELECT block_id FROM run_block")}


def copy_planning_card(src: sqlite3.Connection, dst: sqlite3.Connection, ps_ids: set[str], machine_ids: set[int], group_ids: set[int]) -> set[int]:
    out = []
    for r in fetch_rows(src, choose_source(src, "planning_card")):
        card_id = intv(r, "card_id")
        ps_id = textv(r, "ps_id")
        if not card_id or ps_id not in ps_ids:
            continue
        machine_id = intv(r, "machine_id", default=0) or None
        if machine_id and machine_id not in machine_ids:
            machine_id = None
        group_id = intv(r, "scheduled_block_group_id", default=0) or None
        if group_id and group_id not in group_ids:
            group_id = None
        out.append((card_id, ps_id, textv(r,"operation_label"), floatv(r,"target_qty"), textv(r,"planning_status", default="UNSCHEDULED"), textv(r,"card_type", default="NORMAL"), machine_id, group_id, textv(r,"created_at"), textv(r,"updated_at")))
    insert_many(dst, "planning_card", ["card_id","ps_id","operation_label","target_qty","planning_status","card_type","machine_id","scheduled_block_group_id","created_at","updated_at"], out)
    return {row["card_id"] for row in dst.execute("SELECT card_id FROM planning_card")}


def copy_planning_card_operation(src: sqlite3.Connection, dst: sqlite3.Connection, card_ids: set[int]) -> int:
    out = []
    for r in fetch_rows(src, choose_source(src, "planning_card_operation")):
        card_op_id = intv(r, "card_op_id")
        card_id = intv(r, "card_id")
        if not card_op_id or card_id not in card_ids:
            continue
        out.append((card_op_id, card_id, textv(r,"source_ps_id"), intv(r,"source_op_seq_id","source_step_id"), textv(r,"source_op_no"), intv(r,"op_sequence"), floatv(r,"setup_minutes"), floatv(r,"cycle_minutes_per_qty"), floatv(r,"target_qty")))
    return insert_many(dst, "planning_card_operation", ["card_op_id","card_id","source_ps_id","source_op_seq_id","source_op_no","op_sequence","setup_minutes","cycle_minutes_per_qty","target_qty"], out)


def copy_run_block_segment(src: sqlite3.Connection, dst: sqlite3.Connection, block_ids: set[int], machine_ids: set[int]) -> set[int]:
    out = []
    for r in fetch_rows(src, choose_source(src, "run_block_segment")):
        segment_id = intv(r, "segment_id")
        block_id = intv(r, "block_id")
        machine_id = intv(r, "machine_id")
        if not segment_id or block_id not in block_ids or machine_id not in machine_ids:
            continue
        start_dt = textv(r, "start_datetime")
        end_dt = textv(r, "end_datetime")
        segment_date = textv(r, "segment_date")
        segment_type = textv(r, "segment_type")
        if not start_dt or not end_dt or not segment_date or not segment_type:
            continue
        out.append((segment_id, block_id, machine_id, segment_date, segment_type, floatv(r,"qty_done"), floatv(r,"minutes_used"), start_dt, end_dt, intv(r,"is_actual"), textv(r,"created_at")))
    insert_many(dst, "run_block_segment", ["segment_id","block_id","machine_id","segment_date","segment_type","qty_done","minutes_used","start_datetime","end_datetime","is_actual","created_at"], out)
    return {row["segment_id"] for row in dst.execute("SELECT segment_id FROM run_block_segment")}


def copy_production_actual(src: sqlite3.Connection, dst: sqlite3.Connection, block_ids: set[int], segment_ids: set[int]) -> int:
    out = []
    for r in fetch_rows(src, choose_source(src, "production_actual")):
        actual_id = intv(r, "actual_id")
        block_id = intv(r, "block_id")
        report_date = textv(r, "report_date")
        if not actual_id or block_id not in block_ids or not report_date:
            continue
        segment_id = intv(r, "segment_id", default=0) or None
        if segment_id and segment_id not in segment_ids:
            segment_id = None
        output = scalar(r, "output_qty")
        if output is None:
            output = scalar(r, "actual_good_qty")
        reject = scalar(r, "reject_qty")
        if reject is None:
            reject = scalar(r, "actual_reject_qty")
        out.append((actual_id, block_id, report_date, textv(r,"remarks"), textv(r,"reported_at"), segment_id, output, reject, scalar(r,"target_qty_at_report")))
    return insert_many(dst, "production_actual", ["actual_id","block_id","report_date","remarks","reported_at","segment_id","output_qty","reject_qty","target_qty_at_report"], out)


def copy_public_holiday(src: sqlite3.Connection, dst: sqlite3.Connection) -> int:
    out = []
    for r in fetch_rows(src, choose_source(src, "public_holiday")):
        holiday_date = textv(r, "holiday_date")
        if not holiday_date:
            continue
        out.append((holiday_date, textv(r,"note"), textv(r,"created_at"), textv(r,"updated_at")))
    return insert_many(dst, "public_holiday", ["holiday_date","note","created_at","updated_at"], out)


def current_counts(con: sqlite3.Connection) -> dict[str, int]:
    return {
        name: row_count(con, name)
        for name in sorted(name for name, in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
    }


def logical_plan(src: sqlite3.Connection) -> list[tuple[str, str | None, int]]:
    plan = []
    for logical in LOGICAL_SOURCES:
        src_name = choose_source(src, logical)
        plan.append((logical, src_name, row_count(src, src_name) if src_name else 0))
    return plan


def rebuild(src_path: Path, out_path: Path, execute: bool) -> None:
    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row

    print(f"Source DB: {src_path}")
    print(f"Output DB: {out_path}")
    print(f"Mode: {'EXECUTE' if execute else 'DRY RUN'}")
    print("\nChosen source tables:")
    for logical, src_name, count in logical_plan(src):
        print(f"  - {logical:<34} <- {src_name or '(missing)':<34} rows={count}")

    print("\nSource row counts:")
    for name, count in current_counts(src).items():
        print(f"  - {name}: {count}")

    if not execute:
        print("\nDry run only. Re-run with --execute to build the clean DB.")
        src.close()
        return

    if out_path.exists():
        out_path.unlink()

    dst = sqlite3.connect(out_path)
    dst.row_factory = sqlite3.Row
    try:
        dst.execute("PRAGMA foreign_keys = OFF")
        load_clean_schema(dst)
        # TARGET_DDL ends with foreign_keys ON; keep checks off while loading and validate at end.
        dst.execute("PRAGMA foreign_keys = OFF")

        print("\nCopying data into clean schema:")
        copied = {}
        copied["calendar_days"] = copy_calendar(src, dst)
        copied["data_import_log"] = copy_data_import_log(src, dst)
        machine_ids = copy_machines(src, dst); copied["machines"] = len(machine_ids)
        part_ids = copy_parts(src, dst); copied["parts"] = len(part_ids)
        bom_ids = copy_bom_variation(src, dst, part_ids); copied["bom_variation"] = len(bom_ids)
        op_seq_ids = copy_operation_seq(src, dst, bom_ids); copied["operation_seq"] = len(op_seq_ids)
        ps_ids = copy_process_sheet(src, dst, part_ids, bom_ids); copied["process_sheet"] = len(ps_ids)
        mat_ids = copy_process_sheet_material(src, dst, ps_ids); copied["process_sheet_material"] = len(mat_ids)
        copied["process_sheet_material_order_log"] = copy_process_sheet_material_order_log(src, dst, ps_ids, mat_ids)
        copied["staff"] = copy_staff(src, dst)
        copied["bom_material"] = copy_bom_material(src, dst)
        profile_ids = copy_capacity_profile(src, dst); copied["capacity_profile"] = len(profile_ids)
        copied["machine_capacity_day"] = copy_machine_capacity_day(src, dst, machine_ids, profile_ids)
        copied["material_requirement"] = copy_material_requirement(src, dst, ps_ids)
        operation_ids = copy_operation(src, dst); copied["operation"] = len(operation_ids)
        group_ids = copy_run_block_group(src, dst); copied["run_block_group"] = len(group_ids)
        block_ids = copy_run_block(src, dst, operation_ids, machine_ids, group_ids); copied["run_block"] = len(block_ids)
        card_ids = copy_planning_card(src, dst, ps_ids, machine_ids, group_ids); copied["planning_card"] = len(card_ids)
        copied["planning_card_operation"] = copy_planning_card_operation(src, dst, card_ids)
        segment_ids = copy_run_block_segment(src, dst, block_ids, machine_ids); copied["run_block_segment"] = len(segment_ids)
        copied["production_actual"] = copy_production_actual(src, dst, block_ids, segment_ids)
        copied["public_holiday"] = copy_public_holiday(src, dst)

        for name, count in copied.items():
            print(f"  - {name}: {count}")

        # Recalculate cached block actual totals from production_actual.
        dst.execute(
            """
            UPDATE run_block
            SET actual_good_qty = COALESCE((
                    SELECT SUM(COALESCE(output_qty, 0))
                    FROM production_actual
                    WHERE production_actual.block_id = run_block.block_id
                ), 0),
                actual_reject_qty = COALESCE((
                    SELECT SUM(COALESCE(reject_qty, 0))
                    FROM production_actual
                    WHERE production_actual.block_id = run_block.block_id
                ), 0)
            """
        )

        dst.commit()
        dst.execute("PRAGMA foreign_keys = ON")
        violations = list(dst.execute("PRAGMA foreign_key_check"))
        if violations:
            print("\nforeign_key_check violations:")
            for v in violations:
                print(f"  - {tuple(v)}")
            raise RuntimeError(f"foreign_key_check failed with {len(violations)} violation(s)")

        dst.execute("VACUUM")
        dst.commit()

        print("\nforeign_key_check passed.")
        print("VACUUM completed.")
        print("\nClean DB row counts:")
        for name, count in current_counts(dst).items():
            print(f"  - {name}: {count}")

    finally:
        dst.close()
        src.close()


def replace_original(db_path: Path, clean_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = db_path.with_name(f"{db_path.name}.backup-before-clean-rebuild-{timestamp}")
    shutil.copy2(db_path, backup_path)
    os.replace(clean_path, db_path)
    return backup_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild messy planner.db into the clean target schema.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Input SQLite DB. Default: planner.db")
    parser.add_argument("--output", type=Path, default=None, help="Write clean DB here. If omitted with --replace, overwrites --db after backup.")
    parser.add_argument("--execute", action="store_true", help="Actually write the clean DB. Default is dry-run.")
    parser.add_argument("--replace", action="store_true", help="Replace --db with the clean DB after creating a backup.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = args.db.expanduser().resolve()
    if not db_path.exists():
        raise SystemExit(f"Input DB not found: {db_path}")

    if args.replace and args.output:
        raise SystemExit("Use either --replace or --output, not both.")

    if args.replace:
        with tempfile.NamedTemporaryFile(prefix="trial-clean-", suffix=".db", delete=False) as tmp:
            clean_path = Path(tmp.name)
    elif args.output:
        clean_path = args.output.expanduser().resolve()
    else:
        clean_path = db_path.with_name(f"{db_path.stem}.clean.db")

    rebuild(db_path, clean_path, execute=args.execute)

    if args.execute and args.replace:
        backup_path = replace_original(db_path, clean_path)
        print(f"\nBackup created: {backup_path}")
        print(f"Replaced original DB: {db_path}")

    elif args.execute:
        print(f"\nClean DB written: {clean_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
