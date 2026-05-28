from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, request

from ..actuals import actual_summary_for_block, refresh_block_actual_status
from ..blocks import (
    apply_output_delta_to_block_tail,
    create_rework_from_reject,
    delete_rework_from_reject_segment,
    find_rework_source_for_reject,
    _actual_good_qty,
    _actual_variance,
    apply_actual_variance_delta_to_block_tail,
    actual_daily_rows_for_block_row,
    apply_removed_target_date_to_block_tail,
    reconcile_block_schedule_after_actuals,
    recalculate_all,
    recalculate_machine,
    refresh_planner_alerts,
    refresh_block_schedule_bounds,
    schedule_signature_for_machine,
    removed_actual_dates_for_block_row,
    trial_block_payload,
    trial_block_row,
)
from ..catalog import (
    create_planning_card,
    planning_card_row,
    planning_cards_by_ps,
    schedule_planning_card,
    trial_catalog_items,
)
from ..db import db, one, rows, parse_dt_text
from ..planner_actuals import actual_summaries_for_block_rows, actual_summary_for_process_sheet_rows
from ..materials import material_status_map_for_ps_ids, sync_material_requirements_for_ps_ids
from ..machines import default_profile_for_weekday, fetch_machines, is_public_holiday
from ..machines import machine_work_intervals_for_day
from ..imports import sync_operations_for_flow
from ..planning_scheduler import (
    create_planning_schedule_run,
    recalculate_planning_all as recalculate_planning_all_baseline,
    recalculate_planning_machine as recalculate_planning_machine_baseline,
)
from ..scheduler_state import active_schedule_alert_rows, dismiss_schedule_alert, resolve_schedule_alert
from ..planning_settings import (
    DEFAULT_PLANNING_CALENDAR_POLICY,
    DEFAULT_PLANNING_START_TIME,
    get_planning_calendar_policy,
    get_planning_efficiency,
    get_planning_start_time_text,
    set_planning_setting,
)
from ..visual_time import visual_timing_for_segment
from ..utils import (
    compact_text,
    format_qty,
    normalize_block_status_inputs,
    parse_nullable_number,
    parse_number,
    validate_cycle_minutes,
)

trial_bp = Blueprint("trial", __name__)


def _visual_datetime_text(value):
    dt = parse_dt_text(value)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def _working_minutes_between(con, machine_id, start_text, end_text):
    start_dt = parse_dt_text(start_text)
    end_dt = parse_dt_text(end_text)
    if not start_dt or not end_dt or end_dt <= start_dt or not int(machine_id or 0):
        return 0.0
    total = 0.0
    probe = start_dt.date()
    while probe <= end_dt.date():
        for interval_start, interval_end in machine_work_intervals_for_day(con, int(machine_id), probe):
            overlap_start = max(start_dt, interval_start)
            overlap_end = min(end_dt, interval_end)
            if overlap_end > overlap_start:
                total += (overlap_end - overlap_start).total_seconds() / 60.0
        probe += timedelta(days=1)
    return max(0.0, float(total))


def _void_actual(con, actual_id):
    con.execute(
        """
        UPDATE production_actual
        SET status = 'VOIDED'
        WHERE actual_id = ?
        """,
        (int(actual_id),),
    )


def _insert_actual(con, *, segment_id, block_id, report_date, output_qty, reject_qty, remarks, target_qty, machine_id, entry_type, correction_of_actual_id=None, created_by=""):
    cur = con.execute(
        """
        INSERT INTO production_actual (
          segment_id, block_id, machine_id, report_date, remarks, reported_at,
          output_qty, reject_qty, target_qty_at_report, status, entry_type,
          correction_of_actual_id, good_qty_at_report, created_by
        ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?)
        """,
        (
            segment_id,
            block_id,
            machine_id,
            report_date,
            compact_text(remarks),
            output_qty,
            reject_qty,
            target_qty,
            entry_type,
            correction_of_actual_id,
            None if output_qty is None or reject_qty is None else max(0.0, float(output_qty or 0) - float(reject_qty or 0)),
            created_by,
        ),
    )
    return int(cur.lastrowid)


def _active_actual_for_segment(con, segment_id):
    return one(
        con.execute(
            """
            SELECT *
            FROM production_actual
            WHERE segment_id = ?
              AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
            ORDER BY actual_id DESC
            LIMIT 1
            """,
            (int(segment_id),),
        )
    )


def _active_actual_for_block_date(con, block_id, report_date):
    return one(
        con.execute(
            """
            SELECT *
            FROM production_actual
            WHERE block_id = ?
              AND report_date = ?
              AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
            ORDER BY CASE WHEN segment_id IS NULL THEN 1 ELSE 0 END, actual_id DESC
            LIMIT 1
            """,
            (int(block_id), report_date),
        )
    )


def _planned_target_qty_for_block_date(con, block_id, report_date):
    row = one(
        con.execute(
            """
            SELECT COALESCE(SUM(COALESCE(qty_done, planned_qty, 0)), 0) AS target_qty
            FROM run_block_segment
            WHERE block_id = ?
              AND COALESCE(segment_type, '') = 'production'
              AND segment_date = ?
            """,
            (int(block_id), report_date),
        )
    )
    return max(0.0, float(row["target_qty"] or 0)) if row else 0.0


def _calendar_window_rows(con, start_iso=None, end_iso=None, machine_id=None, active=None, window_type=None):
    clauses = []
    params = []
    if machine_id:
        clauses.append("w.machine_id = ?")
        params.append(int(machine_id))
    if start_iso:
        clauses.append("w.end_at > ?")
        params.append(start_iso)
    if end_iso:
        clauses.append("w.start_at < ?")
        params.append(end_iso)
    if active is not None:
        clauses.append("w.active = ?")
        params.append(1 if int(active) else 0)
    if window_type:
        clauses.append("w.window_type = ?")
        params.append(compact_text(window_type).upper())
    where_clause = " AND ".join(clauses) if clauses else "1 = 1"
    return rows(
        con.execute(
            f"""
            SELECT w.*, m.machine_code
            FROM machine_calendar_window w
            LEFT JOIN machines m ON m.machine_id = w.machine_id
            WHERE {where_clause}
            ORDER BY w.start_at, w.window_id
            """,
            params,
        )
    )


def _calendar_window_payload(row):
    window_type = compact_text(row.get("window_type")).upper()
    return {
        "window_id": int(row.get("window_id") or 0),
        "machine_id": int(row.get("machine_id") or 0),
        "machine_code": compact_text(row.get("machine_code") or ""),
        "start_at": compact_text(row.get("start_at") or ""),
        "end_at": compact_text(row.get("end_at") or ""),
        "window_type": window_type,
        "capacity_minutes": int(row.get("capacity_minutes") or 0),
        "note": compact_text(row.get("note") or ""),
        "active": int(row.get("active") or 0),
        "display_kind": "available" if window_type in {"OVERTIME", "AVAILABLE"} else "blocked",
    }


@trial_bp.get("/api/trial/schedule")
def api_trial_schedule():
    include_completed = int(request.args.get("include_completed") or 0)
    start_iso = compact_text(request.args.get("start") or request.args.get("from")) or date.today().isoformat()
    end_iso = compact_text(request.args.get("end") or request.args.get("to")) or (date.today() + timedelta(days=7)).isoformat()
    with db() as con:
        machines = rows(con.execute("SELECT machine_id, machine_code, machine_category, shift_profile, active FROM machines WHERE active = 1 ORDER BY machine_id"))
        machine_by_id = {int(row["machine_id"]): dict(row) for row in machines}
        raw_blocks = rows(
            con.execute(
                """
                SELECT b.*, o.job_no, o.operation_name, o.total_qty, o.setup_minutes, o.cycle_minutes_per_qty,
                       o.compatible_machine_group, o.source_ps_id, o.source_op_seq_id AS source_op_seq_id, o.source_op_no,
                       m.machine_code, m.machine_category, m.shift_profile
                FROM run_block b
                JOIN operation o ON o.operation_id = b.operation_id
                JOIN machines m ON m.machine_id = b.machine_id
                WHERE COALESCE(b.active, 1) = 1
                ORDER BY b.machine_id, b.queue_position, b.block_id
                """
            )
        )
        raw_segments = rows(
            con.execute(
                """
                SELECT s.*, b.operation_id
                FROM run_block_segment s
                JOIN run_block b ON b.block_id = s.block_id
                WHERE COALESCE(b.active, 1) = 1
                ORDER BY b.machine_id, b.queue_position, s.segment_id
                """
            )
        )
        segments = []
        segments_by_block = {}
        for row in raw_segments:
            item = dict(row)
            machine = machine_by_id.get(int(item.get("machine_id") or 0), {})
            shift_profile = compact_text(machine.get("shift_profile") or item.get("shift_profile") or "")
            start_dt = parse_dt_text(item.get("start_datetime"))
            end_dt = parse_dt_text(item.get("end_datetime"))
            timing = visual_timing_for_segment(
                start_dt,
                item.get("minutes_used") or 0,
                end_dt=end_dt,
                work_date=start_dt.date() if start_dt else None,
                profile_name="",
                shift_profile=shift_profile,
                segment_type=item.get("segment_type") or "production",
            )
            item["shift_profile"] = shift_profile
            item["visual_start_datetime"] = timing["visual_start_datetime"]
            item["visual_end_datetime"] = timing["visual_end_datetime"]
            item["visual_parts"] = timing["visual_parts"]
            item["break_windows"] = timing["break_windows"]
            segments.append(item)
            segments_by_block.setdefault(int(item.get("block_id") or 0), []).append(item)
        blocks = []
        for row in raw_blocks:
            item = dict(row)
            block_segments = segments_by_block.get(int(item.get("block_id") or 0), [])
            if block_segments:
                block_start_dt = parse_dt_text(item.get("start_datetime") or item.get("calculated_start_datetime"))
                block_end_dt = parse_dt_text(item.get("end_datetime") or item.get("calculated_end_datetime"))
                visual_starts = sorted(
                    [compact_text(seg.get("visual_start_datetime")) for seg in block_segments if compact_text(seg.get("visual_start_datetime"))]
                )
                visual_ends = sorted(
                    [compact_text(seg.get("visual_end_datetime")) for seg in block_segments if compact_text(seg.get("visual_end_datetime"))]
                )
                timing = visual_timing_for_segment(
                    block_start_dt,
                    item.get("minutes_used") or 0,
                    end_dt=block_end_dt,
                    work_date=block_start_dt.date() if block_start_dt else None,
                    profile_name="",
                    shift_profile=compact_text(item.get("shift_profile") or machine_by_id.get(int(item.get("machine_id") or 0), {}).get("shift_profile", "")),
                    segment_type=item.get("segment_type") or "production",
                ) if block_start_dt else {"visual_start_datetime": "", "visual_end_datetime": ""}
                item["visual_start_datetime"] = timing.get("visual_start_datetime") or (visual_starts[0] if visual_starts else compact_text(item.get("calculated_start_datetime")))
                item["visual_end_datetime"] = timing.get("visual_end_datetime") or (visual_ends[-1] if visual_ends else compact_text(item.get("calculated_end_datetime")))
                visual_parts = []
                for seg in block_segments:
                    visual_parts.extend(seg.get("visual_parts") or [])
                item["visual_parts"] = visual_parts
                item["break_windows"] = block_segments[0].get("break_windows") or []
                item["shift_profile"] = block_segments[0].get("shift_profile") or machine_by_id.get(int(item.get("machine_id") or 0), {}).get("shift_profile", "")
            else:
                item["visual_start_datetime"] = compact_text(item.get("calculated_start_datetime"))
                item["visual_end_datetime"] = compact_text(item.get("calculated_end_datetime"))
                item["visual_parts"] = []
                item["break_windows"] = []
                item["shift_profile"] = machine_by_id.get(int(item.get("machine_id") or 0), {}).get("shift_profile", "")
            item["actual_daily_rows"] = actual_daily_rows_for_block_row(con, item)
            blocks.append(item)
        actuals = rows(
            con.execute(
                """
                SELECT actual_id, segment_id, block_id, report_date,
                       output_qty, reject_qty, target_qty_at_report,
                       remarks, reported_at
                FROM production_actual
                WHERE COALESCE(status, 'ACTIVE') = 'ACTIVE'
                ORDER BY report_date, actual_id
                """
            )
        )
        capacities = rows(
            con.execute(
                """
                SELECT d.day_id, d.machine_id, d.work_date, d.profile_id, d.capacity_minutes, d.start_minute, d.note, p.profile_name
                FROM machine_capacity_day d
                JOIN capacity_profile p ON p.profile_id = d.profile_id
                ORDER BY d.work_date, d.machine_id
                """
            )
        )
        profiles = rows(con.execute("SELECT profile_name, capacity_minutes, start_minute, note FROM capacity_profile ORDER BY profile_id"))
        calendar_windows = [_calendar_window_payload(row) for row in _calendar_window_rows(con, start_iso, end_iso)]

        ps_ids = set()
        planned_starts = {}
        for row in blocks:
            ps_id = compact_text(row["source_ps_id"])
            if not ps_id:
                continue
            ps_ids.add(ps_id)
            start_text = compact_text(row["calculated_start_datetime"])
            if start_text and (ps_id not in planned_starts or start_text < planned_starts[ps_id]):
                planned_starts[ps_id] = start_text
        sync_material_requirements_for_ps_ids(con, ps_ids)
        material_status_map = material_status_map_for_ps_ids(con, ps_ids, planned_starts)
        default_material_status = {
            "status": "NOT_REQUIRED",
            "label": "",
            "expected_ready_date": "",
            "severity": "none",
        }
        for row in blocks:
            ps_id = compact_text(row["source_ps_id"])
            row["material_status"] = material_status_map.get(ps_id, default_material_status)
        return jsonify(
            {
                "machines": [dict(row) for row in machines],
                "blocks": blocks,
                "segments": segments,
                "actuals": [dict(row) for row in actuals],
                "capacities": [dict(row) for row in capacities],
                "profiles": [dict(row) for row in profiles],
                "calendar_windows": calendar_windows,
            }
        )


@trial_bp.post("/api/trial/capacity")
def api_trial_capacity():
    data = request.get_json(force=True, silent=True) or {}
    work_date = compact_text(data.get("work_date"))
    if not work_date:
        return jsonify({"error": "Work date is required"}), 400
    profile_name = compact_text(data.get("profile_name"))
    try:
        work_day = datetime.fromisoformat(work_date).date()
    except ValueError:
        return jsonify({"error": "Work date must be YYYY-MM-DD"}), 400
    with db() as con:
        machines = fetch_machines(con)
        for machine in machines:
            if work_day.weekday() == 6 or is_public_holiday(con, work_day):
                machine_profile_name = "OFF"
            else:
                machine_profile_name = profile_name or default_profile_for_weekday(work_day.weekday(), machine["shift_profile"])
                if compact_text(machine["shift_profile"]).upper() == "24HR" and machine_profile_name in {"NORMAL_DAY_NIGHT", "SATURDAY"}:
                    machine_profile_name = "FULL_24H"
            profile = one(con.execute("SELECT * FROM capacity_profile WHERE profile_name = ?", (machine_profile_name,)))
            if not profile:
                profile = one(con.execute("SELECT * FROM capacity_profile ORDER BY profile_id LIMIT 1"))
            if not profile:
                return jsonify({"error": "No capacity profiles available"}), 400
            con.execute(
                """
                INSERT INTO machine_capacity_day (machine_id, work_date, profile_id, capacity_minutes, start_minute, note, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(machine_id, work_date) DO UPDATE SET
                  profile_id = excluded.profile_id,
                  capacity_minutes = excluded.capacity_minutes,
                  start_minute = excluded.start_minute,
                  note = excluded.note,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (
                    int(machine["machine_id"]),
                    work_date,
                    int(profile["profile_id"]),
                    int(profile["capacity_minutes"] or 0),
                    int(profile["start_minute"] or 0),
                    compact_text(data.get("note")),
                ),
            )
        recalculate_all(con)
        return jsonify({"ok": True})


@trial_bp.get("/api/trial/machine-calendar-windows")
def api_trial_machine_calendar_windows_list():
    machine_id = int(request.args.get("machine_id") or 0)
    active_arg = request.args.get("active")
    window_type = compact_text(request.args.get("window_type")).upper()
    from_iso = compact_text(request.args.get("from"))
    to_iso = compact_text(request.args.get("to"))
    active = None
    if active_arg is not None and compact_text(active_arg) != "":
        active = 1 if compact_text(active_arg).lower() in {"1", "true", "yes", "on"} else 0
    with db() as con:
        windows = [_calendar_window_payload(row) for row in _calendar_window_rows(con, from_iso or None, to_iso or None, machine_id or None, active, window_type or None)]
        return jsonify({"ok": True, "calendar_windows": windows})


@trial_bp.post("/api/trial/machine-calendar-windows")
def api_trial_machine_calendar_windows_create():
    data = request.get_json(force=True, silent=True) or {}
    machine_id = int(data.get("machine_id") or 0)
    start_at = compact_text(data.get("start_at"))
    end_at = compact_text(data.get("end_at"))
    window_type = compact_text(data.get("window_type")).upper()
    note = compact_text(data.get("note"))
    capacity_minutes = int(parse_number(data.get("capacity_minutes"), 0))
    active = 1 if data.get("active", 1) else 0
    allowed_types = {"AVAILABLE", "DOWN", "OVERTIME", "HOLIDAY", "MAINTENANCE", "BLOCKED"}
    if not machine_id:
        return jsonify({"error": "Machine is required"}), 400
    if not start_at or not end_at:
        return jsonify({"error": "start_at and end_at are required"}), 400
    if window_type not in allowed_types:
        return jsonify({"error": "Invalid window_type"}), 400
    try:
        start_dt = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_at.replace("Z", "+00:00"))
    except ValueError:
        return jsonify({"error": "start_at and end_at must be ISO datetimes"}), 400
    if end_dt <= start_dt:
        return jsonify({"error": "start_at must be earlier than end_at"}), 400
    with db() as con:
        machine = one(con.execute("SELECT machine_id FROM machines WHERE machine_id = ?", (machine_id,)))
        if not machine:
            return jsonify({"error": "Machine not found"}), 404
        cur = con.execute(
            """
            INSERT INTO machine_calendar_window (
              machine_id, start_at, end_at, window_type, capacity_minutes, note, active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                machine_id,
                start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                window_type,
                capacity_minutes,
                note,
                active,
            ),
        )
        recalculate_machine(con, machine_id)
        row = one(
            con.execute(
                """
                SELECT w.*, m.machine_code
                FROM machine_calendar_window w
                LEFT JOIN machines m ON m.machine_id = w.machine_id
                WHERE w.window_id = ?
                """,
                (int(cur.lastrowid),),
            )
        )
        return jsonify({"ok": True, "window": _calendar_window_payload(row)})


