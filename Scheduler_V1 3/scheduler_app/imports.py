from __future__ import annotations

import json

from .blocks import recalculate_machine, trial_block_row
from .db import dt_now_text, one, rows
from .materials import sync_material_requirements_for_ps
from .machines import fetch_machines
from .utils import (
    compact_text,
    first_nonempty,
    load_trial_active_sheet,
    load_trial_workbook,
    parse_date_text,
    parse_number,
    trial_process_sheet_completed,
    workbook_partial_qty,
)


def trial_log_import(con, import_type, workbook_name="", active_sheet_name="", status="STARTED", message=""):
    cur = con.execute(
        """
        INSERT INTO data_import_log (import_type, workbook_name, active_sheet_name, status, message, created_at, completed_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
        """,
        (import_type, workbook_name, active_sheet_name, status, message, "" if status == "STARTED" else dt_now_text()),
    )
    return int(cur.lastrowid)


def trial_finish_import_log(con, log_id, status, message=""):
    con.execute(
        """
        UPDATE data_import_log
        SET status = ?, message = ?, completed_at = ?
        WHERE log_id = ?
        """,
        (status, message, dt_now_text(), int(log_id)),
    )


def trial_catalog_op_key(source_ps_id, source_op_no="", source_op_seq_id=0):
    ps_key = compact_text(source_ps_id)
    op_key = compact_text(source_op_no)
    if op_key:
        return (ps_key, op_key)
    op_seq_id = int(source_op_seq_id or 0)
    if op_seq_id > 0:
        return (ps_key, f"step:{op_seq_id}")
    return (ps_key, "")


def trial_upsert_bom_materials(con, material_rows):
    counts = {"inserted": 0, "updated": 0, "skipped": 0}
    for row in material_rows or []:
        bom_code = compact_text(row.get("bom_code"))
        source_inventory_code = first_nonempty(row, "source_inventory_code", "inventory_code", "part_no", "item_code")
        material_inventory_code = first_nonempty(row, "material_inventory_code", "material_code", "material", "material_no")
        description = first_nonempty(row, "description", "material_description", "material_desc")
        if not bom_code or not source_inventory_code or not material_inventory_code:
            counts["skipped"] += 1
            continue

        existing = one(
            con.execute(
                """
                SELECT bom_material_id, material_description
                FROM bom_material
                WHERE source_inventory_code = ? AND bom_code = ? AND material_inventory_code = ?
                """,
                (source_inventory_code, bom_code, material_inventory_code),
            )
        )
        if existing:
            existing_description = compact_text(existing["material_description"])
            next_description = description or existing_description
            if next_description == existing_description:
                counts["skipped"] += 1
                continue
            con.execute(
                """
                UPDATE bom_material
                SET material_description = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE bom_material_id = ?
                """,
                (next_description, int(existing["bom_material_id"])),
            )
            counts["updated"] += 1
            continue

        con.execute(
            """
            INSERT INTO bom_material (
              source_inventory_code,
              bom_code,
              material_inventory_code,
              material_description,
              updated_at
            ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                source_inventory_code,
                bom_code,
                material_inventory_code,
                description,
            ),
        )
        counts["inserted"] += 1
    return counts


def trial_find_or_create_part(con, part_no, description=""):
    part_no = compact_text(part_no)
    if not part_no:
        return 0
    part = one(con.execute("SELECT * FROM parts WHERE part_no = ?", (part_no,)))
    if part:
        if description and compact_text(part["part_desc"]) != compact_text(description):
            con.execute("UPDATE parts SET part_desc = ? WHERE part_id = ?", (compact_text(description), int(part["part_id"])))
        return int(part["part_id"])
    cur = con.execute("INSERT INTO parts (part_no, part_desc) VALUES (?, ?)", (part_no, compact_text(description)))
    return int(cur.lastrowid)


def trial_find_or_create_flow(con, part_id, bom_code):
    bom_code = compact_text(bom_code)
    if not part_id or not bom_code:
        return 0
    flow = one(con.execute("SELECT * FROM bom_variation WHERE part_id = ? AND bom_code = ?", (int(part_id), bom_code)))
    if flow:
        return int(flow["bom_id"])
    existing_count = int(one(con.execute("SELECT COUNT(*) AS cnt FROM bom_variation WHERE part_id = ?", (int(part_id),)))["cnt"] or 0)
    cur = con.execute(
        "INSERT INTO bom_variation (part_id, bom_code, bom_desc, is_default) VALUES (?, ?, ?, ?)",
        (int(part_id), bom_code, "", 1 if existing_count <= 0 else 0),
    )
    return int(cur.lastrowid)


def trial_upsert_flow_from_rows(con, part_no, bom_code, stage_rows):
    part_id = trial_find_or_create_part(con, part_no, "")
    if not part_id or not bom_code:
        return 0
    bom_id = trial_find_or_create_flow(con, part_id, bom_code)
    if not bom_id:
        return 0
    con.execute("DELETE FROM operation_seq WHERE bom_id = ?", (bom_id,))
    for idx, row in enumerate(stage_rows, 1):
        op_no = first_nonempty(row, "op_no", "stage_no")
        op_type = first_nonempty(row, "op_type", "stage_desc", "stage")
        preferred_machine = first_nonempty(row, "preferred_machine", "machine_no", "machine_code", "machine")
        machine_category = first_nonempty(row, "machine_category")
        if not machine_category and preferred_machine:
            machine_row = one(con.execute("SELECT machine_category FROM machines WHERE machine_code = ?", (preferred_machine,)))
            machine_category = compact_text(machine_row["machine_category"]) if machine_row else "UNKNOWN"
        con.execute(
            """
            INSERT INTO operation_seq (bom_id, seq_no, op_no, op_type, machine_category, preferred_machine, cycle_time, setup_time, is_last_op)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bom_id,
                idx,
                op_no,
                op_type,
                machine_category or "UNKNOWN",
                preferred_machine,
                parse_number(row.get("cycle_time"), 0),
                parse_number(row.get("setup_time"), 0),
                1 if idx == len(stage_rows) else 0,
            ),
        )
    return bom_id


