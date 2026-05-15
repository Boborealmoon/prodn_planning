from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app.blocks import recalculate_machine
from scheduler_app.db import db, ensure_db, one, rows


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


@contextmanager
def savepoint(con, name):
    con.execute(f"SAVEPOINT {name}")
    try:
        yield
    finally:
        con.execute(f"ROLLBACK TO {name}")
        con.execute(f"RELEASE {name}")


def next_weekday(after_day: date, weekday: int) -> date:
    delta = (weekday - after_day.weekday()) % 7
    if delta == 0:
        delta = 7
    return after_day + timedelta(days=delta)


def _insert_block(con, machine_id, tag, ps_id, op_no, qty, cycle_minutes, planned_start_at, allow_pull_forward=1, fresh_monday=0):
    part = one(
        con.execute(
            """
            INSERT INTO parts (part_no, part_desc)
            VALUES (?, ?)
            RETURNING part_id
            """,
            (f"SMOKE-WEEK-PART-{tag}-{op_no}", "Week spillover smoke part"),
        )
    )
    bom = one(
        con.execute(
            """
            INSERT INTO bom_variation (part_id, bom_code, bom_desc, is_default)
            VALUES (?, ?, ?, 1)
            RETURNING bom_id
            """,
            (int(part["part_id"]), f"SMOKE-WEEK-BOM-{tag}-{op_no}", "Week spillover smoke bom"),
        )
    )
    seq = one(
        con.execute(
            """
            INSERT INTO operation_seq (bom_id, seq_no, op_no, op_type, machine_category, cycle_time, setup_time, preferred_machine, is_last_op)
            VALUES (?, ?, ?, 'CUT', 'SMOKE', ?, 0, '', 1)
            RETURNING op_seq_id
            """,
            (int(bom["bom_id"]), int(op_no), str(op_no), float(cycle_minutes)),
        )
    )
    op = one(
        con.execute(
            """
            INSERT INTO operation (
              job_no, operation_name, total_qty, setup_minutes, cycle_minutes_per_qty, compatible_machine_group,
              source_ps_id, source_op_seq_id, source_op_no, status, remarks, updated_at
            ) VALUES (?, ?, ?, 0, ?, 'SMOKE', ?, ?, ?, 'ACTIVE', '', CURRENT_TIMESTAMP)
            RETURNING operation_id
            """,
            (
                f"SMOKE-WEEK-{tag}-{op_no}",
                f"Week Smoke {op_no}",
                float(qty),
                float(cycle_minutes),
                ps_id,
                int(seq["op_seq_id"]),
                str(op_no),
            ),
        )
    )
    block = one(
        con.execute(
            """
            INSERT INTO run_block (
              operation_id, machine_id, queue_position, scheduled_qty, include_setup, status, planning_status, execution_status,
              anchor_datetime, planned_start_at, planned_end_at, allow_pull_forward, active, is_fresh_monday_item,
              calculated_start_datetime, calculated_end_datetime, actual_good_qty, actual_reject_qty, remarks, updated_at
            ) VALUES (?, ?, ?, ?, 0, 'NOT_STARTED', 'PLANNED', 'NOT_STARTED', ?, ?, '', ?, 1, ?, '', '', 0, 0, '', CURRENT_TIMESTAMP)
            RETURNING block_id
            """,
            (
                int(op["operation_id"]),
                int(machine_id),
                float(int(op_no)),
                float(qty),
                planned_start_at,
                planned_start_at,
                int(allow_pull_forward),
                int(fresh_monday),
            ),
        )
    )
    return {
        "part_id": int(part["part_id"]),
        "bom_id": int(bom["bom_id"]),
        "op_seq_id": int(seq["op_seq_id"]),
        "operation_id": int(op["operation_id"]),
        "block_id": int(block["block_id"]),
        "ps_id": ps_id,
        "op_no": str(op_no),
    }


def _cleanup_temp(con, ids_list):
    for ids in ids_list:
        con.execute("DELETE FROM production_actual WHERE block_id = ?", (int(ids["block_id"]),))
        con.execute("DELETE FROM schedule_alert WHERE block_id = ?", (int(ids["block_id"]),))
        con.execute("DELETE FROM machine_queue_state WHERE block_id = ?", (int(ids["block_id"]),))
        con.execute("DELETE FROM run_block_segment WHERE block_id = ?", (int(ids["block_id"]),))
        con.execute("DELETE FROM run_block WHERE block_id = ?", (int(ids["block_id"]),))
        con.execute("DELETE FROM operation WHERE operation_id = ?", (int(ids["operation_id"]),))
        con.execute("DELETE FROM operation_seq WHERE op_seq_id = ?", (int(ids["op_seq_id"]),))
        con.execute("DELETE FROM bom_variation WHERE bom_id = ?", (int(ids["bom_id"]),))
        con.execute("DELETE FROM parts WHERE part_id = ?", (int(ids["part_id"]),))


