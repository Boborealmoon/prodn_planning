from __future__ import annotations

from datetime import date

from flask import Blueprint, jsonify, request

from ..db import db, one, rows
from ..materials import material_requirement_payload, material_status_map_for_ps_ids
from ..utils import compact_text

process_sheets_bp = Blueprint("trial_process_sheets", __name__)


def _to_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _operation_key(source_op_seq_id, source_op_no):
    return (int(source_op_seq_id or 0), compact_text(source_op_no))


def _display_ids(ps):
    ps_id = compact_text(ps.get("ps_id"))
    source_ps_id = compact_text(ps.get("source_ps_id"))
    pp_partial_no = compact_text(ps.get("pp_partial_no"))
    if not source_ps_id and "::" in ps_id:
        source_ps_id, pp_partial_no = ps_id.rsplit("::", 1)
    return source_ps_id or ps_id, pp_partial_no


def _flow_steps_for_ps_ids(con, ps_ids):
    ps_ids = [compact_text(ps_id) for ps_id in ps_ids if compact_text(ps_id)]
    if not ps_ids:
        return {}
    placeholders = ",".join("?" for _ in ps_ids)
    result = {}
    for row in rows(
        con.execute(
            f"""
            SELECT ps.ps_id, pfs.op_seq_id, pfs.seq_no, pfs.op_no, pfs.op_type,
                   pfs.machine_category, pfs.preferred_machine, pfs.cycle_time,
                   pfs.setup_time, pfs.is_last_op
            FROM process_sheet ps
            JOIN operation_seq pfs ON pfs.bom_id = ps.selected_bom_id
            WHERE ps.ps_id IN ({placeholders})
            ORDER BY ps.ps_id, pfs.seq_no, pfs.op_seq_id
            """,
            ps_ids,
        )
    ):
        item = dict(row)
        ps_id = compact_text(item.pop("ps_id"))
        result.setdefault(ps_id, []).append(item)
    return result