def sync_operations_for_flow(con, bom_id):
    bom_id = int(bom_id or 0)
    if not bom_id:
        return set()

    process_sheets = rows(
        con.execute(
            """
            SELECT ps_id
            FROM process_sheet
            WHERE selected_bom_id = ?
              AND COALESCE(status, '') <> 'COMPLETED'
              AND COALESCE(planner_status, '') <> 'COMPLETED'
            """,
            (bom_id,),
        )
    )
    if not process_sheets:
        return set()

    steps = rows(
        con.execute(
            """
            SELECT op_seq_id AS op_seq_id, op_no, op_type, machine_category, preferred_machine, cycle_time, setup_time
            FROM operation_seq
            WHERE bom_id = ?
            ORDER BY seq_no, op_seq_id
            """,
            (bom_id,),
        )
    )
    if not steps:
        return set()

    updated_operation_ids = set()
    for ps_row in process_sheets:
        ps_id = compact_text(ps_row["ps_id"])
        if not ps_id:
            continue
        for step in steps:
            op_no = compact_text(step["op_no"])
            if not op_no:
                continue

            existing_op = one(
                con.execute(
                    """
                    SELECT operation_id
                    FROM operation
                    WHERE source_ps_id = ? AND source_op_no = ?
                    ORDER BY operation_id
                    LIMIT 1
                    """,
                    (ps_id, op_no),
                )
            )
            if not existing_op:
                existing_op = one(
                    con.execute(
                        """
                        SELECT operation_id
                        FROM operation
                        WHERE source_ps_id = ? AND source_op_seq_id = ?
                        ORDER BY operation_id
                        LIMIT 1
                        """,
                        (ps_id, int(step["op_seq_id"] or 0)),
                    )
                )
            if not existing_op:
                operation_name = compact_text(step["op_type"] or "")
                cur = con.execute(
                    """
                    INSERT INTO operation (
                      job_no, operation_name, total_qty, setup_minutes, cycle_minutes_per_qty, compatible_machine_group,
                      source_ps_id, pp_partial_no, selected_bom_id, source_op_seq_id, source_op_no, status, remarks, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        ps_id,
                        operation_name,
                        0.0,
                        parse_number(step["setup_time"], 0),
                        parse_number(step["cycle_time"], 0),
                        compact_text(step["machine_category"]) or "UNKNOWN",
                        ps_id,
                        compact_text(ps_row.get("pp_partial_no") or ""),
                        bom_id,
                        int(step["op_seq_id"] or 0),
                        op_no,
                        "ACTIVE",
                        "",
                    ),
                )
                updated_operation_ids.add(int(cur.lastrowid))
                continue

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
                    updated_at = CURRENT_TIMESTAMP
                WHERE operation_id = ?
                """,
                (
                    compact_text(step["op_type"] or ""),
                    ps_id,
                    compact_text(ps_row.get("pp_partial_no") or ""),
                    bom_id,
                    parse_number(step["setup_time"], 0),
                    parse_number(step["cycle_time"], 0),
                    compact_text(step["machine_category"]) or "UNKNOWN",
                    int(step["op_seq_id"] or 0),
                    op_no,
                    int(existing_op["operation_id"]),
                ),
            )
            updated_operation_ids.add(int(existing_op["operation_id"]))

    if not updated_operation_ids:
        return set()

    placeholders = ",".join("?" for _ in updated_operation_ids)
    affected_machine_ids = {
        int(row["machine_id"])
        for row in rows(
            con.execute(
                f"""
                SELECT DISTINCT machine_id
                FROM run_block
                WHERE operation_id IN ({placeholders})
                """,
                list(updated_operation_ids),
            )
        )
    }
    return {machine_id for machine_id in affected_machine_ids if machine_id}
