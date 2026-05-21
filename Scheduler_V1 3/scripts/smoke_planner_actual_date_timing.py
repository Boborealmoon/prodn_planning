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
from scheduler_app.machines import is_public_holiday
from scheduler_app.planning_scheduler import recalculate_planning_all as recalculate_planning_all_baseline


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


def _current_start(block):
    return (
        block.get("actual_start_at")
        or block.get("expected_start_at")
        or block.get("predicted_start_at")
        or block.get("calculated_start_datetime")
        or ""
    )


def _current_end(block):
    if block.get("actual_end_at"):
        return block.get("actual_end_at")
    return (
        block.get("expected_end_at")
        or block.get("predicted_end_at")
        or block.get("calculated_end_datetime")
        or ""
    )


def _current_status(block):
    if block.get("actual_end_at"):
        return "Completed"
    if block.get("actual_start_at"):
        return "In progress forecast"
    return "Forecast"


def _parse_date(value):
    text = str(value or "").strip().replace("T", " ")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:19] if len(text) >= 19 else text)
    except ValueError:
        return None


def _get_schedule(client):
    res = client.get("/api/trial/planner/schedule")
    if res.status_code != 200:
        raise RuntimeError(f"GET /api/trial/planner/schedule returned {res.status_code}")
    return res.get_json() or {}


def _find_block(data, block_id):
    for block in data.get("blocks") or []:
        if int(block.get("block_id") or 0) == int(block_id):
            return block
    return None


