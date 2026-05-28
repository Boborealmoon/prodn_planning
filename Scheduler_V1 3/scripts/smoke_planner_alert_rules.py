from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app.blocks import refresh_planner_alerts
from scheduler_app.db import db, ensure_db, one
from scheduler_app.machines import capacity_minutes_for_machine_day
from scheduler_app.scheduler_state import dismiss_schedule_alert

ALLOWED_TYPES = {
    "START_DRIFT",
    "END_DRIFT",
    "CYCLE_TIME_DRIFT_AFTER_3_DAYS",
}


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


def _ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def savepoint(con, name):
    con.execute(f"SAVEPOINT {name}")
    try:
        yield
    finally:
        con.execute(f"ROLLBACK TO {name}")
        con.execute(f"RELEASE {name}")


def _pick_machine(con):
    row = one(
        con.execute(
            """
            SELECT machine_id
            FROM machines
            WHERE COALESCE(active, 1) = 1
            ORDER BY machine_id
            LIMIT 1
            """
        )
    )
    return int(row["machine_id"]) if row else 0


def _pick_work_date(con, machine_id, preferred_offset_days=4):
    today = date.today()
    for offset in range(preferred_offset_days, preferred_offset_days + 10):
        work_date = today - timedelta(days=offset)
        capacity = capacity_minutes_for_machine_day(con, machine_id, work_date)
        if float(capacity.get("capacity_minutes") or 0) > 0:
            return work_date
    return today - timedelta(days=preferred_offset_days)


def _pick_future_work_dates(con, machine_id, count, start_offset_days=1):
    picked = []
    work_date = date.today() + timedelta(days=max(1, int(start_offset_days or 1)))
    while len(picked) < int(count):
        capacity = capacity_minutes_for_machine_day(con, machine_id, work_date)
        if float(capacity.get("capacity_minutes") or 0) > 0:
            picked.append(work_date)
        work_date += timedelta(days=1)
        if (work_date - date.today()).days > 60:
            raise RuntimeError(f"could not find {count} future work dates with capacity")
    return picked


