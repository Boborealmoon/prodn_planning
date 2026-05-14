from __future__ import annotations

from .actuals import actual_totals_for_block
from .blocks import trial_block_row
from .db import one, rows
from .imports import trial_catalog_op_key
from .utils import compact_text, parse_number


def trial_catalog_items(con, include_completed=False):
    planned_qty_by_op = {}
    for row in rows(
        con.execute(
            """
            SELECT o.source_ps_id, o.source_op_no, o.source_op_seq_id AS source_op_seq_id,
                   COALESCE(SUM(COALESCE(b.scheduled_qty, 0)), 0) AS planned_qty
            FROM operation o
            JOIN run_block b ON b.operation_id = o.operation_id
            WHERE COALESCE(o.source_ps_id, '') <> ''
              AND COALESCE(b.active, 1) = 1
              AND COALESCE(b.block_type, 'ORIGINAL') <> 'REWORK'
            GROUP BY o.source_ps_id, o.source_op_no, o.source_op_seq_id
            """
        )
    ):
        key = trial_catalog_op_key(row["source_ps_id"], row["source_op_no"], row["source_op_seq_id"])
        planned_qty_by_op[key] = float(row["planned_qty"] or 0)

    records = rows(
        con.execute(
            """
            SELECT ps.ps_id, ps.part_id, ps.part_no AS part_no, ps.part_desc AS part_desc, ps.due_date, ps.total_qty, ps.status, ps.planner_status, ps.selected_bom_id AS selected_bom_id,
                   sf.bom_code AS selected_bom_code,
                   p.part_no AS part_name, pfs.op_seq_id AS op_seq_id, pfs.seq_no, pfs.op_no, pfs.op_type, pfs.machine_category, pfs.preferred_machine,
                   pfs.cycle_time, pfs.setup_time, pfs.is_last_op
            FROM process_sheet ps
            LEFT JOIN parts p ON p.part_id = ps.part_id
            LEFT JOIN bom_variation sf ON sf.bom_id = ps.selected_bom_id
            LEFT JOIN operation_seq pfs ON pfs.bom_id = ps.selected_bom_id
            WHERE COALESCE(ps.selected_bom_id, 0) > 0
              AND (? = 1 OR (COALESCE(ps.planner_status, '') <> 'COMPLETED' AND COALESCE(ps.status, '') <> 'COMPLETED'))
            ORDER BY ps.due_date, ps.ps_id, pfs.seq_no, pfs.op_seq_id
            """,
            (1 if include_completed else 0,),
        )
    )
    unassigned_records = rows(
        con.execute(
            """
            SELECT ps.ps_id, ps.part_id, ps.part_no AS part_no, ps.part_desc AS part_desc, ps.due_date, ps.total_qty, ps.status, ps.planner_status, ps.selected_bom_id AS selected_bom_id,
                   '' AS selected_bom_code,
                   p.part_no AS part_name
            FROM process_sheet ps
            LEFT JOIN parts p ON p.part_id = ps.part_id
            WHERE COALESCE(ps.selected_bom_id, 0) = 0
              AND (? = 1 OR (COALESCE(ps.planner_status, '') <> 'COMPLETED' AND COALESCE(ps.status, '') <> 'COMPLETED'))
            ORDER BY ps.due_date, ps.ps_id
            """,
            (1 if include_completed else 0,),
        )
    )

    grouped = {}
    flow_cache = {}
    for row in records:
        ps_id = compact_text(row["ps_id"])
        op_seq_id = int(row["op_seq_id"] or 0)
        op_key = trial_catalog_op_key(ps_id, row["op_no"], op_seq_id)
        required_qty = float(row["total_qty"] or 0)
        planned_qty = float(planned_qty_by_op.get(op_key, 0) or 0)
        remaining_qty = max(0.0, required_qty - planned_qty)
        item = grouped.setdefault(
            ps_id,
            {
                "ps_id": ps_id,
                "part_id": int(row["part_id"] or 0),
                "part_name": row["part_name"] or "",
                "part_no": row["part_no"] or "",
                "part_desc": row["part_desc"] or "",
                "due_date": row["due_date"] or "",
                "total_qty": float(row["total_qty"] or 0),
                "status": row["status"] or "",
                "planner_status": row["planner_status"] or "",
                "selected_bom_id": int(row["selected_bom_id"] or 0),
                "selected_bom_code": row["selected_bom_code"] or "",
                "ops": [],
                "all_ops": [],
            },
        )
        op_item = {
            "source_ps_id": ps_id,
            "source_op_seq_id": op_seq_id,
            "source_op_no": row["op_no"] or "",
            "op_no": row["op_no"] or "",
            "op_type": row["op_type"] or "",
            "machine_category": row["machine_category"] or "",
            "preferred_machine": row["preferred_machine"] or "",
            "cycle_time": float(row["cycle_time"] or 0),
            "setup_time": float(row["setup_time"] or 0),
            "is_last_op": int(row["is_last_op"] or 0),
            "job_no": ps_id,
            "operation_name": f"{row['op_no'] or ''} {row['op_type'] or ''}".strip(),
            "total_qty": remaining_qty,
            "required_qty": required_qty,
            "planned_qty": planned_qty,
            "remaining_qty": remaining_qty,
            "compatible_machine_group": row["machine_category"] or "",
        }
        item["all_ops"].append(op_item)
        if remaining_qty > 0:
            item["ops"].append(op_item)

    available = []
    planned = []
    planning_cards_map = planning_cards_by_ps(con)
    covered_map = planning_card_covered_op_keys(con)

    def flow_options_for_part(part_id):
        part_id = int(part_id or 0)
        if not part_id:
            return []
        if part_id not in flow_cache:
            flow_cache[part_id] = [dict(flow) for flow in rows(con.execute(
                """
                SELECT bom_id AS bom_id, bom_code AS bom_code, bom_desc AS bom_desc, is_default
                FROM bom_variation
                WHERE part_id = ?
                ORDER BY is_default DESC, bom_id
                """,
                (part_id,),
            ))]
        return flow_cache[part_id]

    for item in grouped.values():
        item["flow_options"] = flow_options_for_part(item["part_id"])
        item["planning_cards"] = planning_cards_map.get(item["ps_id"], [])
        covered_keys = covered_map.get(item["ps_id"], set())
        op_cards = []

        for card in item["planning_cards"]:
            group_operation_name = " + ".join(
                compact_text(op.get("op_type") or op.get("operation_name") or op.get("source_op_no"))
                for op in card.get("ops", [])
                if compact_text(op.get("op_type") or op.get("operation_name") or op.get("source_op_no"))
            )
            op_cards.append(
                {
                    "card_kind": "group",
                    "card_id": int(card["card_id"]),
                    "ps_id": card["ps_id"] or item["ps_id"],
                    "operation_label": card["operation_label"] or "",
                    "operation_name": group_operation_name,
                    "target_qty": float(card["target_qty"] or 0),
                    "remaining_qty": float(card["target_qty"] or 0),
                    "card_type": card["card_type"] or "COMBINED",
                    "planning_status": card["planning_status"] or "UNSCHEDULED",
                    "is_scheduled": bool(card["is_scheduled"]),
                    "machine_id": int(card["machine_id"] or 0),
                    "machine_code": card["machine_code"] or "",
                    "setup_minutes": float(card["setup_minutes"] or 0),
                    "cycle_minutes_per_qty": float(card["cycle_minutes_per_qty"] or 0),
                    "ops": card.get("ops", []),
                }
            )

        for op in item["ops"]:
            op_key = trial_catalog_op_key(op["source_ps_id"], op["source_op_no"], op["source_op_seq_id"])
            if op_key in covered_keys:
                continue
            op_cards.append(
                {
                    "card_kind": "single",
                    "card_id": None,
                    "ps_id": op["source_ps_id"] or item["ps_id"],
                    "operation_label": op["source_op_no"] or op["operation_name"] or op["op_type"] or "",
                    "operation_name": op["op_type"] or op["operation_name"] or "",
                    "target_qty": float(op["remaining_qty"] or 0),
                    "remaining_qty": float(op["remaining_qty"] or 0),
                    "source_op_seq_id": int(op["source_op_seq_id"] or 0),
                    "source_op_no": op["source_op_no"] or "",
                    "job_no": op["job_no"] or "",
                    "planning_status": "UNSCHEDULED",
                    "card_type": "SINGLE",
                    "is_scheduled": False,
                    "setup_minutes": float(op["setup_time"] or 0),
                    "cycle_minutes_per_qty": float(op["cycle_time"] or 0),
                    "compatible_machine_group": op["compatible_machine_group"] or "",
                    "op": op,
                }
            )

        item["op_cards"] = op_cards
        if not item["selected_bom_code"] and item["selected_bom_id"]:
            item["selected_bom_code"] = next(
                (flow["bom_code"] for flow in item["flow_options"] if int(flow["bom_id"]) == int(item["selected_bom_id"])),
                "",
            )
        if item["op_cards"]:
            available.append(item)
        else:
            planned.append(
                {
                    "ps_id": item["ps_id"],
                    "part_id": item["part_id"],
                    "part_name": item["part_name"],
                    "part_no": item["part_no"],
                    "part_desc": item["part_desc"],
                    "due_date": item["due_date"],
                    "total_qty": item["total_qty"],
                    "status": item["status"],
                    "planner_status": item["planner_status"],
                    "selected_bom_id": item["selected_bom_id"],
                    "selected_bom_code": item["selected_bom_code"],
                    "flow_options": item["flow_options"],
                    "planning_cards": item["planning_cards"],
                    "op_cards": item["op_cards"],
                }
            )

    for row in unassigned_records:
        ps_id = compact_text(row["ps_id"])
        part_id = int(row["part_id"] or 0)
        flow_options = flow_options_for_part(part_id)
        planned.append(
            {
                "ps_id": ps_id,
                "part_id": part_id,
                "part_name": row["part_name"] or "",
                "part_no": row["part_no"] or "",
                "part_desc": row["part_desc"] or "",
                "due_date": row["due_date"] or "",
                "total_qty": float(row["total_qty"] or 0),
                "status": row["status"] or "",
                "planner_status": row["planner_status"] or "",
                "selected_bom_id": int(row["selected_bom_id"] or 0),
                "selected_bom_code": row["selected_bom_code"] or "",
                "flow_options": flow_options,
                "planning_cards": planning_cards_map.get(ps_id, []),
                "op_cards": [],
            }
        )

    return {"available": available, "planned": planned}


