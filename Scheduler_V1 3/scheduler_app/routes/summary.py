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
                "actual_report_count": int(row["actual_report_count"] or 0),
            }
            for row in rows(
                con.execute(
                    """
                    SELECT block_id,
                           COALESCE(SUM(COALESCE(output_qty, 0)), 0) AS output_qty,
                           COALESCE(SUM(COALESCE(reject_qty, 0)), 0) AS reject_qty,
                           COUNT(actual_id) AS actual_report_count
                    FROM production_actual
                    WHERE report_date BETWEEN ? AND ?
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
                    b.block_type,
                    b.remarks,
                    b.group_id,
                    o.job_no,
                    o.operation_name,
                    o.total_qty AS operation_total_qty,
                    o.source_ps_id AS backend_ps_id,
                    o.source_op_seq_id,
                    o.source_op_no,
                    o.source_op_seq_id AS source_op_seq_id,
                    pfs.seq_no AS source_step_seq_no,
                    m.machine_code,
                    m.machine_category,
                    m.shift_profile,
                    ps.source_ps_id AS display_source_ps_id,
                    ps.pp_partial_no AS display_partial_no,
                    ps.total_qty AS ps_total_qty,
                    g.group_label,
                    COALESCE(ab.output_qty, 0) AS output_qty,
                    COALESCE(ab.reject_qty, 0) AS reject_qty,
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
                           COUNT(actual_id) AS actual_report_count
                    FROM production_actual
                    WHERE report_date BETWEEN ? AND ?
                    GROUP BY block_id
                ) ab ON ab.block_id = b.block_id
                WHERE s.segment_date BETWEEN ? AND ?
                  AND b.machine_id IN ({q_marks})
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
                "scheduled_qty": 0.0,
                "output_qty": 0.0,
                "reject_qty": 0.0,
                "details_map": {},
            }
            for machine in machines
        }

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
                    "planned_hours": 0.0,
                    "actual_qty_out": 0.0,
                    "output_qty": 0.0,
                    "reject_qty": 0.0,
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
                },
            )
            if start_text and (not detail["start_time"] or start_text < detail["start_time"]):
                detail["start_time"] = start_text
            if end_text and (not detail["end_time"] or end_text > detail["end_time"]):
                detail["end_time"] = end_text
            detail["planned_qty"] += planned_qty
            detail["planned_hours"] += float(row.get("minutes_used") or 0) / 60.0
            detail["segment_id"] = min(detail["segment_id"], int(row.get("segment_id") or 0)) if detail["segment_id"] else int(row.get("segment_id") or 0)
            bucket["planned_minutes"] += float(row.get("minutes_used") or 0)
            bucket["scheduled_qty"] += planned_qty
            if backend_ps_id:
                bucket["ps_ids"].add(backend_ps_id)

        result = []
        global_ps_latest = {}
        for machine in machines:
            bucket = by_machine[int(machine["machine_id"])]
            details = list(bucket["details_map"].values())
            details.sort(key=lambda d: (d.get("start_time") or "", d.get("end_time") or "", d.get("display_ps_id") or "", d.get("source_op_no") or "", d.get("block_id") or 0))

            for detail in details:
                actual = actual_by_block.get(int(detail["block_id"]), {"output_qty": 0.0, "reject_qty": 0.0, "actual_report_count": 0})
                detail["actual_qty_out"] = float(actual.get("output_qty") or 0)
                detail["output_qty"] = detail["actual_qty_out"]
                detail["reject_qty"] = float(actual.get("reject_qty") or 0)
                detail["actual_report_count"] = int(actual.get("actual_report_count") or 0)
                planned_qty = float(detail.get("planned_qty") or 0)
                total_qty = float(detail.get("total_qty") or 0)
                actual_qty = float(detail.get("actual_qty_out") or 0)
                detail["schedule_completion_pct"] = min(100.0, (actual_qty / planned_qty * 100.0) if planned_qty else 0.0)
                detail["order_completion_pct"] = min(100.0, (actual_qty / total_qty * 100.0) if total_qty else 0.0)
                bucket["output_qty"] += actual_qty
                bucket["reject_qty"] += float(detail.get("reject_qty") or 0)

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
        machine_count = len(result)
        active_machine_count = sum(1 for row in result if row["available_hours"] > 0 or row["planned_hours"] > 0)
        total_planned_hours = round(sum(row["planned_hours"] for row in result), 2)
        total_available_hours = round(sum(row["available_hours"] for row in result), 2)
        total_idle_hours = round(sum(row["idle_hours"] for row in result), 2)
        total_scheduled_qty = round(sum(row["scheduled_qty"] for row in result), 2)
        total_output_qty = round(sum(row["output_qty"] for row in result), 2)
        total_reject_qty = round(sum(row["reject_qty"] for row in result), 2)
        utilization_pct = round((total_planned_hours / total_available_hours * 100.0) if total_available_hours else 0.0, 1)
        schedule_completion_pct = round(min(100.0, (total_output_qty / total_scheduled_qty * 100.0) if total_scheduled_qty else 0.0), 1)
        order_total_qty = sum(float(detail.get("total_qty") or 0) for _, detail in global_ps_latest.values())
        order_actual_qty = sum(float(detail.get("actual_qty_out") or 0) for _, detail in global_ps_latest.values())
        order_completion_pct = round(min(100.0, (order_actual_qty / order_total_qty * 100.0) if order_total_qty else 0.0), 1)
        best_machine = result[0].copy() if result else None
        if best_machine:
            best_machine.pop("details", None)

        totals = {
            "machine_count": machine_count,
            "active_machine_count": active_machine_count,
            "total_planned_hours": total_planned_hours,
            "total_available_hours": total_available_hours,
            "utilization_pct": utilization_pct,
            "idle_hours": total_idle_hours,
            "scheduled_qty": total_scheduled_qty,
            "output_qty": total_output_qty,
            "reject_qty": total_reject_qty,
            "schedule_completion_pct": schedule_completion_pct,
            "order_completion_pct": order_completion_pct,
            "best_machine": best_machine,
        }
        payload = {"rows": result, "machine_types": machine_types, "totals": totals}
        if view and view != "machines":
            payload["view"] = "machines"
        return jsonify(payload)