@trial_bp.patch("/api/trial/machine-calendar-windows/<int:window_id>")
def api_trial_machine_calendar_windows_update(window_id):
    data = request.get_json(force=True, silent=True) or {}
    with db() as con:
        row = one(con.execute("SELECT * FROM machine_calendar_window WHERE window_id = ?", (int(window_id),)))
        if not row:
            return jsonify({"error": "Window not found"}), 404
        allowed_types = {"AVAILABLE", "DOWN", "OVERTIME", "HOLIDAY", "MAINTENANCE", "BLOCKED"}
        updates = {}
        machine_id = int(data.get("machine_id") or row["machine_id"])
        if "machine_id" in data:
            machine = one(con.execute("SELECT machine_id FROM machines WHERE machine_id = ?", (machine_id,)))
            if not machine:
                return jsonify({"error": "Machine not found"}), 404
            updates["machine_id"] = machine_id
        if "start_at" in data:
            updates["start_at"] = compact_text(data.get("start_at"))
        if "end_at" in data:
            updates["end_at"] = compact_text(data.get("end_at"))
        if "window_type" in data:
            window_type = compact_text(data.get("window_type")).upper()
            if window_type not in allowed_types:
                return jsonify({"error": "Invalid window_type"}), 400
            updates["window_type"] = window_type
        if "capacity_minutes" in data:
            updates["capacity_minutes"] = int(parse_number(data.get("capacity_minutes"), 0))
        if "note" in data:
            updates["note"] = compact_text(data.get("note"))
        if "active" in data:
            updates["active"] = 1 if data.get("active") else 0
        if "start_at" in updates or "end_at" in updates:
            try:
                start_dt = datetime.fromisoformat((updates.get("start_at") or row["start_at"]).replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat((updates.get("end_at") or row["end_at"]).replace("Z", "+00:00"))
            except ValueError:
                return jsonify({"error": "start_at and end_at must be ISO datetimes"}), 400
            if end_dt <= start_dt:
                return jsonify({"error": "start_at must be earlier than end_at"}), 400
            updates["start_at"] = start_dt.strftime("%Y-%m-%d %H:%M:%S")
            updates["end_at"] = end_dt.strftime("%Y-%m-%d %H:%M:%S")
        if updates:
            set_clause = ", ".join(f"{key} = ?" for key in updates)
            con.execute(
                f"UPDATE machine_calendar_window SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE window_id = ?",
                (*updates.values(), int(window_id)),
            )
        recalculate_machine(con, machine_id)
        row = one(
            con.execute(
                """
                SELECT w.*, m.machine_code
                FROM machine_calendar_window w
                LEFT JOIN machines m ON m.machine_id = w.machine_id
                WHERE w.window_id = ?
                """,
                (int(window_id),),
            )
        )
        return jsonify({"ok": True, "window": _calendar_window_payload(row)})


@trial_bp.delete("/api/trial/machine-calendar-windows/<int:window_id>")
def api_trial_machine_calendar_windows_delete(window_id):
    with db() as con:
        row = one(con.execute("SELECT * FROM machine_calendar_window WHERE window_id = ?", (int(window_id),)))
        if not row:
            return jsonify({"error": "Window not found"}), 404
        con.execute(
            """
            UPDATE machine_calendar_window
            SET active = 0, updated_at = CURRENT_TIMESTAMP
            WHERE window_id = ?
            """,
            (int(window_id),),
        )
        recalculate_machine(con, int(row["machine_id"]))
        return jsonify({"ok": True, "window_id": int(window_id)})


@trial_bp.post("/api/trial/operations")
def api_trial_create_operation():
    data = request.get_json(force=True, silent=True) or {}
    job_no = compact_text(data.get("job_no"))
    operation_name = compact_text(data.get("operation_name"))
    machine_id = int(data.get("machine_id") or 0)
    if not job_no or not operation_name:
        return jsonify({"error": "Job number and operation name are required"}), 400
    if not machine_id:
        return jsonify({"error": "Machine is required"}), 400
    cycle_error = validate_cycle_minutes(
        data.get("total_qty"),
        data.get("scheduled_qty"),
        data.get("cycle_minutes_per_qty"),
    )
    if cycle_error:
        return jsonify({"error": cycle_error}), 400
    with db() as con:
        planning_status, execution_status = normalize_block_status_inputs(data)
        planned_start_at = compact_text(data.get("planned_start_at") or data.get("anchor_datetime"))
        planned_end_at = compact_text(data.get("planned_end_at"))
        allow_pull_forward = 1 if int(data.get("allow_pull_forward", 1) or 0) else 0
        active = 1 if int(data.get("active", 1) or 0) else 0
        is_fresh_monday_item = 1 if int(data.get("is_fresh_monday_item", 0) or 0) else 0
        op_cur = con.execute(
            """
            INSERT INTO operation (
              job_no, operation_name, total_qty, setup_minutes, cycle_minutes_per_qty, compatible_machine_group,
              source_ps_id, source_op_seq_id, source_op_no, status, remarks, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                job_no,
                operation_name,
                parse_number(data.get("total_qty"), parse_number(data.get("scheduled_qty"), 0)),
                parse_number(data.get("setup_minutes"), 0),
                parse_number(data.get("cycle_minutes_per_qty"), 0),
                compact_text(data.get("compatible_machine_group")),
                compact_text(data.get("source_ps_id")),
                int(data.get("source_op_seq_id") or 0),
                compact_text(data.get("source_op_no")),
                compact_text(data.get("status") or "ACTIVE") or "ACTIVE",
                compact_text(data.get("remarks")),
            ),
        )
        operation_id = int(op_cur.lastrowid)
        queue_position = float(data.get("queue_position") or 0)
        if queue_position <= 0:
            queue_position = 10 + float(one(con.execute("SELECT COALESCE(MAX(queue_position), 0) AS mx FROM run_block WHERE machine_id = ?", (machine_id,)))["mx"] or 0)
        block_cur = con.execute(
            """
            INSERT INTO run_block (
              operation_id, machine_id, queue_position, scheduled_qty, include_setup, status, planning_status, execution_status,
              anchor_datetime, planned_start_at, planned_end_at, allow_pull_forward, active, is_fresh_monday_item,
              calculated_start_datetime, calculated_end_datetime, actual_good_qty, actual_reject_qty, remarks, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                operation_id,
                machine_id,
                queue_position,
                parse_number(data.get("scheduled_qty"), parse_number(data.get("total_qty"), 0)),
                1 if data.get("include_setup", 1) else 0,
                execution_status,
                planning_status,
                execution_status,
                planned_start_at or compact_text(data.get("anchor_datetime")),
                planned_start_at,
                planned_end_at,
                allow_pull_forward,
                active,
                is_fresh_monday_item,
                "",
                "",
                0,
                0,
                compact_text(data.get("remarks")),
            ),
        )
        recalculate_machine(con, machine_id)
        return jsonify({"ok": True, "operation_id": operation_id, "block": trial_block_payload(trial_block_row(con, block_cur.lastrowid), con)})


@trial_bp.post("/api/trial/planner/schedule-opn")
def api_trial_planner_schedule_opn():
    data = request.get_json(force=True, silent=True) or {}
    source_ps_id = compact_text(data.get("source_ps_id") or "")
    pp_partial_no = compact_text(data.get("pp_partial_no") or data.get("partial_no") or "")
    source_op_seq_id = int(data.get("source_op_seq_id") or 0)
    source_op_no = compact_text(data.get("source_op_no") or "")
    machine_id = int(data.get("machine_id") or 0)
    queue_position = float(data.get("queue_position") or 0)
    if not source_ps_id:
        return jsonify({"error": "source_ps_id is required"}), 400
    if not machine_id:
        return jsonify({"error": "machine_id is required"}), 400
    if not source_op_seq_id and not source_op_no:
        return jsonify({"error": "source_op_seq_id or source_op_no is required"}), 400
    with db() as con:
        ps_row = _planner_process_sheet_row(con, source_ps_id, pp_partial_no)
        if not ps_row:
            return jsonify({"error": "Process sheet not found"}), 404
        pp_partial_no = compact_text(ps_row.get("pp_partial_no") or ps_row.get("partial_no") or pp_partial_no or "")
        bom_options = _planner_bom_options_for_part(con, ps_row.get("part_id") or 0)
        selected_bom_id = int(data.get("selected_bom_id") or ps_row.get("selected_bom_id") or 0)
        if not selected_bom_id:
            selected_bom_id = _planner_default_bom_id(bom_options)
        if not selected_bom_id:
            return jsonify({"error": "No selected or default BOM found for this process sheet"}), 400
        step_row = None
        if source_op_seq_id:
            step_row = one(
                con.execute(
                    """
                    SELECT *
                    FROM operation_seq
                    WHERE bom_id = ?
                      AND op_seq_id = ?
                    LIMIT 1
                    """,
                    (selected_bom_id, source_op_seq_id),
                )
            )
        if not step_row and source_op_no:
            step_row = one(
                con.execute(
                    """
                    SELECT *
                    FROM operation_seq
                    WHERE bom_id = ?
                      AND op_no = ?
                    ORDER BY seq_no, op_seq_id
                    LIMIT 1
                    """,
                    (selected_bom_id, source_op_no),
                )
            )
        if not step_row:
            return jsonify({"error": "Operation step not found for selected BOM"}), 404
        step_row = dict(step_row)
        source_op_seq_id = int(step_row.get("op_seq_id") or source_op_seq_id or 0)
        source_op_no = compact_text(source_op_no or step_row.get("op_no") or "")
        current_cards = _planner_op_cards_for_source_ps(
            con,
            source_ps_id,
            pp_partial_no,
            selected_bom_id,
            ps_row.get("partial_qty") or ps_row.get("planned_qty") or ps_row.get("total_qty") or 0,
        )
        current_card = next(
            (
                card
                for card in current_cards
                if int(card.get("source_op_seq_id") or 0) == int(source_op_seq_id or 0)
                and compact_text(card.get("source_op_no") or "") == source_op_no
            ),
            None,
        )
        target_qty = max(
            0.0,
            parse_number((current_card or {}).get("display_target_qty") or 0, 0),
            parse_number((current_card or {}).get("target_qty") or 0, 0),
            parse_number(ps_row.get("partial_qty") or 0, 0),
            parse_number(ps_row.get("planned_qty") or 0, 0),
            parse_number(ps_row.get("total_qty") or 0, 0),
        )
        planned_qty = _planner_planned_qty_for_op(con, source_ps_id, pp_partial_no, selected_bom_id, source_op_seq_id, source_op_no)
        remaining_qty = max(0.0, target_qty - planned_qty)
        operation_name = compact_text(step_row.get("op_type") or "")
        setup_minutes = parse_number(step_row.get("setup_time"), 0)
        cycle_minutes_per_qty = parse_number(step_row.get("cycle_time"), 0)
        compatible_machine_group = compact_text(step_row.get("machine_category") or "")
        existing_op = one(
            con.execute(
                """
                SELECT o.*
                FROM operation o
                LEFT JOIN run_block b
                  ON b.operation_id = o.operation_id
                WHERE COALESCE(o.source_ps_id, '') = ?
                  AND COALESCE(o.pp_partial_no, '') = ?
                  AND COALESCE(o.selected_bom_id, 0) = ?
                  AND COALESCE(o.source_op_seq_id, 0) = ?
                  AND COALESCE(o.source_op_no, '') = ?
                GROUP BY o.operation_id
                ORDER BY COALESCE(SUM(CASE WHEN COALESCE(b.active, 1) = 1 THEN COALESCE(b.scheduled_qty, 0) ELSE 0 END), 0) DESC,
                         COALESCE(o.updated_at, '') DESC,
                         o.operation_id DESC
                LIMIT 1
                """,
                (source_ps_id, pp_partial_no, selected_bom_id, source_op_seq_id, source_op_no),
            )
        )
        if existing_op:
            operation_id = int(existing_op["operation_id"])
            op_updates = {}
            if compact_text(existing_op["job_no"]) != source_ps_id:
                op_updates["job_no"] = source_ps_id
            if compact_text(existing_op["pp_partial_no"]) != pp_partial_no:
                op_updates["pp_partial_no"] = pp_partial_no
            if int(existing_op["selected_bom_id"] or 0) != int(selected_bom_id or 0):
                op_updates["selected_bom_id"] = int(selected_bom_id or 0)
            if compact_text(existing_op["operation_name"]) != operation_name:
                op_updates["operation_name"] = operation_name
            if float(existing_op["total_qty"] or 0) != float(target_qty):
                op_updates["total_qty"] = float(target_qty)
            if float(existing_op["setup_minutes"] or 0) != float(setup_minutes):
                op_updates["setup_minutes"] = float(setup_minutes)
            if float(existing_op["cycle_minutes_per_qty"] or 0) != float(cycle_minutes_per_qty):
                op_updates["cycle_minutes_per_qty"] = float(cycle_minutes_per_qty)
            if compact_text(existing_op["compatible_machine_group"]) != compatible_machine_group:
                op_updates["compatible_machine_group"] = compatible_machine_group
            if op_updates:
                set_clause = ", ".join(f"{key} = ?" for key in op_updates)
                con.execute(
                    f"UPDATE operation SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE operation_id = ?",
                    (*op_updates.values(), operation_id),
                )
        else:
            op_cur = con.execute(
                """
                INSERT INTO operation (
                  job_no, operation_name, total_qty, setup_minutes, cycle_minutes_per_qty, compatible_machine_group,
                  source_ps_id, pp_partial_no, selected_bom_id, source_op_seq_id, source_op_no, status, remarks, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    source_ps_id,
                    operation_name,
                    float(target_qty),
                    float(setup_minutes),
                    float(cycle_minutes_per_qty),
                    compatible_machine_group,
                    source_ps_id,
                    pp_partial_no,
                    int(selected_bom_id or 0),
                    source_op_seq_id,
                    source_op_no,
                    "ACTIVE",
                    "",
                ),
            )
            operation_id = int(op_cur.lastrowid)
        planned_qty = max(
            0.0,
            parse_number(
                one(
                    con.execute(
                        """
                        SELECT COALESCE(SUM(scheduled_qty), 0) AS planned_qty
                        FROM run_block
                        WHERE active = 1
                          AND operation_id = ?
                        """,
                        (operation_id,),
                    )
                ).get("planned_qty") if operation_id else 0,
                0,
            ),
        )
        remaining_qty = max(0.0, target_qty - planned_qty)
        visible_cards = _planner_op_cards_for_source_ps(
            con,
            source_ps_id,
            pp_partial_no,
            selected_bom_id,
            ps_row.get("partial_qty") or ps_row.get("planned_qty") or ps_row.get("total_qty") or 0,
        )
        visible_card = next(
            (
                card
                for card in visible_cards
                if int(card.get("source_op_seq_id") or 0) == int(source_op_seq_id or 0)
                and compact_text(card.get("source_op_no") or "") == source_op_no
            ),
            None,
        )
        if visible_card and (not bool(visible_card.get("can_drag", True)) or max(0.0, parse_number(visible_card.get("remaining_qty") or 0, 0)) <= 0):
            return jsonify({"error": "This OPN is already fully planned."}), 409
        if remaining_qty <= 0:
            return jsonify({"error": "This OPN is already fully planned."}), 409
        requested_qty = parse_number(data.get("scheduled_qty"), 0)
        if requested_qty <= 0:
            requested_qty = remaining_qty
        scheduled_qty = min(requested_qty, remaining_qty)
        if queue_position <= 0:
            queue_position = 10 + float(one(con.execute("SELECT COALESCE(MAX(queue_position), 0) AS mx FROM run_block WHERE machine_id = ?", (machine_id,)))["mx"] or 0)
        block_cur = con.execute(
            """
            INSERT INTO run_block (
              operation_id, machine_id, queue_position, scheduled_qty, include_setup, status, planning_status, execution_status,
              anchor_datetime, planned_start_at, planned_end_at, allow_pull_forward, active, is_fresh_monday_item,
              calculated_start_datetime, calculated_end_datetime, actual_good_qty, actual_reject_qty, remarks, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                operation_id,
                machine_id,
                queue_position,
                float(scheduled_qty),
                1,
                "NOT_STARTED",
                "PLANNED",
                "NOT_STARTED",
                "",
                "",
                "",
                1,
                1,
                0,
                "",
                "",
                0,
                0,
                "",
            ),
        )
        planning_run_id = recalculate_planning_all_baseline(con, reason="PLANNER_SCHEDULE_OPN")
        refresh_planner_alerts(con)
        block = trial_block_payload(trial_block_row(con, block_cur.lastrowid), con)
        return jsonify(
            {
                "ok": True,
                "operation_id": operation_id,
                "pp_partial_no": pp_partial_no,
                "selected_bom_id": int(selected_bom_id or 0),
                "block_id": int(block_cur.lastrowid),
                "planning_run_id": planning_run_id,
                "block": block,
            }
        )


@trial_bp.post("/api/trial/planning-cards")
def api_trial_create_planning_card():
    data = request.get_json(force=True, silent=True) or {}
    with db() as con:
        try:
            card = create_planning_card(con, data.get("ps_id"), data.get("ops") or [], data.get("target_qty"))
            return jsonify({"ok": True, "card": card})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400


@trial_bp.post("/api/trial/planning-cards/<int:card_id>/schedule")
def api_trial_schedule_planning_card(card_id):
    data = request.get_json(force=True, silent=True) or {}
    machine_id = int(data.get("machine_id") or 0)
    queue_position = float(data.get("queue_position") or 0)
    with db() as con:
        try:
            result = schedule_planning_card(con, card_id, machine_id, queue_position)
            affected_machine_id = int(
                (result.get("group") or {}).get("machine_id")
                or (result.get("card") or {}).get("machine_id")
                or machine_id
            )
            if affected_machine_id:
                recalculate_machine(con, affected_machine_id)
            return jsonify({"ok": True, **result})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400


@trial_bp.delete("/api/trial/planning-cards/<int:card_id>")
def api_trial_delete_planning_card(card_id):
    with db() as con:
        card = planning_card_row(con, card_id)
        if not card:
            return jsonify({"error": "Planning card not found"}), 404
        if compact_text(card["planning_status"]).upper() == "SCHEDULED" or int(card["scheduled_block_group_id"] or 0) > 0:
            return jsonify({"error": "This planning card is already scheduled. Remove it from the machine schedule first."}), 400
        con.execute("DELETE FROM planning_card WHERE card_id = ?", (int(card_id),))
        return jsonify({"ok": True, "card_id": int(card_id)})


@trial_bp.put("/api/trial/blocks/<int:block_id>")
def api_trial_update_block(block_id):
    data = request.get_json(force=True, silent=True) or {}
    with db() as con:
        block = trial_block_row(con, block_id)
        if not block:
            return jsonify({"error": "Run block not found"}), 404
        cycle_fields_changed = any(key in data for key in ("total_qty", "scheduled_qty", "cycle_minutes_per_qty"))
        if cycle_fields_changed:
            next_total_qty = data.get("total_qty", block["total_qty"])
            next_scheduled_qty = data.get("scheduled_qty", block["scheduled_qty"])
            next_cycle_minutes = data.get("cycle_minutes_per_qty", block["cycle_minutes_per_qty"])
            cycle_error = validate_cycle_minutes(next_total_qty, next_scheduled_qty, next_cycle_minutes)
            if cycle_error:
                return jsonify({"error": cycle_error}), 400
        op_updates = {}
        for key in ("job_no", "operation_name", "compatible_machine_group", "remarks"):
            if key in data:
                op_updates[key] = compact_text(data.get(key))
        for key in ("total_qty", "setup_minutes", "cycle_minutes_per_qty"):
            if key in data:
                op_updates[key] = parse_number(data.get(key), 0)
        if op_updates:
            set_clause = ", ".join(f"{k} = ?" for k in op_updates)
            con.execute(
                f"UPDATE operation SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE operation_id = ?",
                (*op_updates.values(), int(block["operation_id"])),
            )
        block_updates = {}
        if any(key in data for key in ("planning_status", "execution_status", "status")):
            planning_status, execution_status = normalize_block_status_inputs(
                data,
                default_planning=compact_text(block["planning_status"]) or "PLANNED",
                default_execution=compact_text(block["execution_status"] or block["status"]) or "NOT_STARTED",
            )
            block_updates["planning_status"] = planning_status
            block_updates["execution_status"] = execution_status
            block_updates["status"] = execution_status
        if "machine_id" in data:
            block_updates["machine_id"] = int(data.get("machine_id") or block["machine_id"])
        if "queue_position" in data:
            block_updates["queue_position"] = max(1.0, float(data.get("queue_position") or block["queue_position"]))
        if "scheduled_qty" in data:
            block_updates["scheduled_qty"] = max(0.0, parse_number(data.get("scheduled_qty"), block["scheduled_qty"]))
        if "include_setup" in data:
            block_updates["include_setup"] = 1 if data.get("include_setup") else 0
        anchor_related_changed = any(key in data for key in ("anchor_datetime", "planned_start_at", "planned_end_at", "allow_pull_forward"))
        if "anchor_datetime" in data:
            anchor_value = compact_text(data.get("anchor_datetime"))
            block_updates["anchor_datetime"] = anchor_value
            if "planned_start_at" not in data:
                block_updates["planned_start_at"] = anchor_value
        if "planned_start_at" in data:
            block_updates["planned_start_at"] = compact_text(data.get("planned_start_at"))
        if "planned_end_at" in data:
            block_updates["planned_end_at"] = compact_text(data.get("planned_end_at"))
        if "allow_pull_forward" in data:
            block_updates["allow_pull_forward"] = 1 if data.get("allow_pull_forward") else 0
        if "active" in data:
            block_updates["active"] = 1 if data.get("active") else 0
        if "is_fresh_monday_item" in data:
            block_updates["is_fresh_monday_item"] = 1 if data.get("is_fresh_monday_item") else 0
        if "actual_good_qty" in data:
            block_updates["actual_good_qty"] = max(0.0, parse_number(data.get("actual_good_qty"), block["actual_good_qty"]))
        if "actual_reject_qty" in data:
            block_updates["actual_reject_qty"] = max(0.0, parse_number(data.get("actual_reject_qty"), block["actual_reject_qty"]))
        if "scheduler_note" in data:
            block_updates["scheduler_note"] = compact_text(data.get("scheduler_note"))
        if "remarks" in data:
            block_updates["remarks"] = compact_text(data.get("remarks"))
        if block_updates:
            set_clause = ", ".join(f"{k} = ?" for k in block_updates)
            con.execute(
                f"UPDATE run_block SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE block_id = ?",
                (*block_updates.values(), int(block_id)),
            )
        machine_ids = {int(block["machine_id"])}
        if "machine_id" in block_updates:
            machine_ids.add(int(block_updates["machine_id"]))
        for machine_id in machine_ids:
            recalculate_machine(con, machine_id)
        if anchor_related_changed:
            recalculate_planning_all_baseline(con, reason="PLANNER_ANCHOR_UPDATE")
        if anchor_related_changed:
            refresh_planner_alerts(con)
        refreshed_block = trial_block_row(con, block_id)
        return jsonify({"ok": True, "block": trial_block_payload(refreshed_block, con)})


@trial_bp.get("/api/trial/blocks/<int:block_id>")
def api_trial_get_block(block_id):
    with db() as con:
        block = trial_block_row(con, block_id)
        if not block:
            return jsonify({"error": "Run block not found"}), 404
        return jsonify({"ok": True, "block": trial_block_payload(block, con)})


@trial_bp.post("/api/trial/blocks/<int:block_id>/split")
def api_trial_split_block(block_id):
    data = request.get_json(force=True, silent=True) or {}
    split_qty = parse_number(data.get("split_qty"), 0)
    if split_qty <= 0:
        return jsonify({"error": "Split quantity is required"}), 400
    with db() as con:
        block = trial_block_row(con, block_id)
        if not block:
            return jsonify({"error": "Run block not found"}), 404
        original_qty = float(block["scheduled_qty"] or 0)
        if split_qty >= original_qty:
            return jsonify({"error": "Split quantity must be smaller than the scheduled quantity"}), 400
        machine_id = int(block["machine_id"])
        current_position = float(block["queue_position"] or 0)
        next_block = one(
            con.execute(
                """
                SELECT block_id, queue_position
                FROM run_block
                WHERE machine_id = ?
                  AND queue_position > ?
                  AND block_id <> ?
                ORDER BY queue_position, block_id
                LIMIT 1
                """,
                (machine_id, current_position, int(block_id)),
            )
        )
        if next_block:
            next_position = float(next_block["queue_position"] or 0)
            new_queue_position = (current_position + next_position) / 2.0
        else:
            new_queue_position = current_position + 10.0
        remaining = original_qty - split_qty
        planning_status = compact_text(block["planning_status"]) or "PLANNED"
        execution_status = compact_text(block["execution_status"] or block["status"]) or "NOT_STARTED"
        con.execute("UPDATE run_block SET scheduled_qty = ?, updated_at = CURRENT_TIMESTAMP WHERE block_id = ?", (split_qty, block_id))
        cur = con.execute(
            """
            INSERT INTO run_block (
              operation_id, machine_id, queue_position, scheduled_qty, include_setup, status, planning_status, execution_status,
              anchor_datetime, planned_start_at, planned_end_at, allow_pull_forward, active, is_fresh_monday_item,
              calculated_start_datetime, calculated_end_datetime, actual_good_qty, actual_reject_qty, remarks, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                int(block["operation_id"]),
                machine_id,
                float(new_queue_position),
                remaining,
                0,
                execution_status,
                planning_status,
                execution_status,
                "",
                "",
                "",
                int(block.get("allow_pull_forward") if block.get("allow_pull_forward") is not None else 1),
                int(block.get("active") if block.get("active") is not None else 1),
                int(block.get("is_fresh_monday_item") or 0),
                "",
                "",
                0,
                0,
                compact_text(block["remarks"]),
            ),
        )
        planning_run_id = recalculate_planning_all_baseline(con, reason="PLANNER_SPLIT_BLOCK")
        refresh_planner_alerts(con)
        recalculate_machine(con, machine_id)
        return jsonify(
            {
                "ok": True,
                "block": trial_block_payload(trial_block_row(con, block_id), con),
                "new_block": trial_block_payload(trial_block_row(con, cur.lastrowid), con),
                "planning_run_id": planning_run_id,
            }
        )


@trial_bp.post("/api/trial/blocks/<int:block_id>/reorder")
def api_trial_reorder_blocks(block_id):
    data = request.get_json(force=True, silent=True) or {}
    ordered_ids = [int(v) for v in data.get("ordered_ids", []) if v is not None and compact_text(v) != ""]
    if not ordered_ids:
        return jsonify({"error": "ordered_ids are required"}), 400
    with db() as con:
        block = trial_block_row(con, block_id)
        if not block:
            return jsonify({"error": "Run block not found"}), 404
        machine_id = int(data.get("machine_id") or block["machine_id"])
        existing_blocks = rows(
            con.execute(
                f"""
                SELECT block_id, machine_id
                FROM run_block
                WHERE block_id IN ({",".join("?" for _ in ordered_ids)})
                """,
                ordered_ids,
            )
        )
        affected_machine_ids = {int(machine_id)}
        affected_machine_ids.update(int(row["machine_id"]) for row in existing_blocks)
        for idx, ordered_block_id in enumerate(ordered_ids, 1):
            con.execute(
                "UPDATE run_block SET machine_id = ?, queue_position = ?, updated_at = CURRENT_TIMESTAMP WHERE block_id = ?",
                (machine_id, float(idx * 10), ordered_block_id),
            )
        for affected_machine_id in affected_machine_ids:
            recalculate_machine(con, affected_machine_id)
        return jsonify({"ok": True})


@trial_bp.delete("/api/trial/blocks/<int:block_id>")
def api_trial_delete_block(block_id):
    with db() as con:
        block = trial_block_row(con, block_id)
        if not block:
            return jsonify({"error": "Run block not found"}), 404
        machine_id = int(block["machine_id"])
        operation_id = int(block["operation_id"])
        group_id = int(block["group_id"] or 0)
        ps_id = compact_text(block["job_no"] or block["source_ps_id"] or "")
        base_ps_id = ps_id.split("::", 1)[0] if ps_id else ""
        target_block_ids = [int(block_id)]
        if group_id:
            target_block_rows = rows(
                con.execute(
                    """
                    SELECT b.block_id, b.operation_id, b.machine_id, b.block_type,
                           o.source_ps_id, o.source_op_seq_id, o.source_op_no
                    FROM run_block b
                    JOIN operation o ON o.operation_id = b.operation_id
                    WHERE group_id = ?
                    """,
                    (group_id,),
                )
            )
            target_block_ids = [int(row["block_id"]) for row in target_block_rows]
        else:
            target_block_rows = [dict(block)]

        affected_ps_ids = set()
        for row in target_block_rows:
            rework_guard = one(
                con.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM rework_link
                    WHERE source_block_id = ? OR rework_block_id = ?
                    """,
                    (int(row["block_id"]), int(row["block_id"])),
                )
            )
            if int((rework_guard or {})["cnt"] if rework_guard else 0) > 0 or compact_text(row.get("block_type")).upper() == "REWORK":
                return jsonify({"error": "This item is part of rework traceability. Remove or resolve the rework link first."}), 400
            source_ps_id = compact_text(row.get("source_ps_id") or "")
            if source_ps_id:
                affected_ps_ids.add(source_ps_id)
                base_source_ps_id = source_ps_id.split("::", 1)[0].strip()
                if base_source_ps_id:
                    affected_ps_ids.add(base_source_ps_id)

        affected_machine_ids = {int(row["machine_id"]) for row in target_block_rows if int(row["machine_id"] or 0)}
        affected_operation_ids = {int(row["operation_id"]) for row in target_block_rows if int(row["operation_id"] or 0)}

        for row in target_block_rows:
            block_id_value = int(row["block_id"])
            con.execute("DELETE FROM schedule_alert WHERE block_id = ?", (block_id_value,))
            con.execute("DELETE FROM machine_queue_state WHERE block_id = ?", (block_id_value,))
            con.execute("DELETE FROM run_block_segment WHERE block_id = ?", (block_id_value,))

        if group_id:
            if ps_id and base_ps_id and ps_id != base_ps_id:
                con.execute(
                    """
                    DELETE FROM planning_card
                    WHERE scheduled_block_group_id = ?
                       OR (planning_status = 'SCHEDULED' AND ps_id IN (?, ?))
                    """,
                    (group_id, ps_id, base_ps_id),
                )
            elif ps_id:
                con.execute(
                    """
                    DELETE FROM planning_card
                    WHERE scheduled_block_group_id = ?
                       OR (planning_status = 'SCHEDULED' AND ps_id = ?)
                    """,
                    (group_id, ps_id),
                )
            else:
                con.execute("DELETE FROM planning_card WHERE scheduled_block_group_id = ?", (group_id,))
            con.execute("DELETE FROM run_block WHERE group_id = ?", (group_id,))
            con.execute("DELETE FROM run_block_group WHERE group_id = ?", (group_id,))
        else:
            con.execute("DELETE FROM run_block WHERE block_id = ?", (int(block_id),))

        if affected_ps_ids:
            ps_placeholders = ", ".join("?" for _ in affected_ps_ids)
            con.execute(
                f"DELETE FROM planning_process_sheet_state WHERE ps_id IN ({ps_placeholders})",
                tuple(sorted(affected_ps_ids)),
            )

        for op_id in affected_operation_ids:
            remaining = one(con.execute("SELECT COUNT(*) AS cnt FROM run_block WHERE operation_id = ?", (int(op_id),)))
            if int((remaining or {})["cnt"] if remaining else 0) <= 0:
                con.execute("DELETE FROM operation WHERE operation_id = ?", (int(op_id),))

        for mid in affected_machine_ids:
            recalculate_machine(con, int(mid))
        recalculate_planning_all_baseline(con, reason="PLANNER_DELETE_BLOCK")
        return jsonify({"ok": True})


