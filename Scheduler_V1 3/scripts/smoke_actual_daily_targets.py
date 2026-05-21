from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app import create_app
from scheduler_app.db import db, ensure_db, one, rows
from scheduler_app.planning_scheduler import recalculate_planning_all as recalculate_planning_all_baseline


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


def _parse_date(value):
    text = str(value or "").strip().replace("T", " ")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:19] if len(text) >= 19 else text)
    except ValueError:
        return None


def _get_schedule(client):
    res = client.get("/api/trial/schedule")
    if res.status_code != 200:
        raise RuntimeError(f"GET /api/trial/schedule returned {res.status_code}")
    return res.get_json() or {}


def _find_block(data, block_id):
    for block in data.get("blocks") or []:
        if int(block.get("block_id") or 0) == int(block_id):
            return block
    return None


def _create_fixture():
    token = uuid.uuid4().hex[:8]
    temp_ps_id = f"DAILY-TARGET-{token}::1"
    temp_part_no = f"DAILY-TARGET-PART-{token}"
    temp_bom_code = f"DAILY-TARGET-BOM-{token}"
    temp_job_no = f"DT-{token.upper()}"

    with db() as con:
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
            raise RuntimeError("no active machine found for actual daily smoke")

        part = one(
            con.execute(
                """
                INSERT INTO parts (part_no, part_desc)
                VALUES (?, ?)
                RETURNING part_id
                """,
                (temp_part_no, f"Actual daily smoke part {token}"),
            )
        )
        bom = one(
            con.execute(
                """
                INSERT INTO bom_variation (part_id, bom_code, bom_desc, is_default)
                VALUES (?, ?, ?, 1)
                RETURNING bom_id
                """,
                (int(part["part_id"]), temp_bom_code, f"Actual daily smoke bom {token}"),
            )
        )
        seq = one(
            con.execute(
                """
                INSERT INTO operation_seq (
                  bom_id, seq_no, op_no, op_type, machine_category, cycle_time, setup_time, preferred_machine, is_last_op
                ) VALUES (?, 10, '10', 'CUT', ?, 60, 30, ?, 1)
                RETURNING op_seq_id
                """,
                (
                    int(bom["bom_id"]),
                    str(machine["machine_category"] or "PLAN"),
                    str(machine["machine_code"] or ""),
                ),
            )
        )
        con.execute(
            """
            INSERT INTO process_sheet (
              ps_id, part_id, part_no, part_desc, order_date, due_date, total_qty, planned_qty,
              finished_qty, selected_bom_id, planner_status, status, source_ps_id, pp_partial_no
            ) VALUES (?, ?, ?, ?, date('now'), date('now', '+14 day'), ?, 0, 0, ?, 'UNPLANNED', 'ACTIVE', ?, '1')
            """,
            (
                temp_ps_id,
                int(part["part_id"]),
                temp_part_no,
                f"Actual daily smoke part {token}",
                100,
                int(bom["bom_id"]),
                temp_ps_id,
            ),
        )
        operation = one(
            con.execute(
                """
                INSERT INTO operation (
                  job_no, operation_name, total_qty, setup_minutes, cycle_minutes_per_qty, compatible_machine_group,
                  source_ps_id, source_op_seq_id, source_op_no, status, remarks, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', '', CURRENT_TIMESTAMP)
                RETURNING operation_id
                """,
                (
                    temp_job_no,
                    "OP10",
                    100,
                    30,
                    60,
                    str(machine["machine_category"] or "PLAN"),
                    temp_ps_id,
                    int(seq["op_seq_id"]),
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
                ) VALUES (?, ?, 1000, 100, 1, 'PLANNED', 'PLANNED', 'NOT_STARTED', '', '', '', 1, 1, 0, '', '', 0, 0, '', CURRENT_TIMESTAMP)
                RETURNING block_id
                """,
                (
                    int(operation["operation_id"]),
                    int(machine["machine_id"]),
                ),
            )
        )
        recalculate_planning_all_baseline(con, reason="SMOKE_ACTUAL_DAILY_TARGETS")
        con.execute("DELETE FROM run_block_segment WHERE block_id = ?", (int(block["block_id"]),))
        base_date = datetime.now().date() + timedelta(days=1)
        for offset, qty in enumerate((34, 33, 33), start=0):
            work_date = base_date + timedelta(days=offset)
            start_dt = datetime.combine(work_date, datetime.min.time()).replace(hour=8, minute=30)
            end_dt = start_dt + timedelta(minutes=qty * 60)
            con.execute(
                """
                INSERT INTO run_block_segment (
                  block_id, machine_id, schedule_run_id, segment_date, segment_type,
                  qty_done, planned_qty, minutes_used, planned_minutes, segment_status, start_datetime, end_datetime, is_actual
                ) VALUES (?, ?, NULL, ?, 'production', ?, ?, ?, ?, 'PLANNED', ?, ?, 0)
                """,
                (
                    int(block["block_id"]),
                    int(machine["machine_id"]),
                    work_date.isoformat(),
                    qty,
                    qty,
                    qty * 60,
                    qty * 60,
                    start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
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
            for row in rows(
                con.execute(
                    """
                    SELECT b.block_id
                    FROM run_block b
                    JOIN operation o ON o.operation_id = b.operation_id
                    WHERE COALESCE(o.source_ps_id, '') = ?
                    ORDER BY b.block_id
                    """,
                    (fixture["ps_id"],),
                )
            )
        ]
        if block_ids:
            placeholders = ",".join("?" for _ in block_ids)
            con.execute(f"DELETE FROM production_actual WHERE block_id IN ({placeholders})", block_ids)
            con.execute(f"DELETE FROM schedule_alert WHERE block_id IN ({placeholders})", block_ids)
            con.execute(f"DELETE FROM machine_queue_state WHERE block_id IN ({placeholders})", block_ids)
            con.execute(f"DELETE FROM planning_schedule_segment WHERE block_id IN ({placeholders})", block_ids)
            con.execute(f"DELETE FROM planning_block_state WHERE block_id IN ({placeholders})", block_ids)
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
        ensure_db()
        pass_msg("ensure_db() is idempotent")
    except Exception as exc:
        return fail(f"ensure_db() failed: {exc}")

    app = create_app()
    client = app.test_client()
    fixture = None

    try:
        fixture = _create_fixture()
        data = _get_schedule(client)
        block = _find_block(data, fixture["block_id"])
        if not block:
            return fail("fixture block missing from planner schedule")

        daily_rows = block.get("actual_daily_rows") or []
        if len(daily_rows) < 2:
            return fail("expected planned daily rows for multiple dates")
        if not all("target_qty" in row for row in daily_rows):
            return fail("actual_daily_rows missing target_qty")
        pass_msg("planner schedule returns planned daily target rows")

        first_planned_date = next((str(row.get("report_date") or "").strip() for row in daily_rows if str(row.get("report_date") or "").strip()), "")
        if not first_planned_date:
            return fail("planned daily rows missing report_date")
        planned_dt = _parse_date(first_planned_date)
        if not planned_dt:
            return fail("planned report_date is not parseable")
        earlier_date = (planned_dt.date() - timedelta(days=3)).isoformat()

        post_res = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={
                "daily_actuals": [
                    {
                        "report_date": earlier_date,
                        "output_qty": 10,
                        "reject_qty": 0,
                        "remarks": "",
                    }
                ]
            },
        )
        if post_res.status_code != 200:
            return fail(f"actual save returned {post_res.status_code}")

        with db() as con:
            saved = one(
                con.execute(
                    """
                    SELECT report_date, output_qty, reject_qty
                    FROM production_actual
                    WHERE block_id = ?
                      AND report_date = ?
                      AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
                    """,
                    (fixture["block_id"], earlier_date),
                )
            )
        if not saved:
            return fail("saved production_actual row missing for earlier report_date")
        if str(saved["report_date"] or "") != earlier_date:
            return fail("production_actual.report_date is not date-only")
        pass_msg("actual save stores report_date as the chosen day")

        planner_res = client.get("/api/trial/planner/schedule")
        if planner_res.status_code != 200:
            return fail(f"GET /api/trial/planner/schedule returned {planner_res.status_code}")
        block = _find_block(planner_res.get_json() or {}, fixture["block_id"])
        if not block:
            return fail("fixture block missing after actual save")
        if not str(block.get("actual_start_at") or "").startswith(earlier_date):
            return fail(f"actual_start_at did not use the earlier report_date; got {block.get('actual_start_at')!r}")
        if not block.get("actual_end_at") and float(block.get("actual_good_qty") or 0) <= 0:
            pass_msg("actual start is derived from the earliest report_date")
        else:
            pass_msg("actual start and progress are reflected in the planner schedule")

        delete_res = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={
                "delete_actual_dates": [earlier_date],
            },
        )
        if delete_res.status_code != 200:
            return fail(f"delete_actual_dates request returned {delete_res.status_code}")

        planner_res = client.get("/api/trial/planner/schedule")
        if planner_res.status_code != 200:
            return fail(f"GET /api/trial/planner/schedule returned {planner_res.status_code}")
        block = _find_block(planner_res.get_json() or {}, fixture["block_id"])
        if not block:
            return fail("fixture block missing after delete_actual_dates")
        if str(block.get("actual_start_at") or ""):
            return fail("actual_start_at should clear after deleting the only actual date")
        pass_msg("deleting the saved date clears derived actual start")

        print("PASS: smoke_actual_daily_targets completed successfully")
        return 0
    finally:
        if fixture:
            try:
                _cleanup_fixture(fixture)
            except Exception as exc:
                print(f"WARN: fixture cleanup failed: {exc}")


if __name__ == "__main__":
    sys.exit(main())
