from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from .constants import CAPACITY_PROFILES, TRIAL_MACHINES

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "planner.db"


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
    row = cursor.fetchone()
    return row


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
    con.execute("PRAGMA foreign_keys = ON")
    con.row_factory = row_factory
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def table_columns(con, table_name):
    return {row["name"] for row in rows(con.execute(f"PRAGMA table_info({table_name})"))}


def ensure_actual_schema(con):
    cols = table_columns(con, "production_actual")

    if "segment_id" not in cols:
        con.execute("ALTER TABLE production_actual ADD COLUMN segment_id INTEGER")
        cols.add("segment_id")

    if "output_qty" not in cols:
        con.execute("ALTER TABLE production_actual ADD COLUMN output_qty REAL DEFAULT NULL")
        if "actual_good_qty" in cols:
            con.execute(
                """
                UPDATE production_actual
                SET output_qty = actual_good_qty
                WHERE output_qty IS NULL
                """
            )
        cols.add("output_qty")

    if "reject_qty" not in cols:
        con.execute("ALTER TABLE production_actual ADD COLUMN reject_qty REAL DEFAULT NULL")
        if "actual_reject_qty" in cols:
            con.execute(
                """
                UPDATE production_actual
                SET reject_qty = actual_reject_qty
                WHERE reject_qty IS NULL
                """
            )
        cols.add("reject_qty")

    if "target_qty_at_report" not in cols:
        con.execute("ALTER TABLE production_actual ADD COLUMN target_qty_at_report REAL DEFAULT NULL")
        cols.add("target_qty_at_report")

    indexes = {row["name"] for row in rows(con.execute("PRAGMA index_list(production_actual)"))}
    if "idx_trial_actual_segment_unique" not in indexes:
        con.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_trial_actual_segment_unique
            ON production_actual(segment_id)
            WHERE segment_id IS NOT NULL
            """
        )


def ensure_rework_schema(con):
    cols = table_columns(con, "run_block")

    if "block_type" not in cols:
        con.execute("ALTER TABLE run_block ADD COLUMN block_type TEXT NOT NULL DEFAULT 'ORIGINAL'")
    if "source_reject_block_id" not in cols:
        con.execute("ALTER TABLE run_block ADD COLUMN source_reject_block_id INTEGER")
    if "source_reject_segment_id" not in cols:
        con.execute("ALTER TABLE run_block ADD COLUMN source_reject_segment_id INTEGER")
    if "planning_status" not in cols:
        con.execute("ALTER TABLE run_block ADD COLUMN planning_status TEXT NOT NULL DEFAULT 'UNPLANNED'")
    if "execution_status" not in cols:
        con.execute("ALTER TABLE run_block ADD COLUMN execution_status TEXT NOT NULL DEFAULT 'NOT_STARTED'")
    if "group_id" not in cols:
        con.execute("ALTER TABLE run_block ADD COLUMN group_id INTEGER")


def ensure_group_schema(con):
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS run_block_group (
          group_id INTEGER PRIMARY KEY AUTOINCREMENT,
          group_label TEXT NOT NULL DEFAULT '',
          group_type TEXT NOT NULL DEFAULT 'COMBINED',
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def ensure_planning_card_schema(con):
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS planning_card (
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
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS planning_card_operation (
          card_op_id INTEGER PRIMARY KEY AUTOINCREMENT,
          card_id INTEGER NOT NULL REFERENCES planning_card(card_id) ON DELETE CASCADE,
          source_ps_id TEXT NOT NULL DEFAULT '',
          source_op_seq_id INTEGER NOT NULL DEFAULT 0,
          source_op_no TEXT NOT NULL DEFAULT '',
          op_sequence INTEGER NOT NULL DEFAULT 0,
          setup_minutes REAL NOT NULL DEFAULT 0,
          cycle_minutes_per_qty REAL NOT NULL DEFAULT 0,
          target_qty REAL NOT NULL DEFAULT 0
        )
        """
    )