@trial_bp.route("/api/trial/segments/<int:segment_id>/actual", methods=["PATCH", "POST"])
def api_trial_segment_actual(segment_id):
    data = request.get_json(force=True, silent=True) or {}

    with db() as con:
        segment = one(
            con.execute(
                """
                SELECT s.*, b.operation_id, b.machine_id AS block_machine_id
                FROM run_block_segment s
                JOIN run_block b ON b.block_id = s.block_id
                WHERE s.segment_id = ?
                """,
                (int(segment_id),),
            )
        )
        if not segment:
            return jsonify({"error": "Planned segment not found"}), 404

        block_id = int(segment["block_id"])
        machine_id = int(segment["machine_id"])
        report_date = compact_text(segment["segment_date"])
        existing = _active_actual_for_segment(con, segment_id)

        output_provided = "output_qty" in data
        reject_provided = "reject_qty" in data
        remarks_provided = "remarks" in data

        output_qty = parse_nullable_number(data.get("output_qty")) if output_provided else None
        reject_qty = parse_nullable_number(data.get("reject_qty")) if reject_provided else None
        remarks = compact_text(data.get("remarks")) if remarks_provided else None
        stored_target_qty = existing["target_qty_at_report"] if existing and existing["target_qty_at_report"] is not None else None
        target_qty = float(stored_target_qty if stored_target_qty is not None else segment["qty_done"] or 0)
        old_output_qty = existing["output_qty"] if existing else None
        old_reject_qty = existing["reject_qty"] if existing else None
        old_good_qty = 0.0 if old_output_qty is None or old_reject_qty is None else max(0.0, float(old_output_qty) - float(old_reject_qty))
        next_output_qty = output_qty if output_provided else (old_output_qty if existing else None)
        next_reject_qty = reject_qty if reject_provided else (old_reject_qty if existing else None)
        new_good_qty = 0.0 if next_output_qty is None or next_reject_qty is None else max(0.0, float(next_output_qty) - float(next_reject_qty))
        output_delta = new_good_qty - old_good_qty
        rework_source = find_rework_source_for_reject(con, block_id) if reject_provided and reject_qty and reject_qty > 0 else None
        output_adjustment = {"changed": False, "applied_qty": 0.0}
        if output_provided and output_delta != 0:
            output_adjustment = apply_output_delta_to_block_tail(con, block_id, report_date, output_delta)

        if existing:
            _void_actual(con, existing["actual_id"])
        _insert_actual(
            con,
            segment_id=int(segment_id),
            block_id=block_id,
            report_date=report_date,
            output_qty=next_output_qty,
            reject_qty=next_reject_qty,
            remarks=remarks if remarks_provided else (existing["remarks"] if existing else ""),
            target_qty=target_qty,
            machine_id=machine_id,
            entry_type="CORRECTION" if existing else "REPORT",
            correction_of_actual_id=int(existing["actual_id"]) if existing else None,
            created_by=compact_text(data.get("created_by")),
        )

        rework = {"created": False, "machine_id": 0}
        removed_rework_machine_ids = set()
        if reject_provided and reject_qty and reject_qty > 0:
            if rework_source:
                rework = create_rework_from_reject(con, int(rework_source["block_id"]), segment_id, reject_qty)
        elif reject_provided:
            removed_rework_machine_ids = delete_rework_from_reject_segment(con, segment_id)

        refresh_block_actual_status(con, block_id)
        affected_machine_ids = {machine_id}
        if rework.get("created") and int(rework.get("machine_id") or 0):
            affected_machine_ids.add(int(rework["machine_id"]))
        affected_machine_ids.update(int(mid) for mid in removed_rework_machine_ids if int(mid or 0))
        before_signatures = {mid: schedule_signature_for_machine(con, mid) for mid in affected_machine_ids}
        for affected_machine_id in affected_machine_ids:
            recalculate_machine(con, affected_machine_id)
        after_signatures = {mid: schedule_signature_for_machine(con, mid) for mid in affected_machine_ids}
        schedule_adjusted = (
            bool(output_adjustment["changed"])
            or bool(rework["created"])
            or bool(removed_rework_machine_ids)
            or any(before_signatures.get(mid) != after_signatures.get(mid) for mid in affected_machine_ids)
        )
        block = trial_block_row(con, block_id)

        message_parts = []
        if output_provided:
            if output_qty is None:
                message_parts.append(f"Actual output cleared for {report_date}.")
            else:
                message_parts.append(f"Actual output {format_qty(output_qty)} saved for {report_date}.")
        if reject_provided:
            if reject_qty and reject_qty > 0:
                message_parts.append(f"Reject {format_qty(reject_qty)} saved for {report_date}.")
                if not rework["created"]:
                    message_parts.append("Remaining qty updated.")
            elif reject_qty is None:
                message_parts.append(f"Reject cleared for {report_date}.")
            else:
                message_parts.append(f"Reject 0 saved for {report_date}.")
        if remarks_provided and not output_provided and not reject_provided:
            message_parts.append(f"Remark saved for {report_date}.")
        if rework["created"]:
            message_parts.append("Rework block created.")
        if schedule_adjusted:
            message_parts.append("Schedule adjusted.")

        return jsonify(
            {
                "ok": True,
                "segment_id": int(segment_id),
                "block_id": block_id,
                "report_date": report_date,
                "schedule_adjusted": schedule_adjusted,
                "rework_created": bool(rework["created"]),
                "message": " ".join(message_parts).strip() or "Actual saved.",
                "block": trial_block_payload(trial_block_row(con, block_id), con),
            }
        )


@trial_bp.post("/api/trial/blocks/<int:block_id>/actual")
def api_trial_actual(block_id):
    data = request.get_json(force=True, silent=True) or {}
    with db() as con:
        block = trial_block_row(con, block_id)
        if not block:
            return jsonify({"error": "Run block not found"}), 404
        delete_dates = [compact_text(v) for v in (data.get("delete_actual_dates") or []) if compact_text(v)]
        removed_target_dates = [compact_text(v) for v in (data.get("removed_target_dates") or []) if compact_text(v)]
        daily_actuals = data.get("daily_actuals") or []
        rows_before = [
            dict(row)
            for row in rows(
                con.execute(
                    """
                    SELECT actual_id, segment_id, block_id, report_date,
                           output_qty, reject_qty, target_qty_at_report,
                           remarks, status, reported_at
                    FROM production_actual
                    WHERE block_id = ?
                    ORDER BY report_date, actual_id
                    """,
                    (int(block_id),),
                )
            )
        ]
        debug_actual_save = {
            "incoming_daily_actuals": daily_actuals,
            "incoming_delete_dates": delete_dates,
            "incoming_removed_target_dates": removed_target_dates,
            "rows_before": rows_before,
            "rows_after": [],
            "inserted_actual_ids": [],
            "voided_actual_ids": [],
            "skipped_rows": [],
        }
        if not delete_dates and not removed_target_dates and not daily_actuals:
            return jsonify({"error": "No actual rows submitted."}), 400
        saved_count = 0
        deleted_count = 0
        removed_target_count = 0
        removed_target_qty = 0.0
        skipped_count = 0
        inserted_actual_ids = []
        voided_actual_ids = []
        skipped_rows = []
        post_save_errors = []
        removed_target_date_set = set(removed_target_dates)

        for report_date in delete_dates:
            existing_rows = rows(
                con.execute(
                    """
                    SELECT *
                    FROM production_actual
                    WHERE block_id = ?
                      AND report_date = ?
                      AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
                    ORDER BY actual_id DESC
                    """,
                    (int(block_id), report_date),
                )
            )
            for row in existing_rows:
                voided_actual_ids.append(int(row["actual_id"]))
                _void_actual(con, int(row["actual_id"]))
                deleted_count += 1

        for report_date in removed_target_dates:
            existing_rows = rows(
                con.execute(
                    """
                    SELECT *
                    FROM production_actual
                    WHERE block_id = ?
                      AND report_date = ?
                      AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
                    ORDER BY actual_id DESC
                    """,
                    (int(block_id), report_date),
                )
            )
            for row in existing_rows:
                voided_actual_ids.append(int(row["actual_id"]))
                _void_actual(con, int(row["actual_id"]))
                deleted_count += 1
            existing_removed = one(
                con.execute(
                    """
                    SELECT *
                    FROM block_removed_actual_date
                    WHERE block_id = ?
                      AND report_date = ?
                      AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
                    ORDER BY removed_date_id DESC
                    LIMIT 1
                    """,
                    (int(block_id), report_date),
                )
            )
            if existing_removed:
                continue

            target_qty = _planned_target_qty_for_block_date(con, block_id, report_date)
            if target_qty <= 0:
                continue

            con.execute(
                """
                INSERT INTO block_removed_actual_date (
                  block_id, report_date, target_qty_removed, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'ACTIVE', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(block_id, report_date) DO UPDATE SET
                  target_qty_removed = excluded.target_qty_removed,
                  status = 'ACTIVE',
                  updated_at = CURRENT_TIMESTAMP
                """,
                (int(block_id), report_date, float(target_qty)),
            )
            removed_target_count += 1
            removed_target_qty += float(target_qty)

        for row in daily_actuals:
            report_date = compact_text(row.get("report_date"))
            if not report_date:
                skipped_count += 1
                skipped_rows.append({"report_date": "", "reason": "missing report_date"})
                continue

            raw_output = row.get("output_qty") if "output_qty" in row else row.get("actual_good_qty")
            raw_reject = row.get("reject_qty") if "reject_qty" in row else row.get("actual_reject_qty")
            raw_remarks = compact_text(row.get("remarks"))
            raw_target = row.get("target_qty")

            output_text = "" if raw_output is None else str(raw_output).strip()
            reject_text = "" if raw_reject is None else str(raw_reject).strip()
            output_provided = "output_qty" in row and output_text != ""
            reject_provided = "reject_qty" in row and reject_text != ""
            remarks_provided = raw_remarks != ""
            target_provided = "target_qty" in row and compact_text(raw_target) != ""
            if not output_provided and not reject_provided and not remarks_provided:
                skipped_count += 1
                skipped_rows.append({"report_date": report_date, "reason": "blank row"})
                continue

            output_value = parse_nullable_number(raw_output) if output_provided else None
            reject_value = parse_nullable_number(raw_reject) if reject_provided else None
            if output_provided and output_value is None:
                skipped_count += 1
                skipped_rows.append({"report_date": report_date, "reason": "invalid output_qty"})
                continue
            if reject_provided and reject_value is None:
                skipped_count += 1
                skipped_rows.append({"report_date": report_date, "reason": "invalid reject_qty"})
                continue

            remarks_value = raw_remarks
            existing = _active_actual_for_block_date(con, block_id, report_date)
            if target_provided:
                target_qty = parse_nullable_number(raw_target)
            else:
                existing_target = parse_nullable_number(existing["target_qty_at_report"]) if existing and existing.get("target_qty_at_report") is not None else None
                target_qty = existing_target if existing_target is not None else _planned_target_qty_for_block_date(con, block_id, report_date)
            if target_qty is None:
                target_qty = 0.0

            if existing:
                voided_actual_ids.append(int(existing["actual_id"]))
                _void_actual(con, existing["actual_id"])

            inserted_actual_id = _insert_actual(
                con,
                segment_id=None,
                block_id=int(block_id),
                report_date=report_date,
                output_qty=output_value,
                reject_qty=reject_value,
                remarks=remarks_value,
                target_qty=float(target_qty),
                machine_id=int(block["machine_id"]),
                entry_type="CORRECTION" if existing else "REPORT",
                correction_of_actual_id=int(existing["actual_id"]) if existing else None,
                created_by=compact_text(row.get("created_by")),
            )
            saved_count += 1
            inserted_actual_ids.append(int(inserted_actual_id))
            inserted_confirmed = one(
                con.execute(
                    """
                    SELECT actual_id, status
                    FROM production_actual
                    WHERE actual_id = ?
                    """,
                    (int(inserted_actual_id),),
                )
            )
            if not inserted_confirmed or compact_text(inserted_confirmed["status"] or "").upper() != "ACTIVE":
                return jsonify({
                    "ok": False,
                    "error": "Inserted actual row was not persisted as ACTIVE.",
                    "debug_actual_save": debug_actual_save,
                }), 500

        reconciliation = reconcile_block_schedule_after_actuals(con, block_id)

        try:
            refresh_block_actual_status(con, block_id)
            refresh_block_schedule_bounds(con, block_id)
            recalculate_planning_all_baseline(con, reason="ACTUAL_DAILY_SAVE")
            refresh_planner_alerts(con)
        except Exception as exc:
            post_save_errors.append(str(exc))

        updated_block = trial_block_row(con, block_id)
        block_payload = trial_block_payload(updated_block, con)
        rows_after = [
            dict(row)
            for row in rows(
                con.execute(
                    """
                    SELECT actual_id, segment_id, block_id, report_date,
                           output_qty, reject_qty, target_qty_at_report,
                           remarks, status, reported_at
                    FROM production_actual
                    WHERE block_id = ?
                    ORDER BY report_date, actual_id
                    """,
                    (int(block_id),),
                )
            )
        ]
        actuals_active = [
            dict(row)
            for row in rows(
                con.execute(
                    """
                    SELECT actual_id, segment_id, block_id, report_date,
                           output_qty, reject_qty, target_qty_at_report,
                           remarks, status, reported_at
                    FROM production_actual
                    WHERE block_id = ?
                      AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
                    ORDER BY report_date, actual_id
                    """,
                    (int(block_id),),
                )
            )
        ]
        debug_actual_save["rows_after"] = rows_after
        debug_actual_save["inserted_actual_ids"] = inserted_actual_ids
        debug_actual_save["voided_actual_ids"] = voided_actual_ids
        debug_actual_save["skipped_rows"] = skipped_rows
        debug_actual_save["errors"] = post_save_errors
        missing_inserted = [
            actual_id
            for actual_id in inserted_actual_ids
            if not any(int(row["actual_id"]) == int(actual_id) and compact_text(row["status"] or "").upper() == "ACTIVE" for row in rows_after)
        ]
        if missing_inserted:
            return jsonify({
                "ok": False,
                "error": "Inserted actual row disappeared before response.",
                "missing_inserted_actual_ids": missing_inserted,
                "debug_actual_save": debug_actual_save,
            }), 500

        actual_daily_rows = actual_daily_rows_for_block_row(con, updated_block)
        return jsonify({
            "ok": True,
            "saved_count": saved_count,
            "deleted_count": deleted_count,
            "removed_target_count": removed_target_count,
            "removed_target_qty": removed_target_qty,
            "skipped_count": skipped_count,
            "changed_count": saved_count + deleted_count + removed_target_count,
            "block": block_payload,
            "reconciliation": reconciliation,
            "actual_daily_rows": actual_daily_rows,
            "actuals_active": actuals_active,
            "actuals_all": rows_after,
            "actuals": actuals_active,
            "removed_actual_dates": removed_actual_dates_for_block_row(con, updated_block),
            "debug_actual_save": debug_actual_save,
        })