def combined_group_summary(con, group_id):
    group_id = int(group_id or 0)
    if not group_id:
        return None

    group = one(
        con.execute(
            """
            SELECT group_id, group_label, group_type, created_at
            FROM run_block_group
            WHERE group_id = ?
            """,
            (group_id,),
        )
    )
    if not group:
        return None

    blocks = rows(
        con.execute(
            """
            SELECT b.*, o.job_no, o.operation_name, o.total_qty, o.setup_minutes, o.cycle_minutes_per_qty,
                   o.compatible_machine_group, o.source_ps_id, o.source_op_seq_id, o.source_op_no,
                   m.machine_code, m.machine_category, m.shift_profile
            FROM run_block b
            JOIN operation o ON o.operation_id = b.operation_id
            JOIN machines m ON m.machine_id = b.machine_id
            WHERE b.group_id = ?
            ORDER BY b.queue_position, b.block_id
            """,
            (group_id,),
        )
    )
    if not blocks:
        return {
            "group_id": group_id,
            "group_label": group["group_label"] or "",
            "group_type": group["group_type"] or "COMBINED",
            "ps_id": "",
            "operation_label": "",
            "machine_id": 0,
            "machine_code": "",
            "shift_profile": "",
            "blocks": [],
            "target_qty": 0.0,
            "setup_minutes": 0.0,
            "cycle_minutes_per_qty": 0.0,
            "actual_good_qty": 0.0,
            "actual_reject_qty": 0.0,
            "paired_output_qty": 0.0,
            "paired_remaining_qty": 0.0,
            "paired_remaining_minutes": 0.0,
            "remaining_qty": 0.0,
            "remaining_minutes": 0.0,
            "status": "NOT_STARTED",
            "planning_status": "UNPLANNED",
            "group_start": "",
            "group_end": "",
        }

    member_rows = []
    first_block = blocks[0]
    for block in blocks:
        totals = actual_totals_for_block(con, block["block_id"])
        output_qty = float(totals["output_qty"] or 0)
        reject_qty = float(totals["reject_qty"] or 0)
        scheduled_qty = max(0.0, float(block["scheduled_qty"] or 0))
        valid_done = max(0.0, output_qty - reject_qty)
        remaining_qty = max(0.0, scheduled_qty - valid_done)
        member_rows.append(
            {
                "block_id": int(block["block_id"]),
                "operation_id": int(block["operation_id"]),
                "machine_id": int(block["machine_id"]),
                "queue_position": float(block["queue_position"] or 0),
                "job_no": block["job_no"] or "",
                "operation_name": block["operation_name"] or "",
                "source_op_no": block["source_op_no"] or "",
                "scheduled_qty": scheduled_qty,
                "output_qty": output_qty,
                "reject_qty": reject_qty,
                "valid_done_qty": valid_done,
                "remaining_qty": remaining_qty,
                "remaining_minutes": remaining_qty * max(0.0, float(block["cycle_minutes_per_qty"] or 0)),
                "setup_minutes": float(block["setup_minutes"] or 0),
                "cycle_minutes_per_qty": float(block["cycle_minutes_per_qty"] or 0),
                "planning_status": block["planning_status"] or "",
                "execution_status": block["execution_status"] or "",
                "status": block["status"] or "",
                "anchor_datetime": block["anchor_datetime"] or "",
                "calculated_start_datetime": block["calculated_start_datetime"] or "",
                "calculated_end_datetime": block["calculated_end_datetime"] or "",
                "block_type": block["block_type"] or "ORIGINAL",
                "machine_code": block["machine_code"] or "",
                "machine_category": block["machine_category"] or "",
            }
        )

    target_qty = max((row["scheduled_qty"] for row in member_rows), default=0.0)
    max_setup = max((row["setup_minutes"] for row in member_rows), default=0.0)
    cycle_sum = sum(row["cycle_minutes_per_qty"] for row in member_rows)
    actual_good_qty = sum(row["valid_done_qty"] for row in member_rows)
    actual_reject_qty = sum(row["reject_qty"] for row in member_rows)
    paired_good_qty = min((row["valid_done_qty"] for row in member_rows), default=0.0)
    remaining_qty = max(0.0, target_qty - paired_good_qty)
    remaining_minutes = remaining_qty * cycle_sum
    for row in member_rows:
        row["member_net_output"] = row["valid_done_qty"]
        row["paired_excess_qty"] = max(0.0, row["valid_done_qty"] - paired_good_qty)
        row["paired_shortfall_qty"] = max(0.0, paired_good_qty - row["valid_done_qty"])
    starts = [row["calculated_start_datetime"] for row in member_rows if compact_text(row["calculated_start_datetime"])]
    ends = [row["calculated_end_datetime"] for row in member_rows if compact_text(row["calculated_end_datetime"])]
    status_values = [compact_text(row["execution_status"] or row["status"]).upper() for row in member_rows]
    planning_values = [compact_text(row["planning_status"]).upper() for row in member_rows]
    status = "DONE" if status_values and all(text == "DONE" for text in status_values) else ("IN_PROGRESS" if any(text in {"IN_PROGRESS", "DONE"} for text in status_values) else "NOT_STARTED")
    planning_status = "PARTIALLY_PLANNED" if "PARTIALLY_PLANNED" in planning_values else ("PLANNED" if planning_values and all(text == "PLANNED" for text in planning_values) else (planning_values[0] if planning_values else "UNPLANNED"))
    ps_id = compact_text(first_block["job_no"] or first_block["source_ps_id"] or "")
    operation_label = group["group_label"] or " & ".join(
        compact_text(row["source_op_no"] or row["operation_name"] or f"Block {row['block_id']}")
        for row in member_rows
        if compact_text(row["source_op_no"] or row["operation_name"])
    )

    return {
        "group_id": group_id,
        "group_label": group["group_label"] or "",
        "group_type": group["group_type"] or "COMBINED",
        "ps_id": ps_id,
        "operation_label": operation_label,
        "machine_id": int(first_block["machine_id"] or 0),
        "machine_code": first_block["machine_code"] or "",
        "shift_profile": first_block["shift_profile"] or "",
        "blocks": member_rows,
        "target_qty": target_qty,
        "setup_minutes": max_setup,
        "cycle_minutes_per_qty": cycle_sum,
        "actual_good_qty": actual_good_qty,
        "actual_reject_qty": actual_reject_qty,
        "paired_output_qty": paired_output_qty,
        "paired_remaining_qty": remaining_qty,
        "paired_remaining_minutes": remaining_minutes,
        "remaining_qty": remaining_qty,
        "remaining_minutes": remaining_minutes,
        "status": status,
        "planning_status": planning_status,
        "group_start": min(starts) if starts else "",
        "group_end": max(ends) if ends else "",
    }