def ensure_v2_compat_views(con):
    legacy_views = {
        "capacity_profile": "capacity_profile",
        "machine_capacity_day": "machine_capacity_day",
        "trial_public_holiday": "public_holiday",
        "trial_bom_material": "bom_material",
        "material_requirement": "material_requirement",
        "operation": "operation",
        "trial_planning_card": "planning_card",
        "trial_planning_card_operation": "planning_card_operation",
        "production_actual": "production_actual",
        "run_block": "run_block",
        "run_block_group": "run_block_group",
        "run_block_segment": "run_block_segment",
        "bom_variation": "bom_variation",
        "operation_seq": "operation_seq",
    }

    view_sql = {
        "capacity_profile": "SELECT profile_id, profile_name, capacity_minutes, start_minute, note FROM capacity_profile",
        "machine_capacity_day": "SELECT day_id, machine_id, work_date, profile_id, capacity_minutes, start_minute, note, created_at, updated_at FROM machine_capacity_day",
        "trial_public_holiday": "SELECT holiday_date, note, created_at, updated_at FROM public_holiday",
        "trial_bom_material": "SELECT bom_material_id, source_inventory_code, bom_code, material_inventory_code, material_description, created_at, updated_at FROM bom_material",
        "material_requirement": "SELECT requirement_id, ps_id, source_inventory_code, bom_code, material_inventory_code, material_description, material_qty_needed, material_uom, supply_status, expected_ready_date, supplier_ref, remarks, updated_at, created_at FROM material_requirement",
        "operation": "SELECT operation_id, job_no, operation_name, total_qty, setup_minutes, cycle_minutes_per_qty, compatible_machine_group, source_ps_id, source_op_seq_id AS source_op_seq_id, source_op_no, status, remarks, created_at, updated_at FROM operation",
        "trial_planning_card": "SELECT card_id, ps_id, operation_label, target_qty, planning_status, card_type, machine_id, scheduled_block_group_id, created_at, updated_at FROM planning_card",
        "trial_planning_card_operation": "SELECT card_op_id, card_id, source_ps_id, source_op_seq_id AS source_op_seq_id, source_op_no, op_sequence, setup_minutes, cycle_minutes_per_qty, target_qty FROM planning_card_operation",
        "production_actual": "SELECT actual_id, block_id, report_date, output_qty AS actual_good_qty, reject_qty AS actual_reject_qty, target_qty_at_report, remarks, reported_at, segment_id, output_qty, reject_qty FROM production_actual",
        "run_block": "SELECT block_id, operation_id, machine_id, queue_position, scheduled_qty, include_setup, status, anchor_datetime, calculated_start_datetime, calculated_end_datetime, actual_good_qty, actual_reject_qty, remarks, created_at, updated_at, block_type, source_reject_block_id, source_reject_segment_id, planning_status, execution_status, anchor_status, anchor_miss_minutes, group_id FROM run_block",
        "run_block_group": "SELECT group_id, group_label, group_type, created_at FROM run_block_group",
        "run_block_segment": "SELECT segment_id, block_id, machine_id, segment_date, segment_type, qty_done, minutes_used, start_datetime, end_datetime, is_actual, created_at FROM run_block_segment",
        "bom_variation": "SELECT bom_id AS bom_id, part_id, bom_code AS flow_code, bom_desc AS flow_name, is_default, NULL AS created_at, NULL AS updated_at FROM bom_variation",
        "operation_seq": "SELECT op_seq_id AS op_seq_id, bom_id AS bom_id, seq_no, op_no, op_type, machine_category, cycle_time, setup_time, preferred_machine, is_last_op FROM operation_seq",
    }

    for legacy_name, source_name in legacy_views.items():
        if legacy_name in table_columns(con, legacy_name) or one(con.execute("SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1", (legacy_name,))):
            continue
        if not one(con.execute("SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1", (source_name,))):
            continue
        con.execute(f'DROP VIEW IF EXISTS {legacy_name}')
        con.execute(f'CREATE VIEW {legacy_name} AS {view_sql[legacy_name]}')


