from __future__ import annotations

from collections import defaultdict

from .db import one, parse_dt_text, rows
from .utils import compact_text, parse_number

ALLOWED_SUPPLY_STATUSES = {
    "PENDING_CONFIRMATION",
    "READY",
    "NOT_READY",
    "ORDERED",
    "DELAYED",
    "NOT_REQUIRED",
}


def split_ps_id(ps_id):
    raw = compact_text(ps_id)
    if not raw:
        return "", ""
    if "::" not in raw:
        return raw, ""
    base, partial = raw.split("::", 1)
    return base or raw, partial or ""


def normalize_supply_status(value):
    status = compact_text(value).upper() or "PENDING_CONFIRMATION"
    return status if status in ALLOWED_SUPPLY_STATUSES else "PENDING_CONFIRMATION"


def _requirement_inventory_code(row):
    return compact_text(row.get("material_inventory_code"))


def _requirement_description(row):
    return compact_text(row.get("material_description") or row.get("material_desc"))


def _ps_requirement_context(con, ps_id):
    ps_id = compact_text(ps_id)
    if not ps_id:
        return None
    return one(
        con.execute(
            """
            SELECT ps.*,
                   sf.bom_code AS selected_flow_code
            FROM process_sheet ps
            LEFT JOIN bom_variation sf ON sf.bom_id = ps.selected_bom_id
            WHERE ps.ps_id = ?
            """,
            (ps_id,),
        )
    )