def planning_card_row(con, card_id):
    return one(
        con.execute(
            """
            SELECT c.*, m.machine_code
            FROM planning_card c
            LEFT JOIN machines m ON m.machine_id = c.machine_id
            WHERE c.card_id = ?
            """,
            (int(card_id),),
        )
    )


def planning_card_payload(con, card_id):
    card = planning_card_row(con, card_id)
    if not card:
        return None
    ops = rows(
        con.execute(
            """
            SELECT *
            FROM planning_card_operation
            WHERE card_id = ?
            ORDER BY op_sequence, card_op_id
            """,
            (int(card_id),),
        )
    )
    op_rows = [dict(op) for op in ops]
    label = compact_text(card["operation_label"])
    if not label:
        label = " & ".join(
            compact_text(op["source_op_no"] or f"Op {op['op_sequence']}") for op in op_rows if compact_text(op["source_op_no"])
        )
    operation_name = " + ".join(
        compact_text(op.get("op_type") or op.get("operation_name") or op.get("source_op_no"))
        for op in op_rows
        if compact_text(op.get("op_type") or op.get("operation_name") or op.get("source_op_no"))
    )
    target_qty = float(card["target_qty"] or 0)
    setup_minutes = max((float(op["setup_minutes"] or 0) for op in op_rows), default=0.0)
    cycle_minutes_per_qty = sum(float(op["cycle_minutes_per_qty"] or 0) for op in op_rows)
    return {
        "card_id": int(card["card_id"]),
        "ps_id": card["ps_id"] or "",
        "operation_label": label,
        "operation_name": operation_name,
        "target_qty": target_qty,
        "planning_status": card["planning_status"] or "UNSCHEDULED",
        "card_kind": "group",
        "card_type": card["card_type"] or "NORMAL",
        "machine_id": int(card["machine_id"] or 0),
        "machine_code": card["machine_code"] or "",
        "scheduled_block_group_id": int(card["scheduled_block_group_id"] or 0),
        "is_scheduled": compact_text(card["planning_status"]).upper() == "SCHEDULED" or int(card["scheduled_block_group_id"] or 0) > 0,
        "setup_minutes": setup_minutes,
        "cycle_minutes_per_qty": cycle_minutes_per_qty,
        "ops": [
            {
                "card_op_id": int(op["card_op_id"]),
                "source_ps_id": op["source_ps_id"] or "",
                "source_op_seq_id": int(op["source_op_seq_id"] or 0),
                "source_op_no": op["source_op_no"] or "",
                "op_sequence": int(op["op_sequence"] or 0),
                "setup_minutes": float(op["setup_minutes"] or 0),
                "cycle_minutes_per_qty": float(op["cycle_minutes_per_qty"] or 0),
                "target_qty": float(op["target_qty"] or 0),
            }
            for op in op_rows
        ],
    }