def _block_metrics_for_ps_ids(con, ps_ids):
    ps_ids = [compact_text(ps_id) for ps_id in ps_ids if compact_text(ps_id)]
    if not ps_ids:
        return {}, {}
    placeholders = ",".join("?" for _ in ps_ids)
    metrics = {}
    block_rows = {}
    for row in rows(
        con.execute(
            f"""
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
            ),
            segment_bounds AS (
                SELECT
                    block_id,
                    MIN(CASE WHEN segment_type = 'production' THEN start_datetime END) AS expected_start,
                    MAX(CASE WHEN segment_type = 'production' THEN end_datetime END) AS expected_end
                FROM run_block_segment
                GROUP BY block_id
            )
            SELECT o.source_ps_id, o.source_op_seq_id, o.source_op_no,
                   b.block_id, b.operation_id, b.machine_id, b.queue_position,
                   b.scheduled_qty, b.status, b.planning_status, b.execution_status,
                   b.calculated_start_datetime, b.calculated_end_datetime,
                   b.anchor_datetime, b.remarks, m.machine_code,
                    COALESCE(ab.output_qty, 0) AS output_qty,
                    COALESCE(ab.reject_qty, 0) AS reject_qty,
                    COALESCE(ab.good_qty, 0) AS good_qty,
                    COALESCE(ab.actual_report_count, 0) AS actual_report_count,
                    sb.expected_start,
                    sb.expected_end
            FROM operation o
            JOIN run_block b ON b.operation_id = o.operation_id
            LEFT JOIN machines m ON m.machine_id = b.machine_id
            LEFT JOIN actual_by_block ab ON ab.block_id = b.block_id
            LEFT JOIN segment_bounds sb ON sb.block_id = b.block_id
            WHERE o.source_ps_id IN ({placeholders})
              AND COALESCE(b.active, 1) = 1
              AND COALESCE(b.block_type, 'ORIGINAL') <> 'REWORK'
            ORDER BY o.source_ps_id, o.source_op_seq_id, o.source_op_no, b.queue_position, b.block_id
            """,
            ps_ids,
        )
    ):
        item = dict(row)
        ps_id = compact_text(item["source_ps_id"])
        key = _operation_key(item["source_op_seq_id"], item["source_op_no"])
        entry = metrics.setdefault(
            ps_id,
            {
                "by_op": {},
                "planned_qty_total": 0.0,
                "finished_qty_total": 0.0,
                "reject_qty_total": 0.0,
                "expected_start": "",
                "expected_end": "",
            },
        )
        op_entry = entry["by_op"].setdefault(
            key,
            {
                "planned_qty": 0.0,
                "finished_qty": 0.0,
                "reject_qty": 0.0,
                "actual_report_count": 0,
                "expected_start": "",
                "expected_end": "",
                "block_count": 0,
            },
        )
        scheduled_qty = _to_float(item["scheduled_qty"])
        output_qty = _to_float(item["output_qty"])
        reject_qty = _to_float(item["reject_qty"])
        good_qty = _to_float(item["good_qty"])
        actual_report_count = int(item.get("actual_report_count") or 0)
        op_entry["planned_qty"] += scheduled_qty
        op_entry["finished_qty"] += good_qty
        op_entry["reject_qty"] += reject_qty
        op_entry["actual_report_count"] += actual_report_count
        op_entry["block_count"] += 1
        start_text = compact_text(item["expected_start"] or item["calculated_start_datetime"])
        end_text = compact_text(item["expected_end"] or item["calculated_end_datetime"])
        if start_text and (not op_entry["expected_start"] or start_text < op_entry["expected_start"]):
            op_entry["expected_start"] = start_text
        if end_text and (not op_entry["expected_end"] or end_text > op_entry["expected_end"]):
            op_entry["expected_end"] = end_text
        if start_text and (not entry["expected_start"] or start_text < entry["expected_start"]):
            entry["expected_start"] = start_text
        if end_text and (not entry["expected_end"] or end_text > entry["expected_end"]):
            entry["expected_end"] = end_text
        block_rows.setdefault(ps_id, []).append(item)
    for ps_id, entry in metrics.items():
        entry["planned_qty_total"] = sum(op["planned_qty"] for op in entry["by_op"].values())
        entry["finished_qty_total"] = sum(op["finished_qty"] for op in entry["by_op"].values())
        entry["reject_qty_total"] = sum(op["reject_qty"] for op in entry["by_op"].values())
    return metrics, block_rows


def _last_operation_steps(steps):
    last_steps = [step for step in steps if int(step.get("is_last_op") or 0) == 1]
    if last_steps:
        return last_steps
    if not steps:
        return []
    return [max(steps, key=lambda step: (int(step.get("seq_no") or 0), int(step.get("op_seq_id") or 0)))]


def _summary_quantities(total_qty, steps, metrics):
    by_op = metrics.get("by_op", {})
    total_qty = _to_float(total_qty)
    last_steps = _last_operation_steps(steps)
    preferred_keys = [_operation_key(step.get("op_seq_id"), step.get("op_no")) for step in last_steps]
    selected = [by_op[key] for key in preferred_keys if key in by_op]
    if selected:
        planned_qty = sum(_to_float(op["planned_qty"]) for op in selected)
        finished_qty = sum(_to_float(op["finished_qty"]) for op in selected)
        reject_qty = sum(_to_float(op["reject_qty"]) for op in selected)
    else:
        planned_qty = max([_to_float(op["planned_qty"]) for op in by_op.values()] or [0.0])
        finished_qty = max([_to_float(op["finished_qty"]) for op in by_op.values()] or [0.0])
        reject_qty = sum(_to_float(op["reject_qty"]) for op in by_op.values())
    planned_qty = min(total_qty, planned_qty) if total_qty > 0 else planned_qty
    finished_qty = min(total_qty, finished_qty) if total_qty > 0 else finished_qty
    return planned_qty, finished_qty, reject_qty, max(0.0, total_qty - finished_qty)


