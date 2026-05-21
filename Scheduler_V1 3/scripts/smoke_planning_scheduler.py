from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app.db import db, ensure_db, one, rows
from scheduler_app.planning_scheduler import recalculate_planning_all
from scheduler_app.planning_settings import (
    DEFAULT_PLANNING_EFFICIENCY,
    get_planning_efficiency,
    set_planning_setting,
)


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


@contextmanager
def savepoint(con, name):
    con.execute(f"SAVEPOINT {name}")
    try:
        yield
    finally:
        con.execute(f"ROLLBACK TO {name}")
        con.execute(f"RELEASE {name}")


def _next_friday_iso():
    today = date.today()
    offset = (4 - today.weekday()) % 7
    return (today + timedelta(days=offset)).isoformat()


def _table_exists(con, table_name):
    row = one(
        con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        )
    )
    return row is not None


def _planning_block_state(con, planning_run_id, block_id):
    return one(
        con.execute(
            """
            SELECT *
            FROM planning_block_state
            WHERE planning_run_id = ?
              AND block_id = ?
            """,
            (int(planning_run_id), int(block_id)),
        )
    )


def _planning_segment_count(con, planning_run_id, block_id):
    row = one(
        con.execute(
            """
            SELECT COUNT(*) AS c
            FROM planning_schedule_segment
            WHERE planning_run_id = ?
              AND block_id = ?
            """,
            (int(planning_run_id), int(block_id)),
        )
    )
    return int(row["c"] or 0) if row else 0


def _planning_production_bounds(con, planning_run_id, block_id):
    return one(
        con.execute(
            """
            SELECT MIN(start_datetime) AS start_datetime, MAX(end_datetime) AS end_datetime
            FROM planning_schedule_segment
            WHERE planning_run_id = ?
              AND block_id = ?
              AND segment_type = 'production'
            """,
            (int(planning_run_id), int(block_id)),
        )
    )


