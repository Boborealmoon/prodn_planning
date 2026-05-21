from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app import create_app
from scheduler_app.db import db, ensure_db, one

ACTIVE_ALERT_STATUSES = ("ACTIVE", "OPEN", "ACKNOWLEDGED")


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


def _ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _select_candidate_block(con, exclude_block_ids=()):
    clauses = [
        "COALESCE(b.active, 1) = 1",
        "COALESCE(b.scheduled_qty, 0) > 0",
        "COALESCE(o.source_ps_id, '') <> ''",
        "COALESCE(o.source_op_seq_id, 0) > 0",
        "COALESCE(o.source_op_no, '') <> ''",
        "a.actual_id IS NULL",
        "sa.alert_id IS NULL",
    ]
    params = []
    if exclude_block_ids:
        ids = [int(block_id) for block_id in exclude_block_ids if int(block_id or 0) > 0]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            clauses.append(f"b.block_id NOT IN ({placeholders})")
            params.extend(ids)
    where_clause = " AND ".join(clauses)
    params = [
        *ACTIVE_ALERT_STATUSES,
        *params,
    ]
    query = f"""
        SELECT b.block_id, b.machine_id, b.operation_id, b.planned_start_at, b.planned_end_at,
               b.anchor_datetime, b.calculated_start_datetime, b.calculated_end_datetime,
               b.scheduled_qty, b.queue_position,
               o.source_ps_id, o.source_op_seq_id, o.source_op_no, o.total_qty
        FROM run_block b
        JOIN operation o ON o.operation_id = b.operation_id
        LEFT JOIN production_actual a
          ON a.block_id = b.block_id
         AND COALESCE(a.status, 'ACTIVE') = 'ACTIVE'
        LEFT JOIN schedule_alert sa
          ON sa.block_id = b.block_id
         AND sa.status IN ({",".join("?" for _ in ACTIVE_ALERT_STATUSES)})
        WHERE {where_clause}
        ORDER BY b.block_id
        LIMIT 1
    """
    row = one(con.execute(query, params))
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


def _insert_actual(con, block_row, actual_dt, output_qty, reject_qty=0.0, remarks="SMOKE"):
    good_qty = max(0.0, float(output_qty or 0) - float(reject_qty or 0))
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
            remarks,
            _ts(actual_dt),
            float(output_qty or 0),
            float(reject_qty or 0),
            float(block_row["scheduled_qty"] or 0),
            None,
            good_qty,
            "smoke",
        ),
    )
    return int(cur.lastrowid)


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
    return planned_start, planned_end


def _active_alerts(payload, block_id, alert_type=None):
    results = []
    for alert in payload.get("alerts") or []:
        if int(alert.get("block_id") or 0) != int(block_id or 0):
            continue
        if alert_type and str(alert.get("alert_type") or "") != alert_type:
            continue
        results.append(alert)
    return results


def _refresh_and_fetch(client, reason):
    res = client.post("/api/trial/planner/recalculate", json={"reason": reason})
    if res.status_code != 200:
        raise AssertionError(f"recalculate returned {res.status_code}: {(res.get_json() or {}).get('error')}")
    res = client.get("/api/trial/planner/schedule")
    if res.status_code != 200:
        raise AssertionError(f"GET /api/trial/planner/schedule returned {res.status_code}")
    return res.get_json() or {}


def _cleanup_block(con, block_id, backup, inserted_actual_ids):
    if inserted_actual_ids:
        placeholders = ",".join("?" for _ in inserted_actual_ids)
        con.execute(
            f"DELETE FROM production_actual WHERE actual_id IN ({placeholders})",
            tuple(int(actual_id) for actual_id in inserted_actual_ids),
        )
    con.execute("DELETE FROM schedule_alert WHERE block_id = ?", (int(block_id),))
    _restore_block_row(con, block_id, backup)


