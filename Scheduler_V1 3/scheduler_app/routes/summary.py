from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, request

from ..db import db, one, rows, parse_dt_text
from ..machines import capacity_minutes_for_machine_day, fetch_machines
from ..utils import compact_text

trial_summary_bp = Blueprint("trial_summary", __name__)


def _range_dates(start, end):
    try:
        start_d = date.fromisoformat(compact_text(start) or date.today().isoformat())
    except Exception:
        start_d = date.today()
    try:
        end_d = date.fromisoformat(compact_text(end) or start_d.isoformat())
    except Exception:
        end_d = start_d
    if end_d < start_d:
        start_d, end_d = end_d, start_d
    return start_d, end_d


def _date_span(start_d, end_d):
    days = (end_d - start_d).days + 1
    return [start_d + timedelta(days=i) for i in range(days)]


def _split_ps_key(ps_id):
    text = compact_text(ps_id)
    if "::" not in text:
        return text, "1"
    base, partial = text.rsplit("::", 1)
    return base or text, partial or "1"


def _display_ids(backend_ps_id, display_source_ps_id="", display_partial_no=""):
    base, partial = _split_ps_key(backend_ps_id)
    display_ps_id = compact_text(display_source_ps_id) or base
    display_partial_no = compact_text(display_partial_no) or partial or "1"
    return display_ps_id or base, display_partial_no


def _time_text(value):
    dt = parse_dt_text(value)
    if not dt:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _sum_capacity_minutes(con, machine_row, start_d, end_d):
    total = 0
    for work_day in _date_span(start_d, end_d):
        cap = capacity_minutes_for_machine_day(con, int(machine_row["machine_id"]), work_day)
        total += int(cap.get("capacity_minutes") or 0)
    return total


def _next_available_text(con, machine_row, end_d):
    probe = end_d + timedelta(days=1)
    for _ in range(370):
        cap = capacity_minutes_for_machine_day(con, int(machine_row["machine_id"]), probe)
        if int(cap.get("capacity_minutes") or 0) > 0:
            start_minute = int(cap.get("start_minute") or 0)
            return f"{probe.isoformat()} {start_minute // 60:02d}:{start_minute % 60:02d}"
        probe += timedelta(days=1)
    return ""


def _parse_dt_safe(value):
    return parse_dt_text(value) if compact_text(value) else None


def _max_dt_text(values):
    parsed = [dt for dt in (_parse_dt_safe(value) for value in values) if dt]
    if not parsed:
        return ""
    return max(parsed).strftime("%Y-%m-%d %H:%M:%S")


def _date_in_range(text, start_d, end_d):
    try:
        value = date.fromisoformat(compact_text(text)[:10])
    except Exception:
        return False
    return start_d <= value <= end_d


def _datetime_overlaps_range(start_text, end_text, start_d, end_d):
    start_dt = _parse_dt_safe(start_text)
    end_dt = _parse_dt_safe(end_text)
    range_start = datetime.combine(start_d, datetime.min.time())
    range_end = datetime.combine(end_d, datetime.max.time())
    if start_dt and end_dt:
        return start_dt <= range_end and end_dt >= range_start
    if start_dt:
        return start_dt.date() <= end_d and start_dt.date() >= start_d
    if end_dt:
        return end_dt.date() >= start_d and end_dt.date() <= end_d
    return False


def _capacity_day_details(con, machine_id, work_day):
    row = one(
        con.execute(
            """
            SELECT d.day_id, d.machine_id, d.work_date, d.profile_id, d.capacity_minutes, d.start_minute, d.note,
                   p.profile_name
            FROM machine_capacity_day d
            JOIN capacity_profile p ON p.profile_id = d.profile_id
            WHERE d.machine_id = ? AND d.work_date = ?
            """,
            (int(machine_id), work_day.isoformat()),
        )
    )
    if row:
        return {
            "profile_name": compact_text(row.get("profile_name")) or "",
            "capacity_minutes": int(row.get("capacity_minutes") or 0),
            "start_minute": int(row.get("start_minute") or 0),
            "note": compact_text(row.get("note") or ""),
            "has_capacity_day_override": True,
        }
    fallback = capacity_minutes_for_machine_day(con, machine_id, work_day)
    return {
        "profile_name": compact_text(fallback.get("profile_name")) or "",
        "capacity_minutes": int(fallback.get("capacity_minutes") or 0),
        "start_minute": int(fallback.get("start_minute") or 0),
        "note": compact_text(fallback.get("note") or ""),
        "has_capacity_day_override": False,
    }