def planning_cards_by_ps(con):
    cards = rows(
        con.execute(
            """
            SELECT c.*, m.machine_code
            FROM planning_card c
            LEFT JOIN machines m ON m.machine_id = c.machine_id
            ORDER BY c.ps_id, c.card_id
            """
        )
    )
    if not cards:
        return {}
    card_ids = [int(card["card_id"]) for card in cards]
    placeholders = ",".join("?" for _ in card_ids)
    ops_by_card = {}
    for op in rows(
        con.execute(
            f"""
            SELECT *
            FROM planning_card_operation
            WHERE card_id IN ({placeholders})
            ORDER BY card_id, op_sequence, card_op_id
            """,
            card_ids,
        )
    ):
        ops_by_card.setdefault(int(op["card_id"]), []).append(op)

    grouped = {}
    for card in cards:
        card_id = int(card["card_id"])
        op_rows = [dict(op) for op in ops_by_card.get(card_id, [])]
        label = compact_text(card["operation_label"])
        if not label:
            label = " & ".join(
                compact_text(op["source_op_no"] or f"Op {op['op_sequence']}")
                for op in op_rows
                if compact_text(op["source_op_no"])
            )
        operation_name = " + ".join(
            compact_text(op.get("op_type") or op.get("operation_name") or op.get("source_op_no"))
            for op in op_rows
            if compact_text(op.get("op_type") or op.get("operation_name") or op.get("source_op_no"))
        )
        target_qty = float(card["target_qty"] or 0)
        setup_minutes = max((float(op["setup_minutes"] or 0) for op in op_rows), default=0.0)
        cycle_minutes_per_qty = sum(float(op["cycle_minutes_per_qty"] or 0) for op in op_rows)
        item = {
            "card_id": card_id,
            "ps_id": card["ps_id"] or "",
            "operation_label": label,
            "operation_name": operation_name,
            "target_qty": target_qty,
            "planning_status": card["planning_status"] or "UNSCHEDULED",
            "card_kind": "group",
            "card_type": card["card_type"] or "NORMAL",
            "machine_id": int(card["machine_id"] or 0),
            "machine_code": card["machine_code"] or "",
            "scheduled_block_group_id": int(card["scheduled_block_group_id"] or 0),
            "is_scheduled": compact_text(card["planning_status"]).upper() == "SCHEDULED" or int(card["scheduled_block_group_id"] or 0) > 0,
            "setup_minutes": setup_minutes,
            "cycle_minutes_per_qty": cycle_minutes_per_qty,
            "ops": [
                {
                    "card_op_id": int(op["card_op_id"]),
                    "source_ps_id": op["source_ps_id"] or "",
                    "source_op_seq_id": int(op["source_op_seq_id"] or 0),
                    "source_op_no": op["source_op_no"] or "",
                    "op_sequence": int(op["op_sequence"] or 0),
                    "setup_minutes": float(op["setup_minutes"] or 0),
                    "cycle_minutes_per_qty": float(op["cycle_minutes_per_qty"] or 0),
                    "target_qty": float(op["target_qty"] or 0),
                }
                for op in op_rows
            ],
        }
        grouped.setdefault(card["ps_id"] or "", []).append(item)
    return grouped


