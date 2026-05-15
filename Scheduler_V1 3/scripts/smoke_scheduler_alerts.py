from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app.blocks import recalculate_machine
from scheduler_app.db import db, ensure_db, one


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


def _insert_temp_block(con, machine_id, tag, planned_end_at):
    part = one(
        con.execute(
            """
            INSERT INTO parts (part_no, part_desc)
            VALUES (?, ?)
            RETURNING part_id
            """,
            (f"SMOKE-ALERT-PART-{tag}", "Alert smoke part"),
        )
    )
    bom = one(
        con.execute(
            """
            INSERT INTO bom_variation (part_id, bom_code, bom_desc, is_default)
            VALUES (?, ?, ?, 1)
            RETURNING bom_id
            """,
            (int(part["part_id"]), f"SMOKE-ALERT-BOM-{tag}", "Alert smoke BOM"),
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
                f"SMOKE-ALERT-{tag}",
                f"Alert Smoke {tag}",
                10,
                60,
                30,
                "SMOKE",
                f"SMOKE-ALERT-PS::{tag}",
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
    con.execute("DELETE FROM schedule_alert WHERE block_id = ?", (int(ids["block_id"]),))
    con.execute("DELETE FROM machine_queue_state WHERE block_id = ?", (int(ids["block_id"]),))
    con.execute("DELETE FROM run_block_segment WHERE block_id = ?", (int(ids["block_id"]),))
    con.execute("DELETE FROM run_block WHERE block_id = ?", (int(ids["block_id"]),))
    con.execute("DELETE FROM operation WHERE operation_id = ?", (int(ids["operation_id"]),))
    con.execute("DELETE FROM operation_seq WHERE op_seq_id = ?", (int(ids["op_seq_id"]),))
    con.execute("DELETE FROM bom_variation WHERE bom_id = ?", (int(ids["bom_id"]),))
    con.execute("DELETE FROM parts WHERE part_id = ?", (int(ids["part_id"]),))


def _open_alert_ids(con, block_id):
    return [
        int(row["alert_id"])
        for row in con.execute(
            """
            SELECT alert_id
            FROM schedule_alert
            WHERE block_id = ?
              AND alert_type = 'SCHEDULE_DELAYED'
              AND status IN ('OPEN', 'ACKNOWLEDGED')
            ORDER BY alert_id
            """,
            (int(block_id),),
        )
    ]


def main():
    try:
        ensure_db()
        pass_msg("ensure_db() is idempotent")
    except Exception as exc:
        return fail(f"ensure_db() failed: {exc}")

    with db() as con:
        machine = one(con.execute("SELECT machine_id FROM machines WHERE active = 1 ORDER BY machine_id LIMIT 1"))
        if not machine:
            return fail("no active machine found")
        machine_id = int(machine["machine_id"])

        with savepoint(con, "smoke_alerts"):
            tag = uuid4().hex[:8]
            ids = _insert_temp_block(con, machine_id, tag, "2099-01-01 08:31:00")
            recalculate_machine(con, machine_id, reason="SMOKE_ALERTS")

            open_ids = _open_alert_ids(con, ids["block_id"])
            if len(open_ids) != 1:
                return fail(f"expected 1 open delayed alert after first recalc, got {len(open_ids)}")
            first_alert_id = open_ids[0]
            pass_msg("delay alert is created once")

            recalculate_machine(con, machine_id, reason="SMOKE_ALERTS")
            open_ids_again = _open_alert_ids(con, ids["block_id"])
            if len(open_ids_again) != 1:
                return fail("recalc created duplicate open delayed alerts")
            if open_ids_again[0] != first_alert_id:
                return fail("recalc did not reuse the existing delayed alert row")
            pass_msg("recalc reuses the same open alert row")

            con.execute(
                "UPDATE run_block SET planned_end_at = ? WHERE block_id = ?",
                ("2099-01-01 23:59:00", ids["block_id"]),
            )
            recalculate_machine(con, machine_id, reason="SMOKE_ALERTS")
            open_ids_after_fix = _open_alert_ids(con, ids["block_id"])
            if open_ids_after_fix:
                return fail("delayed alert remained open after planned_end_at was moved later")
            resolved = one(
                con.execute(
                    """
                    SELECT status, resolved_at
                    FROM schedule_alert
                    WHERE alert_id = ?
                    """,
                    (first_alert_id,),
                )
            )
            if not resolved or resolved["status"] != "RESOLVED":
                return fail("delayed alert was not resolved after the schedule was fixed")
            pass_msg("delay alert resolves after the schedule is fixed")

    print("PASS: smoke_scheduler_alerts completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