def trial_import_workbook(con, file_storage):
    workbook_data = load_trial_workbook(file_storage)
    workbook_name = compact_text(getattr(file_storage, "filename", "")) or "workbook.xlsx"
    log_id = trial_log_import(con, "workbook", workbook_name=workbook_name)
    inserted_ps = updated_ps = skipped_ps = 0
    material_counts = {"inserted": 0, "updated": 0, "skipped": 0}
    material_requirement_counts = {"inserted": 0, "updated": 0, "skipped": 0}
    try:
        material_counts = trial_upsert_bom_materials(con, workbook_data.get("Material_Per_BOM", []))

        bom_groups = {}
        for row in workbook_data.get("BOM_op_stage", []):
            part_no = first_nonempty(row, "inventory_code", "part_no", "item_code", "source_inventory_code")
            bom_code = first_nonempty(row, "bom_code")
            if not part_no or not bom_code:
                continue
            bom_groups.setdefault((part_no, bom_code), []).append(row)

        bom_ids = {}
        for (part_no, bom_code), stage_rows in bom_groups.items():
            bom_ids[(part_no, bom_code)] = trial_upsert_flow_from_rows(con, part_no, bom_code, stage_rows)

        touched_bom_ids = set()
        for row in workbook_data.get("ProcessSheets", []):
            source_ps_id = compact_text(row.get("ps_id"))
            partial_no = compact_text(row.get("pp_partial_no")) or "1"
            ps_id = f"{source_ps_id}::{partial_no}" if source_ps_id else ""
            part_no = compact_text(row.get("part_no"))
            if not source_ps_id or not part_no:
                skipped_ps += 1
                continue
            description = compact_text(row.get("description"))
            part_id = trial_find_or_create_part(con, part_no, description)
            bom_code = compact_text(row.get("bom_code"))
            selected_bom_id = int(bom_ids.get((part_no, bom_code)) or 0)
            existing = one(con.execute("SELECT ps_id, status, planner_status FROM process_sheet WHERE ps_id = ?", (ps_id,)))
            if existing:
                is_completed = trial_process_sheet_completed(existing)
                con.execute(
                    """
                    UPDATE process_sheet
                    SET source_ps_id = ?, pp_partial_no = ?, part_id = ?, selected_bom_id = ?,
                        part_no = ?, part_desc = ?, order_date = ?, due_date = ?, total_qty = ?,
                        status = ?, planner_status = ?
                    WHERE ps_id = ?
                    """,
                    (
                        source_ps_id,
                        partial_no,
                        part_id,
                        selected_bom_id or None,
                        part_no,
                        description,
                        parse_date_text(row.get("order_date")),
                        parse_date_text(row.get("due_date")),
                        workbook_partial_qty(row),
                        "COMPLETED" if is_completed else existing["status"],
                        "COMPLETED" if is_completed else existing["planner_status"],
                        ps_id,
                    ),
                )
                updated_ps += 1
            else:
                con.execute(
                    """
                    INSERT INTO process_sheet (
                      ps_id, source_ps_id, pp_partial_no, part_id, selected_bom_id,
                      part_no, part_desc, order_date, due_date, total_qty, status, planner_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'UNPLANNED', 'UNPLANNED')
                    """,
                    (
                        ps_id,
                        source_ps_id,
                        partial_no,
                        part_id,
                        selected_bom_id or None,
                        part_no,
                        description,
                        parse_date_text(row.get("order_date")),
                        parse_date_text(row.get("due_date")),
                        workbook_partial_qty(row),
                    ),
                )
                inserted_ps += 1
            material_result = sync_material_requirements_for_ps(con, ps_id)
            material_requirement_counts["inserted"] += int(material_result.get("inserted") or 0)
            material_requirement_counts["updated"] += int(material_result.get("updated") or 0)
            material_requirement_counts["skipped"] += int(material_result.get("skipped") or 0)
            if selected_bom_id:
                touched_bom_ids.add(selected_bom_id)

        for bom_id in touched_bom_ids:
            affected_machine_ids = sync_operations_for_flow(con, bom_id)
            for machine_id in affected_machine_ids:
                recalculate_machine(con, machine_id)
        trial_finish_import_log(
            con,
            log_id,
            "SUCCESS",
            json.dumps(
                {
                    "process_sheets": {"inserted": inserted_ps, "updated": updated_ps, "skipped": skipped_ps},
                    "flows": {"count": len(bom_ids)},
                    "materials": material_counts,
                    "material_requirements": material_requirement_counts,
                }
            ),
        )
        return {
            "inserted": inserted_ps,
            "updated": updated_ps,
            "skipped": skipped_ps,
            "flows": len(bom_ids),
            "materials": material_counts,
            "material_requirements": material_requirement_counts,
        }
    except Exception as exc:
        trial_finish_import_log(con, log_id, "FAILED", str(exc))
        raise