def planning_card_covered_op_keys(con):
    covered = {}
    for row in rows(
        con.execute(
            """
            SELECT pc.ps_id, pco.source_op_seq_id AS source_op_seq_id, pco.source_op_no
            FROM planning_card pc
            JOIN planning_card_operation pco ON pco.card_id = pc.card_id
            """
        )
    ):
        ps_id = compact_text(row["ps_id"])
        op_key = trial_catalog_op_key(ps_id, row["source_op_no"], row["source_op_seq_id"])
        covered.setdefault(ps_id, set()).add(op_key)
    return covered


def planning_card_target_from_ops(ops):
    return max([float(op.get("total_qty") or op.get("remaining_qty") or 0) for op in ops] or [0.0])


def planning_card_label_from_ops(ops):
    labels = []
    for op in ops:
        label = compact_text(op.get("source_op_no") or op.get("op_no") or op.get("operation_name"))
        if label:
            labels.append(label)
    return " & ".join(labels)


def create_planning_card(con, ps_id, ops, target_qty=None):
    ps_id = compact_text(ps_id)
    ops = [op for op in ops if op]
    if not ps_id:
        raise ValueError("Process sheet is required")
    if len(ops) < 2:
        raise ValueError("Choose at least two operations.")
    ps = one(
        con.execute(
            """
            SELECT ps.*, p.part_no AS part_name
            FROM process_sheet ps
            LEFT JOIN parts p ON p.part_id = ps.part_id
            WHERE ps.ps_id = ?
            """,
            (ps_id,),
        )
    )
    if not ps:
        raise ValueError(f"Process sheet not found: {ps_id}")
    selected_bom_id = int(ps["selected_bom_id"] or 0)
    if not selected_bom_id:
        raise ValueError(f"Process sheet has no selected flow: {ps_id}")

    normalized_ops = []
    seen_ops = set()
    for op in ops:
        op_ps_id = compact_text(op.get("source_ps_id") or op.get("ps_id") or ps_id)
        if op_ps_id and op_ps_id != ps_id:
            raise ValueError("Combine operations from the same process sheet.")
        source_op_seq_id = int(op.get("source_op_seq_id") or 0)
        source_op_no = compact_text(op.get("source_op_no") or op.get("op_no"))
        if not source_op_seq_id and not source_op_no:
            raise ValueError("Operation not found in this process sheet.")
        dedupe_key = (source_op_seq_id or 0, source_op_no.upper())
        if dedupe_key in seen_ops:
            raise ValueError("Duplicate operation selected.")
        seen_ops.add(dedupe_key)
        normalized_ops.append(op)

    resolved_ops = []
    for idx, op in enumerate(normalized_ops, 1):
        source_op_seq_id = int(op.get("source_op_seq_id") or 0)
        source_op_no = compact_text(op.get("source_op_no") or op.get("op_no"))
        step = None
        if source_op_seq_id:
            step = one(
                con.execute(
                    """
                    SELECT *
                    FROM operation_seq
                    WHERE op_seq_id = ? AND bom_id = ?
                    """,
                    (source_op_seq_id, selected_bom_id),
                )
            )
        if not step and source_op_no:
            step = one(
                con.execute(
                    """
                    SELECT *
                    FROM operation_seq
                    WHERE bom_id = ? AND op_no = ?
                    ORDER BY seq_no, op_seq_id
                    LIMIT 1
                    """,
                    (selected_bom_id, source_op_no),
                )
            )
        if not step:
            raise ValueError("Operation not found in this process sheet.")
        resolved_ops.append((idx, step, source_op_no))

    actual_target_qty = float(target_qty or 0)
    if actual_target_qty <= 0:
        actual_target_qty = planning_card_target_from_ops(ops)
    if actual_target_qty <= 0:
        raise ValueError("Target qty is required")

    operation_label = planning_card_label_from_ops(ops)
    card_type = "COMBINED"
    card_cur = con.execute(
        """
        INSERT INTO planning_card (
          ps_id, operation_label, target_qty, planning_status, card_type, created_at, updated_at
        ) VALUES (?, ?, ?, 'UNSCHEDULED', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (ps_id, operation_label, actual_target_qty, card_type),
    )
    card_id = int(card_cur.lastrowid)
    for idx, step, source_op_no in resolved_ops:
        con.execute(
            """
            INSERT INTO planning_card_operation (
              card_id, source_ps_id, source_op_seq_id, source_op_no, op_sequence, setup_minutes, cycle_minutes_per_qty, target_qty
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                card_id,
                ps_id,
                int(step["op_seq_id"] or 0),
                source_op_no or (step["op_no"] or ""),
                idx,
                parse_number(step["setup_time"], 0),
                parse_number(step["cycle_time"], 0),
                actual_target_qty,
            ),
        )
    return planning_card_payload(con, card_id)


