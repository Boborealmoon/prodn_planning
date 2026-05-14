from __future__ import annotations

from datetime import date, datetime, timedelta

from .actuals import actual_totals_for_block
from .db import dt_now_text, one, rows


def _text(value):
    return "" if value is None else str(value)


def _float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _segment_bounds(con, block_id):
    row = one(
        con.execute(
            """
            SELECT MIN(start_datetime) AS start_datetime, MAX(end_datetime) AS end_datetime
            FROM run_block_segment
            WHERE block_id = ? AND COALESCE(segment_status, 'PLANNED') = 'PLANNED'
            """,
            (int(block_id),),
        )
    )
    if not row or not row["start_datetime"] or not row["end_datetime"]:
        return None
    return row["start_datetime"], row["end_datetime"]


def _operation_bounds(con, operation_id):
    row = one(
        con.execute(
            """
            SELECT MIN(s.start_datetime) AS start_datetime, MAX(s.end_datetime) AS end_datetime
            FROM run_block_segment s
            JOIN run_block b ON b.block_id = s.block_id
            WHERE b.operation_id = ? AND COALESCE(s.segment_status, 'PLANNED') = 'PLANNED'
            """,
            (int(operation_id),),
        )
    )
    if not row or not row["start_datetime"] or not row["end_datetime"]:
        return None
    return row["start_datetime"], row["end_datetime"]


def _process_sheet_bounds(con, ps_id):
    row = one(
        con.execute(
            """
            SELECT MIN(s.start_datetime) AS start_datetime, MAX(s.end_datetime) AS end_datetime
            FROM run_block_segment s
            JOIN run_block b ON b.block_id = s.block_id
            JOIN operation o ON o.operation_id = b.operation_id
            WHERE COALESCE(o.source_ps_id, '') = ? AND COALESCE(s.segment_status, 'PLANNED') = 'PLANNED'
            """,
            (str(ps_id),),
        )
    )
    if not row or not row["start_datetime"] or not row["end_datetime"]:
        return None
    return row["start_datetime"], row["end_datetime"]


def _ps_id_for_operation(con, operation_id):
    row = one(con.execute("SELECT COALESCE(source_ps_id, '') AS ps_id FROM operation WHERE operation_id = ?", (int(operation_id),)))
    return _text(row["ps_id"]) if row else ""


def _ps_id_for_block(con, block_id):
    row = one(
        con.execute(
            """
            SELECT COALESCE(o.source_ps_id, '') AS ps_id
            FROM run_block b
            JOIN operation o ON o.operation_id = b.operation_id
            WHERE b.block_id = ?
            """,
            (int(block_id),),
        )
    )
    return _text(row["ps_id"]) if row else ""


def _archive_current_runs(con, scope_type, machine_id):
    if machine_id is None:
        con.execute(
            """
            UPDATE schedule_run
            SET status = 'ARCHIVED', updated_at = CURRENT_TIMESTAMP
            WHERE status = 'CURRENT'
              AND scope_type = ?
              AND machine_id IS NULL
            """,
            (scope_type,),
        )
    else:
        con.execute(
            """
            UPDATE schedule_run
            SET status = 'ARCHIVED', updated_at = CURRENT_TIMESTAMP
            WHERE status = 'CURRENT'
              AND scope_type = ?
              AND machine_id = ?
            """,
            (scope_type, int(machine_id)),
        )


