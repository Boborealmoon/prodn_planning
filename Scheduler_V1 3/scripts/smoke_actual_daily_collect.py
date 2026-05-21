from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app import create_app
from scheduler_app.db import db, ensure_db, one


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


def _create_fixture():
    token = uuid.uuid4().hex[:8]
    temp_ps_id = f"ACTUAL-COLLECT-{token}::1"
    temp_part_no = f"ACTUAL-COLLECT-PART-{token}"
    temp_bom_code = f"ACTUAL-COLLECT-BOM-{token}"
    temp_job_no = f"AC-{token.upper()}"

    with db() as con:
        machine = one(con.execute("SELECT * FROM machines WHERE active = 1 ORDER BY machine_id LIMIT 1"))
        if not machine:
            raise RuntimeError("no active machine found for smoke")

        part = one(con.execute(
            """
            INSERT INTO parts (part_no, part_desc)
            VALUES (?, ?)
            RETURNING part_id
            """,
            (temp_part_no, f"Smoke actual collect part {token}"),
        ))
        bom = one(con.execute(
            """
            INSERT INTO bom_variation (part_id, bom_code, bom_desc, is_default)
            VALUES (?, ?, ?, 1)
            RETURNING bom_id
            """,
            (int(part["part_id"]), temp_bom_code, f"Smoke actual collect bom {token}"),
        ))
        seq = one(con.execute(
            """
            INSERT INTO operation_seq (
              bom_id, seq_no, op_no, op_type, machine_category, cycle_time, setup_time, preferred_machine, is_last_op
            ) VALUES (?, 10, '10', 'CUT', ?, 60, 30, ?, 1)
            RETURNING op_seq_id
            """,
            (int(bom["bom_id"]), str(machine["machine_category"] or "PLAN"), str(machine["machine_code"] or "")),
        ))
        con.execute(
            """
            INSERT INTO process_sheet (
              ps_id, part_id, part_no, part_desc, order_date, due_date, total_qty, planned_qty,
              finished_qty, selected_bom_id, planner_status, status, source_ps_id, pp_partial_no
            ) VALUES (?, ?, ?, ?, date('now'), date('now', '+14 day'), ?, 0, 0, ?, 'UNPLANNED', 'ACTIVE', ?, '1')
            """,
            (temp_ps_id, int(part["part_id"]), temp_part_no, f"Smoke actual collect part {token}", 10, int(bom["bom_id"]), temp_ps_id),
        )
        operation = one(con.execute(
            """
            INSERT INTO operation (
              job_no, operation_name, total_qty, setup_minutes, cycle_minutes_per_qty, compatible_machine_group,
              source_ps_id, source_op_seq_id, source_op_no, status, remarks, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', '', CURRENT_TIMESTAMP)
            RETURNING operation_id
            """,
            (temp_job_no, "OP10", 10, 30, 60, str(machine["machine_category"] or "PLAN"), temp_ps_id, int(seq["op_seq_id"]), "10"),
        ))
        block = one(con.execute(
            """
            INSERT INTO run_block (
              operation_id, machine_id, queue_position, scheduled_qty, include_setup, status, planning_status,
              execution_status, anchor_datetime, planned_start_at, planned_end_at, allow_pull_forward,
              active, is_fresh_monday_item, calculated_start_datetime, calculated_end_datetime,
              actual_good_qty, actual_reject_qty, remarks, updated_at
            ) VALUES (?, ?, 1000, 10, 1, 'PLANNED', 'PLANNED', 'NOT_STARTED', '', '', '', 1, 1, 0, '', '', 0, 0, '', CURRENT_TIMESTAMP)
            RETURNING block_id
            """,
            (int(operation["operation_id"]), int(machine["machine_id"])),
        ))
    return {
        "ps_id": temp_ps_id,
        "part_no": temp_part_no,
        "bom_code": temp_bom_code,
        "operation_id": int(operation["operation_id"]),
        "op_seq_id": int(seq["op_seq_id"]),
        "block_id": int(block["block_id"]),
    }