def _planner_status(ps, total_qty, planned_qty, finished_qty, step_count):
    status = compact_text(ps.get("planner_status")).upper()
    if status:
        return status
    if finished_qty >= _to_float(total_qty) and _to_float(total_qty) > 0:
        return "COMPLETED"
    if planned_qty <= 0:
        return "NEEDS_REVIEW" if step_count <= 0 else "UNPLANNED"
    if planned_qty < _to_float(total_qty):
        return "PARTIALLY_PLANNED"
    return "PLANNED"


def _warnings(ps, planner_status, material_status):
    warnings = []
    due_date = compact_text(ps.get("due_date"))
    if due_date and due_date < date.today().isoformat() and planner_status != "COMPLETED":
        warnings.append("OVERDUE")
    if not int(ps.get("selected_bom_id") or 0):
        warnings.append("NO_FLOW")
    severity = compact_text((material_status or {}).get("severity"))
    if severity in {"late", "warning", "pending"}:
        warnings.append("MATERIAL")
    return warnings


def _process_sheet_payload(ps, steps, metrics, material_status):
    raw_planned_qty = _to_float(metrics.get("planned_qty_total"))
    raw_finished_qty = _to_float(metrics.get("finished_qty_total"))
    planned_qty, finished_qty, reject_qty, remaining_qty = _summary_quantities(ps.get("total_qty"), steps, metrics)
    planner_status = _planner_status(ps, ps.get("total_qty"), planned_qty, finished_qty, len(steps))
    display_ps_id, pp_partial_no = _display_ids(ps)
    return {
        "ps_id": ps["ps_id"],
        "source_ps_id": compact_text(ps.get("source_ps_id")) or display_ps_id,
        "pp_partial_no": pp_partial_no,
        "display_ps_id": display_ps_id,
        "part_id": int(ps["part_id"] or 0),
        "part_name": ps["part_name"] or "",
        "part_no": ps["part_no"] or "",
        "part_desc": ps["part_desc"] or "",
        "due_date": ps["due_date"] or "",
        "order_date": ps["order_date"] or "",
        "total_qty": _to_float(ps["total_qty"]),
        "status": ps["status"] or "",
        "planner_status": planner_status,
        "selected_bom_id": int(ps["selected_bom_id"] or 0),
        "selected_bom_code": ps["selected_bom_code"] or "",
        "route_label": ps["selected_bom_code"] or "No BOM code selected",
        "planned_qty": planned_qty,
        "finished_qty": finished_qty,
        "reject_qty": reject_qty,
        "remaining_qty": remaining_qty,
        "output_debug": {
            "total_qty": _to_float(ps.get("total_qty")),
            "raw_planned_qty": raw_planned_qty,
            "raw_finished_qty": raw_finished_qty,
            "displayed_planned_qty": planned_qty,
            "displayed_finished_qty": finished_qty,
            "output_calc_note": "Actuals are aggregated per block before segment joins; PS output uses last operation only.",
        },
        "expected_start": metrics.get("expected_start", ""),
        "expected_end": metrics.get("expected_end", ""),
        "warnings": _warnings(ps, planner_status, material_status),
        "material_status": material_status,
        "ops": [_step_payload(step, metrics.get("by_op", {})) for step in steps],
    }


def _step_payload(step, metrics_by_op):
    key = _operation_key(step.get("op_seq_id"), step.get("op_no"))
    op_metrics = metrics_by_op.get(key, {})
    total_qty = _to_float(step.get("total_qty"))
    planned_qty = _to_float(op_metrics.get("planned_qty"))
    finished_qty = _to_float(op_metrics.get("finished_qty"))
    return {
        "op_seq_id": int(step.get("op_seq_id") or 0),
        "seq_no": int(step.get("seq_no") or 0),
        "op_no": step.get("op_no") or "",
        "op_type": step.get("op_type") or "",
        "machine_category": step.get("machine_category") or "",
        "preferred_machine": step.get("preferred_machine") or "",
        "cycle_time": _to_float(step.get("cycle_time")),
        "setup_time": _to_float(step.get("setup_time")),
        "is_last_op": int(step.get("is_last_op") or 0),
        "planned_qty": planned_qty,
        "finished_qty": finished_qty,
        "reject_qty": _to_float(op_metrics.get("reject_qty")),
        "remaining_qty": max(0.0, total_qty - finished_qty) if total_qty else 0.0,
        "expected_start": op_metrics.get("expected_start", ""),
        "expected_end": op_metrics.get("expected_end", ""),
        "block_count": int(op_metrics.get("block_count") or 0),
    }


