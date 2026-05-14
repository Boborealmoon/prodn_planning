from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from .constants import CAPACITY_PROFILES, TRIAL_MACHINES

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("SCHEDULER_DB_PATH", ROOT / "planner.db"))


class RowMap(dict):
    __slots__ = ("_values",)

    def __init__(self, keys, values):
        super().__init__(zip(keys, values))
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        return dict.get(self, key, default)


def row_factory(cursor, row):
    return RowMap([col[0] for col in cursor.description], row)


def one(cursor):
    return cursor.fetchone()


def rows(cursor):
    return cursor.fetchall()


def dt_now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def date_text(d: date):
    return d.strftime("%Y-%m-%d")


def parse_dt_text(value):
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("T", " ")
    try:
        return datetime.fromisoformat(text[:19])
    except ValueError:
        return None


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = row_factory
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def table_columns(con, table_name):
    try:
        return {row["name"] for row in rows(con.execute(f"PRAGMA table_info({table_name})"))}
    except sqlite3.Error:
        return set()


def table_exists(con, table_name):
    row = one(
        con.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (table_name,),
        )
    )
    return row is not None


def column_exists(con, table_name, column_name):
    try:
        return column_name in table_columns(con, table_name)
    except sqlite3.Error:
        return False


def ensure_column(con, table_name, column_name, column_sql):
    if not table_exists(con, table_name):
        return False
    if column_exists(con, table_name, column_name):
        return False
    con.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}")
    return True


def ensure_index(con, index_name, sql):
    row = one(
        con.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'index' AND name = ?
            """,
            (index_name,),
        )
    )
    if row is None:
        con.execute(sql)


def view_exists(con, view_name):
    row = one(
        con.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'view' AND name = ?
            """,
            (view_name,),
        )
    )
    return row is not None


def _create_view(con, view_name, sql):
    if view_exists(con, view_name):
        return
    con.execute(sql)


def _ensure_state_rows(con):
    if table_exists(con, "operation") and table_exists(con, "process_sheet_operation_state"):
        con.execute(
            """
            INSERT OR IGNORE INTO process_sheet_operation_state (
              operation_id, ps_id, output_qty, reject_qty, good_qty, remaining_qty,
              predicted_start_at, predicted_end_at, execution_status, updated_at
            )
            SELECT
              o.operation_id,
              COALESCE(o.source_ps_id, ''),
              0,
              0,
              0,
              COALESCE(o.total_qty, 0),
              NULL,
              NULL,
              'NOT_STARTED',
              CURRENT_TIMESTAMP
            FROM operation o
            """
        )
    if table_exists(con, "process_sheet") and table_exists(con, "process_sheet_state"):
        con.execute(
            """
            INSERT OR IGNORE INTO process_sheet_state (
              ps_id, predicted_start_at, predicted_end_at, output_qty, reject_qty, good_qty,
              remaining_qty, planner_status, execution_status, is_late, delay_minutes, updated_at
            )
            SELECT
              p.ps_id,
              NULL,
              NULL,
              0,
              0,
              0,
              COALESCE(p.total_qty, 0),
              COALESCE(p.planner_status, 'UNPLANNED'),
              'NOT_STARTED',
              0,
              0,
              CURRENT_TIMESTAMP
            FROM process_sheet p
            """
        )
    if table_exists(con, "run_block") and table_exists(con, "machine_queue_state"):
        con.execute(
            """
            INSERT OR IGNORE INTO machine_queue_state (
              block_id, schedule_run_id, predicted_start_at, predicted_end_at, remaining_qty,
              output_qty, reject_qty, good_qty, planned_minutes, schedule_status, execution_status,
              is_late, delay_minutes, updated_at
            )
            SELECT
              b.block_id,
              NULL,
              CASE WHEN COALESCE(b.planned_start_at, '') <> '' THEN b.planned_start_at ELSE NULL END,
              CASE WHEN COALESCE(b.planned_end_at, '') <> '' THEN b.planned_end_at ELSE NULL END,
              MAX(0, COALESCE(b.scheduled_qty, 0) - COALESCE(b.actual_good_qty, 0) + COALESCE(b.actual_reject_qty, 0)),
              COALESCE(b.actual_good_qty, 0) + COALESCE(b.actual_reject_qty, 0),
              COALESCE(b.actual_reject_qty, 0),
              COALESCE(b.actual_good_qty, 0),
              CASE
                WHEN COALESCE(b.calculated_end_datetime, '') <> '' AND COALESCE(b.calculated_start_datetime, '') <> ''
                THEN (JULIANDAY(b.calculated_end_datetime) - JULIANDAY(b.calculated_start_datetime)) * 24 * 60
                ELSE 0
              END,
              COALESCE(b.planning_status, 'UNSCHEDULED'),
              COALESCE(b.execution_status, 'NOT_STARTED'),
              0,
              0,
              CURRENT_TIMESTAMP
            FROM run_block b
            """
        )


