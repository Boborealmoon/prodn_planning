from __future__ import annotations

import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app import create_app
from scheduler_app.blocks import refresh_block_schedule_bounds
from scheduler_app.db import db, ensure_db, one, rows
from scheduler_app.planning_scheduler import recalculate_planning_all as recalculate_planning_all_baseline


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


def _get_json(client, path):
    res = client.get(path)
    if res.status_code != 200:
        raise RuntimeError(f"GET {path} returned {res.status_code}: {res.get_data(as_text=True)}")
    return res.get_json() or {}


def _find_block(payload, block_id):
    for block in payload.get("blocks") or []:
        if int(block.get("block_id") or 0) == int(block_id):
            return block
    return None


def _planned_rows(block):
    return [row for row in (block.get("actual_daily_rows") or []) if row.get("is_planned_row")]


def _future_total(block, cutoff_date):
    return sum(float(row.get("target_qty") or 0) for row in _planned_rows(block) if str(row.get("report_date") or "") > str(cutoff_date))


def _active_actual_exists(client, block_id, report_date):
    schedule = _get_json(client, "/api/trial/schedule")
    block = _find_block(schedule, block_id)
    if not block:
        return False
    for row in block.get("actual_daily_rows") or []:
        if str(row.get("report_date") or "") == str(report_date) and row.get("is_existing_actual"):
            return True
    return False


def _create_fixture():
    token = uuid.uuid4().hex[:8]
    temp_ps_id = f"TAIL-VAR-{token}::1"
    temp_part_no = f"TAIL-VAR-PART-{token}"
    temp_bom_code = f"TAIL-VAR-BOM-{token}"
    temp_job_no = f"TV-{token.upper()}"

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
            raise RuntimeError("no active machine found for smoke")

        part = one(
            con.execute(
                """
                INSERT INTO parts (part_no, part_desc)
                VALUES (?, ?)
                RETURNING part_id
                """,
                (temp_part_no, f"Smoke tail variance part {token}"),
            )
        )
        bom = one(
            con.execute(
                """
                INSERT INTO bom_variation (part_id, bom_code, bom_desc, is_default)
                VALUES (?, ?, ?, 1)
                RETURNING bom_id
                """,
                (int(part["part_id"]), temp_bom_code, f"Smoke tail variance bom {token}"),
            )
        )
        seq = one(
            con.execute(
                """
                INSERT INTO operation_seq (
                  bom_id, seq_no, op_no, op_type, machine_category, cycle_time, setup_time, preferred_machine, is_last_op
                ) VALUES (?, 10, '10', 'CUT', ?, 1, 0, ?, 1)
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
                f"Smoke tail variance part {token}",
                150,
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
                    150,
                    0,
                    10,
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
                ) VALUES (?, ?, 1000, 150, 1, 'PLANNED', 'PLANNED', 'NOT_STARTED', '', '', '', 1, 1, 0, '', '', 0, 0, '', CURRENT_TIMESTAMP)
                RETURNING block_id
                """,
                (int(operation["operation_id"]), int(machine["machine_id"])),
            )
        )
        recalculate_planning_all_baseline(con, reason="SMOKE_ACTUAL_TAIL_VARIANCE")
        con.execute("DELETE FROM run_block_segment WHERE block_id = ?", (int(block["block_id"]),))
        base_date = date.today()
        for offset, qty in enumerate((50, 50, 50)):
            work_date = base_date + timedelta(days=offset)
            start_dt = datetime.combine(work_date, datetime.min.time()).replace(hour=8, minute=30)
            end_dt = start_dt + timedelta(minutes=qty)
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
                    qty * 10,
                    qty * 10,
                    start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
        refresh_block_schedule_bounds(con, int(block["block_id"]))

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
        con.execute("DELETE FROM production_actual WHERE block_id = ?", (int(fixture["block_id"]),))
        con.execute("DELETE FROM run_block_segment WHERE block_id = ?", (int(fixture["block_id"]),))
        con.execute("DELETE FROM run_block WHERE block_id = ?", (int(fixture["block_id"]),))
        con.execute("DELETE FROM operation WHERE operation_id = ?", (int(fixture["operation_id"]),))
        con.execute("DELETE FROM operation_seq WHERE op_seq_id = ?", (int(fixture["op_seq_id"]),))
        con.execute("DELETE FROM process_sheet WHERE ps_id = ?", (fixture["ps_id"],))
        con.execute("DELETE FROM bom_variation WHERE bom_code = ?", (fixture["bom_code"],))
        con.execute("DELETE FROM parts WHERE part_no = ?", (fixture["part_no"],))