@trial_bp.post("/api/trial/recalc")
def api_trial_recalc():
    with db() as con:
        recalculate_all(con)
        return jsonify({"ok": True})


def _planning_run_payload(row):
    if not row:
        return None
    return {
        "planning_run_id": int(row["planning_run_id"]),
        "reason": compact_text(row["reason"] or ""),
        "status": compact_text(row["status"] or ""),
        "planning_efficiency": float(row["planning_efficiency"] or 0),
        "calendar_policy": compact_text(row["calendar_policy"] or ""),
        "generated_at": compact_text(row["generated_at"] or ""),
        "created_at": compact_text(row["created_at"] or ""),
        "updated_at": compact_text(row["updated_at"] or ""),
        "notes": compact_text(row["notes"] or ""),
    }


def _planning_block_payload(row):
    if not row:
        return None
    planned_start_at = compact_text(row.get("planned_start_at") or "")
    planned_end_at = compact_text(row.get("planned_end_at") or "")
    expected_start_at = compact_text(row.get("expected_start_at") or "")
    expected_end_at = compact_text(row.get("expected_end_at") or "")
    calculated_start_datetime = compact_text(row.get("calculated_start_datetime") or "")
    calculated_end_datetime = compact_text(row.get("calculated_end_datetime") or "")
    return {
        "block_id": int(row["block_id"]),
        "operation_id": int(row["operation_id"]),
        "machine_id": int(row["machine_id"]),
        "queue_position": float(row["queue_position"] or 0),
        "scheduled_qty": float(row["scheduled_qty"] or 0),
        "include_setup": int(row["include_setup"] or 0),
        "planned_start_at": planned_start_at,
        "planned_end_at": planned_end_at,
        "anchor_datetime": compact_text(row["anchor_datetime"] or ""),
        "expected_start_at": expected_start_at,
        "expected_end_at": expected_end_at,
        "forecast_start_at": compact_text(expected_start_at or calculated_start_datetime or ""),
        "forecast_end_at": compact_text(expected_end_at or calculated_end_datetime or ""),
        "calculated_start_datetime": calculated_start_datetime,
        "calculated_end_datetime": calculated_end_datetime,
        "planned_qty": float(row.get("planned_qty") or row.get("planned_qty_state") or 0),
        "planned_minutes": float(row.get("planned_minutes") or row.get("planned_minutes_state") or 0),
        "job_no": compact_text(row["job_no"] or ""),
        "operation_name": compact_text(row["operation_name"] or ""),
        "source_ps_id": compact_text(row["source_ps_id"] or ""),
        "pp_partial_no": compact_text(row.get("pp_partial_no") or ""),
        "partial_no": compact_text(row.get("pp_partial_no") or ""),
        "source_op_no": compact_text(row["source_op_no"] or ""),
        "machine_code": compact_text(row["machine_code"] or ""),
        "machine_category": compact_text(row["machine_category"] or ""),
        "actual_start_at": compact_text(row.get("actual_start_at") or ""),
        "actual_end_at": compact_text(row.get("actual_end_at") or ""),
        "actual_good_qty": float(row.get("actual_good_qty") or 0),
        "actual_row_count": int(row.get("actual_row_count") or 0),
    }


def _planning_process_sheet_payload(row):
    if not row:
        return None
    op_cards = list(row.get("op_cards") or [])
    completed_opn_count = int(row.get("completed_opn_count") or sum(1 for card in op_cards if bool(card.get("is_completed"))) or 0)
    opn_count = int(row.get("opn_count") or len(op_cards) or 0)
    active_opn_count = int(row.get("active_opn_count") or max(0, opn_count - completed_opn_count))
    bom_options = list(row.get("bom_options") or [])
    selected_bom_id = int(row.get("selected_bom_id") or 0)
    return {
        "ps_id": compact_text(row["ps_id"] or ""),
        "source_ps_id": compact_text(row.get("source_ps_id") or ""),
        "partial_no": compact_text(row.get("pp_partial_no") or row.get("partial_no") or ""),
        "pp_partial_no": compact_text(row.get("pp_partial_no") or row.get("partial_no") or ""),
        "part_no": compact_text(row.get("part_no") or ""),
        "part_desc": compact_text(row.get("part_desc") or ""),
        "due_date": compact_text(row.get("due_date") or ""),
        "total_qty": float(row.get("total_qty") or 0),
        "partial_qty": float(row.get("partial_qty") or row.get("planned_qty") or 0),
        "opn_count": opn_count,
        "active_opn_count": active_opn_count,
        "completed_opn_count": completed_opn_count,
        "is_completed": bool(row.get("is_completed") or False),
        "completed_at": compact_text(row.get("completed_at") or ""),
        "completed_by": compact_text(row.get("completed_by") or ""),
        "execution_label": compact_text(row.get("execution_label") or row.get("planner_status") or row.get("status") or ""),
        "selected_bom_id": selected_bom_id,
        "selected_bom_code": compact_text(row.get("selected_bom_code") or ""),
        "default_bom_id": int(row.get("default_bom_id") or 0),
        "bom_options": bom_options,
        "op_cards": op_cards,
        "planning_run_id": int(row["planning_run_id"]) if row["planning_run_id"] is not None else None,
        "expected_start_at": compact_text(row["expected_start_at"] or ""),
        "expected_end_at": compact_text(row["expected_end_at"] or ""),
        "actual_start_at": compact_text(row.get("actual_start_at") or ""),
        "actual_end_at": compact_text(row.get("actual_end_at") or ""),
        "actual_good_qty": float(row.get("actual_good_qty") or 0),
        "actual_block_count": int(row.get("actual_block_count") or 0),
        "planned_qty": float(row["planned_qty"] or 0),
        "planned_minutes": float(row["planned_minutes"] or 0),
    }


def _source_ps_id_text(item):
    if not item:
        return ""
    return compact_text(item.get("source_ps_id") or "")


def _planner_identity_key(source_ps_id, pp_partial_no):
    source_ps_id = compact_text(source_ps_id)
    pp_partial_no = compact_text(pp_partial_no)
    if not source_ps_id:
        return ""
    return f"{source_ps_id}||{pp_partial_no}"


def _partial_no_text(source_ps_id):
    text = compact_text(source_ps_id)
    if "::" not in text:
        return ""
    return compact_text(text.split("::", 1)[1])


def _planner_format_opn(value):
    raw = compact_text(value)
    if not raw:
        return "OPN"
    cleaned = raw.replace("OPN", "", 1).replace("OP", "", 1).strip()
    return f"OPN {cleaned}".strip() if cleaned else "OPN"


def _execution_label_text(item):
    if not item:
        return ""
    for key in ("execution_label", "planner_status", "status", "planning_status"):
        value = compact_text(item.get(key) or "")
        if value:
            return value
    return ""


def _normalize_planner_sidebar_item(item):
    item = dict(item or {})
    source_ps_id = _source_ps_id_text(item)
    partial_no = compact_text(item.get("pp_partial_no") or item.get("partial_no") or _partial_no_text(source_ps_id))
    op_cards = list(item.get("op_cards") or [])
    planned_qty = float(item.get("planned_qty") or 0)
    total_qty = float(item.get("total_qty") or 0)
    ps_id = compact_text(item.get("ps_id") or source_ps_id)
    return {
        **item,
        "ps_id": ps_id,
        "source_ps_id": source_ps_id,
        "partial_no": partial_no,
        "pp_partial_no": partial_no,
        "part_no": compact_text(item.get("part_no") or ""),
        "part_desc": compact_text(item.get("part_desc") or ""),
        "total_qty": total_qty,
        "partial_qty": float(item.get("partial_qty") or planned_qty or total_qty or 0),
        "opn_count": int(item.get("opn_count") or len(op_cards)),
        "execution_label": _execution_label_text(item),
        "op_cards": op_cards,
    }


def _planner_bom_options(item):
    options = []
    seen = set()
    for flow in list(item.get("flow_options") or item.get("bom_options") or []):
        bom_id = int(flow.get("bom_id") or 0)
        if not bom_id or bom_id in seen:
            continue
        seen.add(bom_id)
        options.append(
            {
                "bom_id": bom_id,
                "bom_code": compact_text(flow.get("bom_code") or ""),
                "bom_name": compact_text(flow.get("bom_name") or flow.get("bom_desc") or ""),
                "is_default": bool(int(flow.get("is_default") or 0)),
            }
        )
    options.sort(key=lambda opt: (0 if opt["is_default"] else 1, opt["bom_id"]))
    return options


def _planner_default_bom_id(bom_options):
    if not bom_options:
        return 0
    default = next((int(opt["bom_id"]) for opt in bom_options if opt.get("is_default")), 0)
    return default or int(bom_options[0]["bom_id"])


def _planner_selected_bom_id(item, bom_options):
    selected = int(item.get("selected_bom_id") or 0)
    if selected:
        return selected
    return _planner_default_bom_id(bom_options)


def _planner_bom_code_for_id(bom_options, bom_id):
    bom_id = int(bom_id or 0)
    if not bom_id:
        return ""
    match = next((opt for opt in bom_options if int(opt.get("bom_id") or 0) == bom_id), None)
    return compact_text((match or {}).get("bom_code") or "")


def _planner_bom_options_for_part(con, part_id):
    part_id = int(part_id or 0)
    if not part_id:
        return []
    return [
        {
            "bom_id": int(row["bom_id"] or 0),
            "bom_code": compact_text(row["bom_code"] or ""),
            "bom_name": compact_text(row["bom_desc"] or ""),
            "is_default": bool(int(row["is_default"] or 0)),
        }
        for row in rows(
            con.execute(
                """
                SELECT bom_id, bom_code, bom_desc, is_default
                FROM bom_variation
                WHERE part_id = ?
                ORDER BY is_default DESC, bom_id
                """,
                (part_id,),
            )
        )
        if int(row["bom_id"] or 0) > 0
    ]


def _planner_process_sheet_row(con, source_ps_id, pp_partial_no=""):
    source_ps_id = compact_text(source_ps_id)
    pp_partial_no = compact_text(pp_partial_no)
    if not source_ps_id:
        return None
    return one(
        con.execute(
            """
            SELECT ps.*,
                   p.part_no AS part_name
            FROM process_sheet ps
            LEFT JOIN parts p ON p.part_id = ps.part_id
            WHERE COALESCE(ps.source_ps_id, '') = ?
              AND COALESCE(ps.pp_partial_no, '') = ?
            ORDER BY ps.ps_id
            LIMIT 1
            """,
            (source_ps_id, pp_partial_no),
        )
    )


def _planner_bom_steps_for_selected(con, source_ps_id, pp_partial_no, bom_id):
    source_ps_id = compact_text(source_ps_id)
    pp_partial_no = compact_text(pp_partial_no)
    bom_id = int(bom_id or 0)
    if not source_ps_id or not bom_id:
        return []
    return [
        dict(row)
        for row in rows(
            con.execute(
                """
                SELECT s.op_seq_id, s.seq_no, s.op_no, s.op_type, s.machine_category, s.preferred_machine,
                       s.cycle_time, s.setup_time, s.is_last_op,
                       o.operation_id, COALESCE(o.status, 'ACTIVE') AS operation_status,
                       o.opn_completed, o.opn_completed_at, o.opn_completed_by
                FROM operation_seq s
                LEFT JOIN operation o
                  ON o.source_ps_id = ?
                 AND COALESCE(o.pp_partial_no, '') = ?
                 AND COALESCE(o.selected_bom_id, 0) = ?
                 AND COALESCE(o.source_op_seq_id, 0) = s.op_seq_id
                 AND COALESCE(o.source_op_no, '') = s.op_no
                WHERE s.bom_id = ?
                ORDER BY s.seq_no, s.op_seq_id
                """,
                (source_ps_id, pp_partial_no, bom_id, bom_id),
            )
        )
    ]


def _planner_machine_options(con):
    machine_rows = [dict(row) for row in fetch_machines(con)]
    machine_groups = []
    seen = set()
    for row in machine_rows:
        category = compact_text(row.get("machine_category") or "")
        if not category or category in seen:
            continue
        seen.add(category)
        machine_groups.append(category)
    return machine_rows, machine_groups


def _planner_machine_row_by_code(machine_rows, machine_code):
    machine_code = compact_text(machine_code)
    if not machine_code:
        return None
    return next((row for row in machine_rows if compact_text(row.get("machine_code") or "") == machine_code), None)


def _planner_resolve_machine_context(machine_rows, machine_ids=None, preferred_machine="", machine_category=""):
    machine_ids = [int(value or 0) for value in (machine_ids or []) if int(value or 0) > 0]
    preferred_machine = compact_text(preferred_machine or "")
    machine_category = compact_text(machine_category or "")

    machine_row = None
    if preferred_machine:
        machine_row = _planner_machine_row_by_code(machine_rows, preferred_machine)
        if not machine_row:
            return None, None, None, "Selected preferred machine does not exist."
        resolved_category = compact_text(machine_row.get("machine_category") or "")
        if not resolved_category:
            return None, None, None, "Selected preferred machine has no machine group."
        resolved_machine_id = int(machine_row.get("machine_id") or 0)
        return machine_row, resolved_machine_id, resolved_category, None

    if machine_ids:
        machine_id = int(machine_ids[0] or 0)
        if machine_id > 0:
            machine_row = next((row for row in machine_rows if int(row.get("machine_id") or 0) == machine_id), None)
            if not machine_row:
                return None, None, None, "Invalid machine_id"
            resolved_category = compact_text(machine_row.get("machine_category") or "")
            if not resolved_category:
                return None, None, None, "Selected preferred machine has no machine group."
            resolved_machine_id = int(machine_row.get("machine_id") or 0)
            return machine_row, resolved_machine_id, resolved_category, None

    if not machine_category:
        return None, None, None, "machine_category is required"
    return None, None, machine_category, None


def _planner_operation_history_flags(con, operation_id):
    operation_id = int(operation_id or 0)
    if not operation_id:
        return {"has_planned_blocks": False, "has_actual_output": False, "planned_block_count": 0, "actual_row_count": 0}
    planned_row = one(
        con.execute(
            """
            SELECT COUNT(*) AS planned_block_count
            FROM run_block
            WHERE operation_id = ?
              AND COALESCE(active, 1) = 1
            """,
            (operation_id,),
        )
    ) or {}
    actual_row = one(
        con.execute(
            """
            SELECT COUNT(*) AS actual_row_count
            FROM production_actual a
            JOIN run_block b ON b.block_id = a.block_id
            WHERE b.operation_id = ?
              AND COALESCE(a.status, 'ACTIVE') = 'ACTIVE'
            """,
            (operation_id,),
        )
    ) or {}
    planned_block_count = int(planned_row.get("planned_block_count") or 0)
    actual_row_count = int(actual_row.get("actual_row_count") or 0)
    return {
        "has_planned_blocks": planned_block_count > 0,
        "has_actual_output": actual_row_count > 0,
        "planned_block_count": planned_block_count,
        "actual_row_count": actual_row_count,
    }


def _planner_operation_editor_rows(con, source_ps_id, pp_partial_no, bom_id):
    source_ps_id = compact_text(source_ps_id)
    pp_partial_no = compact_text(pp_partial_no)
    bom_id = int(bom_id or 0)
    if not source_ps_id or not bom_id:
        return []
    machine_rows, machine_groups = _planner_machine_options(con)
    machine_ids_by_group = {}
    for machine in machine_rows:
        group = compact_text(machine.get("machine_category") or "")
        if not group:
            continue
        machine_ids_by_group.setdefault(group, []).append(int(machine.get("machine_id") or 0))
    rows_out = []
    for row in rows(
        con.execute(
            """
            SELECT s.op_seq_id, s.seq_no, s.op_no, s.op_type, s.machine_category, s.preferred_machine,
                   s.cycle_time, s.setup_time, s.is_last_op,
                   o.operation_id, COALESCE(o.status, 'ACTIVE') AS operation_status,
                   o.job_no, o.operation_name, o.total_qty, o.setup_minutes, o.cycle_minutes_per_qty,
                   o.compatible_machine_group, o.remarks
            FROM operation_seq s
            LEFT JOIN operation o
              ON COALESCE(o.source_ps_id, '') = ?
             AND COALESCE(o.pp_partial_no, '') = ?
             AND COALESCE(o.selected_bom_id, 0) = ?
             AND COALESCE(o.source_op_seq_id, 0) = s.op_seq_id
             AND COALESCE(o.source_op_no, '') = s.op_no
            WHERE s.bom_id = ?
            ORDER BY s.seq_no, s.op_seq_id
            """,
            (source_ps_id, pp_partial_no, bom_id, bom_id),
        )
    ):
        step = dict(row)
        operation_id = int(step.get("operation_id") or 0)
        history = _planner_operation_history_flags(con, operation_id)
        machine_category = compact_text(step.get("machine_category") or "")
        preferred_machine = compact_text(step.get("preferred_machine") or "")
        rows_out.append(
            {
                "source_ps_id": source_ps_id,
                "pp_partial_no": pp_partial_no,
                "selected_bom_id": bom_id,
                "op_seq_id": int(step.get("op_seq_id") or 0),
                "source_op_seq_id": int(step.get("op_seq_id") or 0),
                "seq_no": int(step.get("seq_no") or 0),
                "op_no": compact_text(step.get("op_no") or ""),
                "source_op_no": compact_text(step.get("op_no") or ""),
                "op_type": compact_text(step.get("op_type") or ""),
                "operation_name": compact_text(step.get("operation_name") or step.get("op_type") or ""),
                "setup_minutes": float(step.get("setup_minutes") if step.get("setup_minutes") is not None else step.get("setup_time") or 0),
                "cycle_minutes_per_qty": float(step.get("cycle_minutes_per_qty") if step.get("cycle_minutes_per_qty") is not None else step.get("cycle_time") or 0),
                "machine_category": machine_category,
                "compatible_machine_group": compact_text(step.get("compatible_machine_group") or machine_category),
                "preferred_machine": preferred_machine,
                "operation_id": operation_id,
                "operation_status": compact_text(step.get("operation_status") or "ACTIVE") or "ACTIVE",
                "is_active": compact_text(step.get("operation_status") or "ACTIVE").upper() == "ACTIVE",
                "is_last_op": int(step.get("is_last_op") or 0),
                "machine_ids": list(machine_ids_by_group.get(machine_category, [])),
                "machine_groups": machine_groups,
                **history,
            }
        )
    return rows_out


