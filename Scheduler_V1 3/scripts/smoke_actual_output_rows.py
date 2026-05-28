from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app import create_app
from scheduler_app.blocks import actual_daily_rows_for_block_row, recalculate_machine, trial_block_row
from scheduler_app.db import db, one, rows


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


def _parse_date(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:19] if len(text) >= 19 else text)
    except ValueError:
        return None


def _choose_machine(con):
    machine = one(
        con.execute(
            """
            SELECT *
            FROM machines
            WHERE active = 1
            ORDER BY machine_id
            LIMIT 1
            """
        )
    )
    if not machine:
        raise RuntimeError("No active machine available for actual output smoke.")
    return machine


def _insert_operation_and_block(con, machine, token, scheduled_qty, queue_position):
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
                f"ACTUAL-ROWS-{token.upper()}",
                f"Smoke Op {token}",
                float(scheduled_qty or 0),
                30.0,
                10.0,
                str(machine["machine_category"] or "PLAN"),
                f"ACTUAL-ROWS-PS-{token}",
                10,
                "10",
            ),
        )
    )
    block = one(
        con.execute(
            """
            INSERT INTO run_block (
              operation_id, machine_id, queue_position, scheduled_qty, include_setup, status, planning_status,
              execution_status, anchor_datetime, planned_start_at, planned_end_at, allow_pull_forward,
              active, is_fresh_monday_item, calculated_start_datetime, calculated_end_datetime,
              actual_good_qty, actual_reject_qty, remarks, updated_at
            ) VALUES (?, ?, ?, ?, 1, 'NOT_STARTED', 'PLANNED', 'NOT_STARTED', '', '', '', 1, 1, 0, '', '', 0, 0, '', CURRENT_TIMESTAMP)
            RETURNING block_id
            """,
            (int(op["operation_id"]), int(machine["machine_id"]), float(queue_position), float(scheduled_qty or 0)),
        )
    )
    return int(op["operation_id"]), int(block["block_id"])


def _create_fixture():
    token = uuid.uuid4().hex[:8]
    with db() as con:
        machine = _choose_machine(con)
        op_id, scheduled_block_id = _insert_operation_and_block(con, machine, token, scheduled_qty=6, queue_position=10)
        _, empty_block_id = _insert_operation_and_block(con, machine, token + "-empty", scheduled_qty=0, queue_position=20)
    return {
        "machine_id": int(machine["machine_id"]),
        "operation_id": op_id,
        "scheduled_block_id": scheduled_block_id,
        "empty_block_id": empty_block_id,
    }


def _cleanup_fixture(fixture):
    with db() as con:
        for block_id in (fixture["scheduled_block_id"], fixture["empty_block_id"]):
            con.execute("DELETE FROM schedule_alert WHERE block_id = ?", (int(block_id),))
            con.execute("DELETE FROM machine_queue_state WHERE block_id = ?", (int(block_id),))
            con.execute("DELETE FROM production_actual WHERE block_id = ?", (int(block_id),))
            con.execute("DELETE FROM run_block_segment WHERE block_id = ?", (int(block_id),))
            con.execute("DELETE FROM run_block WHERE block_id = ?", (int(block_id),))
        con.execute("DELETE FROM operation WHERE operation_id = ?", (int(fixture["operation_id"]),))


def main():
    app = create_app()
    fixture = _create_fixture()
    client = app.test_client()
    try:
        with db() as con:
            before_segments = rows(
                con.execute(
                    "SELECT segment_id FROM run_block_segment WHERE block_id = ? AND COALESCE(segment_type, '') = 'production'",
                    (fixture["scheduled_block_id"],),
                )
            )
            if before_segments:
                return fail("fixture block unexpectedly already had production segments")

        res = client.get(f"/api/trial/blocks/{fixture['scheduled_block_id']}")
        if res.status_code != 200:
            return fail(f"GET scheduled block returned {res.status_code}")
        data = res.get_json() or {}
        block = data.get("block") or {}
        daily_rows = block.get("actual_daily_rows") or []
        if not daily_rows:
            return fail("scheduled block did not return actual_daily_rows after refresh")
        if not all(row.get("report_date") and row.get("target_qty") is not None for row in daily_rows):
            return fail("actual_daily_rows missing report_date or target_qty")
        pass_msg("scheduled block returns production target rows")

        with db() as con:
            refreshed_block = trial_block_row(con, fixture["scheduled_block_id"])
            planned_rows = actual_daily_rows_for_block_row(con, refreshed_block)
            planned_dates = [str(row.get("report_date") or "") for row in planned_rows if row.get("is_planned_row")]
        if not planned_dates:
            return fail("planned rows were not produced for the scheduled block")
        add_date = (_parse_date(planned_dates[0]) - timedelta(days=1)) if _parse_date(planned_dates[0]) else (datetime.now() - timedelta(days=1))
        add_date_text = add_date.strftime("%Y-%m-%d")

        save_res = client.post(
            f"/api/trial/blocks/{fixture['scheduled_block_id']}/actual",
            json={
                "daily_actuals": [
                    {
                        "report_date": add_date_text,
                        "target_qty": 0,
                        "output_qty": 1,
                        "reject_qty": 0,
                        "remarks": "smoke actual output add date",
                    }
                ]
            },
        )
        if save_res.status_code != 200:
            return fail(f"POST actual returned {save_res.status_code}")
        save_data = save_res.get_json() or {}
        save_rows = save_data.get("actual_daily_rows") or []
        if not any(str(row.get("report_date") or "") == add_date_text for row in save_rows):
            return fail("saved actual date did not appear in actual_daily_rows")
        pass_msg("Add Date actual row saves and appears in returned rows")

        empty_res = client.get(f"/api/trial/blocks/{fixture['empty_block_id']}")
        if empty_res.status_code != 200:
            return fail(f"GET empty block returned {empty_res.status_code}")
        empty_data = empty_res.get_json() or {}
        empty_block = empty_data.get("block") or {}
        if empty_block.get("actual_daily_rows"):
            return fail("empty block unexpectedly returned daily rows")
        if empty_block.get("actual_daily_rows_error") != "NO_PRODUCTION_SEGMENTS":
            return fail("empty block did not expose NO_PRODUCTION_SEGMENTS diagnostic")
        pass_msg("empty block returns diagnostic without fake target rows")

        print("PASS: actual output daily rows smoke completed")
        return 0
    finally:
        _cleanup_fixture(fixture)


if __name__ == "__main__":
    raise SystemExit(main())
