from __future__ import annotations

import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app import create_app
from scheduler_app.blocks import recalculate_machine
from scheduler_app.db import db, ensure_db, one, rows


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


def _planned_rows(block):
    return [row for row in (block.get("actual_daily_rows") or []) if row.get("is_planned_row")]


def _schedule_block(client, block_id):
    res = client.get("/api/trial/schedule")
    if res.status_code != 200:
        raise RuntimeError(f"schedule returned {res.status_code}")
    payload = res.get_json() or {}
    for block in payload.get("blocks") or []:
        if int(block.get("block_id") or 0) == int(block_id):
            return block
    raise RuntimeError("fixture block not found")


def _create_fixture():
    token = uuid.uuid4().hex[:8]
    temp_ps_id = f"RECON-{token}::1"
    temp_part_no = f"RECON-PART-{token}"
    temp_bom_code = f"RECON-BOM-{token}"
    temp_job_no = f"RC-{token.upper()}"

    with db() as con:
        machine = one(con.execute("SELECT * FROM machines WHERE active = 1 ORDER BY machine_id LIMIT 1"))
        if not machine:
            raise RuntimeError("no active machine found for smoke")

        part = one(
            con.execute(
                """
                INSERT INTO parts (part_no, part_desc)
                VALUES (?, ?)
                RETURNING part_id
                """,
                (temp_part_no, f"Smoke reconciliation part {token}"),
            )
        )
        bom = one(
            con.execute(
                """
                INSERT INTO bom_variation (part_id, bom_code, bom_desc, is_default)
                VALUES (?, ?, ?, 1)
                RETURNING bom_id
                """,
                (int(part["part_id"]), temp_bom_code, f"Smoke reconciliation bom {token}"),
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
                (int(bom["bom_id"]), str(machine["machine_category"] or "PLAN"), str(machine["machine_code"] or "")),
            )
        )
        con.execute(
            """
            INSERT INTO process_sheet (
              ps_id, part_id, part_no, part_desc, order_date, due_date, total_qty, planned_qty,
              finished_qty, selected_bom_id, planner_status, status, source_ps_id, pp_partial_no
            ) VALUES (?, ?, ?, ?, date('now'), date('now', '+14 day'), ?, 0, 0, ?, 'UNPLANNED', 'ACTIVE', ?, '1')
            """,
            (temp_ps_id, int(part["part_id"]), temp_part_no, f"Smoke reconciliation part {token}", 150, int(bom["bom_id"]), temp_ps_id),
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
                (temp_job_no, "OP10", 150, 0, 1, str(machine["machine_category"] or "PLAN"), temp_ps_id, int(seq["op_seq_id"]), "10"),
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

        base_date = date.today() + timedelta(days=1)
        for offset, qty in enumerate((50, 50, 50)):
            work_date = base_date + timedelta(days=offset)
            start_dt = datetime.combine(work_date, datetime.min.time()).replace(hour=8, minute=0)
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
                    qty,
                    qty,
                    start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )

    return {
        "part_no": temp_part_no,
        "bom_code": temp_bom_code,
        "ps_id": temp_ps_id,
        "operation_id": int(operation["operation_id"]),
        "op_seq_id": int(seq["op_seq_id"]),
        "block_id": int(block["block_id"]),
        "machine_id": int(machine["machine_id"]),
    }