def _flow_options(con, part_id):
    return [
        dict(row)
        for row in rows(
            con.execute(
                """
                SELECT bom_id, bom_code, bom_desc AS bom_name, is_default
                FROM bom_variation
                WHERE part_id = ?
                ORDER BY is_default DESC, bom_id
                """,
                (int(part_id or 0),),
            )
        )
    ]


def list_process_sheets_payload(con):
    search = compact_text(request.args.get("search")).lower()
    status_filter = compact_text(request.args.get("status")).upper()
    planner_filter = compact_text(request.args.get("planner_status")).upper()
    show_completed = compact_text(request.args.get("show_completed")).lower() in {"1", "true", "yes", "on"}
    overdue_only = compact_text(request.args.get("overdue_only")).lower() in {"1", "true", "yes", "on"}

    ps_rows = [dict(row) for row in rows(
        con.execute(
            """
            SELECT ps.*, p.part_no AS part_name, p.part_desc AS part_desc, sf.bom_code AS selected_bom_code, sf.bom_desc AS selected_bom_name
            FROM process_sheet ps
            LEFT JOIN parts p ON p.part_id = ps.part_id
            LEFT JOIN bom_variation sf ON sf.bom_id = ps.selected_bom_id
            ORDER BY COALESCE(ps.due_date, ''), ps.ps_id
            """
        )
    )]
    ps_ids = [row["ps_id"] for row in ps_rows]
    steps_by_ps = _flow_steps_for_ps_ids(con, ps_ids)
    metrics_by_ps, _ = _block_metrics_for_ps_ids(con, ps_ids)
    material_status_by_ps = material_status_map_for_ps_ids(
        con,
        ps_ids,
        {ps_id: metrics_by_ps.get(ps_id, {}).get("expected_start", "") for ps_id in ps_ids},
    )
    result = []
    today = date.today().isoformat()
    for ps in ps_rows:
        ps_id = compact_text(ps["ps_id"])
        payload = _process_sheet_payload(
            ps,
            steps_by_ps.get(ps_id, []),
            metrics_by_ps.get(ps_id, {}),
            material_status_by_ps.get(ps_id, {}),
        )
        haystack = " ".join(
            compact_text(payload.get(key)).lower()
            for key in (
                "ps_id",
                "source_ps_id",
                "display_ps_id",
                "pp_partial_no",
                "part_name",
                "part_no",
                "part_desc",
                "selected_bom_code",
                "status",
                "planner_status",
            )
        )
        if search and search not in haystack:
            continue
        if status_filter and compact_text(payload["status"]).upper() != status_filter:
            continue
        if planner_filter and compact_text(payload["planner_status"]).upper() != planner_filter:
            continue
        if not show_completed and payload["planner_status"] == "COMPLETED":
            continue
        if overdue_only and not (payload["due_date"] and payload["due_date"] < today and payload["planner_status"] != "COMPLETED"):
            continue
        result.append(payload)
    return result


@process_sheets_bp.get("/api/trial/process-sheets")
@process_sheets_bp.get("/api/process-sheets")
def api_process_sheets():
    with db() as con:
        return jsonify(list_process_sheets_payload(con))


