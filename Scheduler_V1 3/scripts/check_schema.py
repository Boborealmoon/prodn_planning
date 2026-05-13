from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(__file__).resolve().parents[1] / "planner.db"

REQUIRED_COLUMNS = {
    "parts": {"part_id", "part_no", "part_desc", "created_at", "updated_at"},
    "bom_variation": {"bom_id", "part_id", "bom_code", "bom_desc", "is_default", "created_at", "updated_at"},
    "operation_seq": {"op_seq_id", "bom_id", "seq_no", "op_no", "op_type", "machine_category", "cycle_time", "setup_time", "preferred_machine", "is_last_op"},
    "process_sheet": {"ps_id", "part_id", "part_no", "part_desc", "order_date", "due_date", "total_qty", "planned_qty", "finished_qty", "selected_bom_id", "planner_status", "status", "created_at", "updated_at", "source_ps_id", "pp_partial_no"},
    "machines": {"machine_id", "machine_code", "machine_category", "shift_profile", "active", "notes", "created_at", "updated_at"},
    "capacity_profile": {"profile_id", "profile_name", "capacity_minutes", "start_minute", "note"},
    "machine_capacity_day": {"day_id", "machine_id", "work_date", "profile_id", "capacity_minutes", "start_minute", "note", "created_at", "updated_at"},
    "public_holiday": {"holiday_date", "note", "created_at", "updated_at"},
    "bom_material": {"bom_material_id", "source_inventory_code", "bom_code", "material_inventory_code", "material_description", "created_at", "updated_at"},
    "material_requirement": {"requirement_id", "ps_id", "source_inventory_code", "bom_code", "material_inventory_code", "material_description", "material_qty_needed", "material_uom", "supply_status", "expected_ready_date", "supplier_ref", "remarks", "updated_at", "created_at"},
    "operation": {"operation_id", "job_no", "operation_name", "total_qty", "setup_minutes", "cycle_minutes_per_qty", "compatible_machine_group", "source_ps_id", "source_op_seq_id", "source_op_no", "status", "remarks", "created_at", "updated_at"},
    "run_block": {"block_id", "operation_id", "machine_id", "queue_position", "scheduled_qty", "include_setup", "status", "anchor_datetime", "calculated_start_datetime", "calculated_end_datetime", "actual_good_qty", "actual_reject_qty", "remarks", "created_at", "updated_at", "block_type", "source_reject_block_id", "source_reject_segment_id", "planning_status", "execution_status", "anchor_status", "anchor_miss_minutes", "group_id"},
    "run_block_group": {"group_id", "group_label", "group_type", "created_at"},
    "run_block_segment": {"segment_id", "block_id", "machine_id", "segment_date", "segment_type", "qty_done", "minutes_used", "start_datetime", "end_datetime", "is_actual", "created_at"},
    "production_actual": {"actual_id", "block_id", "report_date", "remarks", "reported_at", "segment_id", "output_qty", "reject_qty", "target_qty_at_report"},
    "planning_card": {"card_id", "ps_id", "operation_label", "target_qty", "planning_status", "card_type", "machine_id", "scheduled_block_group_id", "created_at", "updated_at"},
    "planning_card_operation": {"card_op_id", "card_id", "source_ps_id", "source_op_seq_id", "source_op_no", "op_sequence", "setup_minutes", "cycle_minutes_per_qty", "target_qty"},
    "data_import_log": {"log_id", "import_type", "workbook_name", "active_sheet_name", "status", "message", "created_at", "completed_at"},
    "staff": {"staff_id", "staff_name", "role", "active", "created_at", "updated_at"},
    "calendar_days": {"work_date", "is_working_day", "note"},
}

LEGACY_TABLES = {
    "trial_operation",
    "trial_run_block",
    "trial_run_block_segment",
    "trial_production_actual",
    "trial_material_requirement",
    "trial_bom_material",
    "trial_capacity_profile",
    "trial_machine_capacity_day",
    "part_flow_header",
    "part_flow_steps",
}


def main():
    if not DB_PATH.exists():
        print(f"Missing database: {DB_PATH}", file=sys.stderr)
        return 1

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    tables = {row["name"] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    views = {row["name"] for row in con.execute("SELECT name FROM sqlite_master WHERE type='view'")}

    missing_tables = sorted(name for name in REQUIRED_COLUMNS if name not in tables)
    missing_columns = {}
    for table, cols in REQUIRED_COLUMNS.items():
        if table not in tables:
            continue
        actual = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
        missing = sorted(cols - actual)
        if missing:
            missing_columns[table] = missing

    legacy_present = sorted(name for name in LEGACY_TABLES if name in tables or name in views)

    problems = False
    if missing_tables:
        problems = True
        print("Missing tables:", ", ".join(missing_tables))
    if missing_columns:
        problems = True
        for table, cols in missing_columns.items():
            print(f"Missing columns in {table}: {', '.join(cols)}")
    if legacy_present:
        print("Legacy tables/views still present:", ", ".join(legacy_present))

    if problems:
        return 1

    print(f"Schema check passed for {DB_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