def _planner_strip_op_prefix(op_no, value):
    prefix = compact_text(op_no or "")
    text = compact_text(value or "")
    if not prefix or not text:
        return text
    token = f"{prefix} "
    guard = 0
    while text.startswith(token) and guard < 10:
        text = text[len(token):].strip()
        guard += 1
    return text


def _planner_planned_qty_for_op(con, source_ps_id, pp_partial_no, selected_bom_id, source_op_seq_id, source_op_no):
    source_ps_id = compact_text(source_ps_id)
    pp_partial_no = compact_text(pp_partial_no)
    selected_bom_id = int(selected_bom_id or 0)
    source_op_seq_id = int(source_op_seq_id or 0)
    source_op_no = compact_text(source_op_no or "")
    if not source_ps_id or not source_op_seq_id or not source_op_no:
        return 0.0
    row = one(
        con.execute(
            """
            SELECT COALESCE(SUM(b.scheduled_qty), 0) AS planned_qty
            FROM run_block b
            JOIN operation o ON o.operation_id = b.operation_id
            WHERE COALESCE(b.active, 1) = 1
              AND COALESCE(o.status, 'ACTIVE') = 'ACTIVE'
              AND COALESCE(o.source_ps_id, '') = ?
              AND COALESCE(o.pp_partial_no, '') = ?
              AND COALESCE(o.selected_bom_id, 0) = ?
              AND COALESCE(o.source_op_seq_id, 0) = ?
              AND COALESCE(o.source_op_no, '') = ?
            """,
            (source_ps_id, pp_partial_no, selected_bom_id, source_op_seq_id, source_op_no),
        )
    )
    return float((row or {}).get("planned_qty") or 0)


def _planner_op_cards_for_source_ps(con, source_ps_id, pp_partial_no, selected_bom_id, partial_qty=0):
    source_ps_id = compact_text(source_ps_id)
    pp_partial_no = compact_text(pp_partial_no)
    selected_bom_id = int(selected_bom_id or 0)
    if not source_ps_id or not selected_bom_id:
        return []
    target_qty = max(0.0, parse_number(partial_qty, 0))
    op_cards = []
    for row in rows(
        con.execute(
            """
            SELECT s.op_seq_id, s.seq_no, s.op_no, s.op_type, s.machine_category, s.preferred_machine,
                   s.cycle_time, s.setup_time, s.is_last_op,
                   COALESCE(st.operation_id, o.operation_id, 0) AS operation_id,
                   COALESCE(st.opn_completed, o.opn_completed, 0) AS opn_completed,
                   COALESCE(st.opn_completed_at, o.opn_completed_at, '') AS opn_completed_at,
                   COALESCE(st.opn_completed_by, o.opn_completed_by, '') AS opn_completed_by
            FROM operation_seq s
            LEFT JOIN operation o
              ON o.source_ps_id = ?
             AND COALESCE(o.pp_partial_no, '') = ?
             AND COALESCE(o.selected_bom_id, 0) = ?
             AND COALESCE(o.source_op_seq_id, 0) = s.op_seq_id
             AND COALESCE(o.source_op_no, '') = s.op_no
            LEFT JOIN planner_opn_state st
              ON st.source_ps_id = ?
             AND COALESCE(st.pp_partial_no, '') = ?
             AND st.selected_bom_id = ?
             AND st.source_op_seq_id = s.op_seq_id
            WHERE s.bom_id = ?
            ORDER BY s.seq_no, s.op_seq_id
            """,
            (source_ps_id, pp_partial_no, selected_bom_id, source_ps_id, pp_partial_no, selected_bom_id, selected_bom_id),
        )
    ):
        step = dict(row)
        if compact_text(step.get("operation_status") or "ACTIVE").upper() != "ACTIVE":
            continue
        source_op_no = compact_text(step.get("op_no") or "")
        op_type = compact_text(step.get("op_type") or "")
        machine_category = compact_text(step.get("machine_category") or "")
        preferred_machine = compact_text(step.get("preferred_machine") or "")
        setup_minutes = float(step.get("setup_time") or 0)
        cycle_minutes_per_qty = float(step.get("cycle_time") or 0)
        source_op_seq_id = int(step.get("op_seq_id") or 0)
        op_card = {
            "card_kind": "single",
            "card_id": None,
            "ps_id": source_ps_id,
            "source_ps_id": source_ps_id,
            "pp_partial_no": pp_partial_no,
            "selected_bom_id": selected_bom_id,
            "display_target_qty": target_qty,
            "operation_label": source_op_no or op_type or "",
            "operation_name": op_type,
            "target_qty": target_qty,
            "remaining_qty": target_qty,
            "source_op_seq_id": source_op_seq_id,
            "source_op_no": source_op_no,
            "job_no": source_ps_id,
            "planning_status": "UNSCHEDULED",
            "execution_label": "Active",
            "card_type": "SINGLE",
            "is_scheduled": False,
            "setup_minutes": setup_minutes,
            "cycle_minutes_per_qty": cycle_minutes_per_qty,
            "compatible_machine_group": machine_category,
            "opn_completed": int(step.get("opn_completed") or 0),
            "opn_completed_at": compact_text(step.get("opn_completed_at") or ""),
            "opn_completed_by": compact_text(step.get("opn_completed_by") or ""),
            "can_drag": True,
            "can_toggle_completion": True,
            "op": {
                "source_ps_id": source_ps_id,
                "pp_partial_no": pp_partial_no,
                "selected_bom_id": selected_bom_id,
                "source_op_seq_id": source_op_seq_id,
                "source_op_no": source_op_no,
                "op_no": source_op_no,
                "op_type": op_type,
                "operation_name": op_type,
                "compatible_machine_group": machine_category,
                "machine_category": machine_category,
                "preferred_machine": preferred_machine,
                "setup_time": setup_minutes,
                "setup_minutes": setup_minutes,
                "cycle_time": cycle_minutes_per_qty,
                "cycle_minutes_per_qty": cycle_minutes_per_qty,
                "remaining_qty": target_qty,
                "total_qty": target_qty,
                "planned_qty": 0,
                "job_no": source_ps_id,
            },
            "debug_identity": {
                "source_ps_id": source_ps_id,
                "pp_partial_no": pp_partial_no,
                "selected_bom_id": selected_bom_id,
                "source_op_seq_id": source_op_seq_id,
                "source_op_no": source_op_no,
                "planned_qty": 0,
                "target_qty": target_qty,
                "remaining_qty": target_qty,
            },
        }
        op_cards.append(_planner_enrich_op_card(op_card, source_ps_id, pp_partial_no, selected_bom_id, {}, {}, con=con))
    return op_cards


def _planner_upsert_op_state(con, source_ps_id, pp_partial_no, bom_id, op_row, completed, completed_by=""):
    source_ps_id = compact_text(source_ps_id)
    pp_partial_no = compact_text(pp_partial_no)
    if not source_ps_id:
        return None
    source_op_seq_id = int((op_row or {}).get("source_op_seq_id") or 0)
    source_op_no = compact_text((op_row or {}).get("source_op_no") or "")
    operation_id = int((op_row or {}).get("operation_id") or 0)
    completed = bool(completed)
    completed_at = compact_text((op_row or {}).get("opn_completed_at") or "")
    if completed and not completed_at:
        completed_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    if not completed:
        completed_at = ""
        completed_by = ""
    completed_by = compact_text(completed_by or "")
    con.execute(
        """
        INSERT INTO planner_opn_state (
          source_ps_id, pp_partial_no, selected_bom_id, source_op_seq_id, source_op_no,
          operation_id, opn_completed, opn_completed_at, opn_completed_by, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(source_ps_id, pp_partial_no, selected_bom_id, source_op_seq_id, source_op_no)
        DO UPDATE SET
          operation_id = excluded.operation_id,
          opn_completed = excluded.opn_completed,
          opn_completed_at = excluded.opn_completed_at,
          opn_completed_by = excluded.opn_completed_by,
          updated_at = CURRENT_TIMESTAMP
        """,
        (
            source_ps_id,
            pp_partial_no,
            int(bom_id or 0),
            source_op_seq_id,
            source_op_no,
            operation_id,
            1 if completed else 0,
            completed_at,
            completed_by,
        ),
    )
    if operation_id:
        con.execute(
            """
            UPDATE operation
            SET opn_completed = ?,
                opn_completed_at = ?,
                opn_completed_by = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE operation_id = ?
            """,
            (1 if completed else 0, completed_at, completed_by, operation_id),
        )
    return {
        "source_ps_id": source_ps_id,
        "pp_partial_no": pp_partial_no,
        "selected_bom_id": int(bom_id or 0),
        "source_op_seq_id": source_op_seq_id,
        "source_op_no": source_op_no,
        "operation_id": operation_id,
        "opn_completed": 1 if completed else 0,
        "opn_completed_at": completed_at,
        "opn_completed_by": completed_by,
    }


def refresh_process_sheet_completion(con, source_ps_id, pp_partial_no="", bom_id=None, set_completed=None):
    source_ps_id = compact_text(source_ps_id)
    pp_partial_no = compact_text(pp_partial_no)
    if not source_ps_id:
        return None
    ps_row = _planner_process_sheet_row(con, source_ps_id, pp_partial_no)
    if not ps_row:
        return None
    part_id = int(ps_row.get("part_id") or 0)
    bom_options = _planner_bom_options_for_part(con, part_id)
    selected_bom_id = int(bom_id or ps_row.get("selected_bom_id") or 0)
    if not selected_bom_id and bom_options:
        default_option = next((opt for opt in bom_options if opt.get("is_default")), bom_options[0])
        selected_bom_id = int(default_option.get("bom_id") or 0)
    step_rows = _planner_bom_steps_for_selected(con, source_ps_id, pp_partial_no, selected_bom_id) if selected_bom_id else []
    step_states = []
    for row in step_rows:
        source_op_no = compact_text(row.get("op_no") or "")
        state_row = one(
            con.execute(
                """
                SELECT *
                FROM planner_opn_state
                WHERE source_ps_id = ?
                  AND COALESCE(pp_partial_no, '') = ?
                  AND selected_bom_id = ?
                  AND source_op_seq_id = ?
                LIMIT 1
                """,
                (source_ps_id, pp_partial_no, selected_bom_id, int(row.get("op_seq_id") or 0)),
            )
        )
        is_completed = bool(int((state_row or {}).get("opn_completed") or row.get("opn_completed") or 0))
        completed_at = compact_text((state_row or {}).get("opn_completed_at") or row.get("opn_completed_at") or "")
        completed_by = compact_text((state_row or {}).get("opn_completed_by") or row.get("opn_completed_by") or "")
        step_states.append(
            {
                "op_seq_id": int(row.get("op_seq_id") or 0),
                "source_op_no": source_op_no,
                "operation_id": int(row.get("operation_id") or 0),
                "is_completed": is_completed,
                "completed_at": completed_at,
                "completed_by": completed_by,
            }
        )

    opn_count = len(step_states)
    completed_opn_count = sum(1 for step in step_states if step["is_completed"])
    active_opn_count = max(0, opn_count - completed_opn_count)
    all_completed = bool(opn_count) and active_opn_count == 0
    any_completed = completed_opn_count > 0
    row_completed = bool(int(ps_row.get("completed") or 0))
    if set_completed is True:
        row_completed = True
    elif set_completed is False:
        row_completed = False
    completed_at = compact_text(ps_row.get("completed_at") or "")
    completed_by = compact_text(ps_row.get("completed_by") or "")
    if row_completed:
        completed_at = completed_at or max((step["completed_at"] for step in step_states if step["completed_at"]), default="")
        completed_by = completed_by or next((step["completed_by"] for step in step_states if step["completed_by"]), "")
    execution_label = "Completed" if row_completed else ("Ready to complete" if all_completed else ("In Progress" if any_completed else "Active"))
    planner_status = "COMPLETED" if row_completed else ("READY_TO_COMPLETE" if all_completed else ("IN_PROGRESS" if any_completed else "ACTIVE"))
    with con:
        if set_completed is True and step_rows:
            for row in step_rows:
                _planner_upsert_op_state(
                    con,
                    source_ps_id,
                    pp_partial_no,
                    selected_bom_id,
                    {
                        "source_op_seq_id": int(row.get("op_seq_id") or 0),
                        "source_op_no": compact_text(row.get("op_no") or ""),
                        "operation_id": int(row.get("operation_id") or 0),
                    },
                    True,
                )
        update_fields = ["selected_bom_id = ?", "planner_status = ?", "updated_at = CURRENT_TIMESTAMP"]
        update_values = [selected_bom_id, planner_status]
        if set_completed is not None:
            update_fields.extend(["completed = ?", "completed_at = ?", "completed_by = ?"])
            if set_completed:
                update_values.extend([1, completed_at or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), completed_by])
            else:
                update_values.extend([0, "", ""])
        con.execute(
            f"""
            UPDATE process_sheet
            SET {", ".join(update_fields)}
            WHERE ps_id = ?
            """,
            (*update_values, compact_text(ps_row.get("ps_id") or "")),
        )
    return {
        "source_ps_id": source_ps_id,
        "pp_partial_no": pp_partial_no,
        "selected_bom_id": selected_bom_id,
        "is_completed": row_completed,
        "completed_at": completed_at if row_completed else "",
        "completed_by": completed_by if row_completed else "",
        "execution_label": execution_label,
        "active_opn_count": active_opn_count,
        "completed_opn_count": completed_opn_count,
        "opn_count": opn_count,
    }


def _planner_completion_maps(con, source_ps_ids):
    ids = [compact_text(value) for value in source_ps_ids if compact_text(value)]
    if not ids:
        return {}, {}
    placeholders = ",".join("?" for _ in ids)
    state_map = {}
    state_rows = rows(
        con.execute(
            f"""
            SELECT source_ps_id, pp_partial_no, selected_bom_id, source_op_seq_id, source_op_no,
                   operation_id, opn_completed, opn_completed_at, opn_completed_by
            FROM planner_opn_state
            WHERE source_ps_id IN ({placeholders})
            """,
            ids,
        )
    )
    for row in state_rows:
        state = dict(row)
        key = (
            compact_text(state.get("source_ps_id") or ""),
            compact_text(state.get("pp_partial_no") or ""),
            int(state.get("selected_bom_id") or 0),
            int(state.get("source_op_seq_id") or 0),
            compact_text(state.get("source_op_no") or ""),
        )
        state_map[key] = state

    op_map = {}
    op_rows = rows(
        con.execute(
            f"""
            SELECT operation_id, source_ps_id, pp_partial_no, selected_bom_id, source_op_seq_id, source_op_no,
                   COALESCE(opn_completed, 0) AS opn_completed,
                   COALESCE(opn_completed_at, '') AS opn_completed_at,
                   COALESCE(opn_completed_by, '') AS opn_completed_by
            FROM operation
            WHERE source_ps_id IN ({placeholders})
            """,
            ids,
        )
    )
    for row in op_rows:
        op = dict(row)
        partial_no = compact_text(op.get("pp_partial_no") or "")
        key = (
            compact_text(op.get("source_ps_id") or ""),
            partial_no,
            int(op.get("selected_bom_id") or 0),
            int(op.get("source_op_seq_id") or 0),
            compact_text(op.get("source_op_no") or ""),
        )
        op_map[key] = op

    return state_map, op_map


def _planner_completion_info(source_ps_id, pp_partial_no, selected_bom_id, op_row, state_map, op_map):
    source_ps_id = compact_text(source_ps_id)
    pp_partial_no = compact_text(pp_partial_no)
    selected_bom_id = int(selected_bom_id or 0)
    source_op_seq_id = int((op_row or {}).get("source_op_seq_id") or (op_row or {}).get("op_seq_id") or 0)
    source_op_no = compact_text((op_row or {}).get("source_op_no") or (op_row or {}).get("op_no") or "")
    state = state_map.get((source_ps_id, pp_partial_no, selected_bom_id, source_op_seq_id, source_op_no))
    if not state:
        state = state_map.get((source_ps_id, pp_partial_no, selected_bom_id, source_op_seq_id, ""))
    if not state:
        state = op_map.get((source_ps_id, pp_partial_no, selected_bom_id, source_op_seq_id, source_op_no))
    if not state:
        state = op_map.get((source_ps_id, pp_partial_no, selected_bom_id, source_op_seq_id, ""))
    operation_id = int((state or {}).get("operation_id") or (op_row or {}).get("operation_id") or source_op_seq_id or 0)
    is_completed = bool(int((state or {}).get("opn_completed") or (op_row or {}).get("opn_completed") or 0))
    completed_at = compact_text((state or {}).get("opn_completed_at") or (op_row or {}).get("opn_completed_at") or "")
    completed_by = compact_text((state or {}).get("opn_completed_by") or (op_row or {}).get("opn_completed_by") or "")
    return {
        "operation_id": operation_id,
        "source_ps_id": source_ps_id,
        "pp_partial_no": pp_partial_no,
        "selected_bom_id": selected_bom_id,
        "source_op_seq_id": source_op_seq_id,
        "source_op_no": source_op_no,
        "opn_label": _planner_format_opn(source_op_no),
        "is_completed": is_completed,
        "completed_at": completed_at,
        "completed_by": completed_by,
        "execution_label": "Completed" if is_completed else "Active",
    }


def _planner_bom_steps_map(con, bom_ids):
    ids = sorted({int(bom_id or 0) for bom_id in bom_ids if int(bom_id or 0) > 0})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    result = {}
    for row in rows(
        con.execute(
            f"""
            SELECT bom_id, op_seq_id, seq_no, op_no, op_type, machine_category, preferred_machine,
                   cycle_time, setup_time, is_last_op
            FROM operation_seq
            WHERE bom_id IN ({placeholders})
            ORDER BY bom_id, seq_no, op_seq_id
            """,
            ids,
        )
    ):
        step = dict(row)
        result.setdefault(int(step["bom_id"] or 0), []).append(step)
    return result


def _planner_enrich_sidebar_item(item, state_map, op_map, bom_steps_map, con=None):
    item = dict(item or {})
    source_ps_id = _source_ps_id_text(item)
    partial_no = compact_text(item.get("pp_partial_no") or item.get("partial_no") or "")
    bom_options = _planner_bom_options(item)
    default_bom_id = _planner_default_bom_id(bom_options)
    selected_bom_id = _planner_selected_bom_id(item, bom_options)
    if selected_bom_id <= 0:
        selected_bom_id = default_bom_id
    selected_bom_code = _planner_bom_code_for_id(bom_options, selected_bom_id)
    steps = list(bom_steps_map.get(selected_bom_id) or [])
    step_infos = [_planner_completion_info(source_ps_id, partial_no, selected_bom_id, step, state_map, op_map) for step in steps]
    completed_opn_count = sum(1 for info in step_infos if info["is_completed"])
    opn_count = len(step_infos)
    active_opn_count = max(0, opn_count - completed_opn_count)
    completed = bool(item.get("is_completed") or item.get("completed") or False)
    completed_at = max((compact_text(info.get("completed_at") or "") for info in step_infos if info["is_completed"]), default="")
    if not completed_at and compact_text(item.get("completed_at") or ""):
        completed_at = compact_text(item.get("completed_at") or "")
    completed_by = compact_text(item.get("completed_by") or "")
    if not completed_by:
        completed_by = next((compact_text(info.get("completed_by") or "") for info in step_infos if info["completed_by"]), "")
    execution_label = "Completed" if completed else ("Ready to complete" if bool(opn_count) and active_opn_count == 0 else ("In Progress" if completed_opn_count > 0 else _execution_label_text(item) or "Active"))
    op_cards = [_planner_enrich_op_card(card, source_ps_id, partial_no, selected_bom_id, state_map, op_map, con=con) for card in list(item.get("op_cards") or [])]
    item.update(
        {
            "source_ps_id": source_ps_id,
            "pp_partial_no": partial_no,
            "bom_options": bom_options,
            "flow_options": bom_options,
            "default_bom_id": default_bom_id,
            "selected_bom_id": selected_bom_id,
            "selected_bom_code": selected_bom_code,
            "opn_count": opn_count or int(item.get("opn_count") or len(op_cards) or 0),
            "completed_opn_count": completed_opn_count,
            "active_opn_count": active_opn_count,
            "is_completed": completed,
            "completed_at": completed_at,
            "completed_by": completed_by,
            "execution_label": execution_label,
            "op_cards": op_cards,
        }
    )
    return item