def ensure_scheduler_schema(con):
    if table_exists(con, "machines"):
        ensure_index(
            con,
            "idx_machines_machine_code_unique",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_machines_machine_code_unique
            ON machines(machine_code)
            """,
        )
    if table_exists(con, "capacity_profile"):
        ensure_index(
            con,
            "idx_capacity_profile_profile_name_unique",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_capacity_profile_profile_name_unique
            ON capacity_profile(profile_name)
            """,
        )

    # schedule_run
    if not table_exists(con, "schedule_run"):
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS schedule_run (
              schedule_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
              reason TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'CURRENT',
              generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              scope_type TEXT NOT NULL DEFAULT 'FULL',
              machine_id INTEGER REFERENCES machines(machine_id) ON DELETE SET NULL,
              notes TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    ensure_index(
        con,
        "idx_schedule_run_status",
        """
        CREATE INDEX IF NOT EXISTS idx_schedule_run_status
        ON schedule_run(status)
        """,
    )
    ensure_index(
        con,
        "idx_schedule_run_machine_id",
        """
        CREATE INDEX IF NOT EXISTS idx_schedule_run_machine_id
        ON schedule_run(machine_id)
        """,
    )

    # machine_queue_state
    if not table_exists(con, "machine_queue_state"):
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_queue_state (
              block_id INTEGER PRIMARY KEY REFERENCES run_block(block_id) ON DELETE CASCADE,
              schedule_run_id INTEGER REFERENCES schedule_run(schedule_run_id) ON DELETE SET NULL,
              predicted_start_at TEXT,
              predicted_end_at TEXT,
              remaining_qty REAL NOT NULL DEFAULT 0,
              output_qty REAL NOT NULL DEFAULT 0,
              reject_qty REAL NOT NULL DEFAULT 0,
              good_qty REAL NOT NULL DEFAULT 0,
              planned_minutes REAL NOT NULL DEFAULT 0,
              schedule_status TEXT NOT NULL DEFAULT 'UNSCHEDULED',
              execution_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
              is_late INTEGER NOT NULL DEFAULT 0,
              delay_minutes REAL NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    ensure_index(
        con,
        "idx_machine_queue_state_schedule_run_id",
        """
        CREATE INDEX IF NOT EXISTS idx_machine_queue_state_schedule_run_id
        ON machine_queue_state(schedule_run_id)
        """,
    )
    ensure_index(
        con,
        "idx_machine_queue_state_status_execution",
        """
        CREATE INDEX IF NOT EXISTS idx_machine_queue_state_status_execution
        ON machine_queue_state(schedule_status, execution_status)
        """,
    )

    # process_sheet_operation_state
    if not table_exists(con, "process_sheet_operation_state"):
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS process_sheet_operation_state (
              operation_id INTEGER PRIMARY KEY REFERENCES operation(operation_id) ON DELETE CASCADE,
              ps_id TEXT NOT NULL DEFAULT '',
              output_qty REAL NOT NULL DEFAULT 0,
              reject_qty REAL NOT NULL DEFAULT 0,
              good_qty REAL NOT NULL DEFAULT 0,
              remaining_qty REAL NOT NULL DEFAULT 0,
              predicted_start_at TEXT,
              predicted_end_at TEXT,
              execution_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    ensure_index(
        con,
        "idx_process_sheet_operation_state_ps_id",
        """
        CREATE INDEX IF NOT EXISTS idx_process_sheet_operation_state_ps_id
        ON process_sheet_operation_state(ps_id)
        """,
    )
    ensure_index(
        con,
        "idx_process_sheet_operation_state_execution_status",
        """
        CREATE INDEX IF NOT EXISTS idx_process_sheet_operation_state_execution_status
        ON process_sheet_operation_state(execution_status)
        """,
    )

    # process_sheet_state
    if not table_exists(con, "process_sheet_state"):
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS process_sheet_state (
              ps_id TEXT PRIMARY KEY REFERENCES process_sheet(ps_id) ON DELETE CASCADE,
              predicted_start_at TEXT,
              predicted_end_at TEXT,
              output_qty REAL NOT NULL DEFAULT 0,
              reject_qty REAL NOT NULL DEFAULT 0,
              good_qty REAL NOT NULL DEFAULT 0,
              remaining_qty REAL NOT NULL DEFAULT 0,
              planner_status TEXT NOT NULL DEFAULT 'UNPLANNED',
              execution_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
              is_late INTEGER NOT NULL DEFAULT 0,
              delay_minutes REAL NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    ensure_index(
        con,
        "idx_process_sheet_state_planner_execution",
        """
        CREATE INDEX IF NOT EXISTS idx_process_sheet_state_planner_execution
        ON process_sheet_state(planner_status, execution_status)
        """,
    )
    ensure_index(
        con,
        "idx_process_sheet_state_is_late",
        """
        CREATE INDEX IF NOT EXISTS idx_process_sheet_state_is_late
        ON process_sheet_state(is_late)
        """,
    )

    # schedule_alert
    if not table_exists(con, "schedule_alert"):
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS schedule_alert (
              alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
              schedule_run_id INTEGER REFERENCES schedule_run(schedule_run_id) ON DELETE SET NULL,
              block_id INTEGER REFERENCES run_block(block_id) ON DELETE CASCADE,
              operation_id INTEGER REFERENCES operation(operation_id) ON DELETE SET NULL,
              ps_id TEXT REFERENCES process_sheet(ps_id) ON DELETE CASCADE,
              machine_id INTEGER REFERENCES machines(machine_id) ON DELETE SET NULL,
              alert_type TEXT NOT NULL,
              severity TEXT NOT NULL DEFAULT 'INFO',
              message TEXT NOT NULL DEFAULT '',
              old_value TEXT NOT NULL DEFAULT '',
              new_value TEXT NOT NULL DEFAULT '',
              planned_at TEXT,
              predicted_at TEXT,
              delay_minutes REAL NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'OPEN',
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              resolved_at TEXT
            )
            """
        )
    ensure_index(
        con,
        "idx_schedule_alert_status_severity",
        """
        CREATE INDEX IF NOT EXISTS idx_schedule_alert_status_severity
        ON schedule_alert(status, severity)
        """,
    )
    ensure_index(
        con,
        "idx_schedule_alert_block_id",
        """
        CREATE INDEX IF NOT EXISTS idx_schedule_alert_block_id
        ON schedule_alert(block_id)
        """,
    )
    ensure_index(
        con,
        "idx_schedule_alert_ps_id",
        """
        CREATE INDEX IF NOT EXISTS idx_schedule_alert_ps_id
        ON schedule_alert(ps_id)
        """,
    )
    ensure_index(
        con,
        "idx_schedule_alert_machine_id",
        """
        CREATE INDEX IF NOT EXISTS idx_schedule_alert_machine_id
        ON schedule_alert(machine_id)
        """,
    )
    ensure_index(
        con,
        "idx_schedule_alert_schedule_run_id",
        """
        CREATE INDEX IF NOT EXISTS idx_schedule_alert_schedule_run_id
        ON schedule_alert(schedule_run_id)
        """,
    )
    ensure_index(
        con,
        "uq_schedule_alert_open_block_type",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_schedule_alert_open_block_type
        ON schedule_alert(block_id, alert_type)
        WHERE status IN ('OPEN', 'ACKNOWLEDGED')
        """,
    )

    # machine_calendar_window
    # Capacity overlay rows: they are not queue items and are consumed by the scheduler as
    # availability/blocked-time windows on top of the base machine/day capacity.
    if not table_exists(con, "machine_calendar_window"):
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_calendar_window (
              window_id INTEGER PRIMARY KEY AUTOINCREMENT,
              machine_id INTEGER REFERENCES machines(machine_id) ON DELETE CASCADE,
              start_at TEXT NOT NULL,
              end_at TEXT NOT NULL,
              window_type TEXT NOT NULL,
              capacity_minutes INTEGER NOT NULL DEFAULT 0,
              note TEXT NOT NULL DEFAULT '',
              active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    ensure_index(
        con,
        "idx_machine_calendar_window_machine_start_end",
        """
        CREATE INDEX IF NOT EXISTS idx_machine_calendar_window_machine_start_end
        ON machine_calendar_window(machine_id, start_at, end_at)
        """,
    )
    ensure_index(
        con,
        "idx_machine_calendar_window_active_type",
        """
        CREATE INDEX IF NOT EXISTS idx_machine_calendar_window_active_type
        ON machine_calendar_window(active, window_type)
        """,
    )

    # rework_link
    if not table_exists(con, "rework_link"):
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS rework_link (
              rework_link_id INTEGER PRIMARY KEY AUTOINCREMENT,
              source_actual_id INTEGER REFERENCES production_actual(actual_id) ON DELETE SET NULL,
              source_block_id INTEGER REFERENCES run_block(block_id) ON DELETE SET NULL,
              rework_block_id INTEGER NOT NULL REFERENCES run_block(block_id) ON DELETE CASCADE,
              reject_qty REAL NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    ensure_index(
        con,
        "uq_rework_link_rework_block_id",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_rework_link_rework_block_id
        ON rework_link(rework_block_id)
        """,
    )

    # Existing table compatibility columns
    ensure_column(con, "run_block", "planned_start_at", "planned_start_at TEXT")
    ensure_column(con, "run_block", "planned_end_at", "planned_end_at TEXT")
    ensure_column(con, "run_block", "allow_pull_forward", "allow_pull_forward INTEGER NOT NULL DEFAULT 1")
    ensure_column(con, "run_block", "active", "active INTEGER NOT NULL DEFAULT 1")
    ensure_column(con, "run_block", "is_fresh_monday_item", "is_fresh_monday_item INTEGER NOT NULL DEFAULT 0")
    ensure_column(con, "run_block", "last_schedule_run_id", "last_schedule_run_id INTEGER REFERENCES schedule_run(schedule_run_id) ON DELETE SET NULL")
    ensure_column(con, "run_block", "planned_qty_original", "planned_qty_original REAL NOT NULL DEFAULT 0")
    ensure_column(con, "run_block", "split_from_block_id", "split_from_block_id INTEGER REFERENCES run_block(block_id) ON DELETE SET NULL")
    ensure_column(con, "run_block", "scheduler_note", "scheduler_note TEXT NOT NULL DEFAULT ''")

    ensure_index(
        con,
        "idx_run_block_machine_active_queue",
        """
        CREATE INDEX IF NOT EXISTS idx_run_block_machine_active_queue
        ON run_block(machine_id, active, queue_position)
        """,
    )
    ensure_index(
        con,
        "idx_run_block_operation_id",
        """
        CREATE INDEX IF NOT EXISTS idx_run_block_operation_id
        ON run_block(operation_id)
        """,
    )
    ensure_index(
        con,
        "idx_run_block_last_schedule_run_id",
        """
        CREATE INDEX IF NOT EXISTS idx_run_block_last_schedule_run_id
        ON run_block(last_schedule_run_id)
        """,
    )

    ensure_column(con, "run_block_segment", "schedule_run_id", "schedule_run_id INTEGER REFERENCES schedule_run(schedule_run_id) ON DELETE CASCADE")
    ensure_column(con, "run_block_segment", "planned_qty", "planned_qty REAL NOT NULL DEFAULT 0")
    ensure_column(con, "run_block_segment", "planned_minutes", "planned_minutes REAL NOT NULL DEFAULT 0")
    ensure_column(con, "run_block_segment", "segment_status", "segment_status TEXT NOT NULL DEFAULT 'PLANNED'")

    ensure_index(
        con,
        "idx_run_block_segment_schedule_run_id",
        """
        CREATE INDEX IF NOT EXISTS idx_run_block_segment_schedule_run_id
        ON run_block_segment(schedule_run_id)
        """,
    )
    ensure_index(
        con,
        "idx_run_block_segment_block_schedule_run_id",
        """
        CREATE INDEX IF NOT EXISTS idx_run_block_segment_block_schedule_run_id
        ON run_block_segment(block_id, schedule_run_id)
        """,
    )
    ensure_index(
        con,
        "idx_run_block_segment_machine_start_end",
        """
        CREATE INDEX IF NOT EXISTS idx_run_block_segment_machine_start_end
        ON run_block_segment(machine_id, start_datetime, end_datetime)
        """,
    )

    ensure_column(con, "production_actual", "machine_id", "machine_id INTEGER REFERENCES machines(machine_id) ON DELETE SET NULL")
    ensure_column(con, "production_actual", "status", "status TEXT NOT NULL DEFAULT 'ACTIVE'")
    ensure_column(con, "production_actual", "entry_type", "entry_type TEXT NOT NULL DEFAULT 'REPORT'")
    ensure_column(con, "production_actual", "correction_of_actual_id", "correction_of_actual_id INTEGER REFERENCES production_actual(actual_id) ON DELETE SET NULL")
    ensure_column(con, "production_actual", "good_qty_at_report", "good_qty_at_report REAL")
    ensure_column(con, "production_actual", "created_by", "created_by TEXT NOT NULL DEFAULT ''")

    ensure_index(
        con,
        "idx_production_actual_block_status",
        """
        CREATE INDEX IF NOT EXISTS idx_production_actual_block_status
        ON production_actual(block_id, status)
        """,
    )
    ensure_index(
        con,
        "idx_production_actual_report_date",
        """
        CREATE INDEX IF NOT EXISTS idx_production_actual_report_date
        ON production_actual(report_date)
        """,
    )
    ensure_index(
        con,
        "idx_production_actual_correction_of_actual_id",
        """
        CREATE INDEX IF NOT EXISTS idx_production_actual_correction_of_actual_id
        ON production_actual(correction_of_actual_id)
        """,
    )

    # PostgreSQL-friendly compatibility views.
    _create_view(
        con,
        "v_block_actual_totals",
        """
        CREATE VIEW v_block_actual_totals AS
        SELECT
          block_id,
          COALESCE(SUM(COALESCE(output_qty, 0)), 0) AS output_qty,
          COALESCE(SUM(COALESCE(reject_qty, 0)), 0) AS reject_qty,
          COALESCE(SUM(COALESCE(output_qty, 0) - COALESCE(reject_qty, 0)), 0) AS good_qty,
          SUM(CASE WHEN output_qty IS NOT NULL THEN 1 ELSE 0 END) AS output_reports,
          SUM(CASE WHEN reject_qty IS NOT NULL THEN 1 ELSE 0 END) AS reject_reports
        FROM production_actual
        WHERE COALESCE(status, 'ACTIVE') = 'ACTIVE'
        GROUP BY block_id
        """,
    )
    _create_view(
        con,
        "v_block_remaining",
        """
        CREATE VIEW v_block_remaining AS
        SELECT
          b.block_id,
          b.scheduled_qty,
          COALESCE(v.good_qty, 0) AS good_qty,
          CASE
            WHEN COALESCE(b.scheduled_qty, 0) - COALESCE(v.good_qty, 0) > 0
            THEN COALESCE(b.scheduled_qty, 0) - COALESCE(v.good_qty, 0)
            ELSE 0
          END AS remaining_qty
        FROM run_block b
        LEFT JOIN v_block_actual_totals v ON v.block_id = b.block_id
        """,
    )
    _create_view(
        con,
        "v_operation_actual_totals",
        """
        CREATE VIEW v_operation_actual_totals AS
        SELECT
          b.operation_id,
          COALESCE(SUM(COALESCE(a.output_qty, 0)), 0) AS output_qty,
          COALESCE(SUM(COALESCE(a.reject_qty, 0)), 0) AS reject_qty,
          COALESCE(SUM(COALESCE(a.output_qty, 0) - COALESCE(a.reject_qty, 0)), 0) AS good_qty,
          SUM(CASE WHEN a.output_qty IS NOT NULL THEN 1 ELSE 0 END) AS output_reports,
          SUM(CASE WHEN a.reject_qty IS NOT NULL THEN 1 ELSE 0 END) AS reject_reports
        FROM run_block b
        JOIN production_actual a ON a.block_id = b.block_id
        WHERE COALESCE(a.status, 'ACTIVE') = 'ACTIVE'
        GROUP BY b.operation_id
        """,
    )
    _create_view(
        con,
        "v_operation_remaining",
        """
        CREATE VIEW v_operation_remaining AS
        SELECT
          o.operation_id,
          o.total_qty AS required_qty,
          COALESCE(v.good_qty, 0) AS good_qty,
          CASE
            WHEN COALESCE(o.total_qty, 0) - COALESCE(v.good_qty, 0) > 0
            THEN COALESCE(o.total_qty, 0) - COALESCE(v.good_qty, 0)
            ELSE 0
          END AS remaining_qty
        FROM operation o
        LEFT JOIN v_operation_actual_totals v ON v.operation_id = o.operation_id
        """,
    )

    _ensure_state_rows(con)


def ensure_db():
    print(f"Scheduler DB: {DB_PATH}")
    with db() as con:
        ensure_scheduler_schema(con)

        if table_columns(con, "capacity_profile"):
            existing_profiles = {row["profile_name"] for row in rows(con.execute("SELECT profile_name FROM capacity_profile"))}
            for profile_name, capacity_minutes, start_minute, note in CAPACITY_PROFILES:
                if profile_name not in existing_profiles:
                    con.execute(
                        """
                        INSERT INTO capacity_profile (profile_name, capacity_minutes, start_minute, note)
                        VALUES (?, ?, ?, ?)
                        """,
                        (profile_name, capacity_minutes, start_minute, note),
                    )
                else:
                    con.execute(
                        """
                        UPDATE capacity_profile
                        SET capacity_minutes = ?, start_minute = ?, note = ?
                        WHERE profile_name = ?
                          AND (capacity_minutes <> ? OR start_minute <> ? OR COALESCE(note, '') <> COALESCE(?, ''))
                        """,
                        (capacity_minutes, start_minute, note, profile_name, capacity_minutes, start_minute, note),
                    )

        if table_columns(con, "machines"):
            existing_machines = {row["machine_code"] for row in rows(con.execute("SELECT machine_code FROM machines"))}
            for machine_code, machine_category, shift_profile in TRIAL_MACHINES:
                if machine_code not in existing_machines:
                    con.execute(
                        """
                        INSERT INTO machines (machine_code, machine_category, shift_profile, active)
                        VALUES (?, ?, ?, 1)
                        """,
                        (machine_code, machine_category, shift_profile),
                    )


def ensure_actual_schema(con):
    return None


def ensure_rework_schema(con):
    return None


def ensure_group_schema(con):
    return None


def ensure_planning_card_schema(con):
    return None


def ensure_v2_compat_views(con):
    return None


def ensure_v2_compat_columns(con):
    return None


def ensure_material_requirement_schema(con):
    return None
