from __future__ import annotations

import sys
import uuid
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
    temp_ps_id = f"SAVE-ONLY-{token}::1"
    temp_part_no = f"SAVE-ONLY-PART-{token}"
    temp_bom_code = f"SAVE-ONLY-BOM-{token}"
    temp_job_no = f"SO-{token.upper()}"

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
                (temp_part_no, f"Smoke save-only part {token}"),
            )
        )
        bom = one(
            con.execute(
                """
                INSERT INTO bom_variation (part_id, bom_code, bom_desc, is_default)
                VALUES (?, ?, ?, 1)
                RETURNING bom_id
                """,
                (int(part["part_id"]), temp_bom_code, f"Smoke save-only bom {token}"),
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
            (temp_ps_id, int(part["part_id"]), temp_part_no, f"Smoke save-only part {token}", 150, int(bom["bom_id"]), temp_ps_id),
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
                (temp_job_no, "OP10", 150, 0, 15, str(machine["machine_category"] or "PLAN"), temp_ps_id, int(seq["op_seq_id"]), "10"),
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


def _schedule_block(client, block_id):
    res = client.get("/api/trial/schedule")
    if res.status_code != 200:
        raise RuntimeError(f"schedule returned {res.status_code}")
    payload = res.get_json() or {}
    for block in payload.get("blocks") or []:
        if int(block.get("block_id") or 0) == int(block_id):
            return block
    raise RuntimeError("fixture block not found")


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

        report_date = "2099-04-01"
        res1 = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={
                "apply_tail_adjustments": False,
                "daily_actuals": [
                    {
                        "report_date": report_date,
                        "target_qty": "50",
                        "output_qty": "1",
                        "reject_qty": "0",
                        "remarks": "save-only smoke",
                    }
                ],
            },
        )
        if res1.status_code != 200:
            return fail(f"save returned {res1.status_code}: {res1.get_data(as_text=True)}")
        data1 = res1.get_json() or {}
        if int(data1.get("saved_count") or 0) != 1:
            return fail(f"saved_count expected 1, got {data1.get('saved_count')!r}")
        if len(data1.get("debug_actual_save", {}).get("inserted_actual_ids") or []) != 1:
            return fail("inserted_actual_ids should contain one row")

        with db() as con:
            active = one(
                con.execute(
                    """
                    SELECT *
                    FROM production_actual
                    WHERE block_id = ?
                      AND report_date = ?
                      AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
                    ORDER BY actual_id DESC
                    LIMIT 1
                    """,
                    (fixture["block_id"], report_date),
                )
            )
        if not active:
            return fail("ACTIVE production_actual row not found after save")
        if float(active["output_qty"] or 0) != 1.0 or float(active["reject_qty"] or 0) != 0.0:
            return fail(f"saved row values mismatch: {dict(active)}")
        if (active["remarks"] or "").strip() != "save-only smoke":
            return fail("remarks were not saved correctly")
        pass_msg("first save persisted in DB")

        schedule = _schedule_block(client, fixture["block_id"])
        rows_in_schedule = schedule.get("actual_daily_rows") or []
        if not any(
            str(row.get("report_date") or "") == report_date and abs(float(row.get("output_qty") or 0) - 1.0) < 1e-9
            for row in rows_in_schedule
        ):
            return fail("schedule actual_daily_rows does not include the saved actual")
        pass_msg("schedule reflects the saved actual")

        res2 = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={
                "apply_tail_adjustments": False,
                "daily_actuals": [
                    {
                        "report_date": report_date,
                        "target_qty": "50",
                        "output_qty": "2",
                        "reject_qty": "0",
                        "remarks": "save-only smoke update",
                    }
                ],
            },
        )
        if res2.status_code != 200:
            return fail(f"update returned {res2.status_code}: {res2.get_data(as_text=True)}")
        data2 = res2.get_json() or {}
        if int(data2.get("saved_count") or 0) != 1:
            return fail(f"update saved_count expected 1, got {data2.get('saved_count')!r}")
        with db() as con:
            active_rows = rows(
                con.execute(
                    """
                    SELECT *
                    FROM production_actual
                    WHERE block_id = ?
                      AND report_date = ?
                      AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
                    ORDER BY actual_id DESC
                    """,
                    (fixture["block_id"], report_date),
                )
            )
            voided_rows = rows(
                con.execute(
                    """
                    SELECT *
                    FROM production_actual
                    WHERE block_id = ?
                      AND report_date = ?
                      AND status = 'VOIDED'
                    ORDER BY actual_id DESC
                    """,
                    (fixture["block_id"], report_date),
                )
            )
        if len(active_rows) != 1:
            return fail(f"expected exactly one ACTIVE row after update, got {len(active_rows)}")
        if float(active_rows[0]["output_qty"] or 0) != 2.0:
            return fail(f"updated active row output not 2: {dict(active_rows[0])}")
        if not voided_rows:
            return fail("previous row was not voided on update")
        pass_msg("update replaced the active row cleanly")

        res3 = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={
                "apply_tail_adjustments": False,
                "delete_actual_dates": [report_date],
            },
        )
        if res3.status_code != 200:
            return fail(f"delete returned {res3.status_code}: {res3.get_data(as_text=True)}")
        with db() as con:
            final_active = one(
                con.execute(
                    """
                    SELECT *
                    FROM production_actual
                    WHERE block_id = ?
                      AND report_date = ?
                      AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
                    """,
                    (fixture["block_id"], report_date),
                )
            )
        if final_active:
            return fail("ACTIVE production_actual row still exists after delete")
        pass_msg("delete removed the active actual row")

        return 0
    except Exception as exc:
        return fail(f"smoke failed: {exc}")
    finally:
        if fixture:
            _cleanup_fixture(fixture)


if __name__ == "__main__":
    sys.exit(main())