def main():
    try:
        ensure_db()
        ensure_db()
        pass_msg("ensure_db() is idempotent")
    except Exception as exc:
        return fail(f"ensure_db() failed: {exc}")

    with db() as con:
        required_tables = [
            "planning_setting",
            "planning_schedule_run",
            "planning_schedule_segment",
            "planning_block_state",
            "planning_operation_state",
            "planning_process_sheet_state",
        ]
        for table in required_tables:
            if not _table_exists(con, table):
                return fail(f"missing planning table: {table}")
        pass_msg("planning tables exist")

        with savepoint(con, "planning_default_eff"):
            set_planning_setting(con, "planning_efficiency", "0.85")
            efficiency = get_planning_efficiency(con)
            if abs(efficiency - DEFAULT_PLANNING_EFFICIENCY) > 1e-9:
                return fail(f"unexpected default planning efficiency: {efficiency}")
        pass_msg("default planning efficiency can be forced to 0.85")

    with db() as con:
        with savepoint(con, "planning_smoke"):
            machine = one(
                con.execute(
                    """
                    INSERT INTO machines (machine_code, machine_category, shift_profile, active, notes)
                    VALUES ('PLAN-SMOKE-M1', 'PLAN', 'STANDARD', 1, '')
                    RETURNING machine_id
                    """
                )
            )
            machine_dep = one(
                con.execute(
                    """
                    INSERT INTO machines (machine_code, machine_category, shift_profile, active, notes)
                    VALUES ('PLAN-SMOKE-M2', 'PLAN', 'STANDARD', 1, '')
                    RETURNING machine_id
                    """
                )
            )
            machine_queue = one(
                con.execute(
                    """
                    INSERT INTO machines (machine_code, machine_category, shift_profile, active, notes)
                    VALUES ('PLAN-SMOKE-M3', 'PLAN', 'STANDARD', 1, '')
                    RETURNING machine_id
                    """
                )
            )
            part = one(
                con.execute(
                    """
                    INSERT INTO parts (part_no, part_desc)
                    VALUES ('PLAN-SMOKE-PART', 'Planning smoke part')
                    RETURNING part_id
                    """
                )
            )
            bom = one(
                con.execute(
                    """
                    INSERT INTO bom_variation (part_id, bom_code, bom_desc, is_default)
                    VALUES (?, 'PLAN-SMOKE-BOM', 'Planning smoke bom', 1)
                    RETURNING bom_id
                    """,
                    (int(part["part_id"]),),
                )
            )
            seq1 = one(
                con.execute(
                    """
                    INSERT INTO operation_seq (bom_id, seq_no, op_no, op_type, machine_category, cycle_time, setup_time, preferred_machine, is_last_op)
                    VALUES (?, 10, '10', 'CUT', 'PLAN', 10, 60, '', 0)
                    RETURNING op_seq_id
                    """,
                    (int(bom["bom_id"]),),
                )
            )
            seq2 = one(
                con.execute(
                    """
                    INSERT INTO operation_seq (bom_id, seq_no, op_no, op_type, machine_category, cycle_time, setup_time, preferred_machine, is_last_op)
                    VALUES (?, 20, '20', 'PACK', 'PLAN', 8, 30, '', 1)
                    RETURNING op_seq_id
                    """,
                    (int(bom["bom_id"]),),
                )
            )
            ps_id = "PLAN-SMOKE-PS::1"
            op1 = one(
                con.execute(
                    """
                    INSERT INTO operation (
                      job_no, operation_name, total_qty, setup_minutes, cycle_minutes_per_qty, compatible_machine_group,
                      source_ps_id, source_op_seq_id, source_op_no, status, remarks, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', '', CURRENT_TIMESTAMP)
                    RETURNING operation_id
                    """,
                    ("PLAN-001", "OP10", 20, 60, 10, "PLAN", ps_id, int(seq1["op_seq_id"]), "10"),
                )
            )
            op2 = one(
                con.execute(
                    """
                    INSERT INTO operation (
                      job_no, operation_name, total_qty, setup_minutes, cycle_minutes_per_qty, compatible_machine_group,
                      source_ps_id, source_op_seq_id, source_op_no, status, remarks, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', '', CURRENT_TIMESTAMP)
                    RETURNING operation_id
                    """,
                    ("PLAN-001", "OP20", 20, 30, 8, "PLAN", ps_id, int(seq2["op_seq_id"]), "20"),
                )
            )
            con.execute(
                """
                INSERT INTO process_sheet (
                  ps_id, part_id, part_no, part_desc, order_date, due_date, total_qty, planned_qty,
                  finished_qty, selected_bom_id, planner_status, status, source_ps_id, pp_partial_no
                ) VALUES (?, ?, ?, ?, date('now'), date('now', '+7 day'), ?, 0, 0, ?, 'UNPLANNED', 'ACTIVE', ?, '1')
                """,
                (
                    ps_id,
                    int(part["part_id"]),
                    "PLAN-SMOKE-PART",
                    "Planning smoke part",
                    20,
                    int(bom["bom_id"]),
                    ps_id,
                ),
            )
            friday_iso = _next_friday_iso()
            con.execute(
                """
                UPDATE operation
                SET total_qty = 20, cycle_minutes_per_qty = 10, setup_minutes = 60
                WHERE operation_id = ?
                """,
                (int(op1["operation_id"]),),
            )
            con.execute(
                """
                UPDATE operation
                SET total_qty = 20, cycle_minutes_per_qty = 8, setup_minutes = 30
                WHERE operation_id = ?
                """,
                (int(op2["operation_id"]),),
            )
            block1 = one(
                con.execute(
                    """
                    INSERT INTO run_block (
                      operation_id, machine_id, queue_position, scheduled_qty, include_setup, status, planning_status, execution_status,
                      anchor_datetime, planned_start_at, planned_end_at, allow_pull_forward, active, is_fresh_monday_item,
                      calculated_start_datetime, calculated_end_datetime, actual_good_qty, actual_reject_qty, remarks, updated_at
                    ) VALUES (?, ?, 10, 20, 1, 'PLANNED', 'PLANNED', 'NOT_STARTED', ?, ?, '', 0, 1, 0, '', '', 0, 0, '', CURRENT_TIMESTAMP)
                    RETURNING block_id
                    """,
                    (
                        int(op1["operation_id"]),
                        int(machine["machine_id"]),
                        f"{friday_iso} 08:30:00",
                        f"{friday_iso} 08:30:00",
                    ),
                )
            )
            block2 = one(
                con.execute(
                    """
                    INSERT INTO run_block (
                      operation_id, machine_id, queue_position, scheduled_qty, include_setup, status, planning_status, execution_status,
                      anchor_datetime, planned_start_at, planned_end_at, allow_pull_forward, active, is_fresh_monday_item,
                      calculated_start_datetime, calculated_end_datetime, actual_good_qty, actual_reject_qty, remarks, updated_at
                    ) VALUES (?, ?, 20, 20, 1, 'PLANNED', 'PLANNED', 'NOT_STARTED', ?, ?, '', 1, 1, 0, '', '', 0, 0, '', CURRENT_TIMESTAMP)
                    RETURNING block_id
                    """,
                    (
                        int(op2["operation_id"]),
                        int(machine_dep["machine_id"]),
                        f"{friday_iso} 08:30:00",
                        f"{friday_iso} 08:30:00",
                    ),
                )
            )
            queue_block_a = one(
                con.execute(
                    """
                    INSERT INTO run_block (
                      operation_id, machine_id, queue_position, scheduled_qty, include_setup, status, planning_status, execution_status,
                      anchor_datetime, planned_start_at, planned_end_at, allow_pull_forward, active, is_fresh_monday_item,
                      calculated_start_datetime, calculated_end_datetime, actual_good_qty, actual_reject_qty, remarks, updated_at
                    ) VALUES (?, ?, 10, 10, 1, 'PLANNED', 'PLANNED', 'NOT_STARTED', ?, ?, '', 1, 1, 0, '', '', 0, 0, '', CURRENT_TIMESTAMP)
                    RETURNING block_id
                    """,
                    (
                        int(op1["operation_id"]),
                        int(machine_queue["machine_id"]),
                        f"{friday_iso} 08:30:00",
                        f"{friday_iso} 08:30:00",
                    ),
                )
            )
            queue_block_b = one(
                con.execute(
                    """
                    INSERT INTO run_block (
                      operation_id, machine_id, queue_position, scheduled_qty, include_setup, status, planning_status, execution_status,
                      anchor_datetime, planned_start_at, planned_end_at, allow_pull_forward, active, is_fresh_monday_item,
                      calculated_start_datetime, calculated_end_datetime, actual_good_qty, actual_reject_qty, remarks, updated_at
                    ) VALUES (?, ?, 20, 10, 1, 'PLANNED', 'PLANNED', 'NOT_STARTED', ?, ?, '', 1, 1, 0, '', '', 0, 0, '', CURRENT_TIMESTAMP)
                    RETURNING block_id
                    """,
                    (
                        int(op1["operation_id"]),
                        int(machine_queue["machine_id"]),
                        f"{friday_iso} 08:30:00",
                        f"{friday_iso} 08:30:00",
                    ),
                )
            )

            planning_run_id = recalculate_planning_all(con, reason="SMOKE_PLANNING")
            if not planning_run_id:
                return fail("recalculate_planning_all() did not return a planning run id")
            pass_msg("planning baseline recalculated")

            run_row = one(con.execute("SELECT COUNT(*) AS c FROM planning_schedule_run WHERE status = 'CURRENT'"))
            if int(run_row["c"] or 0) != 1:
                return fail("planning_schedule_run should have exactly one CURRENT row")
            pass_msg("planning_schedule_run has a CURRENT row")

            seg_count = one(con.execute("SELECT COUNT(*) AS c FROM planning_schedule_segment WHERE planning_run_id = ?", (int(planning_run_id),)))
            if int(seg_count["c"] or 0) <= 0:
                return fail("planning_schedule_segment is empty after baseline recalc")
            pass_msg("planning_schedule_segment has rows")

            states = one(con.execute("SELECT COUNT(*) AS c FROM planning_block_state WHERE planning_run_id = ?", (int(planning_run_id),)))
            if int(states["c"] or 0) <= 0:
                return fail("planning_block_state missing after baseline recalc")
            pass_msg("planning_block_state is populated")

            dep_state_prev = _planning_block_state(con, planning_run_id, int(block1["block_id"]))
            dep_state_curr = _planning_block_state(con, planning_run_id, int(block2["block_id"]))
            dep_prod_prev = _planning_production_bounds(con, planning_run_id, int(block1["block_id"]))
            dep_prod_curr = _planning_production_bounds(con, planning_run_id, int(block2["block_id"]))
            if not dep_state_prev or not dep_state_curr or not dep_prod_prev or not dep_prod_curr:
                return fail("missing dependency test block state")
            prev_end = datetime.fromisoformat(str(dep_prod_prev["end_datetime"]))
            curr_start = datetime.fromisoformat(str(dep_prod_curr["start_datetime"]))
            if curr_start < prev_end:
                return fail("planning OP dependency allowed later op to start too early")
            pass_msg("planning OP dependency keeps later op after earlier expected end")

            queue_state_a = _planning_block_state(con, planning_run_id, int(queue_block_a["block_id"]))
            queue_state_b = _planning_block_state(con, planning_run_id, int(queue_block_b["block_id"]))
            queue_prod_a = _planning_production_bounds(con, planning_run_id, int(queue_block_a["block_id"]))
            queue_prod_b = _planning_production_bounds(con, planning_run_id, int(queue_block_b["block_id"]))
            if not queue_state_a or not queue_state_b or not queue_prod_a or not queue_prod_b:
                return fail("missing queue-order test block state")
            queue_end_a = datetime.fromisoformat(str(queue_prod_a["end_datetime"]))
            queue_start_b = datetime.fromisoformat(str(queue_prod_b["start_datetime"]))
            if queue_start_b < queue_end_a:
                return fail("machine queue order was not preserved")
            pass_msg("planning respects machine queue order")

            weekend_rows = rows(
                con.execute(
                    """
                    SELECT segment_date
                    FROM planning_schedule_segment
                    WHERE planning_run_id = ?
                    """,
                    (int(planning_run_id),),
                )
            )
            for row in weekend_rows:
                weekday = datetime.fromisoformat(f"{row['segment_date']} 00:00:00").date().weekday()
                if weekday >= 5:
                    return fail(f"planning baseline scheduled weekend segment: {row['segment_date']}")
            pass_msg("planning baseline avoids Saturday and Sunday")

            queue_segments_before = _planning_segment_count(con, planning_run_id, int(queue_block_a["block_id"]))
            con.execute(
                """
                INSERT INTO run_block_segment (
                  block_id, machine_id, segment_date, segment_type, qty_done, minutes_used,
                  start_datetime, end_datetime, is_actual
                ) VALUES (?, ?, ?, 'production', 999, 999, '2099-01-01 00:00:00', '2099-01-01 16:00:00', 0)
                """,
                (int(queue_block_a["block_id"]), int(machine_queue["machine_id"]), friday_iso),
            )
            planning_run_id_after_live_segment = recalculate_planning_all(con, reason="SMOKE_PLANNING_IGNORES_LIVE_SEGMENT")
            queue_segments_after = _planning_segment_count(con, planning_run_id_after_live_segment, int(queue_block_a["block_id"]))
            if queue_segments_after != queue_segments_before:
                return fail("planning baseline was affected by a live run_block_segment row")
            pass_msg("planning baseline ignores live run_block_segment rows")

            con.execute("INSERT INTO production_actual (block_id, report_date, remarks, output_qty, reject_qty, target_qty_at_report, status, entry_type, created_by) VALUES (?, date('now'), 'smoke', 5, 1, 20, 'ACTIVE', 'REPORT', '')", (int(block1["block_id"]),))
            planning_run_id_2 = recalculate_planning_all(con, reason="SMOKE_PLANNING_AFTER_ACTUAL")
            if not planning_run_id_2:
                return fail("second planning run missing")
            block_state = _planning_block_state(con, planning_run_id_2, int(block1["block_id"]))
            if not block_state or abs(float(block_state["planned_qty"] or 0) - 20.0) > 1e-9:
                return fail("planning recalc used actuals or changed scheduled quantity")
            pass_msg("planning ignores actuals and keeps full planned qty")

            set_planning_setting(con, "planning_efficiency", "0.9")
            run_user = recalculate_planning_all(con, reason="SMOKE_PLANNING_EFF_09")
            user_state = _planning_block_state(con, run_user, int(block1["block_id"]))
            if not user_state:
                return fail("planning failed after setting user efficiency to 0.9")
            pass_msg("planning recalculates successfully after user-changed efficiency")

            set_planning_setting(con, "planning_efficiency", "1.0")
            run_one = recalculate_planning_all(con, reason="SMOKE_PLANNING_EFF_1")
            minutes_one = one(
                con.execute(
                    """
                    SELECT SUM(planned_minutes) AS planned_minutes
                    FROM planning_schedule_segment
                    WHERE planning_run_id = ?
                      AND block_id = ?
                      AND segment_type = 'production'
                    """,
                    (int(run_one), int(block1["block_id"])),
                )
            )
            set_planning_setting(con, "planning_efficiency", "0.85")
            run_two = recalculate_planning_all(con, reason="SMOKE_PLANNING_EFF_085")
            minutes_two = one(
                con.execute(
                    """
                    SELECT SUM(planned_minutes) AS planned_minutes
                    FROM planning_schedule_segment
                    WHERE planning_run_id = ?
                      AND block_id = ?
                      AND segment_type = 'production'
                    """,
                    (int(run_two), int(block1["block_id"])),
                )
            )
            if float(minutes_two["planned_minutes"] or 0) <= float(minutes_one["planned_minutes"] or 0):
                return fail("planning efficiency did not increase planned minutes")
            pass_msg("planning efficiency increases planned minutes")

    pass_msg("smoke_planning_scheduler completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
