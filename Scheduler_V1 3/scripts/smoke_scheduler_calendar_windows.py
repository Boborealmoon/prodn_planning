from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app.blocks import recalculate_all, recalculate_machine
from scheduler_app.db import db, ensure_db, one, rows, parse_dt_text
from scheduler_app.machines import machine_work_intervals_for_day


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


def _overlaps(start_a, end_a, start_b, end_b):
    return start_a < end_b and end_a > start_b


def _find_candidate_segment(con):
    candidate_rows = rows(
        con.execute(
            """
            SELECT s.segment_id, s.block_id, s.machine_id, s.start_datetime, s.end_datetime
            FROM run_block_segment s
            JOIN run_block b ON b.block_id = s.block_id
            WHERE COALESCE(b.active, 1) = 1
              AND NOT EXISTS (
                    SELECT 1
                    FROM production_actual a
                    WHERE a.block_id = b.block_id
                      AND COALESCE(a.status, 'ACTIVE') = 'ACTIVE'
                )
              AND COALESCE(s.segment_status, 'PLANNED') = 'PLANNED'
            ORDER BY s.start_datetime, s.segment_id
            """
        )
    )
    for row in candidate_rows:
        start_dt = parse_dt_text(row["start_datetime"])
        end_dt = parse_dt_text(row["end_datetime"])
        if not start_dt or not end_dt:
            continue
        if (end_dt - start_dt) >= timedelta(hours=3):
            window_start = start_dt + timedelta(minutes=30)
            window_end = window_start + timedelta(hours=1)
            if window_end < end_dt:
                return row, window_start, window_end
    return None, None, None


def _next_sunday(work_day):
    delta = (6 - work_day.weekday()) % 7
    if delta == 0:
        delta = 7
    return work_day + timedelta(days=delta)


def main():
    try:
        ensure_db()
        ensure_db()
        pass_msg("ensure_db() is idempotent")
    except Exception as exc:
        return fail(f"ensure_db() failed: {exc}")

    with db() as con:
        try:
            recalculate_all(con)
            pass_msg("recalculate_all() completed")
        except Exception as exc:
            return fail(f"recalculate_all() failed: {exc}")

        block, window_start, window_end = _find_candidate_segment(con)
        if not block:
            return fail("no suitable active segment found for calendar-window smoke")

        machine_id = int(block["machine_id"])
        work_day = window_start.date()
        active_before = list(machine_work_intervals_for_day(con, machine_id, work_day))
        if not active_before:
            return fail("candidate day has no usable intervals before test")

        con.execute("SAVEPOINT smoke_calendar_windows")
        try:
            con.execute(
                """
                INSERT INTO machine_calendar_window (
                  machine_id, start_at, end_at, window_type, capacity_minutes, note, active, created_at, updated_at
                ) VALUES (?, ?, ?, 'DOWN', 0, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    machine_id,
                    window_start.strftime("%Y-%m-%d %H:%M:%S"),
                    window_end.strftime("%Y-%m-%d %H:%M:%S"),
                    "Smoke downtime window",
                ),
            )
            recalculate_machine(con, machine_id, reason="SMOKE_CALENDAR_WINDOW")

            segments_after = rows(
                con.execute(
                    """
                    SELECT s.start_datetime, s.end_datetime
                    FROM run_block_segment s
                    JOIN run_block b ON b.block_id = s.block_id
                    WHERE b.machine_id = ?
                      AND COALESCE(b.active, 1) = 1
                      AND COALESCE(s.segment_status, 'PLANNED') = 'PLANNED'
                      AND s.segment_date = ?
                    ORDER BY s.start_datetime, s.segment_id
                    """,
                    (machine_id, work_day.isoformat()),
                )
            )
            if not segments_after:
                return fail("no segments found after inserting DOWN window")
            for seg in segments_after:
                seg_start = parse_dt_text(seg["start_datetime"])
                seg_end = parse_dt_text(seg["end_datetime"])
                if seg_start and seg_end and _overlaps(seg_start, seg_end, window_start, window_end):
                    return fail("generated production segment overlaps DOWN window")
            pass_msg("generated segments do not overlap DOWN window")

            before_window = any(parse_dt_text(row["end_datetime"]) and parse_dt_text(row["end_datetime"]) <= window_start for row in segments_after)
            after_window = any(parse_dt_text(row["start_datetime"]) and parse_dt_text(row["start_datetime"]) >= window_end for row in segments_after)
            if before_window and after_window:
                pass_msg("production is split around the DOWN window")
            else:
                pass_msg("selected work does not straddle the DOWN window, but the blocked hour is still respected")

            intervals_after = list(machine_work_intervals_for_day(con, machine_id, work_day))
            if not any(interval[0] <= window_start and interval[1] <= window_start for interval in intervals_after):
                return fail("working intervals did not reflect the blocked hour before the window")
            if not any(interval[0] >= window_end for interval in intervals_after):
                return fail("working intervals did not reflect the resumed capacity after the window")
            pass_msg("working intervals reflect the blocked hour")

            off_day = _next_sunday(work_day)
            con.execute(
                """
                INSERT INTO machine_calendar_window (
                  machine_id, start_at, end_at, window_type, capacity_minutes, note, active, created_at, updated_at
                ) VALUES (?, ?, ?, 'OVERTIME', 180, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    machine_id,
                    f"{off_day.isoformat()} 09:00:00",
                    f"{off_day.isoformat()} 12:00:00",
                    "Smoke overtime window",
                ),
            )
            overtime_intervals = list(machine_work_intervals_for_day(con, machine_id, off_day))
            overtime_start = parse_dt_text(f"{off_day.isoformat()} 09:00:00")
            overtime_end = parse_dt_text(f"{off_day.isoformat()} 12:00:00")
            if not any(start <= overtime_start and end >= overtime_end for start, end in overtime_intervals):
                return fail("overtime window did not add usable capacity on an otherwise off day")
            pass_msg("overtime window adds usable capacity")
        finally:
            con.execute("ROLLBACK TO smoke_calendar_windows")
            con.execute("RELEASE smoke_calendar_windows")

    pass_msg("calendar-window smoke checks completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