@process_sheets_bp.get("/api/trial/process-sheets/<path:ps_id>/details")
@process_sheets_bp.get("/api/process-sheets/<path:ps_id>/details")
def api_process_sheet_details(ps_id):
    ps_id = compact_text(ps_id)
    with db() as con:
        ps = one(
            con.execute(
                """
            SELECT ps.*, p.part_no AS part_name, p.part_desc AS part_desc, sf.bom_code AS selected_bom_code, sf.bom_desc AS selected_bom_name
                FROM process_sheet ps
                LEFT JOIN parts p ON p.part_id = ps.part_id
                LEFT JOIN bom_variation sf ON sf.bom_id = ps.selected_bom_id
                WHERE ps.ps_id = ?
                """,
                (ps_id,),
            )
        )
        if not ps:
            return jsonify({"error": "Process sheet not found"}), 404
        steps_by_ps = _flow_steps_for_ps_ids(con, [ps_id])
        metrics_by_ps, block_rows_by_ps = _block_metrics_for_ps_ids(con, [ps_id])
        material_status_by_ps = material_status_map_for_ps_ids(
            con,
            [ps_id],
            {ps_id: metrics_by_ps.get(ps_id, {}).get("expected_start", "")},
        )
        summary = _process_sheet_payload(
            dict(ps),
            steps_by_ps.get(ps_id, []),
            metrics_by_ps.get(ps_id, {}),
            material_status_by_ps.get(ps_id, {}),
        )
        segments = [dict(row) for row in rows(
            con.execute(
                """
                SELECT s.*, m.machine_code, o.source_op_seq_id, o.source_op_no
                FROM run_block_segment s
                JOIN run_block b ON b.block_id = s.block_id
                JOIN operation o ON o.operation_id = b.operation_id
                LEFT JOIN machines m ON m.machine_id = s.machine_id
                WHERE o.source_ps_id = ?
                ORDER BY s.start_datetime, s.segment_id
                """,
                (ps_id,),
            )
        )]
        actuals = [dict(row) for row in rows(
            con.execute(
                """
                SELECT a.actual_id, a.segment_id, a.block_id, a.report_date,
                       a.output_qty, a.reject_qty, a.target_qty_at_report,
                       a.remarks, a.reported_at,
                       o.source_op_seq_id, o.source_op_no
                FROM production_actual a
                JOIN run_block b ON b.block_id = a.block_id
                JOIN operation o ON o.operation_id = b.operation_id
                WHERE o.source_ps_id = ?
                  AND COALESCE(a.status, 'ACTIVE') = 'ACTIVE'
                ORDER BY a.report_date, a.actual_id
                """,
                (ps_id,),
            )
        )]
        actuals_by_block = {}
        for row in actuals:
            actuals_by_block.setdefault(int(row.get("block_id") or 0), []).append(row)
        planned_blocks = []
        for block in block_rows_by_ps.get(ps_id, []):
            item = dict(block)
            item["actuals"] = actuals_by_block.get(int(item.get("block_id") or 0), [])
            planned_blocks.append(item)
        cards = [dict(row) for row in rows(
            con.execute(
                """
                SELECT *
                FROM planning_card
                WHERE ps_id = ?
                ORDER BY card_id
                """,
                (ps_id,),
            )
        )]
        requirements = [
            material_requirement_payload(row)
            for row in rows(
                con.execute(
                    """
                    SELECT *
                    FROM material_requirement
                    WHERE ps_id = ?
                    ORDER BY requirement_id
                    """,
                    (ps_id,),
                )
            )
        ]
        return jsonify(
            {
                **summary,
                "process_sheet": dict(ps),
                "selected_flow": {
                    "bom_id": int(ps["selected_bom_id"] or 0),
                    "bom_code": ps["selected_bom_code"] or "",
                    "bom_name": ps["selected_bom_name"] or "",
                },
                "flow_options": _flow_options(con, ps["part_id"]),
                "flow_steps": [_step_payload(step, metrics_by_ps.get(ps_id, {}).get("by_op", {})) for step in steps_by_ps.get(ps_id, [])],
                "planned_blocks": planned_blocks,
                "segments": segments,
                "actuals": actuals,
                "planning_cards": cards,
                "material_requirements": requirements,
            }
        )