def _cleanup_fixture(fixture):
    with db() as con:
        con.execute("DELETE FROM block_removed_actual_date WHERE block_id = ?", (int(fixture["block_id"]),))
        con.execute("DELETE FROM production_actual WHERE block_id = ?", (int(fixture["block_id"]),))
        con.execute("DELETE FROM schedule_alert WHERE block_id = ?", (int(fixture["block_id"]),))
        con.execute("DELETE FROM machine_queue_state WHERE block_id = ?", (int(fixture["block_id"]),))
        con.execute("DELETE FROM planning_schedule_segment WHERE block_id = ?", (int(fixture["block_id"]),))
        con.execute("DELETE FROM planning_block_state WHERE block_id = ?", (int(fixture["block_id"]),))
        con.execute("DELETE FROM run_block_segment WHERE block_id = ?", (int(fixture["block_id"]),))
        con.execute("DELETE FROM run_block WHERE block_id = ?", (int(fixture["block_id"]),))
        con.execute("DELETE FROM operation WHERE operation_id = ?", (int(fixture["operation_id"]),))
        con.execute("DELETE FROM operation_seq WHERE op_seq_id = ?", (int(fixture["op_seq_id"]),))
        con.execute("DELETE FROM process_sheet WHERE ps_id = ?", (fixture["ps_id"],))
        con.execute("DELETE FROM bom_variation WHERE bom_code = ?", (fixture["bom_code"],))
        con.execute("DELETE FROM parts WHERE part_no = ?", (fixture["part_no"],))