def ensure_v2_compat_columns(con):
    parts_cols = table_columns(con, "parts")
    if "part_no" not in parts_cols:
        con.execute("ALTER TABLE parts ADD COLUMN part_no TEXT DEFAULT ''")
        con.execute("UPDATE parts SET part_no = COALESCE(NULLIF(part_no, ''), part_name) WHERE COALESCE(part_no, '') = ''")
    if "part_desc" not in parts_cols:
        con.execute("ALTER TABLE parts ADD COLUMN part_desc TEXT DEFAULT ''")
        con.execute("UPDATE parts SET part_desc = COALESCE(NULLIF(part_desc, ''), part_name) WHERE COALESCE(part_desc, '') = ''")

    ps_cols = table_columns(con, "process_sheet")
    if "part_no" not in ps_cols:
        con.execute("ALTER TABLE process_sheet ADD COLUMN part_no TEXT DEFAULT ''")
    if "part_desc" not in ps_cols:
        con.execute("ALTER TABLE process_sheet ADD COLUMN part_desc TEXT DEFAULT ''")
    if "selected_bom_id" not in ps_cols:
        con.execute("ALTER TABLE process_sheet ADD COLUMN selected_bom_id INTEGER DEFAULT 0")
    con.execute(
        """
        UPDATE process_sheet
        SET part_no = COALESCE(NULLIF(part_no, ''), part_no),
            part_desc = COALESCE(NULLIF(part_desc, ''), part_desc),
            selected_bom_id = COALESCE(NULLIF(selected_bom_id, 0), selected_bom_id)
        """
    )

    op_cols = table_columns(con, "operation")
    if "source_op_seq_id" not in op_cols:
        con.execute("ALTER TABLE operation ADD COLUMN source_op_seq_id INTEGER NOT NULL DEFAULT 0")
        con.execute("UPDATE operation SET source_op_seq_id = COALESCE(NULLIF(source_op_seq_id, 0), source_op_seq_id)")


def ensure_bom_material_schema(con):
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS trial_bom_material (
          bom_material_id INTEGER PRIMARY KEY AUTOINCREMENT,
          source_inventory_code TEXT NOT NULL DEFAULT '',
          bom_code TEXT NOT NULL DEFAULT '',
          material_inventory_code TEXT NOT NULL DEFAULT '',
          material_description TEXT DEFAULT '',
          created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(source_inventory_code, bom_code, material_inventory_code)
        )
        """
    )


def ensure_material_requirement_schema(con):
    existing_cols = table_columns(con, "material_requirement")
    legacy_shape = {
        "material_code",
        "material_desc",
        "material_spec",
        "material_size",
    }
    needs_rebuild = bool(existing_cols) and (
        legacy_shape.intersection(existing_cols)
        or "source_inventory_code" not in existing_cols
        or "material_inventory_code" not in existing_cols
        or "material_description" not in existing_cols
    )

    if needs_rebuild:
        con.execute("DROP TABLE IF EXISTS material_requirement_new")
        con.execute(
            """
            CREATE TABLE material_requirement_new (
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
              updated_by TEXT DEFAULT '',
              updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(ps_id, material_inventory_code)
            )
            """
        )
        if existing_cols:
            legacy_rows = rows(con.execute("SELECT * FROM material_requirement ORDER BY requirement_id"))
        else:
            legacy_rows = []
        for legacy in legacy_rows:
            con.execute(
                """
                INSERT OR REPLACE INTO material_requirement_new (
                  requirement_id,
                  ps_id,
                  source_inventory_code,
                  bom_code,
                  material_inventory_code,
                  material_description,
                  material_qty_needed,
                  material_uom,
                  supply_status,
                  expected_ready_date,
                  supplier_ref,
                  remarks,
                  updated_by,
                  updated_at,
                  created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    legacy["requirement_id"],
                    legacy["ps_id"],
                    str(legacy.get("source_inventory_code") or legacy.get("part_no") or legacy.get("part_no") or "").strip(),
                    str(legacy.get("bom_code") or "").strip(),
                    str(legacy.get("material_inventory_code") or legacy.get("material_code") or "").strip(),
                    str(legacy.get("material_description") or legacy.get("material_desc") or "").strip(),
                    legacy.get("material_qty_needed") or 0,
                    str(legacy.get("material_uom") or "").strip(),
                    str(legacy.get("supply_status") or "PENDING_CONFIRMATION").strip() or "PENDING_CONFIRMATION",
                    str(legacy.get("expected_ready_date") or "").strip(),
                    str(legacy.get("supplier_ref") or "").strip(),
                    str(legacy.get("remarks") or "").strip(),
                    str(legacy.get("updated_by") or "").strip(),
                    str(legacy.get("updated_at") or dt_now_text()).strip(),
                    str(legacy.get("created_at") or dt_now_text()).strip(),
                ),
            )
        con.execute("DROP TABLE IF EXISTS material_requirement")
        con.execute(
            """
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
              updated_by TEXT DEFAULT '',
              updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(ps_id, material_inventory_code)
            )
            """
        )
        con.execute(
            """
            INSERT OR REPLACE INTO material_requirement (
              requirement_id,
              ps_id,
              source_inventory_code,
              bom_code,
              material_inventory_code,
              material_description,
              material_qty_needed,
              material_uom,
              supply_status,
              expected_ready_date,
              supplier_ref,
              remarks,
              updated_by,
              updated_at,
              created_at
            )
            SELECT
              requirement_id,
              ps_id,
              source_inventory_code,
              bom_code,
              material_inventory_code,
              material_description,
              material_qty_needed,
              material_uom,
              supply_status,
              expected_ready_date,
              supplier_ref,
              remarks,
              updated_by,
              updated_at,
              created_at
            FROM material_requirement_new
            """
        )
        con.execute("DROP TABLE IF EXISTS material_requirement_new")
        existing_cols = table_columns(con, "material_requirement")

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS material_requirement (
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
          updated_by TEXT DEFAULT '',
          updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(ps_id, material_inventory_code)
        )
        """
    )

    indexes = {row["name"] for row in rows(con.execute("PRAGMA index_list(material_requirement)"))}
    if "idx_material_requirement_ps_material_unique" not in indexes:
        con.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_material_requirement_ps_material_unique
            ON material_requirement(ps_id, material_inventory_code)
            WHERE COALESCE(material_inventory_code, '') <> ''
            """
        )
