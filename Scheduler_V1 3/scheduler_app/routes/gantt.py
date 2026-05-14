from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, request

from ..db import db, one, rows, parse_dt_text
from ..machines import fetch_machines, gantt_off_time_blocks, machine_capacity_for_date
from ..visual_time import break_windows_for_date, visual_timing_for_segment
from ..utils import compact_text

trial_gantt_bp = Blueprint("trial_gantt", __name__)


def _date_range(start_iso, end_iso):
    start_d = date.fromisoformat(start_iso)
    end_d = date.fromisoformat(end_iso)
    if end_d < start_d:
        start_d, end_d = end_d, start_d
    days = (end_d - start_d).days + 1
    return [(start_d + timedelta(days=i)).isoformat() for i in range(days)]


def _minute_of_day(value):
    dt = parse_dt_text(value)
    if not dt:
        return 0
    return dt.hour * 60 + dt.minute


def _split_source_ps_id(source_ps_id):
    text = compact_text(source_ps_id)
    if "::" not in text:
        return text, "1"
    base, partial = text.rsplit("::", 1)
    return base or text, partial or "1"


def _display_ids(source_ps_id, display_source_ps_id=None, display_partial_no=None):
    source_base, source_partial = _split_source_ps_id(source_ps_id)
    display_ps_id = compact_text(display_source_ps_id) or source_base
    display_partial_no = compact_text(display_partial_no) or source_partial or "1"
    return display_ps_id, display_partial_no


def _calendar_rows(con, start_iso, end_iso, machines):
    calendar = []
    for iso_day in _date_range(start_iso, end_iso):
        work_day = date.fromisoformat(iso_day)
        per_machine = [machine_capacity_for_date(con, machine["machine_id"], work_day) for machine in machines]
        capacity_minutes = sum(int(cap.get("capacity_minutes") or 0) for cap in per_machine)
        profiles = sorted({compact_text(cap.get("profile_name")) for cap in per_machine if compact_text(cap.get("profile_name"))})
        notes = sorted({compact_text(cap.get("note")) for cap in per_machine if compact_text(cap.get("note"))})
        calendar.append(
            {
                "work_date": iso_day,
                "is_working_day": 1 if any(int(cap.get("capacity_minutes") or 0) > 0 for cap in per_machine) else 0,
                "capacity_minutes": capacity_minutes,
                "profile_name": ", ".join(profiles) if profiles else "OFF",
                "note": "; ".join(notes),
            }
        )
    return calendar