def _planner_enrich_op_card(card, source_ps_id, pp_partial_no, selected_bom_id, state_map, op_map, con=None):
    card = dict(card or {})
    source_ps_id = compact_text(source_ps_id)
    pp_partial_no = compact_text(pp_partial_no)
    selected_bom_id = int(selected_bom_id or 0)
    card["source_ps_id"] = card.get("source_ps_id") or source_ps_id
    card["pp_partial_no"] = card.get("pp_partial_no") or pp_partial_no
    card["selected_bom_id"] = selected_bom_id
    card["source_op_seq_id"] = int(card.get("source_op_seq_id") or card.get("op_seq_id") or 0)
    card["source_op_no"] = compact_text(card.get("source_op_no") or card.get("op_no") or card.get("operation_label") or "")
    card["opn_label"] = _planner_format_opn(card.get("source_op_no") or card.get("operation_label") or "")

    if compact_text(card.get("card_kind") or "") == "group":
        child_cards = []
        for child in list(card.get("ops") or []):
            child = dict(child or {})
            child_info = _planner_completion_info(source_ps_id, pp_partial_no, selected_bom_id, child, state_map, op_map)
            child.update(child_info)
            child["opn_label"] = _planner_format_opn(child.get("source_op_no") or child.get("opn_label") or "")
            child["execution_label"] = "Completed" if child_info["is_completed"] else "Active"
            child_cards.append(child)
        card["ops"] = child_cards
        completed_children = [child for child in child_cards if child.get("is_completed")]
        card["completed_opn_count"] = len(completed_children)
        card["active_opn_count"] = max(0, len(child_cards) - len(completed_children))
        card["opn_count"] = len(child_cards)
        card["is_completed"] = bool(child_cards) and len(completed_children) == len(child_cards)
        card["completed_at"] = max((compact_text(child.get("completed_at") or "") for child in completed_children), default="")
        card["completed_by"] = compact_text(completed_children[0].get("completed_by") or "") if completed_children else ""
        card["operation_id"] = int((child_cards[0].get("operation_id") or 0) if child_cards else 0)
        card["execution_label"] = "Completed" if card["is_completed"] else ("In Progress" if completed_children else "Active")
        card["can_toggle_completion"] = False
        return card

    info = _planner_completion_info(source_ps_id, pp_partial_no, selected_bom_id, card, state_map, op_map)
    card.update(info)
    target_qty = max(0.0, parse_number(card.get("target_qty") or card.get("remaining_qty") or card.get("total_qty"), 0))
    source_op_seq_id = int(card.get("source_op_seq_id") or 0)
    source_op_no = compact_text(card.get("source_op_no") or "")
    planned_qty = max(0.0, parse_number(card.get("planned_qty"), 0))
    if con is not None:
        planned_qty = _planner_planned_qty_for_op(con, source_ps_id, pp_partial_no, selected_bom_id, source_op_seq_id, source_op_no)
    remaining_qty = max(0.0, target_qty - planned_qty)
    if planned_qty <= 0:
        planning_status = "UNPLANNED"
    elif remaining_qty <= 0:
        planning_status = "FULLY_PLANNED"
    else:
        planning_status = "PARTIALLY_PLANNED"
    card["target_qty"] = target_qty
    card["planned_qty"] = planned_qty
    card["remaining_qty"] = remaining_qty
    card["planning_status"] = planning_status
    if bool(card.get("is_completed")):
        card["execution_label"] = "COMPLETED"
    else:
        card["execution_label"] = planning_status.replace("_", " ")
    card["can_drag"] = bool(card.get("can_drag", True)) and remaining_qty > 0 and not bool(card.get("is_completed"))
    card["completed_opn_count"] = 1 if card["is_completed"] else 0
    card["active_opn_count"] = 0 if card["is_completed"] else 1
    card["opn_count"] = 1
    card["can_toggle_completion"] = True
    card["debug_identity"] = {
        "source_ps_id": source_ps_id,
        "pp_partial_no": pp_partial_no,
        "selected_bom_id": selected_bom_id,
        "source_op_seq_id": source_op_seq_id,
        "source_op_no": source_op_no,
        "planned_qty": planned_qty,
        "target_qty": target_qty,
        "remaining_qty": remaining_qty,
    }
    op_payload = card.get("op")
    if isinstance(op_payload, dict):
        op_payload["source_ps_id"] = source_ps_id
        op_payload["pp_partial_no"] = pp_partial_no
        op_payload["selected_bom_id"] = selected_bom_id
        op_payload["target_qty"] = target_qty
        op_payload["planned_qty"] = planned_qty
        op_payload["remaining_qty"] = remaining_qty
        op_payload["planning_status"] = planning_status
        op_payload["execution_label"] = card["execution_label"]
        op_payload["can_drag"] = card["can_drag"]
    return card


@trial_bp.get("/api/trial/planner/settings")
def api_trial_planner_settings_get():
    with db() as con:
        return jsonify(
            {
                "ok": True,
                "settings": {
                    "planning_efficiency": get_planning_efficiency(con),
                    "planning_calendar_policy": get_planning_calendar_policy(con),
                    "planning_start_time": get_planning_start_time_text(con),
                },
            }
        )


@trial_bp.patch("/api/trial/planner/settings")
def api_trial_planner_settings_patch():
    data = request.get_json(force=True, silent=True) or {}
    with db() as con:
        if "planning_efficiency" in data:
            try:
                eff = float(data.get("planning_efficiency"))
            except (TypeError, ValueError):
                return jsonify({"error": "planning_efficiency must be numeric"}), 400
            eff = max(0.1, min(1.5, eff))
            set_planning_setting(con, "planning_efficiency", f"{eff:.4f}".rstrip("0").rstrip("."))
        if "planning_calendar_policy" in data:
            policy = compact_text(data.get("planning_calendar_policy")) or DEFAULT_PLANNING_CALENDAR_POLICY
            set_planning_setting(con, "planning_calendar_policy", policy.upper())
        if "planning_start_time" in data:
            start_time = compact_text(data.get("planning_start_time")) or DEFAULT_PLANNING_START_TIME
            try:
                datetime.strptime(start_time, "%H:%M")
            except ValueError:
                return jsonify({"error": "planning_start_time must be HH:MM"}), 400
            set_planning_setting(con, "planning_start_time", start_time)
        if int(data.get("recalculate") or 0):
            planning_run_id = recalculate_planning_all_baseline(con)
            refresh_planner_alerts(con)
            return jsonify({"ok": True, "recalculated": True, "planning_run_id": planning_run_id})
        return jsonify({"ok": True, "settings": {
            "planning_efficiency": get_planning_efficiency(con),
            "planning_calendar_policy": get_planning_calendar_policy(con),
            "planning_start_time": get_planning_start_time_text(con),
        }})


@trial_bp.post("/api/trial/planner/recalculate")
def api_trial_planner_recalculate():
    data = request.get_json(force=True, silent=True) or {}
    with db() as con:
        planning_run_id = recalculate_planning_all_baseline(con, reason=compact_text(data.get("reason")) or "PLANNER_RECALCULATE")
        refresh_planner_alerts(con)
        return jsonify({"ok": True, "planning_run_id": planning_run_id})


@trial_bp.post("/api/trial/planner/recalculate-machine/<int:machine_id>")
def api_trial_planner_recalculate_machine(machine_id):
    data = request.get_json(force=True, silent=True) or {}
    planning_run_id = int(data.get("planning_run_id") or 0)
    with db() as con:
        if not planning_run_id:
            planning_run_id = create_planning_schedule_run(con, reason=compact_text(data.get("reason")) or "PLANNER_RECALCULATE_MACHINE")
        planning_run_id = recalculate_planning_machine_baseline(con, machine_id, planning_run_id)
        refresh_planner_alerts(con)
        return jsonify({"ok": True, "planning_run_id": planning_run_id, "machine_id": int(machine_id)})


@trial_bp.post("/api/trial/planner/alerts/<int:alert_id>/align-start")
def api_trial_planner_alert_align_start(alert_id):
    with db() as con:
        alert = one(
            con.execute(
                """
                SELECT *
                FROM schedule_alert
                WHERE alert_id = ?
                """,
                (int(alert_id),),
            )
        )
        if not alert:
            return jsonify({"error": "Alert not found"}), 404
        if compact_text(alert.get("alert_type") or "").upper() != "START_DRIFT":
            return jsonify({"error": "Alert type must be START_DRIFT"}), 400
        if compact_text(alert.get("status") or "").upper() not in {"ACTIVE", "OPEN", "ACKNOWLEDGED"}:
            return jsonify({"error": "Alert is not active"}), 400
        block_id = int(alert.get("block_id") or 0)
        block = trial_block_row(con, block_id)
        if not block:
            return jsonify({"error": "Run block not found"}), 404
        actual_start_at = compact_text(alert.get("actual_start_at") or "")
        if not actual_start_at:
            actual_start_at = compact_text(actual_summary_for_block(con, block_id, block.get("scheduled_qty") or 0).get("actual_start_at") or "")
        if not actual_start_at:
            return jsonify({"error": "Actual start is not available"}), 400
        con.execute(
            """
            UPDATE run_block
            SET planned_start_at = ?,
                anchor_datetime = ?,
                allow_pull_forward = 0,
                updated_at = CURRENT_TIMESTAMP
            WHERE block_id = ?
            """,
            (actual_start_at, actual_start_at, block_id),
        )
        planning_state = one(
            con.execute(
                """
                SELECT expected_start_at, expected_end_at
                FROM planning_block_state
                WHERE block_id = ?
                """,
                (block_id,),
            )
        )
        if planning_state:
            old_start_dt = parse_dt_text(planning_state.get("expected_start_at") or "")
            old_end_dt = parse_dt_text(planning_state.get("expected_end_at") or "")
            new_start_dt = parse_dt_text(actual_start_at)
            next_expected_end_at = compact_text(planning_state.get("expected_end_at") or "")
            if old_start_dt and old_end_dt and new_start_dt:
                delta = new_start_dt - old_start_dt
                next_expected_end_at = (old_end_dt + delta).strftime("%Y-%m-%d %H:%M:%S")
            con.execute(
                """
                UPDATE planning_block_state
                SET expected_start_at = ?,
                    expected_end_at = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE block_id = ?
                """,
                (actual_start_at, next_expected_end_at, block_id),
            )
        resolve_schedule_alert(con, int(alert_id))
        planning_run_id = recalculate_planning_all_baseline(con, reason="PLANNER_ALERT_ALIGN_START")
        refresh_planner_alerts(con)
        return jsonify({"ok": True, "planning_run_id": planning_run_id, "block_id": block_id, "alert_id": int(alert_id)})


@trial_bp.post("/api/trial/planner/alerts/<int:alert_id>/dismiss")
def api_trial_planner_alert_dismiss(alert_id):
    with db() as con:
        alert = one(
            con.execute(
                """
                SELECT *
                FROM schedule_alert
                WHERE alert_id = ?
                """,
                (int(alert_id),),
            )
        )
        if not alert:
            return jsonify({"error": "Alert not found"}), 404
        dismiss_schedule_alert(con, int(alert_id))
        return jsonify({"ok": True, "alert_id": int(alert_id)})