def trial_apply_active_sheet(con, active_rows):
    active_keys = set()
    active_prefixes = set()
    for row in active_rows:
        raw = first_nonempty(row, "ps_id", "source_pp_no", "pp_no", "pp_partial_no", "voucher_no", "source_ps_id")
        if not raw:
            continue
        if "::" in raw:
            active_keys.add(raw)
        else:
            active_prefixes.add(raw)

    completed = 0
    for row in rows(con.execute("SELECT ps_id, status, planner_status FROM process_sheet")):
        ps_id = compact_text(row["ps_id"])
        base_ps_id = ps_id.split("::", 1)[0]
        is_active = ps_id in active_keys or base_ps_id in active_prefixes
        if trial_process_sheet_completed(row):
            continue
        if not is_active:
            con.execute(
                "UPDATE process_sheet SET status = 'COMPLETED', planner_status = 'COMPLETED' WHERE ps_id = ?",
                (ps_id,),
            )
            completed += 1
    return completed


def trial_data_management_stats(con):
    ps_totals = one(
        con.execute(
            """
            SELECT
              COUNT(*) AS total_ps,
              SUM(CASE WHEN COALESCE(planner_status, '') = 'UNPLANNED' THEN 1 ELSE 0 END) AS unplanned_ps,
              SUM(CASE WHEN COALESCE(planner_status, '') = 'COMPLETED' THEN 1 ELSE 0 END) AS completed_ps,
              SUM(CASE WHEN COALESCE(planner_status, '') <> 'COMPLETED' THEN 1 ELSE 0 END) AS active_ps
            FROM process_sheet
            """
        )
    ) or {}
    return {
        "total_ps": int(ps_totals.get("total_ps") or 0),
        "unplanned_ps": int(ps_totals.get("unplanned_ps") or 0),
        "completed_ps": int(ps_totals.get("completed_ps") or 0),
        "active_ps": int(ps_totals.get("active_ps") or 0),
        "parts": int(one(con.execute("SELECT COUNT(*) AS cnt FROM parts"))["cnt"] or 0),
        "flows": int(one(con.execute("SELECT COUNT(*) AS cnt FROM bom_variation"))["cnt"] or 0),
        "steps": int(one(con.execute("SELECT COUNT(*) AS cnt FROM operation_seq"))["cnt"] or 0),
        "logs": [dict(row) for row in rows(con.execute("SELECT * FROM data_import_log ORDER BY log_id DESC LIMIT 12"))],
    }
