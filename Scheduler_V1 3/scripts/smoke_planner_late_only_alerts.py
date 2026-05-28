from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app import create_app
from scheduler_app.db import db, ensure_db, one


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


def _ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _active_alerts(payload, block_id, alert_type):
    return [
        alert
        for alert in payload.get("alerts") or []
        if int(alert.get("block_id") or 0) == int(block_id or 0)
        and str(alert.get("alert_type") or "") == alert_type
    ]


def _select_candidate_block(con):
    row = one(
        con.execute(
            """
            SELECT b.block_id, b.machine_id, b.operation_id, b.planned_start_at, b.planned_end_at,
                   b.anchor_datetime, b.calculated_start_datetime, b.calculated_end_datetime,
                   b.scheduled_qty, b.queue_position, b.allow_pull_forward,
                   o.source_ps_id, o.source_op_seq_id, o.source_op_no, o.pp_partial_no, o.total_qty
            FROM run_block b
            JOIN operation o ON o.operation_id = b.operation_id
            LEFT JOIN production_actual a
              ON a.block_id = b.block_id
             AND COALESCE(a.status, 'ACTIVE') = 'ACTIVE'
            LEFT JOIN schedule_alert sa
              ON sa.block_id = b.block_id
             AND sa.status IN ('ACTIVE', 'OPEN', 'ACKNOWLEDGED')
            WHERE COALESCE(b.active, 1) = 1
              AND COALESCE(b.scheduled_qty, 0) > 0
              AND COALESCE(o.source_ps_id, '') <> ''
              AND COALESCE(o.source_op_seq_id, 0) > 0
              AND COALESCE(o.source_op_no, '') <> ''
              AND a.actual_id IS NULL
              AND sa.alert_id IS NULL
            ORDER BY b.block_id
            LIMIT 1
            """
        )
    )
    return dict(row) if row else None


def _backup_block_row(block_row):
    return {
        "planned_start_at": block_row.get("planned_start_at") or "",
        "planned_end_at": block_row.get("planned_end_at") or "",
        "anchor_datetime": block_row.get("anchor_datetime") or "",
        "calculated_start_datetime": block_row.get("calculated_start_datetime") or "",
        "calculated_end_datetime": block_row.get("calculated_end_datetime") or "",
        "allow_pull_forward": int(block_row.get("allow_pull_forward") or 0),
    }


def _restore_block_row(con, block_id, backup):
    con.execute(
        """
        UPDATE run_block
        SET planned_start_at = ?,
            planned_end_at = ?,
            anchor_datetime = ?,
            calculated_start_datetime = ?,
            calculated_end_datetime = ?,
            allow_pull_forward = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE block_id = ?
        """,
        (
            backup["planned_start_at"],
            backup["planned_end_at"],
            backup["anchor_datetime"],
            backup["calculated_start_datetime"],
            backup["calculated_end_datetime"],
            int(backup["allow_pull_forward"] or 0),
            int(block_id),
        ),
    )


def _set_block_window(con, block_row, planned_start, planned_duration_hours=8):
    planned_end = planned_start + timedelta(hours=planned_duration_hours)
    con.execute(
        """
        UPDATE run_block
        SET planned_start_at = ?,
            planned_end_at = ?,
            anchor_datetime = ?,
            calculated_start_datetime = ?,
            calculated_end_datetime = ?,
            allow_pull_forward = 0,
            updated_at = CURRENT_TIMESTAMP
        WHERE block_id = ?
        """,
        (
            _ts(planned_start),
            _ts(planned_end),
            _ts(planned_start),
            _ts(planned_start),
            _ts(planned_end),
            int(block_row["block_id"]),
        ),
    )


def _insert_actual(con, block_row, actual_dt, output_qty, reject_qty=0.0):
    cur = con.execute(
        """
        INSERT INTO production_actual (
          segment_id, block_id, machine_id, report_date, remarks, reported_at,
          output_qty, reject_qty, target_qty_at_report, status, entry_type,
          correction_of_actual_id, good_qty_at_report, created_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 'REPORT', ?, ?, ?)
        """,
        (
            None,
            int(block_row["block_id"]),
            int(block_row["machine_id"] or 0),
            actual_dt.date().isoformat(),
            "SMOKE",
            _ts(actual_dt),
            float(output_qty or 0),
            float(reject_qty or 0),
            float(block_row["scheduled_qty"] or 0),
            None,
            max(0.0, float(output_qty or 0) - float(reject_qty or 0)),
            "smoke",
        ),
    )
    return int(cur.lastrowid)


