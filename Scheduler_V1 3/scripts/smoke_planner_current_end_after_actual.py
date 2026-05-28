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


def _schedule(client):
    res = client.get("/api/trial/planner/schedule")
    if res.status_code != 200:
        raise RuntimeError(f"GET /api/trial/planner/schedule returned {res.status_code}")
    return res.get_json() or {}


def _find_block(payload, block_id):
    for block in payload.get("blocks") or []:
        if int(block.get("block_id") or 0) == int(block_id):
            return block
    return None


def _create_fixture():
    token = uuid.uuid4().hex[:8]
    temp_ps_id = f"CUR-END-SMOKE-{token}::1"
    temp_part_no = f"CUR-END-SMOKE-PART-{token}"
    temp_bom_code = f"CUR-END-SMOKE-BOM-{token}"
    temp_job_no = f"CE-{token.upper()}"
    anchor_dt = (datetime.now().replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=1))
    anchor_text = anchor_dt.strftime("%Y-%m-%d %H:%M:%S")

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
            raise RuntimeError("no active machine found for current-end smoke")

        part = one(
            con.execute(
                """
                INSERT INTO parts (part_no, part_desc)
                VALUES (?, ?)
                RETURNING part_id
                """,
                (temp_part_no, f"Planner current-end smoke part {token}"),
            )
        )
        bom = one(
            con.execute(
                """
                INSERT INTO bom_variation (part_id, bom_code, bom_desc, is_default)
                VALUES (?, ?, ?, 1)
                RETURNING bom_id
                """,
                (int(part["part_id"]), temp_bom_code, f"Planner current-end smoke bom {token}"),
            )
        )
        seq = one(
            con.execute(
                """
                INSERT INTO operation_seq (
                  bom_id, seq_no, op_no, op_type, machine_category, cycle_time, setup_time, preferred_machine, is_last_op
                ) VALUES (?, 10, '10', 'CUT', ?, 30, 45, ?, 1)
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
                f"Planner current-end smoke part {token}",
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
                    45,
                    30,
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
                ) VALUES (?, ?, 1000, 150, 1, 'PLANNED', 'PLANNED', 'NOT_STARTED', ?, ?, '', 0, 1, 0, '', '', 0, 0, '', CURRENT_TIMESTAMP)
                RETURNING block_id
                """,
                (
                    int(operation["operation_id"]),
                    int(machine["machine_id"]),
                    anchor_text,
                    anchor_text,
                ),
            )
        )
        recalculate_planning_all_baseline(con, reason="SMOKE_PLANNER_CURRENT_END")

    return {
        "ps_id": temp_ps_id,
        "part_no": temp_part_no,
        "bom_code": temp_bom_code,
        "op_seq_id": int(seq["op_seq_id"]),
        "operation_id": int(operation["operation_id"]),
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


def _pick_first_actual_row(block):
    rows_list = block.get("actual_daily_rows") or []
    if rows_list:
        return rows_list[0]
    planned = str(block.get("planned_start_at") or block.get("expected_start_at") or block.get("calculated_start_datetime") or "").strip()
    if not planned:
        return None
    return {
        "report_date": planned[:10],
        "target_qty": float(block.get("planned_qty") or block.get("scheduled_qty") or 0),
    }


def _current_end(block):
    if float(block.get("actual_good_qty") or 0) + 1e-9 >= float(block.get("planned_qty") or block.get("scheduled_qty") or 0):
        if str(block.get("actual_end_at") or "").strip():
            return str(block.get("actual_end_at") or "").strip()
    return (
        str(
            block.get("forecast_end_at")
            or block.get("predicted_end_at")
            or block.get("expected_end_at")
            or block.get("calculated_end_datetime")
            or block.get("planned_end_at")
            or ""
        ).strip()
    )


def main():
    fixture = None
    try:
        ensure_db()
    except Exception as exc:
        return fail(f"ensure_db() failed: {exc}")

    app = create_app()
    client = app.test_client()

    try:
        fixture = _create_fixture()
        schedule = _schedule(client)
        block = _find_block(schedule, fixture["block_id"])
        if not block:
            return fail("fixture block missing from planner schedule")

        for field in (
            "planned_start_at",
            "planned_end_at",
            "expected_start_at",
            "expected_end_at",
            "visual_start_datetime",
            "visual_end_datetime",
            "forecast_start_at",
            "forecast_end_at",
            "calculated_start_datetime",
            "calculated_end_datetime",
            "actual_start_at",
            "actual_end_at",
            "actual_good_qty",
            "actual_row_count",
            "scheduled_qty",
            "planned_qty",
        ):
            if field not in block:
                return fail(f"planner schedule block missing {field}")

        forecast_end_at = str(block.get("forecast_end_at") or block.get("calculated_end_datetime") or "").strip()
        visual_end_at = str(block.get("visual_end_datetime") or "").strip()
        if not visual_end_at or not forecast_end_at:
            print("DEBUG BLOCK:", block)
            return fail("fixture block missing scheduler-derived visual/current end")
        pass_msg("planner schedule exposes planned and forecast end fields separately")

        first_row = _pick_first_actual_row(block)
        if not first_row:
            return fail("fixture block has no actual_daily_rows to save against")
        report_date = str(first_row.get("report_date") or "").strip()
        target_qty = first_row.get("target_qty")
        if not report_date:
            return fail("could not determine report_date for actual save smoke")

        save_one = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={
                "apply_tail_adjustments": False,
                "daily_actuals": [
                    {
                        "report_date": report_date,
                        "target_qty": target_qty,
                        "output_qty": 1,
                        "reject_qty": 0,
                        "remarks": "planner-current-end-smoke",
                    }
                ],
            },
        )
        if save_one.status_code != 200:
            return fail(f"first actual save returned {save_one.status_code}")
        save_one_json = save_one.get_json() or {}
        if int(save_one_json.get("saved_count") or 0) < 1:
            return fail(f"first actual save did not report a saved row: {save_one_json!r}")

        schedule = _schedule(client)
        block = _find_block(schedule, fixture["block_id"])
        if not block:
            return fail("fixture block missing after first actual save")
        if not str(block.get("actual_start_at") or "").strip():
            return fail("actual_start_at was not populated after first actual save")
        if str(block.get("actual_end_at") or "").strip():
            return fail("actual_end_at should stay blank while target is not met")
        if not str(block.get("forecast_end_at") or block.get("expected_end_at") or block.get("calculated_end_datetime") or block.get("planned_end_at") or "").strip():
            return fail("forecast/current end is missing after first actual save")
        if _current_end(block) != str(
            block.get("forecast_end_at")
            or block.get("predicted_end_at")
            or block.get("expected_end_at")
            or block.get("calculated_end_datetime")
            or block.get("planned_end_at")
            or ""
        ).strip():
            return fail("Current end did not resolve to forecast/current end while incomplete")
        pass_msg("incomplete actuals resolve current end from forecast/current fields")

        save_two = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={
                "apply_tail_adjustments": False,
                "daily_actuals": [
                    {
                        "report_date": report_date,
                        "target_qty": target_qty,
                        "output_qty": 2,
                        "reject_qty": 0,
                        "remarks": "planner-current-end-smoke",
                    }
                ],
            },
        )
        if save_two.status_code != 200:
            return fail(f"second actual save returned {save_two.status_code}")
        save_two_json = save_two.get_json() or {}
        if int(save_two_json.get("saved_count") or 0) < 1:
            return fail(f"second actual save did not report a saved row: {save_two_json!r}")

        schedule = _schedule(client)
        block = _find_block(schedule, fixture["block_id"])
        if not block:
            return fail("fixture block missing after second actual save")
        if float(block.get("actual_good_qty") or 0) < 2:
            return fail("actual_good_qty did not reflect corrected output")
        if str(block.get("actual_end_at") or "").strip():
            return fail("actual_end_at should still be blank after partial completion")
        if _current_end(block) != str(
            block.get("forecast_end_at")
            or block.get("predicted_end_at")
            or block.get("expected_end_at")
            or block.get("calculated_end_datetime")
            or block.get("planned_end_at")
            or ""
        ).strip():
            return fail("Current end should still resolve to forecast/current fields while incomplete")
        pass_msg("current end remains forecast-driven while the block is incomplete")

        save_complete = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={
                "apply_tail_adjustments": False,
                "daily_actuals": [
                    {
                        "report_date": report_date,
                        "target_qty": target_qty,
                        "output_qty": float(block.get("planned_qty") or block.get("scheduled_qty") or 150),
                        "reject_qty": 0,
                        "remarks": "planner-current-end-smoke",
                    }
                ],
            },
        )
        if save_complete.status_code != 200:
            return fail(f"completion actual save returned {save_complete.status_code}")
        save_complete_json = save_complete.get_json() or {}
        if int(save_complete_json.get("saved_count") or 0) < 1:
            return fail(f"completion actual save did not report a saved row: {save_complete_json!r}")

        schedule = _schedule(client)
        block = _find_block(schedule, fixture["block_id"])
        if not block:
            return fail("fixture block missing after completion actual save")
        if not str(block.get("actual_end_at") or "").strip():
            return fail("actual_end_at was not populated after target was met")
        if _current_end(block) != str(block.get("actual_end_at") or "").strip():
            return fail("Current end did not switch to actual_end_at after completion")
        pass_msg("completed actuals switch current end to actual_end_at")

        print("PASS: smoke_planner_current_end_after_actual completed successfully")
        return 0
    finally:
        if fixture:
            try:
                _cleanup_fixture(fixture)
            except Exception as exc:
                print(f"WARN: fixture cleanup failed: {exc}")


if __name__ == "__main__":
    sys.exit(main())