def _create_fixture():
    token = uuid.uuid4().hex[:8]
    temp_ps_id = f"TIMING-SMOKE-{token}::1"
    temp_part_no = f"TIMING-SMOKE-PART-{token}"
    temp_bom_code = f"TIMING-SMOKE-BOM-{token}"
    temp_job_no = f"TS-{token.upper()}"

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
            raise RuntimeError("no active machine found for timing smoke")

        part = one(
            con.execute(
                """
                INSERT INTO parts (part_no, part_desc)
                VALUES (?, ?)
                RETURNING part_id
                """,
                (temp_part_no, f"Planner timing smoke part {token}"),
            )
        )
        bom = one(
            con.execute(
                """
                INSERT INTO bom_variation (part_id, bom_code, bom_desc, is_default)
                VALUES (?, ?, ?, 1)
                RETURNING bom_id
                """,
                (int(part["part_id"]), temp_bom_code, f"Planner timing smoke bom {token}"),
            )
        )
        seq = one(
            con.execute(
                """
                INSERT INTO operation_seq (
                  bom_id, seq_no, op_no, op_type, machine_category, cycle_time, setup_time, preferred_machine, is_last_op
                ) VALUES (?, 10, '10', 'CUT', ?, 12, 20, ?, 1)
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
                f"Planner timing smoke part {token}",
                4,
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
                    4,
                    20,
                    12,
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
                ) VALUES (?, ?, 1000, 4, 1, 'PLANNED', 'PLANNED', 'NOT_STARTED', '', '', '', 1, 1, 0, '', '', 0, 0, '', CURRENT_TIMESTAMP)
                RETURNING block_id
                """,
                (
                    int(operation["operation_id"]),
                    int(machine["machine_id"]),
                ),
            )
        )
        recalculate_planning_all_baseline(con, reason="SMOKE_PLANNER_TIMING_UI")

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
        planner_res = client.get("/planner")
        if planner_res.status_code != 200:
            return fail(f"/planner returned {planner_res.status_code}")
        planner_html = planner_res.get_data(as_text=True)
        if "Planned" not in planner_html or "Current" not in planner_html:
            return fail("/planner does not contain Planned and Current labels")
        if "planner-block-alert" not in planner_html:
            return fail("/planner does not include block alert markup")
        if "dismissPlannerAlert" not in planner_html:
            return fail("/planner does not include dismissPlannerAlert")
        if '<div id="planner-alerts"' in planner_html:
            return fail("/planner still renders a global alert panel container")
        pass_msg("/planner exposes the simplified timing labels and block-attached alerts")

        actual_res = client.get("/trial")
        if actual_res.status_code != 200:
            return fail(f"/trial returned {actual_res.status_code}")
        actual_html = actual_res.get_data(as_text=True)
        if "Actual output date" not in actual_html:
            return fail("/trial does not expose Actual output date in the actual-entry UI")
        if "trialSaveActualRowDate" not in actual_html:
            return fail("/trial does not include the actual-date save helper")
        pass_msg("/trial exposes the editable actual output date UI")

        data = _get_schedule(client)
        blocks = data.get("blocks") or []
        if not blocks:
            return fail("planner schedule returned no blocks")
        sample = blocks[0]
        for field in ("planned_start_at", "planned_end_at", "expected_start_at", "expected_end_at", "actual_start_at", "actual_end_at"):
            if field not in sample:
                return fail(f"planner schedule block missing {field}")
        if "alerts" not in data:
            return fail("planner schedule missing alerts array")
        pass_msg("planner schedule still returns the backend timing and alert fields")

        with db() as con:
            for seg in rows(
                con.execute(
                    """
                    SELECT segment_date
                    FROM run_block_segment
                    WHERE segment_date IS NOT NULL AND segment_date <> ''
                    ORDER BY segment_date
                    """
                )
            ):
                seg_date = str(seg["segment_date"] or "").strip()
                if not seg_date:
                    continue
                seg_day = _parse_date(seg_date)
                if not seg_day:
                    continue
                if seg_day.weekday() >= 5:
                    return fail(f"planner schedule contains weekend segment: {seg_date}")
                if is_public_holiday(con, seg_day.date()):
                    return fail(f"planner schedule contains public-holiday segment: {seg_date}")
        pass_msg("planner schedule avoids weekends and public holidays")

        fixture = _create_fixture()
        data = _get_schedule(client)
        block = _find_block(data, fixture["block_id"])
        if not block:
            return fail("timing smoke fixture block missing from planner schedule")
        planned_start = str(block.get("planned_start_at") or block.get("expected_start_at") or block.get("calculated_start_datetime") or "")
        if not planned_start:
            return fail("fixture block missing planned start")
        planned_dt = _parse_date(planned_start)
        if not planned_dt:
            return fail("fixture planned start is not parseable")
        earlier_date = (planned_dt.date() - timedelta(days=3)).isoformat()

        post_res = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={
                "daily_actuals": [
                    {
                        "report_date": earlier_date,
                        "output_qty": 1,
                        "reject_qty": 0,
                        "remarks": "timing-smoke",
                    }
                ]
            },
        )
        if post_res.status_code != 200:
            return fail(f"actual save returned {post_res.status_code}")

        data = _get_schedule(client)
        block = _find_block(data, fixture["block_id"])
        if not block:
            return fail("fixture block missing after actual save")
        if not str(block.get("actual_start_at") or "").startswith(earlier_date):
            return fail(
                f"actual_start_at did not use report_date ({earlier_date}); got {block.get('actual_start_at')!r}"
            )
        if _current_start(block) != str(block.get("actual_start_at") or ""):
            return fail("Current helper does not use actual_start_at when it exists")
        if _current_status(block) != "In progress forecast":
            return fail("Current helper status did not match in-progress forecast")
        if _current_end(block) != str(block.get("expected_end_at") or block.get("predicted_end_at") or block.get("calculated_end_datetime") or ""):
            return fail("Current helper end should fall back to forecast while incomplete")
        pass_msg("report_date drives actual_start_at and the current forecast row")

        print("PASS: smoke_planner_actual_date_timing completed successfully")
        return 0
    finally:
        if fixture:
            try:
                _cleanup_fixture(fixture)
            except Exception as exc:
                print(f"WARN: fixture cleanup failed: {exc}")


if __name__ == "__main__":
    sys.exit(main())
