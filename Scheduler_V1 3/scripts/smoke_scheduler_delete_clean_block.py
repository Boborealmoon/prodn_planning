from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app.factory import create_app
from scheduler_app.blocks import recalculate_machine
from scheduler_app.db import db, ensure_db, one


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


def _insert_temp_block(con, machine_id, tag, planned_end_at):
    part = one(
        con.execute(
            """
            INSERT INTO parts (part_no, part_desc)
            VALUES (?, ?)
            RETURNING part_id
            """,
            (f"SMOKE-DEL-PART-{tag}", "Delete smoke part"),
        )
    )
    bom = one(
        con.execute(
            """
            INSERT INTO bom_variation (part_id, bom_code, bom_desc, is_default)
            VALUES (?, ?, ?, 1)
            RETURNING bom_id
            """,
            (int(part["part_id"]), f"SMOKE-DEL-BOM-{tag}", "Delete smoke BOM"),
        )
    )
    seq = one(
        con.execute(
            """
            INSERT INTO operation_seq (bom_id, seq_no, op_no, op_type, machine_category, cycle_time, setup_time, preferred_machine, is_last_op)
            VALUES (?, 10, '10', 'CUT', 'SMOKE', 30, 60, '', 1)
            RETURNING op_seq_id
            """,
            (int(bom["bom_id"]),),
        )
    )
    op = one(
        con.execute(
            """
            INSERT INTO operation (
              job_no, operation_name, total_qty, setup_minutes, cycle_minutes_per_qty, compatible_machine_group,
              source_ps_id, source_op_seq_id, source_op_no, status, remarks, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', '', CURRENT_TIMESTAMP)
            RETURNING operation_id
            """,
            (
                f"SMOKE-DEL-{tag}",
                f"Delete Smoke {tag}",
                10,
                60,
                30,
                "SMOKE",
                f"SMOKE-DEL-PS::{tag}",
                int(seq["op_seq_id"]),
                "10",
            ),
        )
    )
    block = one(
        con.execute(
            """
            INSERT INTO run_block (
              operation_id, machine_id, queue_position, scheduled_qty, include_setup, status, planning_status, execution_status,
              anchor_datetime, planned_start_at, planned_end_at, allow_pull_forward, active, is_fresh_monday_item,
              calculated_start_datetime, calculated_end_datetime, actual_good_qty, actual_reject_qty, remarks, updated_at
            ) VALUES (?, ?, 10, 10, 1, 'NOT_STARTED', 'PLANNED', 'NOT_STARTED', ?, ?, ?, 1, 1, 0, '', '', 0, 0, '', CURRENT_TIMESTAMP)
            RETURNING block_id
            """,
            (
                int(op["operation_id"]),
                int(machine_id),
                "2099-01-01 08:30:00",
                "2099-01-01 08:30:00",
                planned_end_at,
            ),
        )
    )
    return {
        "part_id": int(part["part_id"]),
        "bom_id": int(bom["bom_id"]),
        "op_seq_id": int(seq["op_seq_id"]),
        "operation_id": int(op["operation_id"]),
        "block_id": int(block["block_id"]),
    }


def _cleanup_temp_block(con, ids):
    block_id = int(ids["block_id"])
    operation_id = int(ids["operation_id"])
    op_seq_id = int(ids["op_seq_id"])
    bom_id = int(ids["bom_id"])
    part_id = int(ids["part_id"])

    con.execute("DELETE FROM production_actual WHERE block_id = ?", (block_id,))
    con.execute("DELETE FROM schedule_alert WHERE block_id = ?", (block_id,))
    con.execute("DELETE FROM machine_queue_state WHERE block_id = ?", (block_id,))
    con.execute("DELETE FROM run_block_segment WHERE block_id = ?", (block_id,))
    con.execute("DELETE FROM run_block WHERE block_id = ?", (block_id,))
    con.execute("DELETE FROM operation WHERE operation_id = ?", (operation_id,))
    con.execute("DELETE FROM operation_seq WHERE op_seq_id = ?", (op_seq_id,))
    con.execute("DELETE FROM bom_variation WHERE bom_id = ?", (bom_id,))
    con.execute("DELETE FROM parts WHERE part_id = ?", (part_id,))


def main():
    try:
        ensure_db()
        pass_msg("ensure_db() is idempotent")
    except Exception as exc:
        return fail(f"ensure_db() failed: {exc}")

    app = create_app()
    app.testing = True
    client = app.test_client()

    with db() as con:
        machine = one(con.execute("SELECT machine_id FROM machines WHERE active = 1 ORDER BY machine_id LIMIT 1"))
        if not machine:
            return fail("no active machine found")
        machine_id = int(machine["machine_id"])
        tag = uuid4().hex[:8]
        ids = _insert_temp_block(con, machine_id, tag, "2099-01-01 08:31:00")
        recalculate_machine(con, machine_id, reason="SMOKE_DELETE_CLEAN_BLOCK")

    delete_resp = client.delete(f"/api/trial/blocks/{ids['block_id']}")
    if delete_resp.status_code != 200:
        return fail(f"delete clean block failed: {delete_resp.status_code} {delete_resp.get_data(as_text=True)}")

    with db() as con:
        for table, query in (
            ("run_block", "SELECT block_id FROM run_block WHERE block_id = ?"),
            ("run_block_segment", "SELECT segment_id FROM run_block_segment WHERE block_id = ?"),
            ("machine_queue_state", "SELECT block_id FROM machine_queue_state WHERE block_id = ?"),
            ("schedule_alert", "SELECT alert_id FROM schedule_alert WHERE block_id = ?"),
        ):
            row = one(con.execute(query, (ids["block_id"],)))
            if row:
                return fail(f"{table} row still exists after delete")
    pass_msg("clean delete removes run_block, segments, queue state, and alerts")

    with db() as con:
        machine = one(con.execute("SELECT machine_id FROM machines WHERE active = 1 ORDER BY machine_id LIMIT 1"))
        if not machine:
            return fail("no active machine found for rejection test")
        machine_id = int(machine["machine_id"])
        tag = uuid4().hex[:8]
        ids_reject = _insert_temp_block(con, machine_id, tag, "2099-01-01 08:31:00")
        recalculate_machine(con, machine_id, reason="SMOKE_DELETE_REJECT")
        con.execute(
            """
            INSERT INTO production_actual (
              block_id, machine_id, report_date, remarks, reported_at,
              output_qty, reject_qty, target_qty_at_report, status, entry_type, created_by
            ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, 'ACTIVE', 'MANUAL', '')
            """,
            (
                ids_reject["block_id"],
                machine_id,
                "2099-01-01",
                "smoke delete rejection",
                1.0,
                0.0,
                1.0,
            ),
        )

    reject_resp = client.delete(f"/api/trial/blocks/{ids_reject['block_id']}")
    if reject_resp.status_code == 200:
        return fail("delete unexpectedly succeeded for block with active actuals")

    with db() as con:
        block = one(con.execute("SELECT block_id FROM run_block WHERE block_id = ?", (ids_reject["block_id"],)))
        if not block:
            return fail("block with active actuals disappeared after rejected delete")
        _cleanup_temp_block(con, ids_reject)
    pass_msg("delete rejects blocks with active actuals and leaves them intact")

    print("PASS: smoke_scheduler_delete_clean_block completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