def _insert_temp_block(con, machine_id, tag, cycle_minutes_per_qty=0):
    part = one(
        con.execute(
            """
            INSERT INTO parts (part_no, part_desc)
            VALUES (?, ?)
            RETURNING part_id
            """,
            (f"SMOKE-ALERT-PART-{tag}", "Alert smoke part"),
        )
    )
    bom = one(
        con.execute(
            """
            INSERT INTO bom_variation (part_id, bom_code, bom_desc, is_default)
            VALUES (?, ?, ?, 1)
            RETURNING bom_id
            """,
            (int(part["part_id"]), f"SMOKE-ALERT-BOM-{tag}", "Alert smoke BOM"),
        )
    )
    seq = one(
        con.execute(
            """
            INSERT INTO operation_seq (bom_id, seq_no, op_no, op_type, machine_category, cycle_time, setup_time, preferred_machine, is_last_op)
            VALUES (?, 10, '10', 'CUT', 'SMOKE', 30, 60, '', 1)
            RETURNING op_seq_id
            """,
            (int(bom["bom_id"]),),
        )
    )
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
                f"SMOKE-ALERT-{tag}",
                f"Alert Smoke {tag}",
                1000,
                60,
                float(cycle_minutes_per_qty or 0),
                "SMOKE",
                f"SMOKE-ALERT-PS::{tag}",
                int(seq["op_seq_id"]),
                "10",
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
            ) VALUES (?, ?, 10, 1000, 1, 'NOT_STARTED', 'PLANNED', 'NOT_STARTED', ?, ?, ?, 1, 1, 0, ?, ?, 0, 0, '', CURRENT_TIMESTAMP)
            RETURNING block_id
            """,
            (
                int(op["operation_id"]),
                int(machine_id),
                "",
                "2099-01-01 08:30:00",
                "2099-01-01 16:30:00",
                "2099-01-01 08:30:00",
                "2099-01-01 16:30:00",
            ),
        )
    )
    return {
        "part_id": int(part["part_id"]),
        "bom_id": int(bom["bom_id"]),
        "op_seq_id": int(seq["op_seq_id"]),
        "operation_id": int(op["operation_id"]),
        "block_id": int(block["block_id"]),
        "machine_id": int(machine_id),
        "scheduled_qty": 1000.0,
    }


def _set_block_schedule(con, block_id, planned_start, planned_end, calculated_end=None):
    calculated_end = calculated_end or planned_end
    con.execute(
        """
        UPDATE run_block
        SET planned_start_at = ?,
            planned_end_at = ?,
            anchor_datetime = ?,
            calculated_start_datetime = ?,
            calculated_end_datetime = ?,
            allow_pull_forward = 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE block_id = ?
        """,
        (
            _ts(planned_start),
            _ts(planned_end),
            "",
            _ts(planned_start),
            _ts(calculated_end),
            int(block_id),
        ),
    )


def _set_cycle_minutes(con, operation_id, cycle_minutes_per_qty):
    con.execute(
        """
        UPDATE operation
        SET cycle_minutes_per_qty = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE operation_id = ?
        """,
        (float(cycle_minutes_per_qty or 0), int(operation_id)),
    )


def _set_actual_rows(con, block, rows_spec):
    con.execute("DELETE FROM production_actual WHERE block_id = ?", (int(block["block_id"]),))
    for spec in rows_spec:
        reported_at = spec.get("reported_at") or _ts(datetime.combine(spec["report_date"], datetime.min.time()))
        good_qty = max(0.0, float(spec.get("output_qty") or 0) - float(spec.get("reject_qty") or 0))
        con.execute(
            """
            INSERT INTO production_actual (
              segment_id, block_id, machine_id, report_date, remarks, reported_at,
              output_qty, reject_qty, target_qty_at_report, status, entry_type,
              correction_of_actual_id, good_qty_at_report, created_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 'REPORT', ?, ?, ?)
            """,
            (
                None,
                int(block["block_id"]),
                int(block["machine_id"]),
                spec["report_date"].isoformat(),
                spec.get("remarks", "SMOKE"),
                reported_at,
                float(spec.get("output_qty") or 0),
                float(spec.get("reject_qty") or 0),
            float(block.get("scheduled_qty") or 0),
                None,
                good_qty,
                "smoke",
            ),
        )


def _active_alerts(con, block_id, alert_type):
    return [
        dict(row)
        for row in con.execute(
            """
            SELECT *
            FROM schedule_alert
            WHERE block_id = ?
              AND alert_type = ?
              AND status IN ('ACTIVE', 'OPEN', 'ACKNOWLEDGED')
            ORDER BY updated_at DESC, alert_id DESC
            """,
            (int(block_id), alert_type),
        )
    ]


def _refresh_alerts(con):
    refresh_planner_alerts(con)


def main():
    try:
        ensure_db()
    except Exception as exc:
        return fail(f"ensure_db() failed: {exc}")

    machine_id = 0
    ids = None

    try:
        with db() as con, savepoint(con, "planner_alert_rules"):
            machine_id = _pick_machine(con)
            if not machine_id:
                return fail("no active machine found")

            ids = _insert_temp_block(con, machine_id, "RULES", cycle_minutes_per_qty=0)
            base_date = _pick_work_date(con, machine_id, preferred_offset_days=4)
            planned_start = datetime.combine(base_date, datetime.min.time()).replace(hour=8, minute=30)
            planned_end = planned_start + timedelta(hours=8)
            _set_block_schedule(con, ids["block_id"], planned_start, planned_end)
            _set_actual_rows(
                con,
                ids,
                [
                    {
                        "report_date": base_date - timedelta(days=1),
                        "output_qty": 1,
                        "reject_qty": 0,
                        "remarks": "SMOKE EARLY",
                    }
                ],
            )
            _refresh_alerts(con)
            if _active_alerts(con, ids["block_id"], "START_DRIFT"):
                return fail("START_DRIFT should not exist when actual start is earlier than planned")
            pass_msg("earlier actual start does not create START_DRIFT")

            _set_actual_rows(
                con,
                ids,
                [
                    {
                        "report_date": base_date,
                        "output_qty": 1,
                        "reject_qty": 0,
                        "remarks": "SMOKE SAME DAY",
                    }
                ],
            )
            _refresh_alerts(con)
            if _active_alerts(con, ids["block_id"], "START_DRIFT"):
                return fail("START_DRIFT should not exist when actual start is same day as planned")
            pass_msg("same-day actual start does not create START_DRIFT")

            _set_actual_rows(
                con,
                ids,
                [
                    {
                        "report_date": base_date + timedelta(days=1),
                        "output_qty": 1,
                        "reject_qty": 0,
                        "remarks": "SMOKE LATE",
                    }
                ],
            )
            _refresh_alerts(con)
            start_alerts = _active_alerts(con, ids["block_id"], "START_DRIFT")
            if not start_alerts:
                return fail("START_DRIFT was not created for a later actual start date")
            pass_msg("later actual start creates START_DRIFT")

            alert_id = int(start_alerts[0]["alert_id"])
            dismiss_schedule_alert(con, alert_id)
            _refresh_alerts(con)
            if _active_alerts(con, ids["block_id"], "START_DRIFT"):
                return fail("START_DRIFT reappeared after dismissal with same signature")
            pass_msg("dismissed START_DRIFT does not reappear for the same signature")

            _set_actual_rows(
                con,
                ids,
                [
                    {
                        "report_date": base_date + timedelta(days=2),
                        "output_qty": 1,
                        "reject_qty": 0,
                        "remarks": "SMOKE NEW LATE",
                    }
                ],
            )
            _refresh_alerts(con)
            if not _active_alerts(con, ids["block_id"], "START_DRIFT"):
                return fail("START_DRIFT did not reappear after the signature changed")
            pass_msg("START_DRIFT can reappear when the signature changes")

            _set_actual_rows(con, ids, [])
            _set_block_schedule(
                con,
                ids["block_id"],
                planned_start,
                planned_end,
                calculated_end=planned_end + timedelta(days=1),
            )
            _refresh_alerts(con)
            end_alerts = _active_alerts(con, ids["block_id"], "END_DRIFT")
            if not end_alerts:
                return fail("END_DRIFT was not created for a later forecast end date")
            pass_msg("later forecast end creates END_DRIFT")

            _set_block_schedule(
                con,
                ids["block_id"],
                planned_start,
                planned_end,
                calculated_end=planned_end + timedelta(hours=2),
            )
            _refresh_alerts(con)
            if _active_alerts(con, ids["block_id"], "END_DRIFT"):
                return fail("END_DRIFT should not exist when only the time of day differs")
            pass_msg("same-day forecast end does not create END_DRIFT")

            _set_cycle_minutes(con, ids["operation_id"], 20)

            cycle_dates_2 = _pick_future_work_dates(con, machine_id, 2)
            cycle_planned_start = datetime.combine(cycle_dates_2[0], datetime.min.time()).replace(hour=8, minute=30)
            cycle_planned_end = cycle_planned_start + timedelta(hours=8)
            _set_block_schedule(con, ids["block_id"], cycle_planned_start, cycle_planned_end)
            _set_actual_rows(
                con,
                ids,
                [
                    {
                        "report_date": report_date,
                        "output_qty": float(capacity_minutes_for_machine_day(con, machine_id, report_date).get("capacity_minutes") or 0) / 21.0,
                        "reject_qty": 0,
                        "remarks": "SMOKE CYCLE 21-2DAY",
                    }
                    for report_date in cycle_dates_2
                ],
            )
            _refresh_alerts(con)
            if _active_alerts(con, ids["block_id"], "CYCLE_TIME_DRIFT_AFTER_3_DAYS"):
                return fail("CYCLE_TIME_DRIFT_AFTER_3_DAYS should not exist with only 2 actual report dates")
            pass_msg("two actual report dates do not create CYCLE_TIME_DRIFT_AFTER_3_DAYS")

            cycle_dates_3 = _pick_future_work_dates(con, machine_id, 3)
            _set_actual_rows(
                con,
                ids,
                [
                    {
                        "report_date": report_date,
                        "output_qty": float(capacity_minutes_for_machine_day(con, machine_id, report_date).get("capacity_minutes") or 0) / 21.0,
                        "reject_qty": 0,
                        "remarks": "SMOKE CYCLE 21-3DAY",
                    }
                    for report_date in cycle_dates_3
                ],
            )
            _refresh_alerts(con)
            if _active_alerts(con, ids["block_id"], "CYCLE_TIME_DRIFT_AFTER_3_DAYS"):
                return fail("CYCLE_TIME_DRIFT_AFTER_3_DAYS should not exist for +5% cycle drift")
            pass_msg("small cycle drift does not create CYCLE_TIME_DRIFT_AFTER_3_DAYS")

            cycle_dates_7 = _pick_future_work_dates(con, machine_id, 7)
            _set_actual_rows(
                con,
                ids,
                [
                    {
                        "report_date": report_date,
                        "output_qty": float(capacity_minutes_for_machine_day(con, machine_id, report_date).get("capacity_minutes") or 0) / 40.0,
                        "reject_qty": 0,
                        "remarks": "SMOKE CYCLE 40-7DAY",
                    }
                    for report_date in cycle_dates_7
                ],
            )
            _refresh_alerts(con)
            slower_alerts = _active_alerts(con, ids["block_id"], "CYCLE_TIME_DRIFT_AFTER_3_DAYS")
            if not slower_alerts:
                return fail("CYCLE_TIME_DRIFT_AFTER_3_DAYS was not created for slower cycle time")
            if int(float(slower_alerts[0].get("delay_minutes") or 0)) != len(cycle_dates_7):
                return fail("cycle alert did not store the cumulative day count in delay_minutes")
            if str(len(cycle_dates_7)) not in str(slower_alerts[0].get("message") or ""):
                return fail("cycle alert message did not mention the cumulative day count")
            pass_msg("slower cycle time creates CYCLE_TIME_DRIFT_AFTER_3_DAYS with cumulative day count")

            cycle_dates_9 = _pick_future_work_dates(con, machine_id, 9)
            _set_actual_rows(
                con,
                ids,
                [
                    {
                        "report_date": report_date,
                        "output_qty": float(capacity_minutes_for_machine_day(con, machine_id, report_date).get("capacity_minutes") or 0) / 17.0,
                        "reject_qty": 0,
                        "remarks": "SMOKE CYCLE 17-9DAY",
                    }
                    for report_date in cycle_dates_9
                ],
            )
            _refresh_alerts(con)
            if not _active_alerts(con, ids["block_id"], "CYCLE_TIME_DRIFT_AFTER_3_DAYS"):
                return fail("CYCLE_TIME_DRIFT_AFTER_3_DAYS was not created for faster cycle time")
            pass_msg("faster cycle time creates CYCLE_TIME_DRIFT_AFTER_3_DAYS")

            cycle_dates_9_exact = cycle_dates_9
            _set_actual_rows(
                con,
                ids,
                [
                    {
                        "report_date": report_date,
                        "output_qty": float(capacity_minutes_for_machine_day(con, machine_id, report_date).get("capacity_minutes") or 0) / 18.0,
                        "reject_qty": 0,
                        "remarks": "SMOKE CYCLE 18-9DAY",
                    }
                    for report_date in cycle_dates_9_exact
                ],
            )
            _refresh_alerts(con)
            if _active_alerts(con, ids["block_id"], "CYCLE_TIME_DRIFT_AFTER_3_DAYS"):
                return fail("CYCLE_TIME_DRIFT_AFTER_3_DAYS should not exist for exactly 10% drift")
            pass_msg("exactly 10% drift does not create CYCLE_TIME_DRIFT_AFTER_3_DAYS")

            _set_actual_rows(
                con,
                ids,
                [
                    {
                        "report_date": report_date,
                        "output_qty": float(capacity_minutes_for_machine_day(con, machine_id, report_date).get("capacity_minutes") or 0) / 40.0,
                        "reject_qty": 0,
                        "remarks": "SMOKE CYCLE 40-7DAY-RESET",
                    }
                    for report_date in cycle_dates_7
                ],
            )
            _refresh_alerts(con)
            seven_day_alerts = _active_alerts(con, ids["block_id"], "CYCLE_TIME_DRIFT_AFTER_3_DAYS")
            if not seven_day_alerts:
                return fail("CYCLE_TIME_DRIFT_AFTER_3_DAYS was not recreated for the 7-day cumulative window")
            alert_id = int(seven_day_alerts[0]["alert_id"])
            dismiss_schedule_alert(con, alert_id)
            _refresh_alerts(con)
            if _active_alerts(con, ids["block_id"], "CYCLE_TIME_DRIFT_AFTER_3_DAYS"):
                return fail("CYCLE_TIME_DRIFT_AFTER_3_DAYS reappeared after dismissal with the same cumulative signature")
            pass_msg("dismissed cycle alert does not reappear for the same cumulative signature")

            cycle_dates_8 = _pick_future_work_dates(con, machine_id, 8)
            _set_actual_rows(
                con,
                ids,
                [
                    {
                        "report_date": report_date,
                        "output_qty": float(capacity_minutes_for_machine_day(con, machine_id, report_date).get("capacity_minutes") or 0) / 40.0,
                        "reject_qty": 0,
                        "remarks": "SMOKE CYCLE 40-8DAY",
                    }
                    for report_date in cycle_dates_8
                ],
            )
            _refresh_alerts(con)
            if not _active_alerts(con, ids["block_id"], "CYCLE_TIME_DRIFT_AFTER_3_DAYS"):
                return fail("CYCLE_TIME_DRIFT_AFTER_3_DAYS did not reappear after the cumulative window changed")
            pass_msg("cycle alert can reappear when the cumulative window changes")

            if _active_alerts(con, ids["block_id"], "LOW_OUTPUT_AFTER_3_DAYS"):
                return fail("legacy LOW_OUTPUT_AFTER_3_DAYS alert is still active")
            pass_msg("legacy LOW_OUTPUT_AFTER_3_DAYS is no longer active")

        print("PASS: smoke_planner_alert_rules completed successfully")
        return 0
    except Exception as exc:
        return fail(str(exc))


if __name__ == "__main__":
    sys.exit(main())