def ensure_db():
    with db() as con:
        con.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS machines (
              machine_id INTEGER PRIMARY KEY AUTOINCREMENT,
              machine_code TEXT NOT NULL UNIQUE,
              machine_category TEXT NOT NULL,
              shift_profile TEXT NOT NULL DEFAULT 'STANDARD',
              active INTEGER NOT NULL DEFAULT 1,
              notes TEXT DEFAULT '',
              created_at TEXT DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS parts (
              part_id INTEGER PRIMARY KEY AUTOINCREMENT,
              part_name TEXT NOT NULL,
              part_desc TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS bom_variation (
              bom_id INTEGER PRIMARY KEY AUTOINCREMENT,
              part_id INTEGER NOT NULL REFERENCES parts(part_id) ON DELETE CASCADE,
              flow_code TEXT NOT NULL,
              flow_name TEXT DEFAULT '',
              is_default INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS operation_seq (
              op_seq_id INTEGER PRIMARY KEY AUTOINCREMENT,
              bom_id INTEGER NOT NULL REFERENCES bom_variation(bom_id) ON DELETE CASCADE,
              seq_no INTEGER NOT NULL DEFAULT 0,
              op_no TEXT NOT NULL DEFAULT '',
              op_type TEXT NOT NULL DEFAULT '',
              machine_category TEXT NOT NULL DEFAULT 'UNKNOWN',
              preferred_machine TEXT DEFAULT '',
              cycle_time REAL NOT NULL DEFAULT 0,
              setup_time REAL NOT NULL DEFAULT 0,
              is_last_op INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS process_sheet (
              ps_id TEXT PRIMARY KEY,
              source_ps_id TEXT DEFAULT '',
              pp_partial_no TEXT NOT NULL DEFAULT '1',
              part_id INTEGER REFERENCES parts(part_id),
              selected_bom_id INTEGER REFERENCES bom_variation(bom_id),
              part_no TEXT DEFAULT '',
              part_desc TEXT DEFAULT '',
              order_date TEXT DEFAULT '',
              due_date TEXT DEFAULT '',
              total_qty REAL NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'ACTIVE',
              planner_status TEXT NOT NULL DEFAULT 'UNPLANNED'
            );

            CREATE TABLE IF NOT EXISTS capacity_profile (
              profile_id INTEGER PRIMARY KEY AUTOINCREMENT,
              profile_name TEXT NOT NULL UNIQUE,
              capacity_minutes INTEGER NOT NULL DEFAULT 0,
              start_minute INTEGER NOT NULL DEFAULT 510,
              note TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS machine_capacity_day (
              day_id INTEGER PRIMARY KEY AUTOINCREMENT,
              machine_id INTEGER NOT NULL REFERENCES machines(machine_id) ON DELETE CASCADE,
              work_date TEXT NOT NULL,
              profile_id INTEGER NOT NULL REFERENCES capacity_profile(profile_id),
              capacity_minutes INTEGER NOT NULL DEFAULT 0,
              start_minute INTEGER NOT NULL DEFAULT 510,
              note TEXT DEFAULT '',
              created_at TEXT DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(machine_id, work_date)
            );

            CREATE TABLE IF NOT EXISTS trial_public_holiday (
              holiday_date TEXT PRIMARY KEY,
              note TEXT DEFAULT '',
              created_at TEXT DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS operation (
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

            CREATE TABLE IF NOT EXISTS run_block (
              block_id INTEGER PRIMARY KEY AUTOINCREMENT,
              operation_id INTEGER NOT NULL REFERENCES operation(operation_id) ON DELETE CASCADE,
              machine_id INTEGER NOT NULL REFERENCES machines(machine_id),
              queue_position INTEGER NOT NULL DEFAULT 0,
              scheduled_qty REAL NOT NULL DEFAULT 0,
              include_setup INTEGER NOT NULL DEFAULT 1,
              status TEXT NOT NULL DEFAULT 'NOT_STARTED',
              planning_status TEXT NOT NULL DEFAULT 'UNPLANNED',
              execution_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
              anchor_datetime TEXT DEFAULT '',
              calculated_start_datetime TEXT DEFAULT '',
              calculated_end_datetime TEXT DEFAULT '',
              actual_good_qty REAL NOT NULL DEFAULT 0,
              actual_reject_qty REAL NOT NULL DEFAULT 0,
              remarks TEXT DEFAULT '',
              block_type TEXT NOT NULL DEFAULT 'ORIGINAL',
              source_reject_block_id INTEGER,
              source_reject_segment_id INTEGER,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS run_block_segment (
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

            CREATE TABLE IF NOT EXISTS production_actual (
              actual_id INTEGER PRIMARY KEY AUTOINCREMENT,
              segment_id INTEGER REFERENCES run_block_segment(segment_id) ON DELETE CASCADE,
              block_id INTEGER NOT NULL REFERENCES run_block(block_id) ON DELETE CASCADE,
              report_date TEXT NOT NULL,
              output_qty REAL DEFAULT NULL,
              reject_qty REAL DEFAULT NULL,
              target_qty_at_report REAL DEFAULT NULL,
              remarks TEXT DEFAULT '',
              reported_by TEXT DEFAULT '',
              reported_at TEXT DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(segment_id)
            );

            CREATE TABLE IF NOT EXISTS data_import_log (
              log_id INTEGER PRIMARY KEY AUTOINCREMENT,
              import_type TEXT NOT NULL,
              workbook_name TEXT DEFAULT '',
              active_sheet_name TEXT DEFAULT '',
              status TEXT NOT NULL DEFAULT 'STARTED',
              message TEXT DEFAULT '',
              created_at TEXT DEFAULT CURRENT_TIMESTAMP,
              completed_at TEXT DEFAULT ''
            );
            """
        )

        ensure_actual_schema(con)
        ensure_rework_schema(con)
        ensure_group_schema(con)
        ensure_planning_card_schema(con)
        ensure_bom_material_schema(con)
        ensure_material_requirement_schema(con)
        ensure_v2_compat_columns(con)

        con.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_capacity_profile_profile_name_unique
            ON capacity_profile(profile_name)
            """
        )

        con.executemany(
            """
            INSERT INTO capacity_profile (profile_name, capacity_minutes, start_minute, note)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(profile_name) DO UPDATE SET
              capacity_minutes = excluded.capacity_minutes,
              start_minute = excluded.start_minute,
              note = excluded.note
            """,
            CAPACITY_PROFILES,
        )
        profile_placeholders = ",".join("?" for _ in CAPACITY_PROFILES)
        con.execute(
            f"""
            DELETE FROM capacity_profile
            WHERE profile_name NOT IN ({profile_placeholders})
              AND profile_id NOT IN (SELECT DISTINCT profile_id FROM machine_capacity_day)
            """,
            [profile_name for profile_name, _, _, _ in CAPACITY_PROFILES],
        )

        con.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_machines_machine_code_unique
            ON machines(machine_code)
            """
        )

        con.executemany(
            """
            INSERT INTO machines (machine_code, machine_category, shift_profile, active)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(machine_code) DO UPDATE SET
            machine_category = excluded.machine_category,
            shift_profile = excluded.shift_profile,
            active = 1,
            updated_at = CURRENT_TIMESTAMP
            """,
            TRIAL_MACHINES,
        )

        placeholders = ",".join("?" for _ in TRIAL_MACHINES)
        con.execute(
            f"UPDATE machines SET active = 0 WHERE machine_code NOT IN ({placeholders})",
            [machine_code for machine_code, _, _ in TRIAL_MACHINES],
        )
        ensure_v2_compat_views(con)