def main():
    try:
        ensure_db()
        pass_msg("ensure_db() is idempotent")
    except Exception as exc:
        return fail(f"ensure_db() failed: {exc}")

    with db() as con:
        machine = one(con.execute("SELECT machine_id FROM machines WHERE active = 1 ORDER BY machine_id LIMIT 1"))
        if not machine:
            return fail("no active machine found")
        machine_id = int(machine["machine_id"])

        friday = next_weekday(date.today(), 4)
        saturday = friday + timedelta(days=1)
        monday = friday + timedelta(days=3)

        with savepoint(con, "smoke_weekend_spillover"):
            tag = uuid4().hex[:8]
            friday_ids = _insert_block(
                con,
                machine_id,
                tag,
                f"SMOKE-WEEK-PS-{tag}-FRI",
                10,
                24,
                60,
                f"{friday.isoformat()} 08:30:00",
                allow_pull_forward=0,
                fresh_monday=0,
            )
            monday_ids = _insert_block(
                con,
                machine_id,
                tag,
                f"SMOKE-WEEK-PS-{tag}-MON",
                20,
                4,
                30,
                f"{monday.isoformat()} 08:30:00",
                allow_pull_forward=1,
                fresh_monday=1,
            )
            con.execute(
                """
                INSERT INTO machine_calendar_window (
                  machine_id, start_at, end_at, window_type, capacity_minutes, note, active, created_at, updated_at
                ) VALUES (?, ?, ?, 'OVERTIME', 240, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    machine_id,
                    f"{saturday.isoformat()} 16:15:00",
                    f"{saturday.isoformat()} 20:15:00",
                    "Weekend catch-up smoke overtime",
                ),
            )
            recalculate_machine(con, machine_id, reason="SMOKE_WEEKEND_SPILLOVER")

            friday_segments = rows(
                con.execute(
                    """
                    SELECT segment_date, start_datetime, end_datetime, segment_type
                    FROM run_block_segment
                    WHERE block_id = ?
                      AND segment_type = 'production'
                    ORDER BY start_datetime, segment_id
                    """,
                    (friday_ids["block_id"],),
                )
            )
            friday_dates = {str(row["segment_date"]) for row in friday_segments}
            if friday.isoformat() not in friday_dates:
                return fail("Friday job did not produce Friday segments")
            if saturday.isoformat() not in friday_dates:
                return fail("Friday job did not continue onto Saturday")
            if monday.isoformat() not in friday_dates:
                return fail("Friday job did not continue onto Monday")
            pass_msg("Friday job spills through Saturday and into Monday")

            saturday_after_base = [
                row
                for row in friday_segments
                if str(row["segment_date"]) == saturday.isoformat()
                and str(row["start_datetime"] or "") >= f"{saturday.isoformat()} 16:15:00"
            ]
            if not saturday_after_base:
                return fail("Saturday overtime window was not used")
            pass_msg("Saturday overtime window was used")

            monday_block = one(
                con.execute(
                    """
                    SELECT calculated_start_datetime, planned_start_at
                    FROM run_block
                    WHERE block_id = ?
                    """,
                    (monday_ids["block_id"],),
                )
            )
            if not monday_block:
                return fail("fresh Monday block missing after recalc")
            calc_start = str(monday_block["calculated_start_datetime"] or "")
            planned_start = str(monday_block["planned_start_at"] or "")
            if not calc_start or calc_start <= planned_start:
                return fail("fresh Monday item was not pushed later by spillover")

            alert = one(
                con.execute(
                    """
                    SELECT alert_id, alert_type, status
                    FROM schedule_alert
                    WHERE block_id = ?
                      AND alert_type = 'MONDAY_ANCHOR_DELAYED_BY_SPILLOVER'
                      AND status IN ('OPEN', 'ACKNOWLEDGED')
                    ORDER BY alert_id DESC
                    LIMIT 1
                    """,
                    (monday_ids["block_id"],),
                )
            )
            if not alert:
                return fail("MONDAY_ANCHOR_DELAYED_BY_SPILLOVER alert was not created")
            pass_msg("fresh Monday item is delayed by spillover and alerts correctly")

            _cleanup_temp(con, [friday_ids, monday_ids])

        with savepoint(con, "smoke_pull_forward"):
            tag = uuid4().hex[:8]
            pull_ids = _insert_block(
                con,
                machine_id,
                tag,
                f"SMOKE-WEEK-PS-{tag}-PULL",
                10,
                6,
                45,
                f"{monday.isoformat()} 15:00:00",
                allow_pull_forward=1,
                fresh_monday=0,
            )
            recalculate_machine(con, machine_id, reason="SMOKE_PULL_FORWARD")

            block = one(
                con.execute(
                    """
                    SELECT calculated_start_datetime, planned_start_at
                    FROM run_block
                    WHERE block_id = ?
                    """,
                    (pull_ids["block_id"],),
                )
            )
            if not block:
                return fail("pull-forward block missing after recalc")
            calc_start = str(block["calculated_start_datetime"] or "")
            planned_start = str(block["planned_start_at"] or "")
            if not calc_start or calc_start >= planned_start:
                return fail("normal item did not pull forward before its planned_start_at")
            pass_msg("normal non-anchored item can still pull forward")

            _cleanup_temp(con, [pull_ids])

    print("PASS: smoke_scheduler_saturday_monday completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