@trial_bp.get("/api/trial/planner/schedule")
def api_trial_planner_schedule():
    planning_run_id = int(request.args.get("planning_run_id") or 0)
    with db() as con:
        if not planning_run_id:
            current = one(
                con.execute(
                    """
                    SELECT *
                    FROM planning_schedule_run
                    WHERE status = 'CURRENT'
                    ORDER BY generated_at DESC, planning_run_id DESC
                    LIMIT 1
                    """
                )
            )
            if current:
                planning_run_id = int(current["planning_run_id"])
        planning_run = None
        if planning_run_id:
            planning_run = one(
                con.execute(
                    """
                    SELECT *
                    FROM planning_schedule_run
                    WHERE planning_run_id = ?
                    """,
                    (int(planning_run_id),),
                )
            )
        refresh_planner_alerts(con)
        settings = {
            "planning_efficiency": get_planning_efficiency(con),
            "planning_calendar_policy": get_planning_calendar_policy(con),
            "planning_start_time": get_planning_start_time_text(con),
        }
        machines = [dict(row) for row in rows(con.execute("SELECT * FROM machines WHERE active = 1 ORDER BY machine_id"))]
        raw_catalog = trial_catalog_items(con, include_completed=True)
        planning_cards = [card for cards in planning_cards_by_ps(con).values() for card in cards]
        normalized_available = [_normalize_planner_sidebar_item(item) for item in (raw_catalog.get("available") or [])]
        normalized_planned = [_normalize_planner_sidebar_item(item) for item in (raw_catalog.get("planned") or [])]
        source_ps_ids = sorted(
            {
                _source_ps_id_text(item)
                for item in normalized_available + normalized_planned
                if _source_ps_id_text(item)
            }
        )
        state_map, op_map = _planner_completion_maps(con, source_ps_ids)
        bom_ids = set()
        for item in normalized_available + normalized_planned:
            for option in _planner_bom_options(item):
                if int(option.get("bom_id") or 0) > 0:
                    bom_ids.add(int(option["bom_id"]))
        bom_steps_map = _planner_bom_steps_map(con, bom_ids)
        catalog = {
            "available": [_planner_enrich_sidebar_item(item, state_map, op_map, bom_steps_map, con=con) for item in normalized_available],
            "planned": [_planner_enrich_sidebar_item(item, state_map, op_map, bom_steps_map, con=con) for item in normalized_planned],
        }
        blocks = []
        block_summary_map = {}
        alerts = []
        if planning_run_id:
            block_rows = rows(
                con.execute(
                    """
                    SELECT b.*, o.job_no, o.operation_name, o.total_qty, o.setup_minutes, o.cycle_minutes_per_qty,
                           o.compatible_machine_group, o.source_ps_id, o.pp_partial_no, o.source_op_no,
                           m.machine_code, m.machine_category,
                           s.expected_start_at, s.expected_end_at, s.planned_qty AS planned_qty_state, s.planned_minutes AS planned_minutes_state
                    FROM planning_block_state s
                    JOIN run_block b ON b.block_id = s.block_id
                    JOIN operation o ON o.operation_id = b.operation_id
                    JOIN machines m ON m.machine_id = b.machine_id
                    WHERE s.planning_run_id = ?
                    ORDER BY b.machine_id, b.queue_position, b.block_id
                    """,
                    (int(planning_run_id),),
                )
            )
            block_ids = [int(row["block_id"]) for row in block_rows]
            raw_segments = []
            segments_by_block = {}
            if block_ids:
                placeholders = ",".join("?" for _ in block_ids)
                raw_segments = rows(
                    con.execute(
                        f"""
                        SELECT s.*, b.operation_id
                        FROM run_block_segment s
                        JOIN run_block b ON b.block_id = s.block_id
                        WHERE COALESCE(b.active, 1) = 1
                          AND b.block_id IN ({placeholders})
                        ORDER BY b.machine_id, b.queue_position, s.segment_id
                        """,
                        block_ids,
                    )
                )
                for row in raw_segments:
                    item = dict(row)
                    machine = next((m for m in machines if int(m.get("machine_id") or 0) == int(item.get("machine_id") or 0)), {})
                    shift_profile = compact_text(machine.get("shift_profile") or item.get("shift_profile") or "")
                    start_dt = parse_dt_text(item.get("start_datetime"))
                    end_dt = parse_dt_text(item.get("end_datetime"))
                    timing = visual_timing_for_segment(
                        start_dt,
                        item.get("minutes_used") or 0,
                        end_dt=end_dt,
                        work_date=start_dt.date() if start_dt else None,
                        profile_name="",
                        shift_profile=shift_profile,
                        segment_type=item.get("segment_type") or "production",
                    )
                    item["visual_start_datetime"] = timing["visual_start_datetime"]
                    item["visual_end_datetime"] = timing["visual_end_datetime"]
                    item["visual_parts"] = timing["visual_parts"]
                    item["break_windows"] = timing["break_windows"]
                    segments_by_block.setdefault(int(item.get("block_id") or 0), []).append(item)
            blocks = []
            planned_qty_by_block = {
                int(row["block_id"]): float(row["planned_qty_state"] or row["scheduled_qty"] or 0)
                for row in block_rows
            }
            for row in block_rows:
                row_dict = dict(row)
                payload = _planning_block_payload(row_dict)
                block_segments = segments_by_block.get(int(row.get("block_id") or 0), [])
                visual_start = ""
                visual_end = ""
                visual_error = ""
                calculated_start = compact_text(row.get("calculated_start_datetime") or "")
                calculated_end = compact_text(row.get("calculated_end_datetime") or "")
                if block_segments:
                    block_start_dt = parse_dt_text(row.get("start_datetime") or calculated_start)
                    block_end_dt = parse_dt_text(row.get("end_datetime") or calculated_end)
                    visual_starts = sorted(
                        [compact_text(seg.get("visual_start_datetime")) for seg in block_segments if compact_text(seg.get("visual_start_datetime"))]
                    )
                    visual_ends = sorted(
                        [compact_text(seg.get("visual_end_datetime")) for seg in block_segments if compact_text(seg.get("visual_end_datetime"))]
                    )
                    timing = visual_timing_for_segment(
                        block_start_dt,
                        row.get("minutes_used") or 0,
                        end_dt=block_end_dt,
                        work_date=block_start_dt.date() if block_start_dt else None,
                        profile_name="",
                        shift_profile=compact_text(row.get("shift_profile") or ""),
                        segment_type=row.get("segment_type") or "production",
                    ) if block_start_dt else {"visual_start_datetime": "", "visual_end_datetime": ""}
                    visual_start = compact_text(
                        timing.get("visual_start_datetime")
                        or (visual_starts[0] if visual_starts else "")
                        or calculated_start
                        or compact_text(row.get("expected_start_at") or "")
                    )
                    visual_end = compact_text(
                        timing.get("visual_end_datetime")
                        or (visual_ends[-1] if visual_ends else "")
                        or calculated_end
                        or compact_text(row.get("expected_end_at") or "")
                    )
                else:
                    visual_start = calculated_start or compact_text(row.get("expected_start_at") or "")
                    visual_end = calculated_end or compact_text(row.get("expected_end_at") or "")
                if float(row.get("scheduled_qty") or row.get("planned_qty_state") or 0) > 0 and (not visual_start or not visual_end) and (not calculated_start or not calculated_end) and (not compact_text(row.get("expected_start_at") or "") or not compact_text(row.get("expected_end_at") or "")):
                    visual_error = "Forecast timing unavailable"
                payload["planned_start_at"] = compact_text(row.get("planned_start_at") or "")
                payload["planned_end_at"] = compact_text(row.get("planned_end_at") or "")
                payload["expected_start_at"] = compact_text(row.get("expected_start_at") or "")
                payload["expected_end_at"] = compact_text(row.get("expected_end_at") or "")
                payload["forecast_start_at"] = compact_text(row.get("expected_start_at") or calculated_start or "")
                payload["forecast_end_at"] = compact_text(row.get("expected_end_at") or calculated_end or "")
                payload["calculated_start_datetime"] = calculated_start
                payload["calculated_end_datetime"] = calculated_end
                payload["visual_start_datetime"] = visual_start
                payload["visual_end_datetime"] = visual_end
                payload["forecast_error"] = visual_error
                payload["planned_qty"] = float(row["planned_qty_state"] or 0)
                payload["planned_minutes"] = float(row["planned_minutes_state"] or 0)
                actual_daily_rows = actual_daily_rows_for_block_row(con, row_dict)
                payload["actual_daily_rows"] = actual_daily_rows
                payload["actual_daily_rows_error"] = "NO_PRODUCTION_SEGMENTS" if not actual_daily_rows else ""
                blocks.append(payload)
            actual_summary_map = actual_summaries_for_block_rows(
                con,
                block_rows,
                planned_qty_by_block=planned_qty_by_block,
            )
            for payload in blocks:
                summary = actual_summary_map.get(int(payload["block_id"]), {})
                payload["actual_start_at"] = compact_text(summary.get("actual_start_at") or "")
                payload["actual_end_at"] = compact_text(summary.get("actual_end_at") or "")
                payload["actual_good_qty"] = float(summary.get("actual_good_qty") or 0)
                payload["actual_row_count"] = int(summary.get("actual_row_count") or 0)
            for idx, payload in enumerate(blocks):
                machine_id = int(payload.get("machine_id") or 0)
                next_payload = None
                if idx + 1 < len(blocks):
                    candidate = blocks[idx + 1]
                    if int(candidate.get("machine_id") or 0) == machine_id:
                        next_payload = candidate
                actual_end_at = compact_text(payload.get("actual_end_at") or "")
                next_planned_start = compact_text(next_payload.get("planned_start_at") or "") if next_payload else ""
                if actual_end_at and next_planned_start:
                    actual_end_dt = parse_dt_text(actual_end_at)
                    next_planned_start_dt = parse_dt_text(next_planned_start)
                    if actual_end_dt and next_planned_start_dt and actual_end_dt < next_planned_start_dt:
                        available_minutes = _working_minutes_between(con, machine_id, actual_end_at, next_planned_start)
                        if available_minutes > 0:
                            payload["gap_after"] = {
                                "type": "GAP",
                                "machine_id": machine_id,
                                "gap_id": f"actual-gap-{int(payload['block_id'])}-{int(next_payload['block_id'])}",
                                "after_block_id": int(payload["block_id"]),
                                "before_block_id": int(next_payload["block_id"]),
                                "start_at": actual_end_at,
                                "end_at": next_planned_start,
                                "available_minutes": float(available_minutes),
                                "label": "Available gap after actual end",
                            }
                        else:
                            payload["gap_after"] = None
                    else:
                        payload["gap_after"] = None
                else:
                    payload["gap_after"] = None
            block_summary_map = {}
            for payload in blocks:
                block_summary_map.setdefault(payload["source_ps_id"], []).append(
                    {
                        "actual_start_at": payload["actual_start_at"],
                        "actual_end_at": payload["actual_end_at"],
                        "planned_qty": payload["planned_qty"],
                        "actual_good_qty": payload["actual_good_qty"],
                        "actual_row_count": payload["actual_row_count"],
                    }
                )
        segments = []
        process_sheets = []
        if planning_run_id:
            segment_rows = rows(
                con.execute(
                    """
                    SELECT *
                    FROM planning_schedule_segment
                    WHERE planning_run_id = ?
                    ORDER BY machine_id, start_datetime, planning_segment_id
                    """,
                    (int(planning_run_id),),
                )
            )
            segments = [dict(row) for row in segment_rows]
            ps_rows = rows(
                con.execute(
                    """
                    SELECT pss.*,
                           ps.source_ps_id,
                           ps.pp_partial_no,
                           ps.part_id,
                           ps.part_no,
                           ps.part_desc,
                           ps.selected_bom_id,
                           ps.completed AS process_completed,
                           ps.completed_at AS process_completed_at,
                           ps.completed_by AS process_completed_by,
                           ps.total_qty AS process_total_qty,
                           ps.status AS process_status,
                           ps.planner_status AS process_planner_status,
                           p.part_no AS part_name
                    FROM planning_process_sheet_state pss
                    JOIN process_sheet ps ON ps.ps_id = pss.ps_id
                    LEFT JOIN parts p ON p.part_id = ps.part_id
                    WHERE planning_run_id = ?
                    ORDER BY expected_end_at, ps_id
                    """,
                    (int(planning_run_id),),
                )
            )
            process_sheets = []
            sidebar_map = {}
            for item in catalog["available"] + catalog["planned"]:
                item = dict(item or {})
                item_ps_id = compact_text(item.get("ps_id") or "")
                item_source_ps_id = _source_ps_id_text(item)
                item_partial_no = compact_text(item.get("pp_partial_no") or item.get("partial_no") or "")
                composite_key = _planner_identity_key(item_source_ps_id, item_partial_no)
                if item_ps_id:
                    sidebar_map[item_ps_id] = item
                if composite_key:
                    sidebar_map[composite_key] = item
            for row in ps_rows:
                row_dict = dict(row)
                source_ps_id = compact_text(row_dict.get("source_ps_id") or row_dict.get("ps_id") or "")
                ps_actual = actual_summary_for_process_sheet_rows(block_summary_map.get(source_ps_id, []))
                row_dict.update(ps_actual)
                row_dict.setdefault("source_ps_id", source_ps_id)
                row_dict.setdefault("pp_partial_no", compact_text(row_dict.get("pp_partial_no") or ""))
                row_dict.setdefault("part_id", int(row_dict.get("part_id") or 0))
                row_dict.setdefault("part_name", compact_text(row_dict.get("part_name") or ""))
                row_dict.setdefault("part_no", compact_text(row_dict.get("part_no") or ""))
                row_dict.setdefault("part_desc", compact_text(row_dict.get("part_desc") or ""))
                row_dict.setdefault("due_date", compact_text(row_dict.get("due_date") or ""))
                row_dict.setdefault("total_qty", float(row_dict.get("process_total_qty") or row_dict.get("total_qty") or 0))
                row_dict.setdefault("selected_bom_id", int(row_dict.get("selected_bom_id") or 0))
                sidebar_item = sidebar_map.get(compact_text(row_dict.get("ps_id") or "")) or sidebar_map.get(_planner_identity_key(source_ps_id, row_dict.get("pp_partial_no") or row_dict.get("partial_no") or ""))
                row_completed = bool(
                    row_dict.get("process_completed")
                    or row_dict.get("is_completed")
                    or (sidebar_item and sidebar_item.get("is_completed"))
                    or (sidebar_item and sidebar_item.get("completed"))
                    or False
                )
                row_completed_at = compact_text(
                    row_dict.get("process_completed_at")
                    or row_dict.get("completed_at")
                    or (sidebar_item.get("completed_at") if sidebar_item else "")
                    or ""
                )
                row_completed_by = compact_text(
                    row_dict.get("process_completed_by")
                    or row_dict.get("completed_by")
                    or (sidebar_item.get("completed_by") if sidebar_item else "")
                    or ""
                )
                row_dict["is_completed"] = row_completed
                row_dict["completed"] = row_completed
                row_dict["completed_at"] = row_completed_at
                row_dict["completed_by"] = row_completed_by
                if sidebar_item:
                    row_dict.setdefault("source_ps_id", sidebar_item.get("source_ps_id") or "")
                    row_dict.setdefault("partial_no", sidebar_item.get("pp_partial_no") or sidebar_item.get("partial_no") or row_dict.get("pp_partial_no") or "")
                    row_dict.setdefault("pp_partial_no", sidebar_item.get("pp_partial_no") or sidebar_item.get("partial_no") or row_dict.get("pp_partial_no") or "")
                    row_dict.setdefault("part_no", sidebar_item.get("part_no") or "")
                    row_dict.setdefault("part_desc", sidebar_item.get("part_desc") or "")
                    row_dict.setdefault("due_date", sidebar_item.get("due_date") or row_dict.get("due_date") or "")
                    row_dict.setdefault("total_qty", sidebar_item.get("total_qty") or row_dict.get("process_total_qty") or 0)
                    row_dict.setdefault("partial_qty", sidebar_item.get("partial_qty") or 0)
                    row_dict.setdefault("opn_count", sidebar_item.get("opn_count") or 0)
                    row_dict.setdefault("active_opn_count", sidebar_item.get("active_opn_count") or 0)
                    row_dict.setdefault("completed_opn_count", sidebar_item.get("completed_opn_count") or 0)
                    row_dict["is_completed"] = bool(row_dict.get("is_completed") or sidebar_item.get("is_completed") or sidebar_item.get("completed") or False)
                    row_dict["completed"] = row_dict["is_completed"]
                    row_dict["completed_at"] = compact_text(row_dict.get("completed_at") or sidebar_item.get("completed_at") or "")
                    row_dict["completed_by"] = compact_text(row_dict.get("completed_by") or sidebar_item.get("completed_by") or "")
                    row_dict.setdefault("execution_label", sidebar_item.get("execution_label") or "")
                    row_dict.setdefault("selected_bom_id", int(sidebar_item.get("selected_bom_id") or 0))
                    row_dict.setdefault("selected_bom_code", sidebar_item.get("selected_bom_code") or "")
                    row_dict.setdefault("default_bom_id", int(sidebar_item.get("default_bom_id") or 0))
                    row_dict.setdefault("bom_options", sidebar_item.get("bom_options") or [])
                if not row_dict.get("bom_options"):
                    row_dict["bom_options"] = _planner_bom_options_for_part(con, row_dict.get("part_id") or 0)
                row_dict["default_bom_id"] = int(row_dict.get("default_bom_id") or _planner_default_bom_id(row_dict.get("bom_options") or []))
                selected_bom_id = int(row_dict.get("selected_bom_id") or 0)
                if not selected_bom_id:
                    selected_bom_id = int(row_dict.get("default_bom_id") or 0)
                    if selected_bom_id:
                        row_dict["selected_bom_id"] = selected_bom_id
                row_dict["selected_bom_code"] = compact_text(row_dict.get("selected_bom_code") or _planner_bom_code_for_id(row_dict.get("bom_options") or [], selected_bom_id))
                if sidebar_item and list(sidebar_item.get("op_cards") or []):
                    row_dict["op_cards"] = list(sidebar_item.get("op_cards") or [])
                elif selected_bom_id:
                    row_dict["op_cards"] = _planner_op_cards_for_source_ps(
                        con,
                        source_ps_id,
                        compact_text(row_dict.get("pp_partial_no") or row_dict.get("partial_no") or ""),
                        selected_bom_id,
                        row_dict.get("partial_qty") or row_dict.get("planned_qty") or row_dict.get("total_qty") or 0,
                    )
                else:
                    row_dict["op_cards"] = []
                row_dict["opn_count"] = len(row_dict["op_cards"])
                row_dict["completed_opn_count"] = sum(1 for card in row_dict["op_cards"] if bool(card.get("is_completed")))
                row_dict["active_opn_count"] = max(0, row_dict["opn_count"] - row_dict["completed_opn_count"])
                row_dict.setdefault("ps_id", row_dict.get("ps_id") or source_ps_id)
                process_sheets.append(_planning_process_sheet_payload(row_dict))
            block_lookup = {int(block.get("block_id") or 0): block for block in blocks}
            alerts = []
            for row in active_schedule_alert_rows(con):
                block = block_lookup.get(int(row["block_id"] or 0), {})
                output_efficiency = row["output_efficiency"]
                actual_good_qty = float(block.get("actual_good_qty") or 0)
                expected_qty_by_now = None
                if output_efficiency not in (None, ""):
                    try:
                        eff = float(output_efficiency or 0)
                    except (TypeError, ValueError):
                        eff = 0.0
                    if eff > 0:
                        expected_qty_by_now = actual_good_qty / eff
                alerts.append(
                    {
                        "alert_id": int(row["alert_id"]),
                        "schedule_run_id": int(row["schedule_run_id"] or 0) if row["schedule_run_id"] is not None else 0,
                        "block_id": int(row["block_id"] or 0),
                        "operation_id": int(row["operation_id"] or 0),
                        "ps_id": compact_text(row["ps_id"] or ""),
                        "machine_id": int(row["machine_id"] or 0),
                        "alert_type": compact_text(row["alert_type"] or ""),
                        "severity": compact_text(row["severity"] or ""),
                        "status": compact_text(row["status"] or ""),
                        "message": compact_text(row["message"] or ""),
                        "old_value": compact_text(row["old_value"] or ""),
                        "new_value": compact_text(row["new_value"] or ""),
                        "planned_at": compact_text(row["planned_at"] or ""),
                        "predicted_at": compact_text(row["predicted_at"] or ""),
                        "expected_start_at": compact_text(row["expected_start_at"] or ""),
                        "actual_start_at": compact_text(row["actual_start_at"] or ""),
                        "drift_hours": float(row["drift_hours"] or 0) if row["drift_hours"] is not None else None,
                        "output_efficiency": float(output_efficiency) if output_efficiency not in (None, "") else None,
                        "expected_qty_by_now": float(expected_qty_by_now) if expected_qty_by_now is not None else None,
                        "actual_good_qty": actual_good_qty,
                        "delay_minutes": float(row["delay_minutes"] or 0),
                        "created_at": compact_text(row["created_at"] or ""),
                        "updated_at": compact_text(row["updated_at"] or ""),
                        "resolved_at": compact_text(row["resolved_at"] or ""),
                        "dismissed_at": compact_text(row["dismissed_at"] or ""),
                    }
                )
        return jsonify(
            {
                "ok": True,
                "planning_run": _planning_run_payload(planning_run),
                "settings": settings,
                "machines": machines,
                "blocks": blocks,
                "segments": segments,
                "process_sheets": process_sheets,
                "alerts": alerts,
                "catalog": catalog["available"],
                "planned": catalog["planned"],
                "planning_cards": planning_cards,
            }
        )


@trial_bp.post("/api/trial/planner/opn/<int:operation_id>/completion")
def api_trial_planner_opn_completion(operation_id):
    data = request.get_json(force=True, silent=True) or {}
    completed = bool(data.get("completed"))
    source_ps_id = compact_text(data.get("source_ps_id") or "")
    pp_partial_no = compact_text(data.get("pp_partial_no") or data.get("partial_no") or "")
    bom_id = int(data.get("bom_id") or 0)
    source_op_seq_id = int(data.get("source_op_seq_id") or 0)
    source_op_no = compact_text(data.get("source_op_no") or "")
    with db() as con:
        op_row = one(
            con.execute(
                """
                SELECT *
                FROM operation
                WHERE operation_id = ?
                LIMIT 1
                """,
                (int(operation_id),),
            )
        )
        op_row = dict(op_row) if op_row else {}
        source_ps_id = source_ps_id or compact_text(op_row.get("source_ps_id") or "")
        if not pp_partial_no:
            ps_text = compact_text(op_row.get("source_ps_id") or "")
            if "::" in ps_text:
                pp_partial_no = compact_text(ps_text.split("::", 1)[1])
        source_op_seq_id = source_op_seq_id or int(op_row.get("source_op_seq_id") or 0) or int(operation_id)
        if not source_ps_id:
            return jsonify({"error": "source_ps_id missing"}), 400
        ps_row = _planner_process_sheet_row(con, source_ps_id, pp_partial_no)
        if not ps_row:
            return jsonify({"error": "Process sheet not found"}), 404
        if not bom_id:
            bom_id = int(data.get("selected_bom_id") or ps_row.get("selected_bom_id") or 0)
        if not bom_id:
            bom_options = _planner_bom_options_for_part(con, ps_row.get("part_id") or 0)
            bom_id = _planner_default_bom_id(bom_options)
        if not source_op_no:
            step_row = next(
                (
                    step
                    for step in _planner_bom_steps_for_selected(con, source_ps_id, pp_partial_no, bom_id)
                    if int(step.get("op_seq_id") or 0) == int(source_op_seq_id or 0)
                ),
                None,
            )
            source_op_no = compact_text((step_row or {}).get("op_no") or "")
        state_row = {
            "source_op_seq_id": source_op_seq_id,
            "source_op_no": source_op_no,
            "operation_id": int(op_row.get("operation_id") or operation_id or source_op_seq_id or 0),
        }
        _planner_upsert_op_state(con, source_ps_id, pp_partial_no, bom_id, state_row, completed)
        summary = refresh_process_sheet_completion(con, source_ps_id, pp_partial_no, bom_id)
        return jsonify({"ok": True, "summary": summary})


@trial_bp.post("/api/trial/planner/source-ps/<path:source_ps_id>/completion")
def api_trial_planner_source_ps_completion(source_ps_id):
    data = request.get_json(force=True, silent=True) or {}
    source_ps_id = compact_text(source_ps_id)
    pp_partial_no = compact_text(data.get("pp_partial_no") or data.get("partial_no") or "")
    completed = bool(data.get("completed"))
    if not source_ps_id:
        return jsonify({"error": "source_ps_id is required"}), 400
    if not pp_partial_no:
        return jsonify({"error": "pp_partial_no is required"}), 400
    with db() as con:
        ps_row = _planner_process_sheet_row(con, source_ps_id, pp_partial_no)
        if not ps_row:
            return jsonify({"error": "Process sheet not found"}), 404
        bom_id = int(ps_row.get("selected_bom_id") or 0)
        if not bom_id:
            bom_options = _planner_bom_options_for_part(con, ps_row.get("part_id") or 0)
            bom_id = _planner_default_bom_id(bom_options)
        if completed and bom_id:
            for step in _planner_bom_steps_for_selected(con, source_ps_id, pp_partial_no, bom_id):
                _planner_upsert_op_state(
                    con,
                    source_ps_id,
                    pp_partial_no,
                    bom_id,
                    {
                        "source_op_seq_id": int(step.get("op_seq_id") or 0),
                        "source_op_no": compact_text(step.get("op_no") or ""),
                        "operation_id": int(step.get("operation_id") or 0),
                    },
                    True,
                )
        summary = refresh_process_sheet_completion(con, source_ps_id, pp_partial_no, bom_id, set_completed=completed)
        refresh_planner_alerts(con)
        return jsonify({
            "ok": True,
            "source_ps_id": source_ps_id,
            "pp_partial_no": pp_partial_no,
            "is_completed": bool(summary.get("is_completed") if summary else completed),
            "summary": summary,
        })


@trial_bp.post("/api/trial/planner/source-ps/<path:source_ps_id>/bom")
def api_trial_planner_source_ps_bom(source_ps_id):
    data = request.get_json(force=True, silent=True) or {}
    bom_id = int(data.get("bom_id") or 0)
    pp_partial_no = compact_text(data.get("pp_partial_no") or data.get("partial_no") or "")
    source_ps_id = compact_text(source_ps_id)
    if not source_ps_id:
        return jsonify({"error": "source_ps_id is required"}), 400
    if bom_id <= 0:
        return jsonify({"error": "bom_id is required"}), 400
    with db() as con:
        ps_row = _planner_process_sheet_row(con, source_ps_id, pp_partial_no)
        if not ps_row:
            return jsonify({"error": "Process sheet not found"}), 404
        bom_options = _planner_bom_options_for_part(con, ps_row.get("part_id") or 0)
        if bom_options and not any(int(option.get("bom_id") or 0) == bom_id for option in bom_options):
            return jsonify({"error": "bom_id is not valid for this process sheet"}), 400
        con.execute(
            """
            UPDATE process_sheet
            SET selected_bom_id = ?,
                planner_status = COALESCE(planner_status, 'ACTIVE'),
                updated_at = CURRENT_TIMESTAMP
            WHERE COALESCE(source_ps_id, '') = ?
              AND COALESCE(pp_partial_no, '') = ?
            """,
            (bom_id, source_ps_id, pp_partial_no),
        )
        summary = refresh_process_sheet_completion(con, source_ps_id, pp_partial_no, bom_id)
        return jsonify({"ok": True, "source_ps_id": source_ps_id, "pp_partial_no": pp_partial_no, "bom_id": bom_id, "summary": summary})


@trial_bp.get("/api/trial/planner/source-ps/<path:source_ps_id>/operations")
def api_trial_planner_source_ps_operations(source_ps_id):
    source_ps_id = compact_text(source_ps_id)
    pp_partial_no = compact_text(request.args.get("pp_partial_no") or request.args.get("partial_no") or "")
    bom_id = int(request.args.get("bom_id") or request.args.get("selected_bom_id") or 0)
    if not source_ps_id:
        return jsonify({"error": "source_ps_id is required"}), 400
    with db() as con:
        ps_row = _planner_process_sheet_row(con, source_ps_id, pp_partial_no)
        if not ps_row:
            return jsonify({"error": "Process sheet not found"}), 404
        if not pp_partial_no:
            pp_partial_no = compact_text(ps_row.get("pp_partial_no") or ps_row.get("partial_no") or "")
        if not bom_id:
            bom_id = int(ps_row.get("selected_bom_id") or 0)
        if not bom_id:
            bom_options = _planner_bom_options_for_part(con, ps_row.get("part_id") or 0)
            bom_id = _planner_default_bom_id(bom_options)
        bom_options = _planner_bom_options_for_part(con, ps_row.get("part_id") or 0)
        if bom_options and not any(int(option.get("bom_id") or 0) == int(bom_id or 0) for option in bom_options):
            return jsonify({"error": "bom_id is not valid for this process sheet"}), 400
        operations = _planner_operation_editor_rows(con, source_ps_id, pp_partial_no, bom_id)
        machines, machine_groups = _planner_machine_options(con)
        return jsonify(
            {
                "ok": True,
                "source_ps_id": source_ps_id,
                "pp_partial_no": pp_partial_no,
                "bom_id": int(bom_id or 0),
                "bom_code": _planner_bom_code_for_id(bom_options, bom_id),
                "operations": operations,
                "machines": [dict(machine) for machine in machines],
                "machine_groups": machine_groups,
            }
        )


