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


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


def _create_fixture():
    token = uuid.uuid4().hex[:8]
    temp_part_no = f"DELDATE-PART-{token}"
    temp_bom_code = f"DELDATE-BOM-{token}"
    temp_job_no = f"DD-{token.upper()}"
    temp_ps_id = f"DELDATE-PS-{token}::1"

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
                (temp_part_no, f"Smoke delete-date part {token}"),
            )
        )
        bom = one(
            con.execute(
                """
                INSERT INTO bom_variation (part_id, bom_code, bom_desc, is_default)
                VALUES (?, ?, ?, 1)
                RETURNING bom_id
                """,
                (int(part["part_id"]), temp_bom_code, f"Smoke delete-date bom {token}"),
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
            (temp_ps_id, int(part["part_id"]), temp_part_no, f"Smoke delete-date part {token}", 150, int(bom["bom_id"]), temp_ps_id),
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

        con.execute("DELETE FROM run_block_segment WHERE block_id = ?", (int(block["block_id"]),))
        base_date = (datetime.now().date() + timedelta(days=1))
        for offset in range(3):
            work_date = base_date + timedelta(days=offset)
            start_dt = datetime.combine(work_date, datetime.min.time()).replace(hour=8, minute=0)
            end_dt = start_dt + timedelta(minutes=50)
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
                    50,
                    50,
                    50,
                    50,
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
            con.execute(f"DELETE FROM block_removed_actual_date WHERE block_id IN ({placeholders})", block_ids)
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


def _block_from_schedule(client, block_id):
    res = client.get("/api/trial/schedule")
    if res.status_code != 200:
        raise RuntimeError(f"schedule returned {res.status_code}")
    data = res.get_json() or {}
    blocks = data.get("blocks") or []
    for block in blocks:
        if int(block.get("block_id") or 0) == int(block_id):
            return block
    raise RuntimeError("fixture block not found in schedule")


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

        before = _block_from_schedule(client, fixture["block_id"])
        daily_rows = before.get("actual_daily_rows") or []
        if len(daily_rows) < 3:
            return fail(f"expected at least 3 planned rows, got {len(daily_rows)}")
        first_date = str(daily_rows[0].get("report_date") or "").strip()
        second_date = str(daily_rows[1].get("report_date") or "").strip()
        first_target = float(daily_rows[0].get("target_qty") or 0)
        total_before = sum(float(row.get("target_qty") or 0) for row in daily_rows)
        if first_target <= 0:
            return fail("first planned target should be greater than 0")
        pass_msg(f"planned rows seeded: first={first_date} target={first_target}")

        delete_res = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={"removed_target_dates": [first_date]},
        )
        if delete_res.status_code != 200:
            return fail(f"removed_target_dates returned {delete_res.status_code}")
        delete_data = delete_res.get_json() or {}
        if int(delete_data.get("removed_target_count") or 0) != 1:
            return fail(f"removed_target_count expected 1, got {delete_data.get('removed_target_count')!r}")
        if abs(float(delete_data.get("removed_target_qty") or 0) - first_target) > 1e-6:
            return fail("removed_target_qty does not match the deleted date target")

        after = _block_from_schedule(client, fixture["block_id"])
        after_rows = after.get("actual_daily_rows") or []
        if any(str(row.get("report_date") or "") == first_date for row in after_rows):
            return fail("removed target date still appears after refresh")
        total_after = sum(float(row.get("target_qty") or 0) for row in after_rows)
        if abs(total_after - total_before) > 1e-6:
            return fail(f"total target expected {total_before}, got {total_after}; rows={after_rows}")
        if str(after.get("actual_start_at") or "") == first_date:
            return fail("actual_start_at should not be the deleted date")
        pass_msg("planned target date is removed and its target moves to the tail")

        save_res = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={
                "daily_actuals": [
                    {
                        "report_date": second_date,
                        "output_qty": 10,
                        "reject_qty": 0,
                        "remarks": "delete smoke",
                    }
                ]
            },
        )
        if save_res.status_code != 200:
            return fail(f"save actual returned {save_res.status_code}")

        remove_saved_res = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={"delete_actual_dates": [second_date], "removed_target_dates": [second_date]},
        )
        if remove_saved_res.status_code != 200:
            return fail(f"delete saved actual returned {remove_saved_res.status_code}")

        with db() as con:
            active_actual = one(
                con.execute(
                    """
                    SELECT actual_id
                    FROM production_actual
                    WHERE block_id = ?
                      AND report_date = ?
                      AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
                    LIMIT 1
                    """,
                    (fixture["block_id"], second_date),
                )
            )
            if active_actual:
                return fail("saved actual row still active after delete date")
        final_block = _block_from_schedule(client, fixture["block_id"])
        final_rows = final_block.get("actual_daily_rows") or []
        if any(str(row.get("report_date") or "") == second_date for row in final_rows):
            return fail("saved deleted date still appears in actual_daily_rows")
        pass_msg("saved actual date is voided and removed from the modal")

    except Exception as exc:
        return fail(str(exc))
    finally:
        if fixture:
            _cleanup_fixture(fixture)

    print("PASS: smoke_actual_delete_date_moves_target completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
