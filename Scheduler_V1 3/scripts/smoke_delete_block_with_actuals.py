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
            (f"SMOKE-DEL-ACT-PART-{tag}", "Delete smoke part"),
        )
    )
    bom = one(
        con.execute(
            """
            INSERT INTO bom_variation (part_id, bom_code, bom_desc, is_default)
            VALUES (?, ?, ?, 1)
            RETURNING bom_id
            """,
            (int(part["part_id"]), f"SMOKE-DEL-ACT-BOM-{tag}", "Delete smoke BOM"),
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
                f"SMOKE-DEL-ACT-{tag}",
                f"Delete Actual Smoke {tag}",
                10,
                60,
                30,
                "SMOKE",
                f"SMOKE-DEL-ACT-PS::{tag}",
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


def _fetch_block_ids(payload):
    return {int(item.get("block_id") or 0) for item in (payload.get("blocks") or []) if int(item.get("block_id") or 0)}


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
        recalculate_machine(con, machine_id, reason="SMOKE_DELETE_WITH_ACTUALS")
        con.execute(
            """
            INSERT INTO production_actual (
              block_id, machine_id, report_date, remarks, reported_at,
              output_qty, reject_qty, target_qty_at_report, status, entry_type, created_by
            ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, 'ACTIVE', 'MANUAL', '')
            """,
            (
                ids["block_id"],
                machine_id,
                "2099-01-01",
                "smoke delete with actuals",
                1.0,
                0.0,
                1.0,
            ),
        )
        actual_before = one(
            con.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM production_actual
                WHERE block_id = ?
                  AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
                """,
                (ids["block_id"],),
            )
        )
        if int((actual_before or {})["cnt"] if actual_before else 0) <= 0:
            return fail("failed to seed active actuals for delete smoke")

    delete_resp = client.delete(f"/api/trial/blocks/{ids['block_id']}")
    if delete_resp.status_code != 200:
        return fail(f"delete with actuals failed: {delete_resp.status_code} {delete_resp.get_data(as_text=True)}")

    with db() as con:
        block_row = one(con.execute("SELECT block_id FROM run_block WHERE block_id = ?", (ids["block_id"],)))
        if block_row:
            return fail("run_block row still exists after delete")
        actual_row = one(
            con.execute(
                """
                SELECT actual_id
                FROM production_actual
                WHERE block_id = ?
                  AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
                LIMIT 1
                """,
                (ids["block_id"],),
            )
        )
        if actual_row:
            return fail("active production_actual row still exists after delete")

    for path in ("/api/trial/schedule", "/api/trial/planner/schedule"):
        resp = client.get(path)
        if resp.status_code != 200:
            return fail(f"GET {path} returned {resp.status_code}")
        payload = resp.get_json() or {}
        if ids["block_id"] in _fetch_block_ids(payload):
            return fail(f"{path} still returns deleted block")
    pass_msg("deleted block is absent from both schedule APIs")

    print("PASS: smoke_delete_block_with_actuals completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