@trial_bp.post("/api/trial/planner/source-ps/<path:source_ps_id>/operations")
def api_trial_planner_source_ps_operations_add(source_ps_id):
    data = request.get_json(force=True, silent=True) or {}
    source_ps_id = compact_text(source_ps_id)
    pp_partial_no = compact_text(data.get("pp_partial_no") or data.get("partial_no") or "")
    bom_id = int(data.get("bom_id") or 0)
    operation_name = compact_text(data.get("operation_name"))
    source_op_no = compact_text(data.get("source_op_no"))
    setup_minutes = parse_number(data.get("setup_minutes"), 0)
    cycle_minutes_per_qty = parse_number(data.get("cycle_minutes_per_qty"), 0)
    machine_category = compact_text(data.get("machine_category") or "")
    preferred_machine = compact_text(data.get("preferred_machine") or "")
    machine_ids = data.get("machine_ids") or []
    if not source_ps_id:
        return jsonify({"error": "source_ps_id is required"}), 400
    if not operation_name:
        return jsonify({"error": "operation_name is required"}), 400
    if setup_minutes < 0:
        return jsonify({"error": "setup_minutes must be 0 or more"}), 400
    if cycle_minutes_per_qty <= 0:
        return jsonify({"error": "cycle_minutes_per_qty must be greater than 0"}), 400
    with db() as con:
        ps_row = _planner_process_sheet_row(con, source_ps_id, pp_partial_no)
        if not ps_row:
            return jsonify({"error": "Process sheet not found"}), 404
        if not pp_partial_no:
            pp_partial_no = compact_text(ps_row.get("pp_partial_no") or ps_row.get("partial_no") or "")
        if not bom_id:
            bom_id = int(ps_row.get("selected_bom_id") or 0)
        if not bom_id:
            bom_options = _planner_bom_options_for_part(con, ps_row.get("part_id") or 0)
            bom_id = _planner_default_bom_id(bom_options)
        bom_options = _planner_bom_options_for_part(con, ps_row.get("part_id") or 0)
        if bom_options and not any(int(option.get("bom_id") or 0) == int(bom_id or 0) for option in bom_options):
            return jsonify({"error": "bom_id is not valid for this process sheet"}), 400
        machine_rows = [dict(row) for row in fetch_machines(con)]
        machine_row, resolved_machine_id, machine_category, machine_error = _planner_resolve_machine_context(
            machine_rows,
            machine_ids=machine_ids,
            preferred_machine=preferred_machine,
            machine_category=machine_category,
        )
        if machine_error:
            return jsonify({"error": machine_error}), 400
        if machine_row and not preferred_machine:
            preferred_machine = compact_text(machine_row.get("machine_code") or "")
        max_seq_row = one(
            con.execute(
                "SELECT COALESCE(MAX(seq_no), 0) AS max_seq FROM operation_seq WHERE bom_id = ?",
                (int(bom_id),),
            )
        ) or {}
        next_seq = int(max_seq_row.get("max_seq") or 0) + 1
        if not source_op_no:
            source_op_no = str(next_seq * 10)
        duplicate_row = one(
            con.execute(
                """
                SELECT op_seq_id
                FROM operation_seq
                WHERE bom_id = ?
                  AND COALESCE(op_no, '') = ?
                LIMIT 1
                """,
                (int(bom_id), source_op_no),
            )
        )
        if duplicate_row:
            return jsonify({"error": "source_op_no already exists for this BOM"}), 400
        con.execute(
            """
            UPDATE operation_seq
            SET is_last_op = 0
            WHERE bom_id = ?
            """,
            (int(bom_id),),
        )
        cur = con.execute(
            """
            INSERT INTO operation_seq (
              bom_id, seq_no, op_no, op_type, machine_category, preferred_machine, cycle_time, setup_time, is_last_op
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                int(bom_id),
                next_seq,
                source_op_no,
                operation_name,
                machine_category,
                preferred_machine,
                float(cycle_minutes_per_qty),
                float(setup_minutes),
            ),
        )
        op_seq_id = int(cur.lastrowid)
        affected_machine_ids = sync_operations_for_flow(con, bom_id)
        current_operation = one(
            con.execute(
                """
                SELECT operation_id
                FROM operation
                WHERE COALESCE(source_ps_id, '') = ?
                  AND COALESCE(pp_partial_no, '') = ?
                  AND COALESCE(selected_bom_id, 0) = ?
                  AND COALESCE(source_op_seq_id, 0) = ?
                  AND COALESCE(source_op_no, '') = ?
                LIMIT 1
                """,
                (source_ps_id, pp_partial_no, int(bom_id), int(op_seq_id), source_op_no),
            )
        )
        if current_operation:
            current_operation_id = int(current_operation["operation_id"] or 0)
            con.execute(
                """
                UPDATE operation
                SET operation_name = ?,
                    job_no = ?,
                    pp_partial_no = ?,
                    selected_bom_id = ?,
                    setup_minutes = ?,
                    cycle_minutes_per_qty = ?,
                    compatible_machine_group = ?,
                    source_op_seq_id = ?,
                    source_op_no = ?,
                    status = 'ACTIVE',
                    updated_at = CURRENT_TIMESTAMP
                WHERE operation_id = ?
                """,
                (
                    _planner_strip_op_prefix(source_op_no, operation_name),
                    source_ps_id,
                    pp_partial_no,
                    int(bom_id),
                    float(setup_minutes),
                    float(cycle_minutes_per_qty),
                    machine_category or "UNKNOWN",
                    int(op_seq_id),
                    source_op_no,
                    current_operation_id,
                ),
            )
        else:
            cur_op = con.execute(
                """
                INSERT INTO operation (
                  job_no, operation_name, total_qty, setup_minutes, cycle_minutes_per_qty, compatible_machine_group,
                  source_ps_id, pp_partial_no, selected_bom_id, source_op_seq_id, source_op_no, status, remarks, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    source_ps_id,
                    _planner_strip_op_prefix(source_op_no, operation_name),
                    0.0,
                    float(setup_minutes),
                    float(cycle_minutes_per_qty),
                    machine_category or "UNKNOWN",
                    source_ps_id,
                    pp_partial_no,
                    int(bom_id),
                    int(op_seq_id),
                    source_op_no,
                    "ACTIVE",
                    "",
                ),
            )
            current_operation_id = int(cur_op.lastrowid)
        for machine_id in affected_machine_ids:
            recalculate_machine(con, machine_id)
        planning_run_id = recalculate_planning_all_baseline(con, reason="BOM_OPERATION_EDIT")
        summary = refresh_process_sheet_completion(con, source_ps_id, pp_partial_no, bom_id)
        refresh_planner_alerts(con)
        operations = _planner_operation_editor_rows(con, source_ps_id, pp_partial_no, bom_id)
        created = next((row for row in operations if int(row.get("operation_id") or 0) == current_operation_id), None)
        if not created:
            created = next((row for row in operations if int(row.get("op_seq_id") or 0) == op_seq_id), None)
        return jsonify(
            {
                "ok": True,
                "mode": "added",
                "message": "Operation added.",
                "planning_run_id": planning_run_id,
                "source_ps_id": source_ps_id,
                "pp_partial_no": pp_partial_no,
                "bom_id": int(bom_id),
                "operation": created,
                "operations": operations,
                "summary": summary,
                "warnings": [],
            }
        )


@trial_bp.put("/api/trial/planner/operations/<int:operation_id>")
def api_trial_planner_operation_update(operation_id):
    data = request.get_json(force=True, silent=True) or {}
    operation_name = compact_text(data.get("operation_name"))
    source_op_no = compact_text(data.get("source_op_no"))
    setup_minutes = parse_number(data.get("setup_minutes"), None)
    cycle_minutes_per_qty = parse_number(data.get("cycle_minutes_per_qty"), None)
    machine_category = compact_text(data.get("machine_category") or "")
    preferred_machine = compact_text(data.get("preferred_machine") or "")
    machine_ids = data.get("machine_ids") or []
    is_active = data.get("is_active")
    with db() as con:
        op_row = one(
            con.execute(
                """
                SELECT *
                FROM operation
                WHERE operation_id = ?
                LIMIT 1
                """,
                (int(operation_id),),
            )
        )
        if not op_row:
            return jsonify({"error": "Operation not found"}), 404
        op_row = dict(op_row)
        source_ps_id = compact_text(op_row.get("source_ps_id") or "")
        pp_partial_no = compact_text(op_row.get("pp_partial_no") or "")
        bom_id = int(op_row.get("selected_bom_id") or 0)
        source_op_seq_id = int(op_row.get("source_op_seq_id") or 0)
        if not source_ps_id or not bom_id or not source_op_seq_id:
            return jsonify({"error": "Operation context is incomplete"}), 400
        step_row = one(
            con.execute(
                """
                SELECT *
                FROM operation_seq
                WHERE bom_id = ?
                  AND op_seq_id = ?
                LIMIT 1
                """,
                (bom_id, source_op_seq_id),
            )
        )
        if not step_row:
            return jsonify({"error": "Operation step not found"}), 404
        step_row = dict(step_row)
        next_operation_name = operation_name or compact_text(step_row.get("op_type") or "")
        next_source_op_no = source_op_no or compact_text(step_row.get("op_no") or "")
        if setup_minutes is None:
            setup_minutes = parse_number(step_row.get("setup_time"), 0)
        if cycle_minutes_per_qty is None:
            cycle_minutes_per_qty = parse_number(step_row.get("cycle_time"), 0)
        if setup_minutes < 0:
            return jsonify({"error": "setup_minutes must be 0 or more"}), 400
        if cycle_minutes_per_qty <= 0:
            return jsonify({"error": "cycle_minutes_per_qty must be greater than 0"}), 400
        machine_rows = [dict(row) for row in fetch_machines(con)]
        machine_row, resolved_machine_id, machine_category, machine_error = _planner_resolve_machine_context(
            machine_rows,
            machine_ids=machine_ids,
            preferred_machine=preferred_machine,
            machine_category=machine_category or compact_text(step_row.get("machine_category") or ""),
        )
        if machine_error:
            return jsonify({"error": machine_error}), 400
        if machine_row and not preferred_machine:
            preferred_machine = compact_text(machine_row.get("machine_code") or "")
        duplicate_row = one(
            con.execute(
                """
                SELECT op_seq_id
                FROM operation_seq
                WHERE bom_id = ?
                  AND COALESCE(op_no, '') = ?
                  AND op_seq_id <> ?
                LIMIT 1
                """,
                (bom_id, next_source_op_no, source_op_seq_id),
            )
        )
        if duplicate_row:
            return jsonify({"error": "source_op_no already exists for this BOM"}), 400
        con.execute(
            """
            UPDATE operation_seq
            SET op_no = ?,
                op_type = ?,
                machine_category = ?,
                preferred_machine = ?,
                cycle_time = ?,
                setup_time = ?
            WHERE bom_id = ?
              AND op_seq_id = ?
            """,
            (
                next_source_op_no,
                next_operation_name,
                machine_category,
                preferred_machine,
                float(cycle_minutes_per_qty),
                float(setup_minutes),
                bom_id,
                source_op_seq_id,
            ),
        )
        current_operation = one(
            con.execute(
                """
                SELECT operation_id, status
                FROM operation
                WHERE COALESCE(source_ps_id, '') = ?
                  AND COALESCE(pp_partial_no, '') = ?
                  AND COALESCE(selected_bom_id, 0) = ?
                  AND COALESCE(source_op_seq_id, 0) = ?
                  AND COALESCE(source_op_no, '') = ?
                LIMIT 1
                """,
                (source_ps_id, pp_partial_no, bom_id, source_op_seq_id, next_source_op_no),
            )
        )
        next_status = compact_text(current_operation.get("status") if current_operation else "ACTIVE") or "ACTIVE"
        if is_active is not None:
            next_status = 'ACTIVE' if bool(is_active) else 'INACTIVE'
        if current_operation:
            con.execute(
                """
                UPDATE operation
                SET operation_name = ?,
                    job_no = ?,
                    pp_partial_no = ?,
                    selected_bom_id = ?,
                    setup_minutes = ?,
                    cycle_minutes_per_qty = ?,
                    compatible_machine_group = ?,
                    source_op_seq_id = ?,
                    source_op_no = ?,
                    status = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE operation_id = ?
                """,
                (
                    _planner_strip_op_prefix(next_source_op_no, next_operation_name),
                    source_ps_id,
                    pp_partial_no,
                    bom_id,
                    float(setup_minutes),
                    float(cycle_minutes_per_qty),
                    machine_category or "UNKNOWN",
                    source_op_seq_id,
                    next_source_op_no,
                    next_status,
                    int(current_operation["operation_id"] or 0),
                ),
            )
        else:
            cur = con.execute(
                """
                INSERT INTO operation (
                  job_no, operation_name, total_qty, setup_minutes, cycle_minutes_per_qty, compatible_machine_group,
                  source_ps_id, pp_partial_no, selected_bom_id, source_op_seq_id, source_op_no, status, remarks, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    source_ps_id,
                    _planner_strip_op_prefix(next_source_op_no, next_operation_name),
                    0.0,
                    float(setup_minutes),
                    float(cycle_minutes_per_qty),
                    machine_category or "UNKNOWN",
                    source_ps_id,
                    pp_partial_no,
                    bom_id,
                    source_op_seq_id,
                    next_source_op_no,
                    next_status,
                    "",
                ),
            )
            current_operation = {"operation_id": int(cur.lastrowid), "status": next_status}
        if is_active is not None:
            status = 'ACTIVE' if bool(is_active) else 'INACTIVE'
            con.execute(
                """
                UPDATE operation
                SET status = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE COALESCE(selected_bom_id, 0) = ?
                  AND COALESCE(source_op_seq_id, 0) = ?
                  AND COALESCE(source_op_no, '') = ?
                """,
                (status, bom_id, source_op_seq_id, compact_text(next_source_op_no)),
            )
        affected_machine_ids = sync_operations_for_flow(con, bom_id)
        for machine_id in affected_machine_ids:
            recalculate_machine(con, machine_id)
        planning_run_id = recalculate_planning_all_baseline(con, reason="BOM_OPERATION_EDIT")
        summary = refresh_process_sheet_completion(con, source_ps_id, pp_partial_no, bom_id)
        refresh_planner_alerts(con)
        operations = _planner_operation_editor_rows(con, source_ps_id, pp_partial_no, bom_id)
        current = next((row for row in operations if int(row.get("op_seq_id") or 0) == source_op_seq_id), None)
        warnings = []
        if machine_category and compact_text(step_row.get("machine_category") or "") != machine_category:
            if _planner_operation_history_flags(con, int(op_row.get("operation_id") or 0)).get("has_planned_blocks"):
                warnings.append("This operation has planned blocks and was not deleted. Review machine compatibility on existing plans.")
        return jsonify(
            {
                "ok": True,
                "mode": "updated",
                "message": "Operation saved.",
                "planning_run_id": planning_run_id,
                "source_ps_id": source_ps_id,
                "pp_partial_no": pp_partial_no,
                "bom_id": bom_id,
                "operation": current,
                "operations": operations,
                "summary": summary,
                "warnings": warnings,
            }
        )


@trial_bp.post("/api/trial/planner/source-ps/<path:source_ps_id>/operations/reorder")
def api_trial_planner_operations_reorder(source_ps_id):
    data = request.get_json(force=True, silent=True) or {}
    source_ps_id = compact_text(source_ps_id)
    pp_partial_no = compact_text(data.get("pp_partial_no") or data.get("partial_no") or "")
    bom_id = int(data.get("bom_id") or 0)
    operation_ids = [int(value or 0) for value in (data.get("operation_ids") or data.get("operation_seq_ids") or []) if int(value or 0) > 0]
    if not source_ps_id:
        return jsonify({"error": "source_ps_id is required"}), 400
    if not bom_id:
        return jsonify({"error": "bom_id is required"}), 400
    if not operation_ids:
        return jsonify({"error": "operation_ids are required"}), 400
    with db() as con:
        ps_row = _planner_process_sheet_row(con, source_ps_id, pp_partial_no)
        if not ps_row:
            return jsonify({"error": "Process sheet not found"}), 404
        if not pp_partial_no:
            pp_partial_no = compact_text(ps_row.get("pp_partial_no") or ps_row.get("partial_no") or "")
        bom_options = _planner_bom_options_for_part(con, ps_row.get("part_id") or 0)
        if bom_options and not any(int(option.get("bom_id") or 0) == int(bom_id or 0) for option in bom_options):
            return jsonify({"error": "bom_id is not valid for this process sheet"}), 400
        steps = _planner_operation_editor_rows(con, source_ps_id, pp_partial_no, bom_id)
        seq_by_operation_id = {int(row.get("operation_id") or 0): int(row.get("op_seq_id") or 0) for row in steps if int(row.get("operation_id") or 0) > 0}
        ordered_seq_ids = []
        for operation_id in operation_ids:
            seq_id = int(seq_by_operation_id.get(operation_id) or 0)
            if seq_id and seq_id not in ordered_seq_ids:
                ordered_seq_ids.append(seq_id)
        if not ordered_seq_ids:
            return jsonify({"error": "No valid operation ids found"}), 400
        existing_seq_ids = [int(row["op_seq_id"] or 0) for row in rows(con.execute("SELECT op_seq_id FROM operation_seq WHERE bom_id = ? ORDER BY seq_no, op_seq_id", (bom_id,)))]
        for seq_id in existing_seq_ids:
            if seq_id not in ordered_seq_ids:
                ordered_seq_ids.append(seq_id)
        con.execute(
            """
            UPDATE operation_seq
            SET seq_no = -ABS(seq_no)
            WHERE bom_id = ?
            """,
            (bom_id,),
        )
        for idx, seq_id in enumerate(ordered_seq_ids, 1):
            con.execute(
                """
                UPDATE operation_seq
                SET seq_no = ?,
                    is_last_op = ?
                WHERE bom_id = ?
                  AND op_seq_id = ?
                """,
                (idx, 1 if idx == len(ordered_seq_ids) else 0, bom_id, seq_id),
            )
        affected_machine_ids = sync_operations_for_flow(con, bom_id)
        for machine_id in affected_machine_ids:
            recalculate_machine(con, machine_id)
        planning_run_id = recalculate_planning_all_baseline(con, reason="BOM_OPERATION_EDIT")
        summary = refresh_process_sheet_completion(con, source_ps_id, pp_partial_no, bom_id)
        refresh_planner_alerts(con)
        operations = _planner_operation_editor_rows(con, source_ps_id, pp_partial_no, bom_id)
        return jsonify(
            {
                "ok": True,
                "mode": "reordered",
                "message": "Operations reordered.",
                "planning_run_id": planning_run_id,
                "source_ps_id": source_ps_id,
                "pp_partial_no": pp_partial_no,
                "bom_id": bom_id,
                "operations": operations,
                "summary": summary,
            }
        )


@trial_bp.delete("/api/trial/planner/operations/<int:operation_id>")
def api_trial_planner_operation_delete(operation_id):
    with db() as con:
        op_row = one(
            con.execute(
                """
                SELECT *
                FROM operation
                WHERE operation_id = ?
                LIMIT 1
                """,
                (int(operation_id),),
            )
        )
        if not op_row:
            return jsonify({"error": "Operation not found"}), 404
        op_row = dict(op_row)
        source_ps_id = compact_text(op_row.get("source_ps_id") or "")
        pp_partial_no = compact_text(op_row.get("pp_partial_no") or "")
        bom_id = int(op_row.get("selected_bom_id") or 0)
        source_op_seq_id = int(op_row.get("source_op_seq_id") or 0)
        source_op_no = compact_text(op_row.get("source_op_no") or "")
        if not bom_id or not source_op_seq_id:
            return jsonify({"error": "Operation context is incomplete"}), 400
        matching_operation_ids = [
            int(row["operation_id"] or 0)
            for row in rows(
                con.execute(
                    """
                    SELECT operation_id
                    FROM operation
                    WHERE COALESCE(selected_bom_id, 0) = ?
                      AND COALESCE(source_op_seq_id, 0) = ?
                      AND COALESCE(source_op_no, '') = ?
                    """,
                    (bom_id, source_op_seq_id, source_op_no),
                )
            )
        ]
        history_flags = _planner_operation_history_flags(con, int(operation_id))
        con.execute(
            """
            UPDATE operation
            SET status = 'INACTIVE',
                updated_at = CURRENT_TIMESTAMP
            WHERE COALESCE(selected_bom_id, 0) = ?
              AND COALESCE(source_op_seq_id, 0) = ?
              AND COALESCE(source_op_no, '') = ?
            """,
            (bom_id, source_op_seq_id, source_op_no),
        )
        affected_machine_ids = {
            int(row["machine_id"])
            for row in rows(
                con.execute(
                    f"""
                    SELECT DISTINCT machine_id
                    FROM run_block
                    WHERE operation_id IN ({",".join("?" for _ in matching_operation_ids)}) AND COALESCE(active, 1) = 1
                    """,
                    matching_operation_ids,
                )
            )
        } if matching_operation_ids else set()
        for machine_id in affected_machine_ids:
            recalculate_machine(con, machine_id)
        planning_run_id = recalculate_planning_all_baseline(con, reason="BOM_OPERATION_EDIT")
        summary = refresh_process_sheet_completion(con, source_ps_id, pp_partial_no, bom_id)
        refresh_planner_alerts(con)
        operations = _planner_operation_editor_rows(con, source_ps_id, pp_partial_no, bom_id)
        return jsonify(
            {
                "ok": True,
                "mode": "deactivated",
                "message": "Operation has history, so it was deactivated instead of deleted." if history_flags.get("has_planned_blocks") or history_flags.get("has_actual_output") else "Operation was deactivated.",
                "planning_run_id": planning_run_id,
                "source_ps_id": source_ps_id,
                "pp_partial_no": pp_partial_no,
                "bom_id": bom_id,
                "operation_ids": matching_operation_ids,
                "operations": operations,
                "summary": summary,
                "warnings": [],
            }
        )
