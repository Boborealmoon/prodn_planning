from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "TRIAL" / "trial.db"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from TRIAL.trial_app.db import ensure_db

REQUIRED = {
    "calendar_days": {"work_date", "is_working_day"},
    "data_import_log": {"log_id", "import_type", "workbook_name", "active_sheet_name", "status", "message", "created_at", "completed_at"},
    "machines": {"machine_id", "machine_code", "machine_category", "shift_profile", "active"},
    "parts": {"part_id", "part_name", "part_desc", "part_no"},
    "process_sheet": {"ps_id", "part_id", "part_no", "part_desc", "order_date", "due_date", "total_qty", "planned_qty", "finished_qty", "selected_flow_id", "selected_bom_id", "planner_status", "status", "source_ps_id", "pp_partial_no"},
    "bom_variation": {"bom_id", "part_id", "bom_code", "bom_desc", "is_default"},
    "operation_seq": {"op_seq_id", "bom_id", "seq_no", "op_no", "op_type", "machine_category", "cycle_time", "setup_time", "preferred_machine", "is_last_op"},
    "operation": {"operation_id", "job_no", "operation_name", "total_qty", "setup_minutes", "cycle_minutes_per_qty", "compatible_machine_group", "source_ps_id", "source_op_seq_id", "source_op_no", "status", "remarks"},
    "capacity_profile": {"profile_id", "profile_name", "capacity_minutes", "start_minute", "note"},
    "machine_capacity_day": {"day_id", "machine_id", "work_date", "profile_id", "capacity_minutes", "start_minute", "note", "created_at", "updated_at"},
    "public_holiday": {"holiday_date", "work_date", "note", "created_at", "updated_at"},
    "run_block_group": {"group_id", "group_label", "group_type", "created_at"},
    "run_block": {"block_id", "operation_id", "machine_id", "queue_position", "scheduled_qty", "include_setup", "status", "anchor_datetime", "calculated_start_datetime", "calculated_end_datetime", "actual_good_qty", "actual_reject_qty", "remarks", "created_at", "updated_at", "block_type", "group_id"},
    "run_block_segment": {"segment_id", "block_id", "machine_id", "segment_date", "segment_type", "qty_done", "minutes_used", "start_datetime", "end_datetime", "is_actual", "created_at"},
    "production_actual": {"actual_id", "block_id", "report_date", "output_qty", "reject_qty", "target_qty_at_report", "remarks", "reported_at", "segment_id"},
    "bom_material": {"bom_material_id", "source_inventory_code", "bom_code", "material_inventory_code", "material_description"},
    "material_requirement": {"requirement_id", "ps_id", "source_inventory_code", "bom_code", "material_inventory_code", "material_description", "material_qty_needed", "material_uom", "supply_status", "expected_ready_date", "supplier_ref", "remarks", "updated_at", "created_at"},
    "planning_card": {"card_id", "ps_id", "operation_label", "target_qty", "planning_status", "card_type", "machine_id", "scheduled_block_group_id"},
    "planning_card_operation": {"card_op_id", "card_id", "source_ps_id", "source_op_seq_id", "source_op_no", "op_sequence", "setup_minutes", "cycle_minutes_per_qty", "target_qty"},
}


def main() -> int:
    ensure_db()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    missing = []
    for name, required_cols in REQUIRED.items():
        row = con.execute(
            "SELECT type FROM sqlite_master WHERE name = ? AND type IN ('table', 'view')",
            (name,),
        ).fetchone()
        if not row:
            missing.append(f"missing object: {name}")
            continue
        cols = {r["name"] for r in con.execute(f"PRAGMA table_info({name})")}
        absent = sorted(required_cols - cols)
        if absent:
            missing.append(f"missing columns in {name}: {', '.join(absent)}")
    if missing:
        print("TRIAL schema check failed:")
        for item in missing:
            print(f"- {item}")
        return 1
    print("TRIAL schema check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