@trial_gantt_bp.get("/api/trial/gantt")
def api_trial_gantt():
    start_iso = compact_text(request.args.get("from")) or date.today().isoformat()
    end_iso = compact_text(request.args.get("to")) or (date.today() + timedelta(days=7)).isoformat()
    include_completed = compact_text(request.args.get("include_completed")).lower() in {"1", "true", "yes", "on"}
    try:
        _date_range(start_iso, end_iso)
    except ValueError:
        return jsonify({"error": "Invalid date range"}), 400

    with db() as con:
        machines = [dict(row) for row in fetch_machines(con)]
        machine_by_id = {int(machine.get("machine_id") or 0): machine for machine in machines}
        calendar = _calendar_rows(con, start_iso, end_iso, machines)
        dates = [row["work_date"] for row in calendar]
        off_time_blocks = gantt_off_time_blocks(machines, dates, con)
        calendar_windows = [dict(row) for row in rows(
            con.execute(
                """
                SELECT w.*, m.machine_code
                FROM machine_calendar_window w
                LEFT JOIN machines m ON m.machine_id = w.machine_id
                WHERE w.active = 1
                  AND w.start_at < ?
                  AND w.end_at > ?
                ORDER BY w.start_at, w.window_id
                """,
                (f"{end_iso} 23:59:59", f"{start_iso} 00:00:00"),
            )
        )]
        for window in calendar_windows:
            window_type = compact_text(window.get("window_type") or "").upper()
            window["display_kind"] = "available" if window_type in {"OVERTIME", "AVAILABLE"} else "blocked"
        break_windows = [
            {
                "work_date": work_date,
                "windows": break_windows_for_date(
                    date.fromisoformat(work_date),
                ),
            }
            for work_date in dates
        ]

        blocks = rows(
            con.execute(
                """
                WITH actual_by_block AS (
                    SELECT
                        block_id,
                        COALESCE(SUM(COALESCE(output_qty, 0)), 0) AS output_qty,
                        COALESCE(SUM(COALESCE(reject_qty, 0)), 0) AS reject_qty,
                        COALESCE(SUM(COALESCE(output_qty, 0) - COALESCE(reject_qty, 0)), 0) AS good_qty,
                        COUNT(actual_id) AS actual_report_count
                    FROM production_actual
                    WHERE COALESCE(status, 'ACTIVE') = 'ACTIVE'
                    GROUP BY block_id
                )
                SELECT
                    s.segment_id,
                    s.block_id,
                    b.operation_id,
                    b.machine_id,
                    m.machine_code,
                    m.machine_category,
                    m.shift_profile,
                    s.segment_date AS plan_date,
                    s.start_datetime,
                    s.end_datetime,
                    CAST(strftime('%H', s.start_datetime) AS INTEGER) * 60 + CAST(strftime('%M', s.start_datetime) AS INTEGER) AS start_min,
                    CAST(strftime('%H', s.end_datetime) AS INTEGER) * 60 + CAST(strftime('%M', s.end_datetime) AS INTEGER) AS end_min,
                    s.segment_type,
                    s.qty_done AS qty,
                    s.minutes_used,
                    b.scheduled_qty,
                    COALESCE(ab.output_qty, 0) AS output_qty,
                    COALESCE(ab.reject_qty, 0) AS reject_qty,
                    COALESCE(ab.good_qty, 0) AS good_qty,
                    COALESCE(ab.actual_report_count, 0) AS actual_report_count,
                    b.status,
                    b.planning_status,
                    b.execution_status,
                    ps.total_qty AS ps_total_qty,
                    COALESCE(ab.good_qty, 0) AS ps_finished_qty,
                    ps.status AS ps_status,
                    ps.planner_status AS ps_planner_status,
                    pfs.is_last_op AS is_last_op,
                    o.source_ps_id,
                    o.source_op_seq_id AS source_op_seq_id,
                    o.source_op_no,
                    o.job_no,
                    o.operation_name,
                    b.group_id,
                    g.group_label,
                    b.block_type,
                    b.remarks,
                    b.calculated_start_datetime,
                    b.calculated_end_datetime,
                    ps.source_ps_id AS display_source_ps_id,
                    ps.pp_partial_no AS display_partial_no
                FROM run_block_segment s
                JOIN run_block b ON b.block_id = s.block_id
                JOIN operation o ON o.operation_id = b.operation_id
                JOIN machines m ON m.machine_id = b.machine_id
                LEFT JOIN operation_seq pfs ON pfs.op_seq_id = o.source_op_seq_id
                LEFT JOIN process_sheet ps ON ps.ps_id = o.source_ps_id
                LEFT JOIN run_block_group g ON g.group_id = b.group_id
                LEFT JOIN actual_by_block ab ON ab.block_id = b.block_id
                WHERE s.segment_date BETWEEN ? AND ?
                  AND COALESCE(b.active, 1) = 1
                  AND (? = 1 OR COALESCE(b.status, '') <> 'COMPLETED')
                ORDER BY s.segment_date, b.machine_id, s.start_datetime, s.segment_id
                """,
                (start_iso, end_iso, 1 if include_completed else 0),
            )
        )

        actual_by_segment = {
            int(row["segment_id"]): {
                "output_qty": float(row["output_qty"] or 0),
                "reject_qty": float(row["reject_qty"] or 0),
                "good_qty": float(row["good_qty"] or 0),
                "actual_report_count": int(row["actual_report_count"] or 0),
                "target_qty_at_report": float(row["target_qty_at_report"] or 0),
            }
            for row in rows(
                con.execute(
                """
                SELECT
                    segment_id,
                    COALESCE(SUM(COALESCE(output_qty, 0)), 0) AS output_qty,
                    COALESCE(SUM(COALESCE(reject_qty, 0)), 0) AS reject_qty,
                    COALESCE(SUM(COALESCE(output_qty, 0) - COALESCE(reject_qty, 0)), 0) AS good_qty,
                    COUNT(actual_id) AS actual_report_count,
                    MAX(COALESCE(target_qty_at_report, 0)) AS target_qty_at_report
                FROM production_actual
                WHERE segment_id IS NOT NULL
                  AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
                GROUP BY segment_id
                """,
            )
        )
        }

        blocks_by_block_id = {}
        blocks_by_ps = {}
        payload_blocks = []
        for row in blocks:
            item = dict(row)
            machine = machine_by_id.get(int(item.get("machine_id") or 0), {})
            shift_profile = compact_text(machine.get("shift_profile") or item.get("shift_profile") or "")
            start_dt = parse_dt_text(item.get("start_datetime"))
            end_dt = parse_dt_text(item.get("end_datetime"))
            work_date = start_dt.date() if start_dt else None
            timing = visual_timing_for_segment(
                start_dt,
                item.get("minutes_used") or 0,
                end_dt=end_dt,
                work_date=work_date,
                profile_name="",
                shift_profile=shift_profile,
                segment_type=item.get("segment_type") or "production",
            )
            display_ps_id, display_partial_no = _display_ids(
                item.get("source_ps_id"),
                item.get("display_source_ps_id"),
                item.get("display_partial_no"),
            )
            item["display_ps_id"] = display_ps_id
            item["display_partial_no"] = display_partial_no
            item["pp_partial_no"] = display_partial_no
            item["source_ps_id"] = compact_text(item.get("source_ps_id"))
            item["plan_date"] = item.get("plan_date") or ""
            item["start_min"] = int(item.get("start_min") or 0)
            item["end_min"] = int(item.get("end_min") or 0)
            item["visual_start_datetime"] = timing["visual_start_datetime"]
            item["visual_end_datetime"] = timing["visual_end_datetime"]
            item["visual_start_min"] = _minute_of_day(timing["visual_start_datetime"])
            item["visual_end_min"] = _minute_of_day(timing["visual_end_datetime"])
            item["visual_parts"] = timing["visual_parts"]
            item["break_windows"] = timing["break_windows"]
            item["shift_profile"] = shift_profile
            item["qty"] = float(item.get("qty") or 0)
            item["minutes_used"] = float(item.get("minutes_used") or 0)
            item["scheduled_qty"] = float(item.get("scheduled_qty") or 0)
            item["output_qty"] = float(item.get("output_qty") or 0)
            item["reject_qty"] = float(item.get("reject_qty") or 0)
            item["good_qty"] = float(item.get("good_qty") or max(0.0, item["output_qty"] - item["reject_qty"]))
            item["actual_report_count"] = int(item.get("actual_report_count") or 0)
            item["op_output_qty"] = float(item.get("good_qty") or 0)
            item["op_scheduled_qty"] = float(item.get("scheduled_qty") or 0)
            item["ps_total_qty"] = float(item.get("ps_total_qty") or 0)
            item["ps_finished_qty"] = float(item.get("ps_finished_qty") or 0)
            item["ps_status"] = compact_text(item.get("ps_status") or "")
            item["ps_planner_status"] = compact_text(item.get("ps_planner_status") or "")
            item["segment_output_qty"] = float(actual_by_segment.get(int(item.get("segment_id") or 0), {}).get("good_qty", 0))
            item["segment_reject_qty"] = float(actual_by_segment.get(int(item.get("segment_id") or 0), {}).get("reject_qty", 0))
            item["segment_actual_report_count"] = int(actual_by_segment.get(int(item.get("segment_id") or 0), {}).get("actual_report_count", 0))
            item["segment_target_qty_at_report"] = float(actual_by_segment.get(int(item.get("segment_id") or 0), {}).get("target_qty_at_report", 0))
            item["is_last_op"] = int(item.get("is_last_op") or 0)
            item["group_label"] = item.get("group_label") or ""
            item["remarks"] = item.get("remarks") or ""
            item["actuals"] = []
            item["segment_label"] = "Setup" if compact_text(item.get("segment_type")).lower() == "setup" else "Production"
            item["op_finished"] = bool(
                compact_text(item.get("execution_status") or item.get("planning_status") or item.get("status") or "").upper() in {"COMPLETED", "DONE"}
                or (item["op_scheduled_qty"] > 0 and item["op_output_qty"] + 1e-9 >= item["op_scheduled_qty"])
            )
            item["ps_finished"] = bool(
                compact_text(item.get("ps_status") or item.get("ps_planner_status") or "").upper() == "COMPLETED"
                or (item["ps_total_qty"] > 0 and item["ps_finished_qty"] + 1e-9 >= item["ps_total_qty"])
            )
            blocks_by_block_id.setdefault(int(item.get("block_id") or 0), []).append(item)
            ps_key = item["source_ps_id"]
            if ps_key:
                blocks_by_ps.setdefault(ps_key, []).append(item)
            payload_blocks.append(item)

        def _segment_sort_key(item):
            return (
                item.get("plan_date") or "",
                item.get("start_datetime") or "",
                int(item.get("segment_id") or 0),
            )

        def _marker_segment_for_block(segments, scheduled_qty, completed):
            production_segments = [segment for segment in segments if compact_text(segment.get("segment_type")).lower() != "setup"]
            ordered_segments = sorted(production_segments or segments, key=_segment_sort_key)
            cumulative = 0.0
            for segment in ordered_segments:
                cumulative += float(segment.get("segment_output_qty") or 0)
                if scheduled_qty > 0 and cumulative + 1e-9 >= scheduled_qty:
                    return int(segment.get("segment_id") or 0)
            if completed and ordered_segments:
                return int(ordered_segments[-1].get("segment_id") or 0)
            return 0

        for block_id, block_segments in blocks_by_block_id.items():
            scheduled_qty = max([float(seg.get("op_scheduled_qty") or 0) for seg in block_segments] or [0.0])
            completed = any(bool(seg.get("op_finished")) for seg in block_segments)
            marker_segment_id = _marker_segment_for_block(block_segments, scheduled_qty, completed)
            if not marker_segment_id and completed and block_segments:
                marker_segment_id = int(sorted(block_segments, key=_segment_sort_key)[-1].get("segment_id") or 0)
            for seg in block_segments:
                seg["op_finished_marker_segment_id"] = marker_segment_id
                seg["op_finished_marker"] = int(seg.get("segment_id") or 0) == marker_segment_id and marker_segment_id > 0

        final_op_seq_by_ps = {}
        for ps_key, ps_segments in blocks_by_ps.items():
            final_candidates = [seg for seg in ps_segments if int(seg.get("is_last_op") or 0) == 1]
            final_op_seq_by_ps[ps_key] = max([int(seg.get("source_op_seq_id") or 0) for seg in (final_candidates or ps_segments)] or [0])

        for ps_key, ps_segments in blocks_by_ps.items():
            final_op_seq_id = int(final_op_seq_by_ps.get(ps_key) or 0)
            final_segments = [seg for seg in ps_segments if int(seg.get("source_op_seq_id") or 0) == final_op_seq_id]
            final_segments = [seg for seg in final_segments if compact_text(seg.get("segment_type")).lower() != "setup"] or final_segments
            ordered_final_segments = sorted(final_segments, key=_segment_sort_key)
            ps_total_qty = max([float(seg.get("ps_total_qty") or 0) for seg in ps_segments] or [0.0])
            ps_finished_qty = max([float(seg.get("ps_finished_qty") or 0) for seg in ps_segments] or [0.0])
            ps_completed = any(bool(seg.get("ps_finished")) for seg in ps_segments)
            cumulative = 0.0
            marker_segment_id = 0
            for seg in ordered_final_segments:
                cumulative += float(seg.get("segment_output_qty") or 0)
                if ps_total_qty > 0 and cumulative + 1e-9 >= ps_total_qty:
                    marker_segment_id = int(seg.get("segment_id") or 0)
                    break
            if not marker_segment_id and (ps_completed or (ps_total_qty > 0 and ps_finished_qty + 1e-9 >= ps_total_qty)) and ordered_final_segments:
                marker_segment_id = int(ordered_final_segments[-1].get("segment_id") or 0)
            for seg in ps_segments:
                seg["is_final_ps_operation"] = int(seg.get("source_op_seq_id") or 0) == final_op_seq_id and final_op_seq_id > 0
                seg["ps_finished_marker_segment_id"] = marker_segment_id
                seg["ps_finished_marker"] = int(seg.get("segment_id") or 0) == marker_segment_id and marker_segment_id > 0

        if compact_text(request.args.get("debug_finish")).lower() in {"1", "true", "yes", "on"}:
            for block_id, block_segments in list(blocks_by_block_id.items())[:40]:
                for seg in block_segments:
                    print(
                        "[gantt finish debug]",
                        "block_id=", block_id,
                        "segment_id=", int(seg.get("segment_id") or 0),
                        "op_finished=", bool(seg.get("op_finished")),
                        "op_finished_marker=", bool(seg.get("op_finished_marker")),
                        "ps_finished=", bool(seg.get("ps_finished")),
                        "ps_finished_marker=", bool(seg.get("ps_finished_marker")),
                        "is_final_ps_operation=", bool(seg.get("is_final_ps_operation")),
                    )

        return jsonify(
            {
                "from": start_iso,
                "to": end_iso,
                "dates": dates,
                "machines": machines,
                "calendar": calendar,
                "break_windows": break_windows,
                "off_time_blocks": off_time_blocks,
                "calendar_windows": calendar_windows,
                "blocks": payload_blocks,
            }
        )