@trial_summary_bp.get("/api/trial/summary")
def api_trial_summary():
    view = compact_text(request.args.get("view")).lower()
    start = request.args.get("from") or date.today().isoformat()
    end = request.args.get("to") or date.today().isoformat()
    category = compact_text(request.args.get("category"))
    start_d, end_d = _range_dates(start, end)

    with db() as con:
        all_machines = [dict(row) for row in fetch_machines(con)]
        machine_types = sorted({compact_text(row.get("machine_category")) for row in all_machines if compact_text(row.get("machine_category"))})
        machines = all_machines if not category or category == "all" else [m for m in all_machines if compact_text(m.get("machine_category")) == category]
        machine_ids = [int(row["machine_id"]) for row in machines]
        if not machine_ids:
            payload = {"rows": [], "machine_types": machine_types, "totals": {
                "machine_count": 0,
                "active_machine_count": 0,
                "calendar_hours": 0.0,
                "total_planned_hours": 0.0,
                "total_available_hours": 0.0,
                "utilization_pct": 0.0,
                "idle_hours": 0.0,
                "scheduled_qty": 0.0,
                "output_qty": 0.0,
                "reject_qty": 0.0,
                "schedule_completion_pct": 0.0,
                "order_completion_pct": 0.0,
                "best_machine": None,
            }}
            return jsonify(payload)

        q_marks = ",".join("?" for _ in machine_ids)
        actual_by_block = {
            int(row["block_id"]): {
                "output_qty": float(row["output_qty"] or 0),
                "reject_qty": float(row["reject_qty"] or 0),
                "good_qty": float(row["good_qty"] or 0),
                "actual_report_count": int(row["actual_report_count"] or 0),
            }
            for row in rows(
                con.execute(
                    """
                    SELECT block_id,
                           COALESCE(SUM(COALESCE(output_qty, 0)), 0) AS output_qty,
                           COALESCE(SUM(COALESCE(reject_qty, 0)), 0) AS reject_qty,
                           COALESCE(SUM(COALESCE(output_qty, 0) - COALESCE(reject_qty, 0)), 0) AS good_qty,
                           COUNT(actual_id) AS actual_report_count
                    FROM production_actual
                    WHERE COALESCE(status, 'ACTIVE') = 'ACTIVE'
                      AND report_date BETWEEN ? AND ?
                    GROUP BY block_id
                    """,
                    (start_d.isoformat(), end_d.isoformat()),
                )
            )
        }

        segment_rows = rows(
            con.execute(
                f"""
                SELECT
                    s.segment_id,
                    s.block_id,
                    s.segment_date,
                    s.segment_type,
                    s.qty_done,
                    s.minutes_used,
                    s.start_datetime,
                    s.end_datetime,
                    b.machine_id,
                    b.queue_position,
                    b.scheduled_qty,
                    b.status,
                    b.planning_status,
                    b.execution_status,
                    b.calculated_end_datetime,
                    b.block_type,
                    b.remarks,
                    b.group_id,
                    o.job_no,
                    o.operation_name,
                    o.total_qty AS operation_total_qty,
                    o.setup_minutes AS op_setup_minutes,
                    o.cycle_minutes_per_qty AS op_cycle_minutes_per_qty,
                    o.source_ps_id AS backend_ps_id,
                    o.source_op_seq_id,
                    o.source_op_no,
                    o.source_op_seq_id AS source_op_seq_id,
                    pfs.seq_no AS source_step_seq_no,
                    pfs.setup_time AS seq_setup_time,
                    pfs.cycle_time AS seq_cycle_time,
                    pfs.is_last_op AS is_last_op,
                    m.machine_code,
                    m.machine_category,
                    m.shift_profile,
                    ps.source_ps_id AS display_source_ps_id,
                    ps.pp_partial_no AS display_partial_no,
                    ps.total_qty AS ps_total_qty,
                    ps.status AS ps_status,
                    ps.planner_status AS ps_planner_status,
                    ps.due_date AS ps_due_date,
                    ps.planned_qty AS ps_planned_qty,
                    g.group_label,
                    COALESCE(ab.output_qty, 0) AS output_qty,
                    COALESCE(ab.reject_qty, 0) AS reject_qty,
                    COALESCE(ab.good_qty, 0) AS ps_finished_qty,
                    COALESCE(ab.actual_report_count, 0) AS actual_report_count
                FROM run_block_segment s
                JOIN run_block b ON b.block_id = s.block_id
                JOIN operation o ON o.operation_id = b.operation_id
                LEFT JOIN operation_seq pfs ON pfs.op_seq_id = o.source_op_seq_id
                JOIN machines m ON m.machine_id = b.machine_id
                LEFT JOIN process_sheet ps ON ps.ps_id = o.source_ps_id
                LEFT JOIN run_block_group g ON g.group_id = b.group_id
                LEFT JOIN (
                    SELECT block_id,
                           COALESCE(SUM(COALESCE(output_qty, 0)), 0) AS output_qty,
                           COALESCE(SUM(COALESCE(reject_qty, 0)), 0) AS reject_qty,
                           COALESCE(SUM(COALESCE(output_qty, 0) - COALESCE(reject_qty, 0)), 0) AS good_qty,
                           COUNT(actual_id) AS actual_report_count
                    FROM production_actual
                    WHERE COALESCE(status, 'ACTIVE') = 'ACTIVE'
                      AND report_date BETWEEN ? AND ?
                    GROUP BY block_id
                ) ab ON ab.block_id = b.block_id
                WHERE s.segment_date BETWEEN ? AND ?
                  AND b.machine_id IN ({q_marks})
                  AND COALESCE(b.active, 1) = 1
                ORDER BY b.machine_id, s.segment_date, s.start_datetime, s.segment_id
                """,
                (start_d.isoformat(), end_d.isoformat(), start_d.isoformat(), end_d.isoformat(), *machine_ids),
            )
        )

        by_machine = {
            int(machine["machine_id"]): {
                "machine_id": int(machine["machine_id"]),
                "machine_code": compact_text(machine.get("machine_code")) or "UNKNOWN",
                "machine_category": compact_text(machine.get("machine_category")) or "UNKNOWN",
                "shift_profile": compact_text(machine.get("shift_profile")) or "STANDARD",
                "ps_ids": set(),
                "planned_minutes": 0.0,
                "actual_used_minutes": 0.0,
                "scheduled_qty": 0.0,
                "output_qty": 0.0,
                "reject_qty": 0.0,
                "details_map": {},
            }
            for machine in machines
        }
        ps_map = {}
        heatmap_map = {}
        heatmap_seen_segments = set()

        for row in segment_rows:
            machine_id = int(row["machine_id"] or 0)
            bucket = by_machine.get(machine_id)
            if not bucket:
                continue

            backend_ps_id = compact_text(row.get("backend_ps_id"))
            display_ps_id, display_partial_no = _display_ids(
                backend_ps_id,
                row.get("display_source_ps_id"),
                row.get("display_partial_no"),
            )
            block_id = int(row.get("block_id") or 0)
            start_text = _time_text(row.get("start_datetime"))
            end_text = _time_text(row.get("end_datetime"))
            planned_qty = float(row.get("qty_done") or 0) if compact_text(row.get("segment_type")).lower() == "production" else 0.0
            detail = bucket["details_map"].setdefault(
                block_id,
                {
                    "display_ps_id": display_ps_id,
                    "display_partial_no": display_partial_no,
                    "backend_ps_id": backend_ps_id,
                    "sheet_id": backend_ps_id,
                    "source_op_no": compact_text(row.get("source_op_no")),
                    "operation_name": compact_text(row.get("operation_name")),
                    "start_datetime": start_text,
                    "end_datetime": end_text,
                    "start_time": "",
                    "end_time": "",
                    "total_qty": float(row.get("ps_total_qty") or 0),
                    "planned_qty": 0.0,
                    "planned_minutes": 0.0,
                    "setup_minutes": 0.0,
                    "production_minutes": 0.0,
                    "planned_hours": 0.0,
                    "effective_hours": 0.0,
                    "calendar_hours": 0.0,
                    "actual_qty_out": 0.0,
                    "output_qty": 0.0,
                    "reject_qty": 0.0,
                    "segment_count": 0,
                    "schedule_completion_pct": 0.0,
                    "order_completion_pct": 0.0,
                    "block_id": block_id,
                    "segment_id": int(row.get("segment_id") or 0),
                    "machine_code": compact_text(row.get("machine_code")) or "",
                    "segment_type": compact_text(row.get("segment_type")) or "",
                    "actual_report_count": int(row.get("actual_report_count") or 0),
                    "group_label": compact_text(row.get("group_label")),
                    "block_type": compact_text(row.get("block_type")) or "ORIGINAL",
                    "remarks": compact_text(row.get("remarks")),
                    "source_step_seq_no": int(row.get("source_step_seq_no") or 0),
                    "source_op_seq_id": int(row.get("source_op_seq_id") or 0),
                    "job_no": compact_text(row.get("job_no")),
                    "machine_id": machine_id,
                    "plan_date": compact_text(row.get("segment_date")),
                    "status": compact_text(row.get("status")),
                    "planning_status": compact_text(row.get("planning_status")),
                    "execution_status": compact_text(row.get("execution_status")),
                    "calculated_end_datetime": compact_text(row.get("calculated_end_datetime")),
                    "op_setup_minutes": float(row.get("op_setup_minutes") or 0),
                    "op_cycle_minutes_per_qty": float(row.get("op_cycle_minutes_per_qty") or 0),
                    "seq_setup_time": float(row.get("seq_setup_time") or 0),
                    "seq_cycle_time": float(row.get("seq_cycle_time") or 0),
                    "is_last_op": int(row.get("is_last_op") or 0),
                    "ps_due_date": compact_text(row.get("ps_due_date")),
                    "ps_planned_qty": float(row.get("ps_planned_qty") or 0),
                    "ps_finished_qty": float(row.get("ps_finished_qty") or 0),
                    "ps_status": compact_text(row.get("ps_status") or ""),
                    "ps_planner_status": compact_text(row.get("ps_planner_status") or ""),
                },
            )
            if start_text and (not detail["start_time"] or start_text < detail["start_time"]):
                detail["start_time"] = start_text
            if end_text and (not detail["end_time"] or end_text > detail["end_time"]):
                detail["end_time"] = end_text
            detail["planned_qty"] += planned_qty
            segment_minutes = float(row.get("minutes_used") or 0)
            detail["planned_minutes"] += segment_minutes
            detail["planned_hours"] = round(detail["planned_minutes"] / 60.0, 4)
            detail["segment_count"] += 1
            segment_type = compact_text(row.get("segment_type")).lower()
            if segment_type == "setup":
                detail["setup_minutes"] += segment_minutes
            elif segment_type == "production":
                detail["production_minutes"] += segment_minutes
            detail["segment_id"] = min(detail["segment_id"], int(row.get("segment_id") or 0)) if detail["segment_id"] else int(row.get("segment_id") or 0)
            bucket["planned_minutes"] += float(row.get("minutes_used") or 0)
            bucket["scheduled_qty"] += planned_qty
            if backend_ps_id:
                bucket["ps_ids"].add(backend_ps_id)
                ps_entry = ps_map.setdefault(
                    backend_ps_id,
                    {
                        "ps_id": backend_ps_id,
                        "display_ps_id": display_ps_id,
                        "display_partial_no": display_partial_no,
                        "due_date": compact_text(row.get("ps_due_date")),
                        "planned_qty": 0.0,
                        "finished_qty": float(row.get("ps_finished_qty") or 0),
                        "total_qty": float(row.get("ps_total_qty") or 0),
                        "status": compact_text(row.get("ps_status") or ""),
                        "planner_status": compact_text(row.get("ps_planner_status") or ""),
                        "details": [],
                    },
                )
                ps_entry["planned_qty"] += planned_qty
                ps_entry["finished_qty"] = max(ps_entry["finished_qty"], float(row.get("ps_finished_qty") or 0))
                ps_entry["total_qty"] = max(ps_entry["total_qty"], float(row.get("ps_total_qty") or 0))
                if not ps_entry["due_date"]:
                    ps_entry["due_date"] = compact_text(row.get("ps_due_date"))
                ps_entry["details"].append(detail)

            segment_id = int(row.get("segment_id") or 0)
            segment_date = compact_text(row.get("segment_date") or "")
            if machine_id and segment_id and segment_date:
                heatmap_bucket = heatmap_map.setdefault(
                    (machine_id, segment_date),
                    {
                        "machine_id": machine_id,
                        "machine_code": compact_text(row.get("machine_code")) or "UNKNOWN",
                        "machine_category": compact_text(row.get("machine_category")) or "UNKNOWN",
                        "plan_date": segment_date,
                        "capacity_minutes": 0.0,
                        "planned_minutes": 0.0,
                        "setup_minutes": 0.0,
                        "production_minutes": 0.0,
                        "details": [],
                        "detail_ids": set(),
                        "segment_ids": set(),
                        "block_ids": set(),
                        "ps_ids": set(),
                        "op_keys": set(),
                        "planned_segment_count": 0,
                    },
                )
                if segment_id not in heatmap_seen_segments:
                    heatmap_seen_segments.add(segment_id)
                    segment_minutes = float(row.get("minutes_used") or 0)
                    segment_type = compact_text(row.get("segment_type") or "").lower()
                    heatmap_bucket["planned_minutes"] += segment_minutes
                    heatmap_bucket["planned_segment_count"] += 1
                    heatmap_bucket["segment_ids"].add(segment_id)
                    heatmap_bucket["block_ids"].add(int(row.get("block_id") or 0))
                    heatmap_bucket["op_keys"].add(
                        (
                            int(row.get("source_step_seq_no") or 0),
                            int(row.get("source_op_seq_id") or 0),
                            compact_text(row.get("source_op_no") or ""),
                        )
                    )
                    if backend_ps_id:
                        heatmap_bucket["ps_ids"].add(backend_ps_id)
                    if segment_type == "setup":
                        heatmap_bucket["setup_minutes"] += segment_minutes
                    elif segment_type == "production":
                        heatmap_bucket["production_minutes"] += segment_minutes
                if detail and id(detail) not in heatmap_bucket["detail_ids"]:
                    heatmap_bucket["detail_ids"].add(id(detail))
                    heatmap_bucket["details"].append(detail)

        result = []
        global_ps_latest = {}
        range_days = (end_d - start_d).days + 1
        machine_calendar_hours = round(range_days * 24.0, 2)
        for machine in machines:
            bucket = by_machine[int(machine["machine_id"])]
            details = list(bucket["details_map"].values())
            details.sort(key=lambda d: (d.get("start_time") or "", d.get("end_time") or "", d.get("display_ps_id") or "", d.get("source_op_no") or "", d.get("block_id") or 0))

            for detail in details:
                actual = actual_by_block.get(int(detail["block_id"]), {"output_qty": 0.0, "reject_qty": 0.0, "actual_report_count": 0})
                raw_output_qty = float(actual.get("output_qty") or 0)
                reject_qty = float(actual.get("reject_qty") or 0)
                good_qty = float(actual.get("good_qty") or max(0.0, raw_output_qty - reject_qty))
                detail["actual_qty_out"] = good_qty
                detail["good_qty"] = good_qty
                detail["output_qty"] = raw_output_qty
                detail["reject_qty"] = reject_qty
                detail["actual_report_count"] = int(actual.get("actual_report_count") or 0)
                detail["op_actual_units"] = raw_output_qty
                cycle_time = float(detail.get("seq_cycle_time") or detail.get("op_cycle_minutes_per_qty") or 0)
                setup_time = float(detail.get("seq_setup_time") or detail.get("op_setup_minutes") or 0)
                detail["actual_used_minutes"] = (setup_time + (cycle_time * detail["op_actual_units"])) if detail["actual_report_count"] > 0 else 0.0
                detail["actual_used_hours"] = round(detail["actual_used_minutes"] / 60.0, 2)
                detail["effective_hours"] = detail["actual_used_hours"]
                planned_qty = float(detail.get("planned_qty") or 0)
                total_qty = float(detail.get("total_qty") or 0)
                actual_qty = good_qty
                detail["schedule_completion_pct"] = min(100.0, (actual_qty / planned_qty * 100.0) if planned_qty else 0.0)
                detail["order_completion_pct"] = min(100.0, (actual_qty / total_qty * 100.0) if total_qty else 0.0)
                op_scheduled_qty = float(detail.get("op_scheduled_qty") or planned_qty or 0)
                op_output_qty = good_qty
                detail["op_scheduled_qty"] = op_scheduled_qty
                detail["op_output_qty"] = op_output_qty
                detail["op_finished"] = bool(
                    compact_text(detail.get("execution_status") or detail.get("planning_status") or detail.get("status") or "").upper() in {"COMPLETED", "DONE"}
                    or (op_scheduled_qty > 0 and op_output_qty + 1e-9 >= op_scheduled_qty)
                )
                bucket["output_qty"] += actual_qty
                bucket["reject_qty"] += float(detail.get("reject_qty") or 0)
                bucket["actual_used_minutes"] += float(detail["actual_used_minutes"] or 0)

                key = detail.get("backend_ps_id") or detail.get("display_ps_id") or f"block:{detail.get('block_id')}"
                seq_key = (
                    int(detail.get("source_step_seq_no") or 0),
                    int(detail.get("source_op_seq_id") or 0),
                    int(detail.get("block_id") or 0),
                    int(detail.get("segment_id") or 0),
                )
                prev = global_ps_latest.get(key)
                if prev is None or seq_key > prev[0]:
                    global_ps_latest[key] = (seq_key, detail)

            capacity_minutes = _sum_capacity_minutes(con, machine, start_d, end_d)
            available_hours = round(capacity_minutes / 60.0, 2)
            planned_hours = round(bucket["planned_minutes"] / 60.0, 2)
            idle_hours = round(max(0.0, capacity_minutes - bucket["planned_minutes"]) / 60.0, 2)
            utilization_pct = round((bucket["planned_minutes"] / capacity_minutes * 100.0) if capacity_minutes else 0.0, 1)
            scheduled_qty = round(bucket["scheduled_qty"], 2)
            output_qty = round(bucket["output_qty"], 2)
            reject_qty = round(bucket["reject_qty"], 2)
            schedule_completion_pct = round(min(100.0, (output_qty / scheduled_qty * 100.0) if scheduled_qty else 0.0), 1)
            order_details = [item[1] for item in global_ps_latest.values() if compact_text(item[1].get("machine_code")) == compact_text(machine.get("machine_code"))]
            order_qty_total = sum(float(detail.get("total_qty") or 0) for detail in order_details)
            order_actual_qty = sum(float(detail.get("actual_qty_out") or 0) for detail in order_details)
            order_completion_pct = round(min(100.0, (order_actual_qty / order_qty_total * 100.0) if order_qty_total else 0.0), 1)
            next_available = _next_available_text(con, machine, end_d)
            result.append(
                {
                    "machine_id": int(machine["machine_id"]),
                    "machine_code": compact_text(machine.get("machine_code")) or "UNKNOWN",
                    "machine_category": compact_text(machine.get("machine_category")) or "UNKNOWN",
                    "shift_profile": compact_text(machine.get("shift_profile")) or "STANDARD",
                    "ps_count": len(bucket["ps_ids"]),
                    "calendar_hours": machine_calendar_hours,
                    "planned_hours": planned_hours,
                    "available_hours": available_hours,
                    "utilization_pct": utilization_pct,
                    "idle_hours": idle_hours,
                    "scheduled_qty": scheduled_qty,
                    "output_qty": output_qty,
                    "reject_qty": reject_qty,
                    "schedule_completion_pct": schedule_completion_pct,
                    "order_completion_pct": order_completion_pct,
                    "next_available": next_available,
                    "details": details,
                }
            )

        result.sort(key=lambda item: (-item["planned_hours"], item["machine_code"]))
        for row in result:
            actual_used_hours = round(sum(float(detail.get("actual_used_hours") or 0) for detail in row.get("details", [])), 2)
            row["actual_used_hours"] = actual_used_hours
            row["effective_hours"] = actual_used_hours
            row["oee1_pct"] = round((actual_used_hours / row["calendar_hours"] * 100.0) if row["calendar_hours"] else 0.0, 1)
            row["oee2_pct"] = round((actual_used_hours / row["planned_hours"] * 100.0) if row["planned_hours"] else 0.0, 1)
            row["idle_hours"] = round(max(0.0, row["available_hours"] - row["planned_hours"]), 2)
        all_details = [detail for row in result for detail in row.get("details", [])]

        machine_count = len(result)
        active_machine_count = machine_count
        total_planned_hours = round(sum(row["planned_hours"] for row in result), 2)
        total_available_hours = round(sum(row["available_hours"] for row in result), 2)
        total_calendar_hours = round(sum(row["calendar_hours"] for row in result), 2)
        total_idle_hours = round(sum(row["idle_hours"] for row in result), 2)
        total_actual_used_hours = round(sum(row["actual_used_hours"] for row in result), 2)
        total_scheduled_qty = round(sum(row["scheduled_qty"] for row in result), 2)
        total_output_qty = round(sum(row["output_qty"] for row in result), 2)
        total_reject_qty = round(sum(row["reject_qty"] for row in result), 2)
        utilization_pct = round((total_planned_hours / total_available_hours * 100.0) if total_available_hours else 0.0, 1)
        oee1_pct = round((total_actual_used_hours / total_calendar_hours * 100.0) if total_calendar_hours else 0.0, 1)
        oee2_pct = round((total_actual_used_hours / total_planned_hours * 100.0) if total_planned_hours else 0.0, 1)

        ps_entries = list(ps_map.values())
        for ps in ps_entries:
            details = sorted(ps.get("details", []), key=lambda d: (
                int(d.get("source_step_seq_no") or 0),
                int(d.get("source_op_seq_id") or 0),
                int(d.get("block_id") or 0),
                int(d.get("segment_id") or 0),
            ))
            unique_ops = []
            seen_ops = set()
            for detail in details:
                op_key = (int(detail.get("source_step_seq_no") or 0), int(detail.get("source_op_seq_id") or 0))
                if op_key in seen_ops:
                    continue
                seen_ops.add(op_key)
                unique_ops.append(detail)
            final_ops = [detail for detail in unique_ops if int(detail.get("is_last_op") or 0) == 1] or unique_ops[-1:]
            final_op_output = sum(float(detail.get("actual_qty_out") or 0) for detail in final_ops)
            final_op_finished = any(bool(detail.get("op_finished")) for detail in final_ops)
            planned_finish = max([
                detail.get("end_datetime")
                or detail.get("end_time")
                or detail.get("calculated_end_datetime")
                or ""
                for detail in details
            ] or [ps.get("projected_end_date") or ""])
            ps["details"] = unique_ops
            ps["current_op"] = next((detail for detail in unique_ops if not detail.get("op_finished")), unique_ops[-1] if unique_ops else None)
            current_index = unique_ops.index(ps["current_op"]) if ps.get("current_op") in unique_ops else -1
            ps["next_op"] = unique_ops[current_index + 1] if current_index >= 0 and current_index + 1 < len(unique_ops) else None
            ps["current_machine"] = ps["current_op"].get("machine_code") if ps.get("current_op") else ""
            ps["latest_machine"] = unique_ops[-1].get("machine_code") if unique_ops else ""
            ps["planned_finish"] = planned_finish
            ps["projected_end_date"] = planned_finish
            ps["ps_finished"] = bool(
                compact_text(ps.get("status") or ps.get("planner_status") or "").upper() == "COMPLETED"
                or (float(ps.get("total_qty") or 0) > 0 and float(ps.get("finished_qty") or 0) + 1e-9 >= float(ps.get("total_qty") or 0))
                or (float(ps.get("total_qty") or 0) > 0 and final_op_finished and final_op_output + 1e-9 >= float(ps.get("total_qty") or 0))
            )
            ps["ps_finished_marker"] = False
            ps["ps_finished_marker_segment_id"] = 0
            ps["op_finished"] = any(bool(detail.get("op_finished")) for detail in unique_ops)
            ps["op_finished_marker"] = False
            ps["op_finished_marker_segment_id"] = 0
            due_date = compact_text(ps.get("due_date") or "")
            ps["risk_label"] = "Finished" if ps["ps_finished"] else ("Late" if due_date and due_date < date.today().isoformat() else "On track")

        heatmap = []
        for (machine_id, plan_date), bucket in sorted(heatmap_map.items(), key=lambda item: (item[0][1], item[0][0])):
            work_day = date.fromisoformat(plan_date)
            cap = _capacity_day_details(con, machine_id, work_day)
            capacity_minutes = int(cap.get("capacity_minutes") or 0)
            planned_minutes = round(float(bucket["planned_minutes"] or 0), 2)
            setup_minutes = round(float(bucket["setup_minutes"] or 0), 2)
            production_minutes = round(float(bucket["production_minutes"] or 0), 2)
            raw_load_pct = round((planned_minutes / capacity_minutes * 100.0) if capacity_minutes else (999.0 if planned_minutes > 0 else 0.0), 1)
            if capacity_minutes <= 0 and planned_minutes > 0:
                status = "CAPACITY_CONFLICT"
                status_label = "Capacity conflict"
            elif capacity_minutes <= 0:
                status = "NO_CAPACITY"
                status_label = "No capacity"
            elif planned_minutes <= 0:
                status = "NO_LOAD"
                status_label = "No load"
            elif raw_load_pct > 100.0:
                status = "OVER_CAPACITY"
                status_label = "Over capacity"
            elif raw_load_pct > 95.0:
                status = "FULL"
                status_label = "Full"
            elif raw_load_pct >= 70.0:
                status = "HEALTHY"
                status_label = "Healthy"
            else:
                status = "UNDERLOADED"
                status_label = "Underloaded"
            visible_load_pct = min(max(raw_load_pct, 0.0), 100.0)
            ps_count = len({compact_text(ps_id) for ps_id in bucket["ps_ids"] if compact_text(ps_id)})
            block_count = len(bucket["block_ids"])
            ops_count = len(bucket["op_keys"])
            denominator_source = "machine_capacity_day" if cap.get("has_capacity_day_override") else "capacity_profile"
            heatmap.append(
                {
                    "machine_id": machine_id,
                    "machine_code": bucket["machine_code"],
                    "machine_category": bucket["machine_category"],
                    "plan_date": plan_date,
                    "capacity_minutes": capacity_minutes,
                    "planned_minutes": planned_minutes,
                    "setup_minutes": setup_minutes,
                    "production_minutes": production_minutes,
                    "available_hours": round(capacity_minutes / 60.0, 2),
                    "planned_hours": round(planned_minutes / 60.0, 2),
                    "setup_hours": round(setup_minutes / 60.0, 2),
                    "production_hours": round(production_minutes / 60.0, 2),
                    "raw_load_pct": raw_load_pct,
                    "load_pct": raw_load_pct,
                    "visible_load_pct": visible_load_pct,
                    "utilization_pct": raw_load_pct,
                    "status": status,
                    "status_label": status_label,
                    "ps_count": ps_count,
                    "ops_count": ops_count,
                    "block_count": block_count,
                    "details": bucket["details"],
                    "denominator_source": denominator_source,
                    "capacity_profile_name": cap.get("profile_name") or "",
                    "has_capacity_day_override": bool(cap.get("has_capacity_day_override")),
                    "planned_segment_count": int(bucket["planned_segment_count"] or 0),
                    "debug_note": cap.get("note") or "",
                }
            )

        late_ps = []
        due_soon_ps = []
        not_fully_planned_ps = []
        for ps in ps_entries:
            due_date = compact_text(ps.get("due_date") or "")
            finished = bool(ps.get("ps_finished"))
            planned_qty = float(ps.get("planned_qty") or 0)
            total_qty = float(ps.get("total_qty") or 0)
            current_machine = compact_text(ps.get("current_machine") or "")
            risk = compact_text(ps.get("risk_label") or "")
            if due_date and not finished and due_date < date.today().isoformat():
                late_ps.append(ps)
            if due_date and not finished and due_date <= (min(end_d, date.today() + timedelta(days=3))).isoformat():
                due_soon_ps.append(ps)
            if not finished and planned_qty + 1e-9 < total_qty:
                not_fully_planned_ps.append(ps)
            if not ps.get("risk_label"):
                ps["risk_label"] = risk or ("On track" if not finished else "Finished")

        over_capacity_machines = [row for row in result if row["utilization_pct"] > 90]
        underused_machines = [row for row in result if row["utilization_pct"] < 30 and row["available_hours"] > 0]
        missing_actuals = [detail for detail in all_details if compact_text(detail.get("plan_date") or "") < date.today().isoformat() and not detail.get("op_finished") and int(detail.get("actual_report_count") or 0) == 0]
        cycle_time_high_variance = [detail for detail in all_details if float(detail.get("actual_used_hours") or 0) > float(detail.get("planned_hours") or 0) * 1.25 and int(detail.get("actual_report_count") or 0) > 0]
        cycle_time_low_variance = [detail for detail in all_details if float(detail.get("actual_used_hours") or 0) < float(detail.get("planned_hours") or 0) * 0.70 and int(detail.get("actual_report_count") or 0) > 0]
        output_over_scheduled = [detail for detail in all_details if float(detail.get("actual_qty_out") or 0) > float(detail.get("op_scheduled_qty") or 0) * 1.10]
        output_under_scheduled = [detail for detail in all_details if bool(detail.get("op_finished")) and float(detail.get("actual_qty_out") or 0) < float(detail.get("op_scheduled_qty") or 0) * 0.90]
        missing_cycle_time = [detail for detail in all_details if float(detail.get("seq_cycle_time") or detail.get("op_cycle_minutes_per_qty") or 0) <= 0]
        missing_setup_time = [detail for detail in all_details if float(detail.get("seq_setup_time") or detail.get("op_setup_minutes") or 0) <= 0]
        at_risk_ps_count = len([ps for ps in ps_entries if ps.get("risk_label") in {"Late", "Due soon", "Not fully planned"}])
        late_ps_count = len(late_ps)

        bottleneck_machine = max(result, key=lambda row: (row.get("utilization_pct") or 0, row.get("planned_hours") or 0), default=None)
        next_available_machine = min((row for row in result if row.get("next_available")), key=lambda row: row.get("next_available"), default=None)

        due_rows = rows(
            con.execute(
                f"""
                SELECT
                    ps.ps_id AS ps_id,
                    ps.source_ps_id AS display_source_ps_id,
                    ps.pp_partial_no AS display_partial_no,
                    ps.total_qty AS ps_total_qty,
                    COALESCE(ab.good_qty, 0) AS ps_finished_qty,
                    ps.status AS ps_status,
                    ps.planner_status AS ps_planner_status,
                    ps.due_date AS ps_due_date,
                    o.operation_id,
                    o.source_op_seq_id,
                    o.source_op_no,
                    o.operation_name,
                    pfs.seq_no AS source_step_seq_no,
                    pfs.is_last_op AS is_last_op,
                    b.block_id,
                    b.machine_id,
                    b.scheduled_qty,
                    b.status,
                    b.planning_status,
                    b.execution_status,
                    b.anchor_datetime,
                    b.calculated_start_datetime,
                    b.calculated_end_datetime,
                    m.machine_code,
                    m.machine_category,
                    m.shift_profile,
                    s.segment_id,
                    s.segment_date,
                    s.segment_type,
                    s.qty_done,
                    s.minutes_used,
                    s.start_datetime,
                    s.end_datetime,
                    a.actual_id,
                    a.report_date,
                    a.output_qty,
                    a.reject_qty,
                    a.target_qty_at_report
                FROM process_sheet ps
                JOIN operation o ON o.source_ps_id = ps.ps_id
                LEFT JOIN operation_seq pfs ON pfs.op_seq_id = o.source_op_seq_id
                LEFT JOIN run_block b ON b.operation_id = o.operation_id
                LEFT JOIN machines m ON m.machine_id = b.machine_id
                LEFT JOIN run_block_segment s ON s.block_id = b.block_id
                LEFT JOIN production_actual a ON a.block_id = b.block_id
                 AND COALESCE(a.status, 'ACTIVE') = 'ACTIVE'
                WHERE b.machine_id IN ({q_marks})
                  AND COALESCE(b.active, 1) = 1
                  AND (
                    EXISTS (
                      SELECT 1
                      FROM run_block_segment sx
                      WHERE sx.block_id = b.block_id
                        AND sx.segment_date BETWEEN ? AND ?
                    )
                    OR EXISTS (
                      SELECT 1
                      FROM production_actual ax
                      WHERE ax.block_id = b.block_id
                        AND COALESCE(ax.status, 'ACTIVE') = 'ACTIVE'
                        AND ax.report_date BETWEEN ? AND ?
                    )
                    OR (
                      COALESCE(b.calculated_start_datetime, '') <> ''
                      AND COALESCE(b.calculated_end_datetime, '') <> ''
                      AND b.calculated_start_datetime <= ?
                      AND b.calculated_end_datetime >= ?
                    )
                    OR (
                      COALESCE(b.anchor_datetime, '') <> ''
                      AND substr(b.anchor_datetime, 1, 10) BETWEEN ? AND ?
                    )
                  )
                ORDER BY ps.ps_id,
                         COALESCE(pfs.seq_no, 999999),
                         COALESCE(o.source_op_seq_id, 999999),
                         o.source_op_no,
                         b.block_id,
                         s.segment_id,
                         a.actual_id
                """,
                (
                    *machine_ids,
                    start_d.isoformat(),
                    end_d.isoformat(),
                    start_d.isoformat(),
                    end_d.isoformat(),
                    f"{end_d.isoformat()} 23:59:59",
                    f"{start_d.isoformat()} 00:00:00",
                    start_d.isoformat(),
                    end_d.isoformat(),
                ),
            )
        )

        due_groups = {}
        for row in due_rows:
            ps_id = compact_text(row.get("ps_id"))
            if not ps_id:
                continue
            group = due_groups.setdefault(
                ps_id,
                {
                    "ps_id": ps_id,
                    "display_ps_id": compact_text(row.get("display_source_ps_id")) or ps_id.split("::")[0] or ps_id,
                    "display_partial_no": compact_text(row.get("display_partial_no")) or (ps_id.rsplit("::", 1)[-1] if "::" in ps_id else "1"),
                    "source_ps_id": compact_text(row.get("display_source_ps_id")) or ps_id.split("::")[0] or ps_id,
                    "pp_partial_no": compact_text(row.get("display_partial_no")) or (ps_id.rsplit("::", 1)[-1] if "::" in ps_id else "1"),
                    "due_date": compact_text(row.get("ps_due_date") or ""),
                    "total_qty": float(row.get("ps_total_qty") or 0),
                    "finished_qty": float(row.get("ps_finished_qty") or 0),
                    "status": compact_text(row.get("ps_status") or ""),
                    "planner_status": compact_text(row.get("ps_planner_status") or ""),
                    "ops": {},
                    "qualifying_segment_ids": set(),
                    "qualifying_actual_ids": set(),
                    "processed_in_range_reasons": set(),
                },
            )

            op_seq_id = int(row.get("source_op_seq_id") or 0)
            op_key = (
                int(row.get("source_step_seq_no") or 0),
                op_seq_id,
                compact_text(row.get("source_op_no") or ""),
            )
            op = group["ops"].setdefault(
                op_key,
                {
                    "operation_id": int(row.get("operation_id") or 0),
                    "source_op_seq_id": op_seq_id,
                    "source_step_seq_no": int(row.get("source_step_seq_no") or 0),
                    "source_op_no": compact_text(row.get("source_op_no") or ""),
                    "operation_name": compact_text(row.get("operation_name") or ""),
                    "is_last_op": int(row.get("is_last_op") or 0),
                    "blocks": {},
                },
            )
            if not op.get("source_op_no"):
                op["source_op_no"] = compact_text(row.get("source_op_no") or "")
            if not op.get("operation_name"):
                op["operation_name"] = compact_text(row.get("operation_name") or "")
            op["is_last_op"] = max(int(op.get("is_last_op") or 0), int(row.get("is_last_op") or 0))
            op["operation_id"] = int(op.get("operation_id") or 0) or int(row.get("operation_id") or 0)

            block_id = int(row.get("block_id") or 0)
            if block_id:
                block = op["blocks"].setdefault(
                    block_id,
                    {
                        "block_id": block_id,
                        "machine_id": int(row.get("machine_id") or 0),
                        "machine_code": compact_text(row.get("machine_code") or ""),
                        "machine_category": compact_text(row.get("machine_category") or ""),
                        "shift_profile": compact_text(row.get("shift_profile") or ""),
                        "scheduled_qty": float(row.get("scheduled_qty") or 0),
                        "status": compact_text(row.get("status") or ""),
                        "planning_status": compact_text(row.get("planning_status") or ""),
                        "execution_status": compact_text(row.get("execution_status") or ""),
                        "anchor_datetime": compact_text(row.get("anchor_datetime") or ""),
                        "calculated_start_datetime": compact_text(row.get("calculated_start_datetime") or ""),
                        "calculated_end_datetime": compact_text(row.get("calculated_end_datetime") or ""),
                        "segments": {},
                        "actuals": {},
                    },
                )
                if not block.get("machine_code"):
                    block["machine_code"] = compact_text(row.get("machine_code") or "")
                if not block.get("machine_category"):
                    block["machine_category"] = compact_text(row.get("machine_category") or "")
                if not block.get("shift_profile"):
                    block["shift_profile"] = compact_text(row.get("shift_profile") or "")
                block["scheduled_qty"] = max(float(block.get("scheduled_qty") or 0), float(row.get("scheduled_qty") or 0))
                block["status"] = block["status"] or compact_text(row.get("status") or "")
                block["planning_status"] = block["planning_status"] or compact_text(row.get("planning_status") or "")
                block["execution_status"] = block["execution_status"] or compact_text(row.get("execution_status") or "")
                block["anchor_datetime"] = block["anchor_datetime"] or compact_text(row.get("anchor_datetime") or "")
                block["calculated_start_datetime"] = block["calculated_start_datetime"] or compact_text(row.get("calculated_start_datetime") or "")
                block["calculated_end_datetime"] = block["calculated_end_datetime"] or compact_text(row.get("calculated_end_datetime") or "")

                segment_id = int(row.get("segment_id") or 0)
                if segment_id:
                    block["segments"][segment_id] = {
                        "segment_id": segment_id,
                        "segment_date": compact_text(row.get("segment_date") or ""),
                        "segment_type": compact_text(row.get("segment_type") or ""),
                        "qty_done": float(row.get("qty_done") or 0),
                        "minutes_used": float(row.get("minutes_used") or 0),
                        "start_datetime": compact_text(row.get("start_datetime") or ""),
                        "end_datetime": compact_text(row.get("end_datetime") or ""),
                    }
                    group["qualifying_segment_ids"].add(segment_id)
                    if _date_in_range(row.get("segment_date"), start_d, end_d):
                        group["processed_in_range_reasons"].add("segment")

                actual_id = int(row.get("actual_id") or 0)
                if actual_id:
                    block["actuals"][actual_id] = {
                        "actual_id": actual_id,
                        "report_date": compact_text(row.get("report_date") or ""),
                        "output_qty": float(row.get("output_qty") or 0),
                        "reject_qty": float(row.get("reject_qty") or 0),
                        "target_qty_at_report": float(row.get("target_qty_at_report") or 0),
                    }
                    group["qualifying_actual_ids"].add(actual_id)
                    if _date_in_range(row.get("report_date"), start_d, end_d):
                        group["processed_in_range_reasons"].add("actual")

                if _datetime_overlaps_range(row.get("calculated_start_datetime"), row.get("calculated_end_datetime"), start_d, end_d):
                    group["processed_in_range_reasons"].add("schedule overlap")
                if _date_in_range(row.get("anchor_datetime"), start_d, end_d):
                    group["processed_in_range_reasons"].add("anchor")

        effective_hours = total_actual_used_hours
        machine_candidates = [row for row in result if float(row.get("available_hours") or 0) > 0] or result
        highest_oee2 = sorted(machine_candidates, key=lambda row: (
            float(row.get("oee2_pct") or 0),
            float(row.get("planned_hours") or 0),
            compact_text(row.get("machine_code") or ""),
        ), reverse=True)[:3]
        lowest_oee2 = sorted(machine_candidates, key=lambda row: (
            float(row.get("oee2_pct") or 0),
            float(row.get("planned_hours") or 0),
            compact_text(row.get("machine_code") or ""),
        ))[:3]

        def _snapshot_row(row):
            return {
                "machine_id": int(row.get("machine_id") or 0),
                "machine_code": compact_text(row.get("machine_code")) or "UNKNOWN",
                "machine_category": compact_text(row.get("machine_category")) or "UNKNOWN",
                "shift_profile": compact_text(row.get("shift_profile")) or "STANDARD",
                "available_hours": round(float(row.get("available_hours") or 0), 2),
                "planned_hours": round(float(row.get("planned_hours") or 0), 2),
                "calendar_hours": round(float(row.get("calendar_hours") or 0), 2),
                "effective_hours": round(float(row.get("effective_hours") or row.get("actual_used_hours") or 0), 2),
                "oee1_pct": round(float(row.get("oee1_pct") or 0), 1),
                "oee2_pct": round(float(row.get("oee2_pct") or 0), 1),
                "utilization_pct": round(float(row.get("utilization_pct") or 0), 1),
            }

        today_iso = date.today().isoformat()
        range_end_iso = end_d.isoformat()
        soon_cutoff_iso = min(range_end_iso, (date.today() + timedelta(days=7)).isoformat())
        late_due_ps = []
        soon_due_ps = []

        def _ps_order_key(op):
            return (
                int(op.get("source_step_seq_no") or 999999),
                int(op.get("source_op_seq_id") or 999999),
                compact_text(op.get("source_op_no") or ""),
            )

        def _block_order_key(block):
            return (
                _parse_dt_safe(block.get("calculated_start_datetime")) or _parse_dt_safe(block.get("anchor_datetime")) or datetime.max,
                _parse_dt_safe(block.get("calculated_end_datetime")) or datetime.max,
                int(block.get("block_id") or 0),
            )

        def _due_ps_row(group):
            ops = sorted(group.get("ops", {}).values(), key=_ps_order_key)
            if not ops:
                return None
            all_blocks = [block for op in ops for block in op.get("blocks", {}).values()]
            all_blocks.sort(key=_block_order_key)
            identified_last_ops = [op for op in ops if int(op.get("is_last_op") or 0) == 1]
            has_identified_last_op = bool(identified_last_ops)
            last_op = (identified_last_ops[-1] if identified_last_ops else ops[-1]) if ops else None
            if not last_op:
                return None
            last_op_blocks = sorted(last_op.get("blocks", {}).values(), key=_block_order_key)
            last_op_block_count = len(last_op_blocks)
            last_op_segment_count = sum(len(block.get("segments", {})) for block in last_op_blocks)
            all_segment_count = sum(len(block.get("segments", {})) for block in all_blocks)
            qualifying_actual_count = len(group.get("qualifying_actual_ids", set()))
            processed_in_range_reason = ", ".join(sorted(group.get("processed_in_range_reasons", set()))) or "processed in range"
            last_op_output_qty = 0.0
            last_op_reject_qty = 0.0
            last_op_status_bits = []
            last_op_projected_end_candidates = []
            latest_machine = ""
            for block in last_op_blocks:
                block_actuals = list(block.get("actuals", {}).values())
                last_op_output_qty += sum(float(actual.get("output_qty") or 0) for actual in block_actuals)
                last_op_reject_qty += sum(float(actual.get("reject_qty") or 0) for actual in block_actuals)
                if compact_text(block.get("machine_code") or ""):
                    latest_machine = compact_text(block.get("machine_code") or "")
                block_status = compact_text(block.get("execution_status") or block.get("status") or block.get("planning_status") or "")
                if block_status:
                    last_op_status_bits.append(block_status)
                last_op_projected_end_candidates.extend([
                    block.get("calculated_end_datetime") or "",
                    block.get("anchor_datetime") or "",
                ])
                last_op_projected_end_candidates.extend(
                    seg.get("end_datetime") or ""
                    for seg in block.get("segments", {}).values()
                )
            last_op_effective_output = max(0.0, last_op_output_qty - last_op_reject_qty)
            last_op_projected_end = _max_dt_text(last_op_projected_end_candidates)
            total_qty = float(group.get("total_qty") or 0)
            due_date = compact_text(group.get("due_date") or "")
            last_op_planned = bool(last_op_block_count or last_op_segment_count)
            last_op_finished = bool(
                any(bit.upper() in {"COMPLETED", "DONE"} for bit in last_op_status_bits)
                or (total_qty > 0 and last_op_effective_output + 1e-9 >= total_qty)
            )
            if not has_identified_last_op and not any(int(op.get("source_op_seq_id") or 0) for op in ops):
                planning_state = "NO_LAST_OP"
            elif not last_op_planned:
                planning_state = "NOT_STARTED" if not all_blocks else "PARTIALLY_PLANNED"
            elif last_op_finished:
                planning_state = "FINISHED"
            else:
                planning_state = "LAST_OP_PLANNED"

            risk_label = "On track"
            risk_reason = ""
            due_is_late = bool(due_date and due_date < today_iso and not last_op_finished)
            due_is_soon = bool(due_date and today_iso <= due_date <= soon_cutoff_iso and not last_op_finished)
            if due_is_late:
                if not last_op_planned:
                    risk_label = "Late"
                    risk_reason = "Overdue — last op not planned"
                elif last_op_projected_end and _parse_dt_safe(last_op_projected_end) and _parse_dt_safe(last_op_projected_end).date().isoformat() > due_date:
                    risk_label = "Late"
                    risk_reason = "Overdue — projected late"
                else:
                    risk_label = "Late"
                    risk_reason = "Overdue — last op not finished"
            elif due_is_soon:
                if not last_op_planned:
                    risk_label = "Due soon"
                    risk_reason = "Due soon — last op not planned"
                elif last_op_projected_end and _parse_dt_safe(last_op_projected_end) and _parse_dt_safe(last_op_projected_end).date().isoformat() > due_date:
                    risk_label = "Due soon"
                    risk_reason = "Due soon — projected late"
                else:
                    risk_label = "Due soon"
                    risk_reason = "Due soon"

            row = {
                "ps_id": group.get("ps_id") or "",
                "display_ps_id": group.get("display_ps_id") or "",
                "display_partial_no": group.get("display_partial_no") or "1",
                "source_ps_id": group.get("source_ps_id") or "",
                "pp_partial_no": group.get("pp_partial_no") or "1",
                "due_date": due_date,
                "projected_end_date": last_op_projected_end or "",
                "status": group.get("status") or "",
                "planner_status": group.get("planner_status") or "",
                "total_qty": total_qty,
                "finished_qty": round(last_op_effective_output if last_op_effective_output > 0 else float(group.get("finished_qty") or 0), 2),
                "last_op_operation_id": int(last_op.get("operation_id") or 0),
                "last_op_seq_id": int(last_op.get("source_op_seq_id") or 0),
                "last_op_no": compact_text(last_op.get("source_op_no") or ""),
                "last_op_name": compact_text(last_op.get("operation_name") or ""),
                "last_op_has_operation": bool(int(last_op.get("operation_id") or 0)),
                "last_op_has_block": bool(last_op_block_count),
                "last_op_has_segment": bool(last_op_segment_count),
                "last_op_planned": bool(last_op_planned),
                "last_op_finished": bool(last_op_finished),
                "ps_planning_state": planning_state,
                "latest_machine": latest_machine,
                "risk_label": risk_label,
                "risk_reason": risk_reason,
                "processed_in_range_reason": processed_in_range_reason,
                "qualifying_segment_count": int(all_segment_count),
                "qualifying_actual_count": int(qualifying_actual_count),
                "last_op_block_count": int(last_op_block_count),
                "last_op_segment_count": int(last_op_segment_count),
            }
            row["display_ps_id"], row["display_partial_no"] = _display_ids(row["ps_id"], row["display_ps_id"], row["display_partial_no"])
            return row

        due_ps_rows = []
        for group in due_groups.values():
            row = _due_ps_row(group)
            if not row:
                continue
            due_ps_rows.append(row)
            due_date = compact_text(row.get("due_date") or "")
            if row.get("last_op_finished"):
                continue
            if due_date and due_date < today_iso:
                late_due_ps.append(row)
            if due_date and today_iso <= due_date <= soon_cutoff_iso:
                soon_due_ps.append(row)

        dashboard_cards = [
            {"label": "Calendar Hrs", "value": round(total_calendar_hours, 1), "sub": "24/7 time in range"},
            {"label": "Planned Hrs", "value": round(total_planned_hours, 1), "sub": "Segment productive time"},
            {"label": "Effective Hrs", "value": round(total_actual_used_hours, 1), "sub": "Earned work from actuals"},
            {"label": "OEE1", "value": f"{oee1_pct:.1f}%", "sub": "Effective Hrs / Calendar Hrs"},
            {"label": "OEE2", "value": f"{oee2_pct:.1f}%", "sub": "Effective Hrs / Planned Production Hrs"},
        ]

        totals = {
            "machine_count": machine_count,
            "active_machine_count": active_machine_count,
            "calendar_hours": total_calendar_hours,
            "available_hours": total_available_hours,
            "planned_hours": total_planned_hours,
            "effective_hours": effective_hours,
            "output_qty": total_output_qty,
            "reject_qty": total_reject_qty,
            "total_available_hours": total_available_hours,
            "total_calendar_hours": total_calendar_hours,
            "total_planned_hours": total_planned_hours,
            "total_actual_used_hours": total_actual_used_hours,
            "utilization_pct": utilization_pct,
            "idle_hours": total_idle_hours,
            "scheduled_qty": total_scheduled_qty,
            "oee1_pct": oee1_pct,
            "oee2_pct": oee2_pct,
            "ops_total": len(all_details),
            "ops_finished": sum(1 for d in all_details if d.get("op_finished")),
            "ps_total": len(ps_entries),
            "ps_finished": sum(1 for ps in ps_entries if ps.get("ps_finished")),
            "at_risk_ps_count": at_risk_ps_count,
            "late_ps_count": late_ps_count,
            "bottleneck_machine": bottleneck_machine.copy() if bottleneck_machine else None,
            "next_available_machine": next_available_machine.copy() if next_available_machine else None,
            "calculation_inputs": {
                "calendar_hours": total_calendar_hours,
                "available_hours": total_available_hours,
                "planned_hours": total_planned_hours,
                "effective_hours": effective_hours,
                "oee1_pct": oee1_pct,
                "oee2_pct": oee2_pct,
            },
        }
        payload = {
            "from": start_d.isoformat(),
            "to": end_d.isoformat(),
            "dates": [day.isoformat() for day in _date_span(start_d, end_d)],
            "rows": result,
            "machine_types": machine_types,
            "totals": totals,
            "dashboard_cards": dashboard_cards,
            "machine_snapshot": {
                "highest_oee2": [_snapshot_row(row) for row in highest_oee2],
                "lowest_oee2": [_snapshot_row(row) for row in lowest_oee2],
            },
            "due_process_sheets": {
                "late": late_due_ps,
                "due_soon": soon_due_ps,
            },
            "watchlist": {
                "late_ps": late_ps,
                "due_soon_ps": due_soon_ps,
                "over_capacity_machines": over_capacity_machines,
                "underused_machines": underused_machines,
                "missing_actuals": missing_actuals,
                "not_fully_planned_ps": not_fully_planned_ps,
            },
            "data_quality": {
                "cycle_time_high_variance": cycle_time_high_variance,
                "cycle_time_low_variance": cycle_time_low_variance,
                "output_over_scheduled": output_over_scheduled,
                "output_under_scheduled": output_under_scheduled,
                "missing_cycle_time": missing_cycle_time,
                "missing_setup_time": missing_setup_time,
            },
            "heatmap": heatmap,
            "machines": result,
            "process_sheets": ps_entries,
        }
        if view and view != "machines":
            payload["view"] = "machines"
        return jsonify(payload)