def _cleanup_fixture(fixture):
    with db() as con:
        block_ids = [
            int(row["block_id"])
            for row in con.execute(
                """
                SELECT b.block_id
                FROM run_block b
                JOIN operation o ON o.operation_id = b.operation_id
                WHERE COALESCE(o.source_ps_id, '') = ?
                ORDER BY b.block_id
                """,
                (fixture["ps_id"],),
            )
        ]
        if block_ids:
            placeholders = ",".join("?" for _ in block_ids)
            con.execute(f"DELETE FROM production_actual WHERE block_id IN ({placeholders})", block_ids)
            con.execute(f"DELETE FROM schedule_alert WHERE block_id IN ({placeholders})", block_ids)
            con.execute(f"DELETE FROM machine_queue_state WHERE block_id IN ({placeholders})", block_ids)
            con.execute(f"DELETE FROM run_block_segment WHERE block_id IN ({placeholders})", block_ids)
            con.execute(f"DELETE FROM run_block WHERE block_id IN ({placeholders})", block_ids)
        con.execute("DELETE FROM operation_seq WHERE op_seq_id = ?", (fixture["op_seq_id"],))
        con.execute("DELETE FROM operation WHERE operation_id = ?", (fixture["operation_id"],))
        con.execute("DELETE FROM process_sheet WHERE ps_id = ?", (fixture["ps_id"],))
        con.execute("DELETE FROM bom_variation WHERE bom_code = ?", (fixture["bom_code"],))
        con.execute("DELETE FROM parts WHERE part_no = ?", (fixture["part_no"],))


def main():
    try:
        ensure_db()
        pass_msg("ensure_db() is idempotent")
    except Exception as exc:
        return fail(f"ensure_db() failed: {exc}")

    app = create_app()
    client = app.test_client()
    fixture = None

    try:
        fixture = _create_fixture()

        page_res = client.get("/actual-production")
        if page_res.status_code != 200:
            return fail(f"GET /actual-production returned {page_res.status_code}")
        page_html = page_res.get_data(as_text=True)
        for needle in [
            'data-trial-actual-field="report_date"',
            'data-trial-actual-field="output_qty"',
            'data-trial-actual-field="reject_qty"',
            'data-trial-actual-field="remarks"',
            'trialActualInputKeydown',
            'Collected actual payload',
        ]:
            if needle not in page_html:
                return fail(f"planner page missing {needle}")
        pass_msg("planner page exposes actual daily row fields and keydown/save hooks")

        save_res = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={
                "daily_actuals": [
                    {
                        "report_date": "2099-02-01",
                        "output_qty": "0",
                        "reject_qty": "0",
                        "remarks": "collect smoke",
                    }
                ]
            },
        )
        if save_res.status_code != 200:
            return fail(f"save returned {save_res.status_code}")
        save_data = save_res.get_json() or {}
        if int(save_data.get("saved_count") or 0) != 1:
            return fail(f"saved_count expected 1, got {save_data.get('saved_count')!r}")
        if int(save_data.get("changed_count") or 0) < 1:
            return fail("changed_count should be >= 1")
        pass_msg("save response reports one saved actual row")

        with db() as con:
            saved = one(con.execute(
                """
                SELECT actual_id, report_date, output_qty, reject_qty, remarks
                FROM production_actual
                WHERE block_id = ?
                  AND report_date = ?
                  AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
                ORDER BY actual_id DESC
                LIMIT 1
                """,
                (fixture["block_id"], "2099-02-01"),
            ))
            if not saved:
                return fail("saved actual row not found in database")
            if str(saved["output_qty"]) != '0.0' and float(saved["output_qty"] or 0) != 0.0:
                return fail("saved output_qty is not 0")
            if str(saved["reject_qty"]) != '0.0' and float(saved["reject_qty"] or 0) != 0.0:
                return fail("saved reject_qty is not 0")
        pass_msg("zero actual row persists in database")

        empty_res = client.post(f"/api/trial/blocks/{fixture['block_id']}/actual", json={})
        if empty_res.status_code != 400:
            return fail(f"empty payload expected 400, got {empty_res.status_code}")
        empty_data = empty_res.get_json() or {}
        if empty_data.get("error") != "No actual rows submitted.":
            return fail(f"unexpected empty payload error: {empty_data!r}")
        pass_msg("empty actual payload is rejected clearly")
    except Exception as exc:
        return fail(str(exc))
    finally:
        if fixture:
            _cleanup_fixture(fixture)

    print("PASS: smoke_actual_daily_collect completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