def _refresh_and_fetch(client, reason):
    res = client.post("/api/trial/planner/recalculate", json={"reason": reason})
    if res.status_code != 200:
        raise RuntimeError(f"planner recalculate returned {res.status_code}")
    res = client.get("/api/trial/planner/schedule")
    if res.status_code != 200:
        raise RuntimeError(f"planner schedule returned {res.status_code}")
    return res.get_json() or {}


def main():
    try:
        ensure_db()
    except Exception as exc:
        return fail(f"ensure_db() failed: {exc}")

    app = create_app()
    client = app.test_client()
    block = None
    backup = None
    actual_id = None

    try:
        with db() as con:
            block = _select_candidate_block(con)
            if not block:
                return fail("no candidate block found for planner alert smoke")
            backup = _backup_block_row(block)
            planned_start = datetime.now() - timedelta(days=4)
            _set_block_window(con, block, planned_start)
            actual_id = _insert_actual(con, block, planned_start - timedelta(days=1), output_qty=float(block["scheduled_qty"] or 0))

        payload = _refresh_and_fetch(client, "SMOKE_PLANNER_START_EARLY")
        if _active_alerts(payload, block["block_id"], "START_DRIFT"):
            return fail("START_DRIFT alert should not exist when actual start is earlier than planned")
        pass_msg("earlier actual start does not create START_DRIFT")

        with db() as con:
            later_dt = datetime.now() - timedelta(days=1)
            con.execute(
                """
                UPDATE production_actual
                SET report_date = ?, reported_at = ?, output_qty = ?, reject_qty = ?, target_qty_at_report = ?, good_qty_at_report = ?
                WHERE actual_id = ?
                """,
                (
                    later_dt.date().isoformat(),
                    _ts(later_dt),
                    float(block["scheduled_qty"] or 0),
                    0.0,
                    float(block["scheduled_qty"] or 0),
                    float(block["scheduled_qty"] or 0),
                    int(actual_id),
                ),
            )

        payload = _refresh_and_fetch(client, "SMOKE_PLANNER_START_LATE")
        if not _active_alerts(payload, block["block_id"], "START_DRIFT"):
            return fail("START_DRIFT alert was not created for a late actual start")
        pass_msg("late actual start creates START_DRIFT")

        with db() as con:
            earlier_dt = datetime.now() - timedelta(days=5)
            con.execute(
                """
                UPDATE production_actual
                SET report_date = ?, reported_at = ?, output_qty = ?, reject_qty = ?, target_qty_at_report = ?, good_qty_at_report = ?
                WHERE actual_id = ?
                """,
                (
                    earlier_dt.date().isoformat(),
                    _ts(earlier_dt),
                    float(block["scheduled_qty"] or 0),
                    0.0,
                    float(block["scheduled_qty"] or 0),
                    float(block["scheduled_qty"] or 0),
                    int(actual_id),
                ),
            )

        payload = _refresh_and_fetch(client, "SMOKE_PLANNER_START_RESOLVE")
        if _active_alerts(payload, block["block_id"], "START_DRIFT"):
            return fail("START_DRIFT alert did not resolve after moving actual earlier")
        pass_msg("START_DRIFT alert resolves again when actual is no longer late")

        return 0
    except Exception as exc:
        return fail(str(exc))
    finally:
        if block and backup is not None:
            with db() as con:
                if actual_id:
                    con.execute("DELETE FROM production_actual WHERE actual_id = ?", (int(actual_id),))
                con.execute("DELETE FROM schedule_alert WHERE block_id = ?", (int(block["block_id"]),))
                _restore_block_row(con, block["block_id"], backup)


if __name__ == "__main__":
    sys.exit(main())
