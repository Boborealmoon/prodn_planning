from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app.blocks import recalculate_machine
from scheduler_app.db import db, ensure_db, one, parse_dt_text


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


def _segment_start(con, block_id):
    row = one(
        con.execute(
            """
            SELECT MIN(start_datetime) AS start_datetime
            FROM run_block_segment
            WHERE block_id = ?
              AND segment_type = 'production'
            """,
            (int(block_id),),
        )
    )
    if not row or not row["start_datetime"]:
        return None
    return parse_dt_text(row["start_datetime"])


def _assert_minutes(diff, expected, label):
    actual = int(round(diff.total_seconds() / 60.0))
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected} minutes, got {actual} minutes")


def main():
    try:
        ensure_db()
        ensure_db()
        pass_msg("ensure_db() is idempotent")
    except Exception as exc:
        return fail(f"ensure_db() failed: {exc}")

    with db() as con:
        with savepoint(con, "transfer_dependency_smoke"):
            machine_a = one(
                con.execute(
                    """
                    INSERT INTO machines (machine_code, machine_category, shift_profile, active, notes)
                    VALUES ('SMOKE-DEP-A', 'SMOKE', 'STANDARD', 1, '')
                    RETURNING machine_id
                    """
                )
            )
            machine_b = one(
                con.execute(
                    """
                    INSERT INTO machines (machine_code, machine_category, shift_profile, active, notes)
                    VALUES ('SMOKE-DEP-B', 'SMOKE', 'STANDARD', 1, '')
                    RETURNING machine_id
                    """
                )
            )
            part = one(
                con.execute(
                    """
                    INSERT INTO parts (part_no, part_desc)
                    VALUES ('SMOKE-DEP-PART', 'Transfer dependency smoke part')
                    RETURNING part_id
                    """
                )
            )
            bom = one(
                con.execute(
                    """
                    INSERT INTO bom_variation (part_id, bom_code, bom_desc, is_default)
                    VALUES (?, 'SMOKE-DEP-BOM', 'Transfer dependency smoke bom', 1)
                    RETURNING bom_id
                    """,
                    (int(part["part_id"]),),
                )
            )
            seq_prev = one(
                con.execute(
                    """
                    INSERT INTO operation_seq (bom_id, seq_no, op_no, op_type, machine_category, cycle_time, setup_time, preferred_machine, is_last_op)
                    VALUES (?, 10, '10', 'CUT', 'SMOKE', 10, 0, '', 0)
                    RETURNING op_seq_id
                    """,
                    (int(bom["bom_id"]),),
                )
            )
            seq_curr = one(
                con.execute(
                    """
                    INSERT INTO operation_seq (bom_id, seq_no, op_no, op_type, machine_category, cycle_time, setup_time, preferred_machine, is_last_op)
                    VALUES (?, 20, '20', 'ASSY', 'SMOKE', 5, 0, '', 1)
                    RETURNING op_seq_id
                    """,
                    (int(bom["bom_id"]),),
                )
            )

            ps_id = "SMOKE-DEP-PS::1"
            op_prev = one(
                con.execute(
                    """
                    INSERT INTO operation (
                      job_no, operation_name, total_qty, setup_minutes, cycle_minutes_per_qty, compatible_machine_group,
                      source_ps_id, source_op_seq_id, source_op_no, status, remarks, updated_at
                    ) VALUES (?, ?, ?, 0, ?, 'SMOKE', ?, ?, ?, 'ACTIVE', '', CURRENT_TIMESTAMP)
                    RETURNING operation_id
                    """,
                    ("SMOKE-DEP-001", "OP10", 2, 10, ps_id, int(seq_prev["op_seq_id"]), "10"),
                )
            )
            op_curr = one(
                con.execute(
                    """
                    INSERT INTO operation (
                      job_no, operation_name, total_qty, setup_minutes, cycle_minutes_per_qty, compatible_machine_group,
                      source_ps_id, source_op_seq_id, source_op_no, status, remarks, updated_at
                    ) VALUES (?, ?, ?, 0, ?, 'SMOKE', ?, ?, ?, 'ACTIVE', '', CURRENT_TIMESTAMP)
                    RETURNING operation_id
                    """,
                    ("SMOKE-DEP-001", "OP20", 2, 5, ps_id, int(seq_curr["op_seq_id"]), "20"),
                )
            )

            block_prev = one(
                con.execute(
                    """
                    INSERT INTO run_block (
                      operation_id, machine_id, queue_position, scheduled_qty, include_setup, status, planning_status, execution_status,
                      anchor_datetime, planned_start_at, planned_end_at, allow_pull_forward, active, is_fresh_monday_item,
                      calculated_start_datetime, calculated_end_datetime, actual_good_qty, actual_reject_qty, remarks, updated_at
                    ) VALUES (?, ?, 10, 2, 0, 'NOT_STARTED', 'PLANNED', 'NOT_STARTED', '', '', '', 1, 1, 0, '', '', 0, 0, '', CURRENT_TIMESTAMP)
                    RETURNING block_id
                    """,
                    (int(op_prev["operation_id"]), int(machine_a["machine_id"])),
                )
            )
            block_curr = one(
                con.execute(
                    """
                    INSERT INTO run_block (
                      operation_id, machine_id, queue_position, scheduled_qty, include_setup, status, planning_status, execution_status,
                      anchor_datetime, planned_start_at, planned_end_at, allow_pull_forward, active, is_fresh_monday_item,
                      calculated_start_datetime, calculated_end_datetime, actual_good_qty, actual_reject_qty, remarks, updated_at
                    ) VALUES (?, ?, 10, 2, 0, 'NOT_STARTED', 'PLANNED', 'NOT_STARTED', '', '', '', 1, 1, 0, '', '', 0, 0, '', CURRENT_TIMESTAMP)
                    RETURNING block_id
                    """,
                    (int(op_curr["operation_id"]), int(machine_b["machine_id"])),
                )
            )

            recalculate_machine(con, int(machine_a["machine_id"]), reason="TRANSFER_DEP_SMOKE")
            recalculate_machine(con, int(machine_b["machine_id"]), reason="TRANSFER_DEP_SMOKE")

            prev_start = _segment_start(con, int(block_prev["block_id"]))
            curr_start = _segment_start(con, int(block_curr["block_id"]))
            if not prev_start or not curr_start:
                return fail("missing production segments for transfer smoke")
            _assert_minutes(curr_start - prev_start, 15, "first scenario")
            pass_msg("OP20 starts after enough OP10 buffer in 10/5 case")

            con.execute("UPDATE operation SET cycle_minutes_per_qty = 5 WHERE operation_id = ?", (int(op_prev["operation_id"]),))
            con.execute("UPDATE operation SET cycle_minutes_per_qty = 10 WHERE operation_id = ?", (int(op_curr["operation_id"]),))

            recalculate_machine(con, int(machine_a["machine_id"]), reason="TRANSFER_DEP_SMOKE")
            recalculate_machine(con, int(machine_b["machine_id"]), reason="TRANSFER_DEP_SMOKE")

            prev_start = _segment_start(con, int(block_prev["block_id"]))
            curr_start = _segment_start(con, int(block_curr["block_id"]))
            if not prev_start or not curr_start:
                return fail("missing production segments after inverse update")
            _assert_minutes(curr_start - prev_start, 5, "inverse scenario")
            pass_msg("OP20 starts after enough OP10 buffer in 5/10 case")

    print("PASS: smoke_scheduler_transfer_dependency completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