def schedule_planning_card(con, card_id, machine_id, queue_position=0):
    card = planning_card_row(con, card_id)
    if not card:
        raise ValueError("Combined op card not found")
    if compact_text(card["planning_status"]).upper() == "SCHEDULED" and int(card["scheduled_block_group_id"] or 0) > 0:
        raise ValueError("Combined op card is already scheduled")
    machine_id = int(machine_id or card["machine_id"] or 0)
    if not machine_id:
        raise ValueError("Machine is required")
    machine = one(
        con.execute(
            "SELECT * FROM machines WHERE machine_id = ? AND active = 1",
            (machine_id,),
        )
    )
    if not machine:
        raise ValueError("Machine not found")
    ops = rows(
        con.execute(
            """
            SELECT *
            FROM planning_card_operation
            WHERE card_id = ?
            ORDER BY op_sequence, card_op_id
            """,
            (int(card_id),),
        )
    )
    if not ops:
        raise ValueError("Combined op card has no operations")

    group_label = compact_text(card["operation_label"])
    if not group_label:
        group_label = " & ".join(compact_text(op["source_op_no"] or f"Op {op['op_sequence']}") for op in ops if compact_text(op["source_op_no"]))
    if not group_label:
        group_label = "Planned card"

    group_cur = con.execute(
        "INSERT INTO run_block_group (group_label, group_type) VALUES (?, 'PLANNED_CARD')",
        (group_label,),
    )
    group_id = int(group_cur.lastrowid)
    max_position = float(
        one(
            con.execute(
                "SELECT COALESCE(MAX(queue_position), 0) AS mx FROM run_block WHERE machine_id = ?",
                (machine_id,),
            )
        )["mx"]
        or 0
    )
    if queue_position <= 0:
        queue_position = max_position + 1
    created_block_ids = []
    for idx, op in enumerate(ops, 1):
        ps_id = compact_text(op["source_ps_id"])
        ps = one(con.execute("SELECT * FROM process_sheet WHERE ps_id = ?", (ps_id,)))
        if not ps:
            raise ValueError(f"Process sheet not found: {ps_id}")
        step = one(
            con.execute(
                """
                SELECT *
                FROM operation_seq
                WHERE op_seq_id = ? AND bom_id = ?
                """,
                (int(op["source_op_seq_id"] or 0), int(ps["selected_bom_id"] or 0)),
            )
        )
        if not step and compact_text(op["source_op_no"]):
            step = one(
                con.execute(
                    """
                    SELECT *
                    FROM operation_seq
                    WHERE bom_id = ? AND op_no = ?
                    ORDER BY seq_no, op_seq_id
                    LIMIT 1
                    """,
                    (int(ps["selected_bom_id"] or 0), compact_text(op["source_op_no"])),
                )
            )
        if not step:
            raise ValueError(f"Operation not found for scheduling: {op['source_op_no'] or op['source_op_seq_id']}")

        op_name = f"{step['op_no'] or ''} {step['op_type'] or ''}".strip()
        op_cur = con.execute(
            """
            INSERT INTO operation (
              job_no, operation_name, total_qty, setup_minutes, cycle_minutes_per_qty, compatible_machine_group,
              source_ps_id, source_op_seq_id, source_op_no, status, remarks, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', '', CURRENT_TIMESTAMP)
            """,
            (
                ps_id,
                op_name,
                float(card["target_qty"] or 0),
                parse_number(step["setup_time"], 0),
                parse_number(step["cycle_time"], 0),
                compact_text(step["machine_category"]) or compact_text(machine["machine_category"]) or "UNKNOWN",
                ps_id,
                int(step["op_seq_id"] or 0),
                compact_text(step["op_no"]),
            ),
        )
        operation_id = int(op_cur.lastrowid)
        block_cur = con.execute(
            """
            INSERT INTO run_block (
              operation_id, machine_id, queue_position, scheduled_qty, include_setup,
              status, planning_status, execution_status, anchor_datetime,
              calculated_start_datetime, calculated_end_datetime,
              actual_good_qty, actual_reject_qty, remarks, group_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'NOT_STARTED', 'PLANNED', 'NOT_STARTED', '',
                      '', '', 0, 0, '', ?, CURRENT_TIMESTAMP)
            """,
            (
                operation_id,
                machine_id,
                float(queue_position) + idx - 1,
                float(card["target_qty"] or 0),
                1,
                group_id,
            ),
        )
        created_block_ids.append(int(block_cur.lastrowid))

    con.execute(
        """
        UPDATE planning_card
        SET planning_status = 'SCHEDULED',
            machine_id = ?,
            scheduled_block_group_id = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE card_id = ?
        """,
        (machine_id, group_id, int(card_id)),
    )
    return {
        "card": planning_card_payload(con, card_id),
        "group": combined_group_summary(con, group_id),
        "block_ids": created_block_ids,
        "group_id": group_id,
    }
