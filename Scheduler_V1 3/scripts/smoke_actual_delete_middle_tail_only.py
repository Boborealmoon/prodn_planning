from __future__ import annotations

import sys
import uuid
from datetime import date
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


def _create_fixture():
    token = uuid.uuid4().hex[:8]
    temp_ps_id = f"DEL-MID-{token}::1"
    temp_part_no = f"DEL-MID-PART-{token}"
    temp_bom_code = f"DEL-MID-BOM-{token}"
    temp_job_no = f"DM-{token.upper()}"

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
                (temp_part_no, f"Smoke delete-middle part {token}"),
            )
        )
        bom = one(
            con.execute(
                """
                INSERT INTO bom_variation (part_id, bom_code, bom_desc, is_default)
                VALUES (?, ?, ?, 1)
                RETURNING bom_id
                """,
                (int(part["part_id"]), temp_bom_code, f"Smoke delete-middle bom {token}"),
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
            (temp_ps_id, int(part["part_id"]), temp_part_no, f"Smoke delete-middle part {token}", 150, int(bom["bom_id"]), temp_ps_id),
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
                (temp_job_no, "OP10", 150, 0, 20, str(machine["machine_category"] or "PLAN"), temp_ps_id, int(seq["op_seq_id"]), "10"),
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
        recalculate_machine(con, int(machine["machine_id"]))

    return {
        "ps_id": temp_ps_id,
        "part_no": temp_part_no,
        "bom_code": temp_bom_code,
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


def _schedule_block(client, block_id):
    res = client.get("/api/trial/schedule")
    if res.status_code != 200:
        raise RuntimeError(f"schedule returned {res.status_code}")
    payload = res.get_json() or {}
    for block in payload.get("blocks") or []:
        if int(block.get("block_id") or 0) == int(block_id):
            return block
    raise RuntimeError("fixture block not found")


def _planned_rows(block):
    return [row for row in (block.get("actual_daily_rows") or []) if row.get("is_planned_row")]


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
        if len(rows_before) < 3:
            return fail(f"expected 3 planned rows, got {len(rows_before)}")

        day1 = str(rows_before[0].get("report_date") or "").strip()
        day2 = str(rows_before[1].get("report_date") or "").strip()
        day3 = str(rows_before[2].get("report_date") or "").strip()
        day2_qty = float(rows_before[1].get("target_qty") or 0)
        total_before = sum(float(row.get("target_qty") or 0) for row in rows_before)
        if day2_qty <= 0:
            return fail("middle day target must be greater than 0")
        pass_msg(f"seeded planned rows: {day1}, {day2}, {day3}")

        res = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={"removed_target_dates": [day2]},
        )
        if res.status_code != 200:
            return fail(f"removed_target_dates returned {res.status_code}")
        payload = res.get_json() or {}
        if int(payload.get("removed_target_count") or 0) != 1:
            return fail(f"removed_target_count expected 1, got {payload.get('removed_target_count')!r}")
        if abs(float(payload.get("removed_target_qty") or 0) - day2_qty) > 1e-6:
            return fail("removed_target_qty does not match day2 target")

        after = _schedule_block(client, fixture["block_id"])
        planned_after = _planned_rows(after)
        if any(str(row.get("report_date") or "") == day2 for row in planned_after):
            return fail("removed middle date still appears in planned rows after save")
        total_after = sum(float(row.get("target_qty") or 0) for row in planned_after)
        if abs(total_after - total_before) > 1e-6:
            return fail(f"total planned qty changed after removal: before={total_before}, after={total_after}")
        if str(after.get("actual_start_at") or "") == day2:
            return fail("actual_start_at should not be the removed date")

        with db() as con:
            recalculate_machine(con, fixture["machine_id"])

        recalced = _schedule_block(client, fixture["block_id"])
        recalced_rows = _planned_rows(recalced)
        if any(str(row.get("report_date") or "") == day2 for row in recalced_rows):
            return fail("removed middle date came back after recalculation")
        recalced_total = sum(float(row.get("target_qty") or 0) for row in recalced_rows)
        if abs(recalced_total - total_before) > 1e-6:
            return fail(f"total planned qty changed after recalculation: before={total_before}, after={recalced_total}")
        future_before = sum(float(row.get("target_qty") or 0) for row in rows_before if str(row.get("report_date") or "") > day2)
        future_after = sum(float(row.get("target_qty") or 0) for row in recalced_rows if str(row.get("report_date") or "") > day2)
        if abs(future_after - (future_before + day2_qty)) > 1e-6:
            return fail(
                f"removed qty did not move to future tail as expected: before_future={future_before}, "
                f"after_future={future_after}, removed={day2_qty}"
            )

        pass_msg("middle date stays removed and its quantity is preserved at the tail")
        return 0
    except Exception as exc:
        return fail(f"smoke failed: {exc}")
    finally:
        if fixture:
            _cleanup_fixture(fixture)


if __name__ == "__main__":
    sys.exit(main())