def sync_material_requirements_for_ps(con, ps_id):
    ps_id = compact_text(ps_id)
    if not ps_id:
        return {"inserted": 0, "updated": 0, "skipped": 0, "requirement_ids": []}

    ps = _ps_requirement_context(con, ps_id)
    if not ps:
        return {"inserted": 0, "updated": 0, "skipped": 0, "requirement_ids": []}

    source_inventory_code = compact_text(ps["part_no"])
    if not source_inventory_code:
        return {"inserted": 0, "updated": 0, "skipped": 0, "requirement_ids": []}
    bom_code = compact_text(ps["selected_flow_code"] or "")
    material_qty_needed = max(0.0, parse_number(ps["total_qty"], 0))
    material_uom = "EA" if material_qty_needed > 0 else ""

    bom_rows = rows(
        con.execute(
            """
            SELECT bom_material_id, source_inventory_code, bom_code, material_inventory_code, material_description
            FROM bom_material
            WHERE source_inventory_code = ? AND bom_code = ?
            ORDER BY bom_material_id
            """,
            (source_inventory_code, bom_code),
        )
    )

    counts = {"inserted": 0, "updated": 0, "skipped": 0, "requirement_ids": []}
    if not bom_rows:
        return counts

    current_codes = []
    seen_codes = set()
    for bom_row in bom_rows:
        material_inventory_code = compact_text(bom_row["material_inventory_code"])
        if not material_inventory_code or material_inventory_code in seen_codes:
            continue
        seen_codes.add(material_inventory_code)
        current_codes.append(material_inventory_code)

    existing_rows = rows(
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
    existing_by_code = {}
    duplicate_ids = []
    stale_ids = []
    for existing in existing_rows:
        code = compact_text(existing.get("material_inventory_code"))
        if not code:
            stale_ids.append(int(existing["requirement_id"]))
            continue
        if code not in current_codes:
            stale_ids.append(int(existing["requirement_id"]))
            continue
        if code in existing_by_code:
            duplicate_ids.append(int(existing["requirement_id"]))
            continue
        existing_by_code[code] = existing

    for bom_row in bom_rows:
        material_inventory_code = compact_text(bom_row["material_inventory_code"])
        if not material_inventory_code or material_inventory_code not in current_codes:
            counts["skipped"] += 1
            continue

        material_description = compact_text(bom_row["material_description"])
        existing = existing_by_code.get(material_inventory_code)

        if existing:
            current_description = _requirement_description(existing)
            next_description = material_description or current_description
            current_material_code = _requirement_inventory_code(existing)
            current_source_inventory = compact_text(existing.get("source_inventory_code") or existing.get("part_no"))
            current_bom_code = compact_text(existing.get("bom_code"))
            current_qty = float(parse_number(existing.get("material_qty_needed"), 0))
            current_uom = compact_text(existing.get("material_uom"))

            if (
                current_source_inventory == source_inventory_code
                and current_bom_code == bom_code
                and current_material_code == material_inventory_code
                and current_description == next_description
                and current_qty == float(material_qty_needed)
                and current_uom == material_uom
            ):
                counts["skipped"] += 1
                counts["requirement_ids"].append(int(existing["requirement_id"]))
                continue

            con.execute(
                """
                UPDATE material_requirement
                SET source_inventory_code = ?,
                    bom_code = ?,
                    material_inventory_code = ?,
                    material_description = ?,
                    material_qty_needed = ?,
                    material_uom = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE requirement_id = ?
                """,
                (
                    source_inventory_code,
                    bom_code,
                    material_inventory_code,
                    next_description,
                    material_qty_needed,
                    material_uom,
                    int(existing["requirement_id"]),
                ),
            )
            counts["updated"] += 1
            counts["requirement_ids"].append(int(existing["requirement_id"]))
            continue

        cur = con.execute(
            """
            INSERT INTO material_requirement (
              ps_id,
              source_inventory_code,
              bom_code,
              material_inventory_code,
              material_description,
              material_qty_needed,
              material_uom,
              supply_status,
              expected_ready_date,
              supplier_ref,
              remarks,
              updated_at,
              created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING_CONFIRMATION', '', '', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                ps_id,
                source_inventory_code,
                bom_code,
                material_inventory_code,
                material_description,
                material_qty_needed,
                material_uom,
            ),
        )
        counts["inserted"] += 1
        counts["requirement_ids"].append(int(cur.lastrowid))

    if stale_ids or duplicate_ids:
        delete_ids = sorted({*stale_ids, *duplicate_ids})
        placeholders = ",".join("?" for _ in delete_ids)
        con.execute(
            f"DELETE FROM material_requirement WHERE requirement_id IN ({placeholders})",
            delete_ids,
        )

    return counts


def sync_material_requirements_for_ps_ids(con, ps_ids):
    aggregated = {"inserted": 0, "updated": 0, "skipped": 0, "requirement_ids": []}
    for ps_id in sorted({compact_text(ps_id) for ps_id in ps_ids if compact_text(ps_id)}):
        result = sync_material_requirements_for_ps(con, ps_id)
        aggregated["inserted"] += int(result.get("inserted") or 0)
        aggregated["updated"] += int(result.get("updated") or 0)
        aggregated["skipped"] += int(result.get("skipped") or 0)
        aggregated["requirement_ids"].extend(result.get("requirement_ids") or [])
    return aggregated


def sync_material_requirements_for_all_ps(con):
    return sync_material_requirements_for_ps_ids(
        con,
        [row["ps_id"] for row in rows(con.execute("SELECT ps_id FROM process_sheet"))],
    )


def material_requirement_rows_for_ps(con, ps_id):
    ps_id = compact_text(ps_id)
    if not ps_id:
        return []

    ps = one(
        con.execute(
            """
            SELECT ps.ps_id, sf.bom_code AS selected_flow_code
            FROM process_sheet ps
            LEFT JOIN bom_variation sf ON sf.bom_id = ps.selected_bom_id
            WHERE ps.ps_id = ?
            """,
            (ps_id,),
        )
    )
    if not ps:
        return []

    flow_code = compact_text(ps["selected_flow_code"])
    if flow_code:
        matched = rows(
            con.execute(
                """
                SELECT *
                FROM material_requirement
                WHERE ps_id = ?
                  AND bom_code = ?
                ORDER BY requirement_id
                """,
                (ps_id, flow_code),
            )
        )
        if matched:
            return matched

    return rows(
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


def _parse_date(value):
    dt = parse_dt_text(value)
    return dt.date() if dt else None


def material_status_from_requirement_rows(requirement_rows, planned_start_text=""):
    if not requirement_rows:
        return {
            "status": "PENDING_CONFIRMATION",
            "label": "Material pending",
            "expected_ready_date": "",
            "severity": "pending",
        }

    statuses = [normalize_supply_status(row.get("supply_status")) for row in requirement_rows]
    if all(status in {"READY", "NOT_REQUIRED"} for status in statuses):
        return {
            "status": "READY",
            "label": "",
            "expected_ready_date": "",
            "severity": "none",
        }

    actionable_rows = [
        row
        for row in requirement_rows
        if normalize_supply_status(row.get("supply_status")) not in {"READY", "NOT_REQUIRED"}
    ]
    planned_start = _parse_date(planned_start_text)
    expected_dates = [d for d in (_parse_date(row.get("expected_ready_date")) for row in actionable_rows) if d]
    if expected_dates:
        expected_date = max(expected_dates)
        label = f"Material ready {expected_date.isoformat()}"
        severity = "warning"
        if planned_start and expected_date > planned_start:
            label = f"Material late: {expected_date.isoformat()}"
            severity = "late"
        return {
            "status": normalize_supply_status(actionable_rows[0].get("supply_status")) or "NOT_READY",
            "label": label,
            "expected_ready_date": expected_date.isoformat(),
            "severity": severity,
        }

    primary_status = normalize_supply_status(actionable_rows[0].get("supply_status")) if actionable_rows else "PENDING_CONFIRMATION"
    if primary_status == "PENDING_CONFIRMATION":
        return {
            "status": "PENDING_CONFIRMATION",
            "label": "Material pending",
            "expected_ready_date": "",
            "severity": "pending",
        }
    if primary_status == "ORDERED":
        return {
            "status": "ORDERED",
            "label": "Material ordered",
            "expected_ready_date": "",
            "severity": "warning",
        }
    if primary_status == "DELAYED":
        return {
            "status": "DELAYED",
            "label": "Material delayed",
            "expected_ready_date": "",
            "severity": "late",
        }
    return {
        "status": "NOT_READY",
        "label": "Material not ready",
        "expected_ready_date": "",
        "severity": "warning",
    }


def material_status_for_ps(con, ps_id, planned_start_text=""):
    return material_status_from_requirement_rows(material_requirement_rows_for_ps(con, ps_id), planned_start_text)


def material_status_map_for_ps_ids(con, ps_ids, planned_starts=None):
    planned_starts = planned_starts or {}
    result = {}
    for ps_id in sorted({compact_text(ps_id) for ps_id in ps_ids if compact_text(ps_id)}):
        result[ps_id] = material_status_for_ps(con, ps_id, planned_starts.get(ps_id, ""))
    return result


def material_ps_summary_map(con):
    summary = {}
    for row in rows(
        con.execute(
            """
            SELECT o.source_ps_id AS ps_id,
                   b.block_id,
                   b.calculated_start_datetime,
                   b.calculated_end_datetime,
                   b.planning_status,
                   b.execution_status,
                   b.status,
                   m.machine_code
            FROM run_block b
            JOIN operation o ON o.operation_id = b.operation_id
            JOIN machines m ON m.machine_id = b.machine_id
            WHERE COALESCE(o.source_ps_id, '') <> ''
              AND COALESCE(b.active, 1) = 1
            ORDER BY b.calculated_start_datetime, b.queue_position, b.block_id
            """
        )
    ):
        ps_id = compact_text(row["ps_id"])
        if not ps_id:
            continue
        entry = summary.setdefault(
            ps_id,
            {
                "block_count": 0,
                "not_started_count": 0,
                "in_progress_count": 0,
                "done_count": 0,
                "first_planned_start": "",
                "machine_code": "",
                "planning_status": "UNPLANNED",
                "execution_status": "NOT_STARTED",
            },
        )
        entry["block_count"] += 1
        execution_status = compact_text(row["execution_status"] or row["status"]).upper() or "NOT_STARTED"
        planning_status = compact_text(row["planning_status"]).upper() or "UNPLANNED"
        if execution_status == "NOT_STARTED":
            entry["not_started_count"] += 1
        elif execution_status == "IN_PROGRESS":
            entry["in_progress_count"] += 1
        elif execution_status == "DONE":
            entry["done_count"] += 1
        if not entry["first_planned_start"] and compact_text(row["calculated_start_datetime"]):
            entry["first_planned_start"] = compact_text(row["calculated_start_datetime"])
            entry["machine_code"] = compact_text(row["machine_code"])
        if planning_status and entry["planning_status"] == "UNPLANNED":
            entry["planning_status"] = planning_status
        if execution_status in {"IN_PROGRESS", "DONE"}:
            entry["execution_status"] = "IN_PROGRESS" if execution_status == "IN_PROGRESS" else "DONE"
        elif entry["execution_status"] == "NOT_STARTED":
            entry["execution_status"] = execution_status
    return summary


def _requirement_join_rows(con):
    return rows(
        con.execute(
            """
            SELECT mr.*,
                   ps.pp_partial_no,
                   ps.part_no,
                   ps.part_desc,
                   ps.total_qty AS ps_total_qty,
                   ps.status AS ps_status,
                   ps.planner_status,
                   ps.selected_bom_id,
                   sf.bom_code AS selected_flow_code,
                   p.part_no AS part_name,
                   p.part_desc AS part_desc
            FROM material_requirement mr
            JOIN process_sheet ps ON ps.ps_id = mr.ps_id
            LEFT JOIN parts p ON p.part_id = ps.part_id
            LEFT JOIN bom_variation sf ON sf.bom_id = ps.selected_bom_id
            WHERE COALESCE(sf.bom_code, '') = '' OR mr.bom_code = sf.bom_code
            ORDER BY ps.due_date, ps.ps_id, mr.requirement_id
            """
        )
    )


def material_requirement_overview_rows(con, include_unplanned=False, include_active=False, include_completed=False, search=""):
    search = compact_text(search).lower()
    requirement_rows = _requirement_join_rows(con)
    grouped = defaultdict(list)
    for row in requirement_rows:
        grouped[compact_text(row["ps_id"])].append(row)

    ps_summary = material_ps_summary_map(con)
    payloads = []
    for ps_id, rows_for_ps in grouped.items():
        ps_row = rows_for_ps[0]
        summary = ps_summary.get(ps_id, {})
        is_completed = compact_text(ps_row["ps_status"]).upper() == "COMPLETED" or compact_text(ps_row["planner_status"]).upper() == "COMPLETED"
        block_count = int(summary.get("block_count") or 0)
        has_active = int(summary.get("in_progress_count") or 0) > 0 or int(summary.get("done_count") or 0) > 0
        is_unplanned = block_count <= 0
        is_active = has_active and not is_completed
        is_planned_not_started = block_count > 0 and not is_active and not is_completed

        if is_completed and not include_completed:
            continue
        if is_active and not include_active:
            continue
        if is_unplanned and not include_unplanned:
            continue
        if not (is_planned_not_started or is_completed or is_active or is_unplanned):
            continue

        material_status = material_status_from_requirement_rows(rows_for_ps, summary.get("first_planned_start", ""))
        first_planned_start = compact_text(summary.get("first_planned_start", ""))
        machine_code = compact_text(summary.get("machine_code", ""))
        if is_completed:
            planning_status = "COMPLETED"
            execution_status = "COMPLETED"
        elif is_active:
            planning_status = compact_text(summary.get("planning_status") or ps_row["planner_status"] or "PLANNED").upper()
            execution_status = "IN_PROGRESS" if int(summary.get("in_progress_count") or 0) > 0 else ("DONE" if int(summary.get("done_count") or 0) > 0 else "NOT_STARTED")
        elif is_planned_not_started:
            planning_status = "PLANNED"
            execution_status = "NOT_STARTED"
        else:
            planning_status = "UNPLANNED"
            execution_status = "NOT_STARTED"

        for row in rows_for_ps:
            base_ps, partial = split_ps_id(row["ps_id"])
            material_code = _requirement_inventory_code(row)
            material_desc = _requirement_description(row)
            payload = {
                "requirement_id": int(row["requirement_id"]),
                "ps_id": compact_text(row["ps_id"]),
                "base_ps_number": base_ps,
                "partial": partial,
                "part_no": compact_text(row["source_inventory_code"] or row["part_no"] or row["ps_id"]),
                "part_desc": compact_text(row["part_desc"] or row["part_desc"]),
                "bom_code": compact_text(row["bom_code"]),
                "material_inventory_code": material_code,
                "material_description": material_desc,
                "material_qty_needed": float(row["material_qty_needed"] or 0),
                "material_uom": compact_text(row["material_uom"]),
                "first_planned_start": first_planned_start,
                "machine_code": machine_code,
                "planning_status": planning_status,
                "execution_status": execution_status,
                "ps_status": compact_text(row["ps_status"]),
                "planner_status": compact_text(row["planner_status"]),
                "supply_status": normalize_supply_status(row["supply_status"]),
                "expected_ready_date": compact_text(row["expected_ready_date"]),
                "supplier_ref": compact_text(row["supplier_ref"]),
                "remarks": compact_text(row["remarks"]),
                "updated_at": compact_text(row["updated_at"]),
                "material_status": material_status,
                "is_completed": is_completed,
                "is_active": is_active,
                "is_unplanned": is_unplanned,
                "is_planned_not_started": is_planned_not_started,
            }
            haystack = " ".join(
                compact_text(part).lower()
                for part in (
                    payload["ps_id"],
                    payload["part_no"],
                    payload["part_desc"],
                    payload["bom_code"],
                    payload["material_inventory_code"],
                    payload["material_description"],
                    payload["supplier_ref"],
                    payload["remarks"],
                    payload["machine_code"],
                    payload["supply_status"],
                    payload["expected_ready_date"],
                    payload["planning_status"],
                    payload["execution_status"],
                )
            )
            if search and search not in haystack:
                continue
            payloads.append(payload)

    return payloads


def material_requirement_payload(row):
    payload = dict(row)
    payload["base_ps_number"] = split_ps_id(payload.get("ps_id"))[0]
    payload["partial"] = split_ps_id(payload.get("ps_id"))[1]
    payload["material_code"] = compact_text(payload.get("material_inventory_code"))
    payload["material_desc"] = compact_text(payload.get("material_description"))
    return payload