def main():
    try:
        ensure_db()
    except Exception as exc:
        return fail(f"ensure_db() failed: {exc}")

    app = create_app()
    client = app.test_client()

    start_drift_block = None
    start_backup = None
    low_output_block = None
    low_backup = None

    try:
        with db() as con:
            start_drift_block = _select_candidate_block(con)
            if not start_drift_block:
                return fail("no candidate block found for START_DRIFT smoke")
            start_backup = _backup_block_row(start_drift_block)
            planned_start = datetime.now() - timedelta(days=4)
            actual_start = planned_start + timedelta(days=2)
            _set_block_window(con, start_drift_block, planned_start, planned_duration_hours=8)
            start_actual_id = _insert_actual(
                con,
                start_drift_block,
                actual_start,
                output_qty=float(start_drift_block["scheduled_qty"] or 0) or 1.0,
                reject_qty=0.0,
                remarks="SMOKE START_DRIFT",
            )

        payload = _refresh_and_fetch(client, "SMOKE_START_DRIFT")
        start_alerts = _active_alerts(payload, start_drift_block["block_id"], "START_DRIFT")
        if not start_alerts:
            return fail("START_DRIFT alert was not created")
        pass_msg("START_DRIFT alert created")

        start_alert = start_alerts[0]
        res = client.post(f"/api/trial/planner/alerts/{int(start_alert['alert_id'])}/align-start", json={})
        if res.status_code != 200:
            return fail(f"align-start returned {res.status_code}: {(res.get_json() or {}).get('error')}")
        with db() as con:
            row = one(
                con.execute(
                    """
                    SELECT planned_start_at, anchor_datetime
                    FROM run_block
                    WHERE block_id = ?
                    """,
                    (int(start_drift_block["block_id"]),),
                )
            )
            if not row:
                return fail("aligned block not found in run_block")
            actual_start_text = _ts(actual_start)
            if str(row["planned_start_at"] or "") != actual_start_text:
                return fail(f"planned_start_at was not aligned to actual start: {row['planned_start_at']} != {actual_start_text}")
            if str(row["anchor_datetime"] or "") != actual_start_text:
                return fail(f"anchor_datetime was not aligned to actual start: {row['anchor_datetime']} != {actual_start_text}")
        payload = _refresh_and_fetch(client, "SMOKE_START_DRIFT_AFTER_ALIGN")
        if _active_alerts(payload, start_drift_block["block_id"], "START_DRIFT"):
            return fail("START_DRIFT alert still active after align-start")
        pass_msg("START_DRIFT alert aligns and resolves")

        with db() as con:
            _cleanup_block(con, start_drift_block["block_id"], start_backup or _backup_block_row(start_drift_block), [start_actual_id])
        client.post("/api/trial/planner/recalculate", json={"reason": "SMOKE_START_DRIFT_CLEANUP"})

        with db() as con:
            low_output_block = _select_candidate_block(con)
            if not low_output_block:
                return fail("no candidate block found for LOW_OUTPUT_AFTER_3_DAYS smoke")
            low_backup = _backup_block_row(low_output_block)
            planned_start = datetime.now() - timedelta(days=4)
            _set_block_window(con, low_output_block, planned_start, planned_duration_hours=8)
            low_actual_id = _insert_actual(
                con,
                low_output_block,
                planned_start + timedelta(hours=1),
                output_qty=1.0,
                reject_qty=0.0,
                remarks="SMOKE LOW_OUTPUT",
            )

        payload = _refresh_and_fetch(client, "SMOKE_LOW_OUTPUT")
        low_alerts = _active_alerts(payload, low_output_block["block_id"], "LOW_OUTPUT_AFTER_3_DAYS")
        if not low_alerts:
            return fail("LOW_OUTPUT_AFTER_3_DAYS alert was not created")
        pass_msg("LOW_OUTPUT_AFTER_3_DAYS alert created")

        low_alert = low_alerts[0]
        res = client.post(f"/api/trial/planner/alerts/{int(low_alert['alert_id'])}/dismiss", json={})
        if res.status_code != 200:
            return fail(f"dismiss returned {res.status_code}: {(res.get_json() or {}).get('error')}")
        payload = _refresh_and_fetch(client, "SMOKE_LOW_OUTPUT_AFTER_DISMISS")
        if _active_alerts(payload, low_output_block["block_id"], "LOW_OUTPUT_AFTER_3_DAYS"):
            return fail("LOW_OUTPUT_AFTER_3_DAYS alert still active after dismiss")
        pass_msg("LOW_OUTPUT_AFTER_3_DAYS alert dismisses cleanly")

        print("PASS: smoke_planner_drift_alerts completed successfully")
        return 0
    finally:
        try:
            with db() as con:
                if start_drift_block:
                    _cleanup_block(con, start_drift_block["block_id"], start_backup or _backup_block_row(start_drift_block), [start_actual_id] if 'start_actual_id' in locals() else [])
                if low_output_block:
                    _cleanup_block(con, low_output_block["block_id"], low_backup or _backup_block_row(low_output_block), [low_actual_id] if 'low_actual_id' in locals() else [])
            client.post("/api/trial/planner/recalculate", json={"reason": "SMOKE_PLANNER_ALERTS_CLEANUP"})
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