def _future_total(block, cutoff_date):
    return sum(float(row.get("target_qty") or 0) for row in _planned_rows(block) if str(row.get("report_date") or "") > str(cutoff_date))


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
        before = _schedule_block(client, fixture["block_id"])
        rows_before = _planned_rows(before)
        if len(rows_before) != 3:
            return fail(f"expected 3 planned rows, got {len(rows_before)}")
        day1 = str(rows_before[0].get("report_date") or "").strip()
        day2 = str(rows_before[1].get("report_date") or "").strip()
        day3 = str(rows_before[2].get("report_date") or "").strip()
        if [float(r.get("target_qty") or 0) for r in rows_before] != [50.0, 50.0, 50.0]:
            return fail(f"expected 50/50/50 planned targets, got {rows_before}")
        pass_msg(f"seeded exact 50/50/50 planned rows: {day1}, {day2}, {day3}")

        res1 = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={
                "daily_actuals": [
                    {"report_date": day1, "target_qty": "50", "output_qty": "1", "reject_qty": "0", "remarks": "recon"}
                ]
            },
        )
        if res1.status_code != 200:
            return fail(f"first save returned {res1.status_code}: {res1.get_data(as_text=True)}")
        data1 = res1.get_json() or {}
        recon1 = data1.get("reconciliation") or {}
        if int(data1.get("saved_count") or 0) != 1:
            return fail(f"saved_count expected 1, got {data1.get('saved_count')!r}")
        if abs(float(recon1.get("scheduled_qty") or 0) - 150.0) > 1e-6:
            return fail(f"scheduled_qty expected 150, got {recon1}")
        if abs(float(recon1.get("active_actual_good_qty") or 0) - 1.0) > 1e-6:
            return fail(f"active_actual_good_qty expected 1, got {recon1}")
        if abs(float(recon1.get("future_required_qty") or 0) - 149.0) > 1e-6:
            return fail(f"future_required_qty expected 149, got {recon1}")
        if abs(float(recon1.get("delta") or 0) - 49.0) > 1e-6:
            return fail(f"delta expected 49, got {recon1}")
        if not any(str(row.get("report_date") or "") == day1 and abs(float(row.get("output_qty") or 0) - 1.0) < 1e-6 for row in data1.get("actual_daily_rows") or []):
            return fail("day1 actual row not present after first save")
        future_total_1 = _future_total(data1.get("block") or {}, day1)
        if abs(future_total_1 - 149.0) > 1e-6:
            return fail(f"future total after first save should be 149, got {future_total_1}")
        pass_msg("first save reconciles to 149 future qty with day1 actual = 1")

        res2 = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={
                "daily_actuals": [
                    {"report_date": day1, "target_qty": "50", "output_qty": "2", "reject_qty": "0", "remarks": "recon"}
                ]
            },
        )
        if res2.status_code != 200:
            return fail(f"correction returned {res2.status_code}: {res2.get_data(as_text=True)}")
        data2 = res2.get_json() or {}
        recon2 = data2.get("reconciliation") or {}
        if abs(float(recon2.get("future_required_qty") or 0) - 148.0) > 1e-6:
            return fail(f"future_required_qty expected 148, got {recon2}")
        if abs(float(recon2.get("delta") or 0) + 1.0) > 1e-6:
            return fail(f"delta expected -1, got {recon2}")
        if not any(str(row.get("report_date") or "") == day1 and abs(float(row.get("output_qty") or 0) - 2.0) < 1e-6 for row in data2.get("actual_daily_rows") or []):
            return fail("day1 actual row not present after correction to 2")
        if abs(_future_total(data2.get("block") or {}, day1) - 148.0) > 1e-6:
            return fail("future total after correction should be 148")
        pass_msg("correction reconciles to 148 future qty with day1 actual = 2")

        res3 = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={
                "daily_actuals": [
                    {"report_date": day1, "target_qty": "50", "output_qty": "2", "reject_qty": "0", "remarks": "recon"}
                ]
            },
        )
        if res3.status_code != 200:
            return fail(f"idempotent save returned {res3.status_code}: {res3.get_data(as_text=True)}")
        data3 = res3.get_json() or {}
        recon3 = data3.get("reconciliation") or {}
        if abs(float(recon3.get("delta") or 0)) > 1e-6:
            return fail(f"idempotent save should have delta 0, got {recon3}")
        if abs(_future_total(data3.get("block") or {}, day1) - 148.0) > 1e-6:
            return fail("future total should remain 148 on idempotent save")
        pass_msg("idempotent save makes no further change")

        res_removed = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={"removed_target_dates": [day2]},
        )
        if res_removed.status_code != 200:
            return fail(f"removed_target_dates returned {res_removed.status_code}: {res_removed.get_data(as_text=True)}")
        data_removed = res_removed.get_json() or {}
        if int(data_removed.get("removed_target_count") or 0) != 1:
            return fail(f"removed_target_count expected 1, got {data_removed.get('removed_target_count')!r}")
        if day2 not in (data_removed.get("removed_actual_dates") or []):
            return fail("removed day2 should be present in removed_actual_dates")
        if any(str(row.get("report_date") or "") == day2 and row.get("is_planned_row") for row in data_removed.get("actual_daily_rows") or []):
            return fail("removed day2 should not appear as a planned row")
        if abs(sum(float(row.get("target_qty") or 0) for row in _planned_rows(data_removed.get("block") or {})) - 148.0) > 1e-6:
            return fail("planned total after removing day2 should be 148")
        pass_msg("removed middle date moves its qty into the tail")

        res4 = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={"delete_actual_dates": [day1]},
        )
        if res4.status_code != 200:
            return fail(f"delete actual returned {res4.status_code}: {res4.get_data(as_text=True)}")
        data4 = res4.get_json() or {}
        if int(data4.get("deleted_count") or 0) < 1:
            return fail("delete_count should be at least 1")
        if any(str(row.get("report_date") or "") == day1 for row in data4.get("actuals_active") or []):
            return fail("day1 should not appear in actuals_active after delete")
        future_total_4 = sum(float(row.get("target_qty") or 0) for row in _planned_rows(data4.get("block") or {}))
        if abs(future_total_4 - 150.0) > 1e-6:
            return fail(f"planned total after deleting actual should be 150, got {future_total_4}")
        pass_msg("delete actual restores future qty to 150")

        with db() as con:
            recalculate_machine(con, fixture["machine_id"])
        again = _schedule_block(client, fixture["block_id"])
        if any(str(row.get("report_date") or "") == day2 for row in _planned_rows(again)):
            return fail("removed middle date came back after machine recalc")
        pass_msg("recalculation does not bring the removed date back")

        return 0
    except Exception as exc:
        return fail(f"smoke failed: {exc}")
    finally:
        if fixture:
            _cleanup_fixture(fixture)


if __name__ == "__main__":
    sys.exit(main())