def main():
    try:
        ensure_db()
    except Exception as exc:
        return fail(f"ensure_db failed: {exc}")

    app = create_app()
    client = app.test_client()
    fixture = _create_fixture()

    try:
        schedule = _get_json(client, "/api/trial/schedule")
        block = _find_block(schedule, fixture["block_id"])
        if not block:
            return fail("fixture block not visible in schedule")

        planned_rows = _planned_rows(block)
        if len(planned_rows) != 3:
            return fail(f"expected 3 planned rows, found {len(planned_rows)}")

        original_total = sum(float(row.get("target_qty") or 0) for row in planned_rows)
        first_row = planned_rows[0]
        report_date = str(first_row.get("report_date") or "")
        first_target = float(first_row.get("target_qty") or 0)
        future_before = _future_total(block, report_date)
        if abs((future_before + first_target) - original_total) > 1e-9:
            return fail("Initial planned rows do not add up to the original total")

        save1 = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={
                "daily_actuals": [
                    {
                        "report_date": report_date,
                        "output_qty": 1,
                        "reject_qty": 0,
                        "remarks": "smoke tail save 1",
                    }
                ],
                "delete_actual_dates": [],
            },
        )
        if save1.status_code != 200:
            return fail(f"First save returned {save1.status_code}: {save1.get_data(as_text=True)}")
        data1 = save1.get_json() or {}
        if int(data1.get("saved_count") or 0) != 1:
            return fail("First save did not report saved_count=1")
        change1 = (data1.get("tail_adjustments") or [{}])[0]
        expected_delta1 = 1.0 - first_target
        if abs(float(change1.get("old_variance") or 0) - 0.0) > 1e-9:
            return fail("First save old_variance should be 0")
        if abs(float(change1.get("new_variance") or 0) - expected_delta1) > 1e-9:
            return fail("First save new_variance is incorrect")
        if abs(float(change1.get("variance_delta") or 0) - expected_delta1) > 1e-9:
            return fail("First save variance_delta is incorrect")
        planner_after1 = _get_json(client, "/api/trial/schedule")
        block_after1 = _find_block(planner_after1, fixture["block_id"])
        future_after1 = _future_total(block_after1, report_date)
        if abs((future_after1 + 1.0) - original_total) > 1e-9:
            return fail("First save did not preserve total planned quantity")
        pass_msg("First actual save shaves/adds the tail by exact variance")

        save2 = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={
                "daily_actuals": [
                    {
                        "report_date": report_date,
                        "output_qty": 2,
                        "reject_qty": 0,
                        "remarks": "smoke tail save 2",
                    }
                ],
                "delete_actual_dates": [],
            },
        )
        if save2.status_code != 200:
            return fail(f"Second save returned {save2.status_code}: {save2.get_data(as_text=True)}")
        data2 = save2.get_json() or {}
        change2 = (data2.get("tail_adjustments") or [{}])[0]
        if abs(float(change2.get("old_variance") or 0) - expected_delta1) > 1e-9:
            return fail("Second save old_variance is incorrect")
        expected_new_variance2 = 2.0 - first_target
        if abs(float(change2.get("new_variance") or 0) - expected_new_variance2) > 1e-9:
            return fail("Second save new_variance is incorrect")
        if abs(float(change2.get("variance_delta") or 0) - 1.0) > 1e-9:
            return fail("Second save should change variance by exactly +1")
        planner_after2 = _get_json(client, "/api/trial/schedule")
        block_after2 = _find_block(planner_after2, fixture["block_id"])
        future_after2 = _future_total(block_after2, report_date)
        if abs((future_after2 + 2.0) - original_total) > 1e-9:
            return fail("Second save did not preserve total planned quantity")
        if abs(future_after1 - future_after2 - 1.0) > 1e-9:
            return fail("Second save did not reduce the tail by exactly one unit")
        pass_msg("Correction from 1 to 2 changes tail by exactly one unit")

        save3 = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={
                "daily_actuals": [
                    {
                        "report_date": report_date,
                        "output_qty": 2,
                        "reject_qty": 0,
                        "remarks": "smoke tail save 2 repeat",
                    }
                ],
                "delete_actual_dates": [],
            },
        )
        if save3.status_code != 200:
            return fail(f"Repeat save returned {save3.status_code}: {save3.get_data(as_text=True)}")
        data3 = save3.get_json() or {}
        if data3.get("tail_adjustments"):
            delta = float((data3.get("tail_adjustments") or [{}])[0].get("variance_delta") or 0)
            if abs(delta) > 1e-9:
                return fail("Repeat save should not change variance")
        planner_after3 = _get_json(client, "/api/trial/schedule")
        block_after3 = _find_block(planner_after3, fixture["block_id"])
        future_after3 = _future_total(block_after3, report_date)
        if abs(future_after3 - future_after2) > 1e-9:
            return fail("Repeat save changed the tail unexpectedly")
        pass_msg("Repeat save is idempotent")

        delete_res = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={"daily_actuals": [], "delete_actual_dates": [report_date]},
        )
        if delete_res.status_code != 200:
            return fail(f"Delete actual returned {delete_res.status_code}: {delete_res.get_data(as_text=True)}")
        delete_data = delete_res.get_json() or {}
        change_del = (delete_data.get("tail_adjustments") or [{}])[0]
        if abs(float(change_del.get("old_variance") or 0) - expected_new_variance2) > 1e-9:
            return fail("Delete old_variance is incorrect")
        if abs(float(change_del.get("new_variance") or 0) - 0.0) > 1e-9:
            return fail("Delete new_variance should be 0")
        planner_after_delete = _get_json(client, "/api/trial/schedule")
        block_after_delete = _find_block(planner_after_delete, fixture["block_id"])
        future_after_delete = _future_total(block_after_delete, report_date)
        if abs(future_after_delete - future_before) > 1e-9:
            return fail("Delete did not restore the original future tail")
        if _active_actual_exists(client, fixture["block_id"], report_date):
            return fail("Deleted actual row is still active")
        pass_msg("Delete reverses the tail by the exact saved variance")

        print("PASS: smoke_actual_tail_variance completed successfully")
        return 0
    finally:
        _cleanup_fixture(fixture)


if __name__ == "__main__":
    sys.exit(main())
