from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..blocks import recalculate_machine
from ..db import db, one, rows
from ..materials import sync_material_requirements_for_ps
from ..imports import sync_operations_for_flow
from ..utils import compact_text, parse_number

flows_bp = Blueprint("flows", __name__)
trial_prefixed_flows_bp = Blueprint("trial_flows", __name__)


@trial_prefixed_flows_bp.get("/api/trial/process-sheets/<path:ps_id>")
@flows_bp.get("/api/process-sheets/<path:ps_id>")
def api_process_sheet(ps_id):
    with db() as con:
        row = one(
            con.execute(
                """
                SELECT
                    ps.*,
                    p.part_no AS part_name,
                    p.part_desc AS part_desc,
                    sf.bom_code AS selected_bom_code
                FROM process_sheet ps
                LEFT JOIN parts p ON p.part_id = ps.part_id
                LEFT JOIN bom_variation sf ON sf.bom_id = ps.selected_bom_id
                WHERE ps.ps_id = ?
                """,
                (ps_id,),
            )
        )
        if not row:
            return jsonify({"error": "Process sheet not found"}), 404
        payload = dict(row)
        payload["part_name"] = payload.get("part_name") or payload.get("part_no") or ""
        payload["part_desc"] = payload.get("part_desc") or payload.get("part_desc") or ""
        payload["selected_bom_code"] = payload.get("selected_bom_code") or ""
        payload["planning_cards"] = []
        return jsonify(payload)


@trial_prefixed_flows_bp.put("/api/trial/process-sheets/<path:ps_id>/flow")
@flows_bp.put("/api/process-sheets/<path:ps_id>/flow")
def api_process_sheet_selected_flow(ps_id):
    data = request.get_json(force=True, silent=True) or {}
    bom_id = int(data.get("bom_id") or 0)
    bom_code = compact_text(data.get("bom_code"))
    with db() as con:
        ps = one(
            con.execute(
                """
                SELECT ps.*, p.part_no AS part_name, p.part_desc AS part_desc
                FROM process_sheet ps
                LEFT JOIN parts p ON p.part_id = ps.part_id
                WHERE ps.ps_id = ?
                """,
                (ps_id,),
            )
        )
        if not ps:
            return jsonify({"error": "Process sheet not found"}), 404
        part_id = int(ps["part_id"] or 0)
        flow = None
        if bom_id > 0:
            flow = one(
                con.execute(
                    """
                    SELECT bom_id, bom_code
                    FROM bom_variation
                    WHERE bom_id = ? AND part_id = ?
                    """,
                    (bom_id, part_id),
                )
            )
        elif bom_code:
            flow = one(
                con.execute(
                    """
                    SELECT bom_id, bom_code
                    FROM bom_variation
                    WHERE part_id = ? AND bom_code = ?
                    """,
                    (part_id, bom_code),
                )
            )
        if not flow:
            return jsonify({"error": "Flow not found for this PS"}), 404
        con.execute(
            """
            UPDATE process_sheet
            SET selected_bom_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE ps_id = ?
            """,
            (int(flow["bom_id"]), ps_id),
        )
        sync_material_requirements_for_ps(con, ps_id)
        return jsonify(
            {
                "ok": True,
                "ps_id": ps_id,
                "selected_bom_id": int(flow["bom_id"]),
                "selected_bom_code": flow["bom_code"] or "",
            }
        )


@trial_prefixed_flows_bp.get("/api/trial/parts/<int:part_id>/flows")
@flows_bp.get("/api/parts/<int:part_id>/flows")
def api_part_flows(part_id):
    with db() as con:
        part = one(con.execute("SELECT * FROM parts WHERE part_id = ?", (int(part_id),)))
        if not part:
            return jsonify({"error": "Part not found"}), 404
        flows = rows(
            con.execute(
                "SELECT bom_id, bom_code, bom_desc AS bom_name, is_default FROM bom_variation WHERE part_id = ? ORDER BY is_default DESC, bom_id",
                (int(part_id),),
            )
        )
        result = []
        for flow in flows:
            steps = rows(
                con.execute(
                    """
                    SELECT op_seq_id, bom_id, seq_no, op_no, op_type, machine_category, preferred_machine,
                           cycle_time, setup_time, is_last_op
                    FROM operation_seq
                    WHERE bom_id = ?
                    ORDER BY seq_no, op_seq_id
                    """,
                    (int(flow["bom_id"]),),
                )
            )
            item = dict(flow)
            item["steps"] = [dict(step) for step in steps]
            result.append(item)
        return jsonify(result)


@trial_prefixed_flows_bp.put("/api/trial/flows/<int:bom_id>")
@flows_bp.put("/api/flows/<int:bom_id>")
def api_update_flow(bom_id):
    data = request.get_json(force=True, silent=True) or {}
    with db() as con:
        flow = one(con.execute("SELECT * FROM bom_variation WHERE bom_id = ?", (int(bom_id),)))
        if not flow:
            return jsonify({"error": "Flow not found"}), 404
        bom_code = compact_text(data.get("bom_code")) or flow["bom_code"]
        is_default = 1 if data.get("is_default") else 0
        con.execute(
            "UPDATE bom_variation SET bom_code = ?, is_default = ? WHERE bom_id = ?",
            (bom_code, is_default, int(bom_id)),
        )
        steps = data.get("steps") or []
        con.execute("DELETE FROM operation_seq WHERE bom_id = ?", (int(bom_id),))
        for idx, step in enumerate(steps, 1):
            preferred_machine = compact_text(step.get("preferred_machine"))
            machine_category = compact_text(step.get("machine_category")) or "UNKNOWN"
            if preferred_machine:
                machine_row = one(con.execute("SELECT machine_category FROM machines WHERE machine_code = ?", (preferred_machine,)))
                machine_category = compact_text(machine_row["machine_category"] if machine_row else machine_category) or "UNKNOWN"
            con.execute(
                """
                INSERT INTO operation_seq (
                  bom_id, seq_no, op_no, op_type, machine_category, preferred_machine, cycle_time, setup_time, is_last_op
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(bom_id),
                    idx,
                    compact_text(step.get("op_no")),
                    compact_text(step.get("op_type")),
                    machine_category,
                    preferred_machine,
                    parse_number(step.get("cycle_time"), 0),
                    parse_number(step.get("setup_time"), 0),
                    1 if idx == len(steps) else int(step.get("is_last_op") or 0),
                ),
            )
        affected_machine_ids = sync_operations_for_flow(con, bom_id)
        for machine_id in affected_machine_ids:
            recalculate_machine(con, machine_id)
        return jsonify({"ok": True})