def create_schedule_run(con, reason, scope_type="FULL", machine_id=None, notes=""):
    _archive_current_runs(con, scope_type, machine_id)
    cur = con.execute(
        """
        INSERT INTO schedule_run (reason, status, scope_type, machine_id, notes, generated_at, created_at, updated_at)
        VALUES (?, 'CURRENT', ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (str(reason or ""), scope_type, int(machine_id) if machine_id is not None else None, str(notes or "")),
    )
    return int(cur.lastrowid)


def active_calendar_windows_for_machine_day(con, machine_id, work_day):
    day_text = work_day.strftime("%Y-%m-%d")
    next_day_text = (work_day + timedelta(days=1)).strftime("%Y-%m-%d")
    return rows(
        con.execute(
            """
            SELECT *
            FROM machine_calendar_window
            WHERE machine_id = ?
              AND active = 1
              AND start_at < ?
              AND end_at > ?
            ORDER BY start_at, window_id
            """,
            (int(machine_id), next_day_text, day_text),
        )
    )


def refresh_machine_queue_state(con, block_id, schedule_run_id=None):
    block = one(
        con.execute(
            """
            SELECT b.*, COALESCE(v.remaining_qty, 0) AS remaining_qty,
                   COALESCE(v.good_qty, 0) AS good_qty,
                   COALESCE(a.output_qty, 0) AS output_qty,
                   COALESCE(a.reject_qty, 0) AS reject_qty
            FROM run_block b
            LEFT JOIN v_block_remaining v ON v.block_id = b.block_id
            LEFT JOIN v_block_actual_totals a ON a.block_id = b.block_id
            WHERE b.block_id = ?
            """,
            (int(block_id),),
        )
    )
    if not block:
        return None
    bounds = _segment_bounds(con, block_id)
    planned_start = _text(block["planned_start_at"] or (bounds[0] if bounds else ""))
    planned_end = _text(block["planned_end_at"] or (bounds[1] if bounds else ""))
    planned_minutes = 0.0
    if planned_start and planned_end:
        try:
            start_dt = datetime.fromisoformat(planned_start.replace("T", " "))
            end_dt = datetime.fromisoformat(planned_end.replace("T", " "))
            planned_minutes = max(0.0, (end_dt - start_dt).total_seconds() / 60.0)
        except ValueError:
            planned_minutes = 0.0
    output_qty = _float(block["output_qty"])
    reject_qty = _float(block["reject_qty"])
    good_qty = _float(block["good_qty"])
    remaining_qty = _float(block["remaining_qty"])
    execution_status = _text(block["execution_status"] or block["status"] or "NOT_STARTED")
    schedule_status = _text(block["planning_status"] or "UNSCHEDULED")
    is_late = 0
    delay_minutes = 0.0
    if planned_end:
        try:
            end_dt = datetime.fromisoformat(planned_end.replace("T", " "))
            now_dt = datetime.now()
            if end_dt < now_dt and execution_status not in {"DONE", "COMPLETED"}:
                is_late = 1
                delay_minutes = max(0.0, (now_dt - end_dt).total_seconds() / 60.0)
        except ValueError:
            pass
    con.execute(
        """
        INSERT INTO machine_queue_state (
          block_id, schedule_run_id, predicted_start_at, predicted_end_at, remaining_qty, output_qty,
          reject_qty, good_qty, planned_minutes, schedule_status, execution_status, is_late, delay_minutes, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(block_id) DO UPDATE SET
          schedule_run_id = excluded.schedule_run_id,
          predicted_start_at = excluded.predicted_start_at,
          predicted_end_at = excluded.predicted_end_at,
          remaining_qty = excluded.remaining_qty,
          output_qty = excluded.output_qty,
          reject_qty = excluded.reject_qty,
          good_qty = excluded.good_qty,
          planned_minutes = excluded.planned_minutes,
          schedule_status = excluded.schedule_status,
          execution_status = excluded.execution_status,
          is_late = excluded.is_late,
          delay_minutes = excluded.delay_minutes,
          updated_at = CURRENT_TIMESTAMP
        """,
        (
            int(block_id),
            int(schedule_run_id) if schedule_run_id is not None else block["last_schedule_run_id"],
            planned_start or None,
            planned_end or None,
            remaining_qty,
            output_qty,
            reject_qty,
            good_qty,
            planned_minutes,
            schedule_status,
            execution_status,
            is_late,
            delay_minutes,
        ),
    )
    return {
        "block_id": int(block_id),
        "remaining_qty": remaining_qty,
        "good_qty": good_qty,
        "planned_start_at": planned_start,
        "planned_end_at": planned_end,
        "execution_status": execution_status,
    }


def refresh_operation_state(con, operation_id):
    operation = one(con.execute("SELECT * FROM operation WHERE operation_id = ?", (int(operation_id),)))
    if not operation:
        return None
    actual_totals = one(
        con.execute(
            """
            SELECT
              COALESCE(SUM(COALESCE(a.output_qty, 0)), 0) AS output_qty,
              COALESCE(SUM(COALESCE(a.reject_qty, 0)), 0) AS reject_qty,
              COALESCE(SUM(COALESCE(a.output_qty, 0) - COALESCE(a.reject_qty, 0)), 0) AS good_qty,
              SUM(CASE WHEN a.output_qty IS NOT NULL THEN 1 ELSE 0 END) AS output_reports,
              SUM(CASE WHEN a.reject_qty IS NOT NULL THEN 1 ELSE 0 END) AS reject_reports
            FROM run_block b
            LEFT JOIN production_actual a ON a.block_id = b.block_id
            WHERE b.operation_id = ?
              AND COALESCE(a.status, 'ACTIVE') = 'ACTIVE'
            """,
            (int(operation_id),),
        )
    )
    bounds = _operation_bounds(con, operation_id)
    output_qty = _float(actual_totals["output_qty"] if actual_totals else 0)
    reject_qty = _float(actual_totals["reject_qty"] if actual_totals else 0)
    good_qty = _float(actual_totals["good_qty"] if actual_totals else 0)
    remaining_qty = max(0.0, _float(operation["total_qty"]) - good_qty)
    execution_status = "NOT_STARTED"
    if int((actual_totals or {})["output_reports"] or 0) > 0 or int((actual_totals or {})["reject_reports"] or 0) > 0:
        execution_status = "IN_PROGRESS"
    if remaining_qty <= 0 and _float(operation["total_qty"]) > 0:
        execution_status = "DONE"
    con.execute(
        """
        INSERT INTO process_sheet_operation_state (
          operation_id, ps_id, output_qty, reject_qty, good_qty, remaining_qty,
          predicted_start_at, predicted_end_at, execution_status, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(operation_id) DO UPDATE SET
          ps_id = excluded.ps_id,
          output_qty = excluded.output_qty,
          reject_qty = excluded.reject_qty,
          good_qty = excluded.good_qty,
          remaining_qty = excluded.remaining_qty,
          predicted_start_at = excluded.predicted_start_at,
          predicted_end_at = excluded.predicted_end_at,
          execution_status = excluded.execution_status,
          updated_at = CURRENT_TIMESTAMP
        """,
        (
            int(operation_id),
            _text(operation["source_ps_id"]),
            output_qty,
            reject_qty,
            good_qty,
            remaining_qty,
            bounds[0] if bounds else None,
            bounds[1] if bounds else None,
            execution_status,
        ),
    )
    return {
        "operation_id": int(operation_id),
        "ps_id": _text(operation["source_ps_id"]),
        "remaining_qty": remaining_qty,
        "execution_status": execution_status,
    }


def refresh_process_sheet_state(con, ps_id):
    ps = one(con.execute("SELECT * FROM process_sheet WHERE ps_id = ?", (str(ps_id),)))
    if not ps:
        return None
    actual_totals = one(
        con.execute(
            """
            SELECT
              COALESCE(SUM(COALESCE(s.output_qty, 0)), 0) AS output_qty,
              COALESCE(SUM(COALESCE(s.reject_qty, 0)), 0) AS reject_qty,
              COALESCE(SUM(COALESCE(s.good_qty, 0)), 0) AS good_qty,
              COALESCE(SUM(COALESCE(s.remaining_qty, 0)), 0) AS remaining_qty
            FROM process_sheet_operation_state s
            WHERE s.ps_id = ?
            """,
            (str(ps_id),),
        )
    )
    bounds = _process_sheet_bounds(con, ps_id)
    output_qty = _float(actual_totals["output_qty"] if actual_totals else 0)
    reject_qty = _float(actual_totals["reject_qty"] if actual_totals else 0)
    good_qty = _float(actual_totals["good_qty"] if actual_totals else 0)
    remaining_qty = _float(actual_totals["remaining_qty"] if actual_totals else 0)
    execution_status = "NOT_STARTED"
    if good_qty > 0:
        execution_status = "IN_PROGRESS"
    if remaining_qty <= 0 and _float(ps["total_qty"]) > 0:
        execution_status = "DONE"
    delay_minutes = 0.0
    is_late = 0
    if bounds and bounds[1]:
        try:
            end_dt = datetime.fromisoformat(bounds[1].replace("T", " "))
            now_dt = datetime.now()
            if end_dt < now_dt and execution_status != "DONE":
                is_late = 1
                delay_minutes = max(0.0, (now_dt - end_dt).total_seconds() / 60.0)
        except ValueError:
            pass
    con.execute(
        """
        INSERT INTO process_sheet_state (
          ps_id, predicted_start_at, predicted_end_at, output_qty, reject_qty, good_qty,
          remaining_qty, planner_status, execution_status, is_late, delay_minutes, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(ps_id) DO UPDATE SET
          predicted_start_at = excluded.predicted_start_at,
          predicted_end_at = excluded.predicted_end_at,
          output_qty = excluded.output_qty,
          reject_qty = excluded.reject_qty,
          good_qty = excluded.good_qty,
          remaining_qty = excluded.remaining_qty,
          planner_status = excluded.planner_status,
          execution_status = excluded.execution_status,
          is_late = excluded.is_late,
          delay_minutes = excluded.delay_minutes,
          updated_at = CURRENT_TIMESTAMP
        """,
        (
            str(ps_id),
            bounds[0] if bounds else None,
            bounds[1] if bounds else None,
            output_qty,
            reject_qty,
            good_qty,
            remaining_qty,
            _text(ps["planner_status"] or "UNPLANNED"),
            execution_status,
            is_late,
            delay_minutes,
        ),
    )
    return {
        "ps_id": str(ps_id),
        "remaining_qty": remaining_qty,
        "execution_status": execution_status,
    }


def refresh_states_for_machine(con, machine_id, schedule_run_id=None):
    block_rows = rows(con.execute("SELECT block_id, operation_id FROM run_block WHERE machine_id = ?", (int(machine_id),)))
    operation_ids = set()
    ps_ids = set()
    for row in block_rows:
        block_id = int(row["block_id"])
        operation_id = int(row["operation_id"] or 0)
        refresh_machine_queue_state(con, block_id, schedule_run_id=schedule_run_id)
        if operation_id:
            operation_ids.add(operation_id)
            op_state = refresh_operation_state(con, operation_id)
            if op_state and op_state.get("ps_id"):
                ps_ids.add(op_state["ps_id"])
    for operation_id in operation_ids:
        op_row = one(con.execute("SELECT COALESCE(source_ps_id, '') AS ps_id FROM operation WHERE operation_id = ?", (int(operation_id),)))
        if op_row and op_row["ps_id"]:
            ps_ids.add(str(op_row["ps_id"]))
    for ps_id in ps_ids:
        refresh_process_sheet_state(con, ps_id)


def upsert_schedule_alert(
    con,
    *,
    schedule_run_id=None,
    block_id=None,
    operation_id=None,
    ps_id=None,
    machine_id=None,
    alert_type,
    severity="INFO",
    message="",
    old_value="",
    new_value="",
    planned_at=None,
    predicted_at=None,
    delay_minutes=0,
    status="OPEN",
):
    existing = one(
        con.execute(
            """
            SELECT alert_id
            FROM schedule_alert
            WHERE block_id IS ?
              AND alert_type = ?
              AND status IN ('OPEN', 'ACKNOWLEDGED')
            ORDER BY created_at DESC, alert_id DESC
            LIMIT 1
            """,
            (block_id, str(alert_type)),
        )
    )
    params = (
        schedule_run_id,
        block_id,
        operation_id,
        ps_id,
        machine_id,
        str(alert_type),
        severity,
        message,
        old_value,
        new_value,
        planned_at,
        predicted_at,
        float(delay_minutes or 0),
        status,
    )
    if existing:
        con.execute(
            """
            UPDATE schedule_alert
            SET schedule_run_id = ?,
                operation_id = ?,
                ps_id = ?,
                machine_id = ?,
                severity = ?,
                message = ?,
                old_value = ?,
                new_value = ?,
                planned_at = ?,
                predicted_at = ?,
                delay_minutes = ?,
                status = ?,
                resolved_at = CASE WHEN ? = 'RESOLVED' THEN CURRENT_TIMESTAMP ELSE resolved_at END,
                updated_at = CURRENT_TIMESTAMP
            WHERE alert_id = ?
            """,
            (
                schedule_run_id,
                operation_id,
                ps_id,
                machine_id,
                severity,
                message,
                old_value,
                new_value,
                planned_at,
                predicted_at,
                float(delay_minutes or 0),
                status,
                status,
                int(existing["alert_id"]),
            ),
        )
        return int(existing["alert_id"])
    cur = con.execute(
        """
        INSERT INTO schedule_alert (
          schedule_run_id, block_id, operation_id, ps_id, machine_id, alert_type, severity,
          message, old_value, new_value, planned_at, predicted_at, delay_minutes, status,
          created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        params,
    )
    return int(cur.lastrowid)


def resolve_schedule_alert(con, alert_id):
    con.execute(
        """
        UPDATE schedule_alert
        SET status = 'RESOLVED',
            resolved_at = COALESCE(resolved_at, CURRENT_TIMESTAMP),
            updated_at = CURRENT_TIMESTAMP
        WHERE alert_id = ?
        """,
        (int(alert_id),),
    )
