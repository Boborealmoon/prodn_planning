from __future__ import annotations

from datetime import datetime

from flask import Blueprint, jsonify, request

from ..actuals import refresh_block_actual_status
from ..blocks import (
    apply_output_delta_to_block_tail,
    create_rework_from_reject,
    delete_rework_from_reject_segment,
    find_rework_source_for_reject,
    recalculate_all,
    recalculate_machine,
    refresh_block_group_label,
    schedule_signature_for_machine,
    trial_block_payload,
    trial_block_row,
)
from ..catalog import (
    combined_group_summary,
    create_planning_card,
    planning_card_row,
    planning_cards_by_ps,
    schedule_planning_card,
    trial_catalog_items,
)
from ..db import db, one, rows, parse_dt_text
from ..materials import material_status_map_for_ps_ids, sync_material_requirements_for_ps_ids
from ..machines import default_profile_for_weekday, fetch_machines, is_public_holiday
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


def _visual_minutes_of_day(value):
    dt = parse_dt_text(value)
    if not dt:
        return 0
    return dt.hour * 60 + dt.minute


@trial_bp.get("/api/trial/schedule")
def api_trial_schedule():
    include_completed = int(request.args.get("include_completed") or 0)
    with db() as con:
        stale_cards = rows(
            con.execute(
                """
                SELECT card_id, ps_id, scheduled_block_group_id
                FROM planning_card
                WHERE card_type = 'COMBINED'
                  AND planning_status = 'SCHEDULED'
                  AND COALESCE(scheduled_block_group_id, 0) > 0
                """
            )
        )
        for card in stale_cards:
            group_id = int(card["scheduled_block_group_id"] or 0)
            live_group = one(
                con.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM run_block
                    WHERE group_id = ?
                    """,
                    (group_id,),
                )
            )
            if int((live_group or {})["cnt"] if live_group else 0) > 0:
                continue
            ps_id = compact_text(card["ps_id"])
            base_ps_id = ps_id.split("::", 1)[0] if ps_id else ""
            delete_ps_ids = {ps_id}
            if base_ps_id:
                delete_ps_ids.add(base_ps_id)
            for delete_ps_id in delete_ps_ids:
                if delete_ps_id:
                    con.execute(
                        """
                        DELETE FROM planning_card
                        WHERE card_type = 'COMBINED'
                          AND ps_id = ?
                        """,
                        (delete_ps_id,),
                    )
            con.execute(
                """
                DELETE FROM planning_card
                WHERE card_type = 'COMBINED'
                  AND scheduled_block_group_id = ?
                """,
                (group_id,),
            )
        machines = rows(con.execute("SELECT machine_id, machine_code, machine_category, shift_profile, active FROM machines WHERE active = 1 ORDER BY machine_id"))
        machine_by_id = {int(row["machine_id"]): dict(row) for row in machines}
        raw_blocks = rows(
            con.execute(
                """
                SELECT b.*, o.job_no, o.operation_name, o.total_qty, o.setup_minutes, o.cycle_minutes_per_qty,
                       o.compatible_machine_group, o.source_ps_id, o.source_op_seq_id AS source_op_seq_id, o.source_op_no,
                       m.machine_code, m.machine_category, m.shift_profile,
                       g.group_label AS group_label, g.group_type AS group_type
                FROM run_block b
                JOIN operation o ON o.operation_id = b.operation_id
                JOIN machines m ON m.machine_id = b.machine_id
                LEFT JOIN run_block_group g ON g.group_id = b.group_id
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
            blocks.append(item)
        actuals = rows(
            con.execute(
                """
                SELECT actual_id, segment_id, block_id, report_date,
                       output_qty, reject_qty, target_qty_at_report,
                       remarks, reported_at
                FROM production_actual
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
        catalog = trial_catalog_items(con, include_completed=bool(include_completed))
        planning_cards = [card for cards in planning_cards_by_ps(con).values() for card in cards]
        group_ids = sorted({int(row["group_id"]) for row in blocks if int(row["group_id"] or 0) > 0})
        block_groups = [combined_group_summary(con, group_id) for group_id in group_ids]
        block_groups = [group for group in block_groups if group]

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
        for group in block_groups:
            ps_id = compact_text(group.get("ps_id") or "")
            if not ps_id:
                continue
            ps_ids.add(ps_id)
            start_text = compact_text(group.get("group_start"))
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
        for group in block_groups:
            ps_id = compact_text(group.get("ps_id") or "")
            group["material_status"] = material_status_map.get(ps_id, default_material_status)

        return jsonify(
            {
                "machines": [dict(row) for row in machines],
                "blocks": blocks,
                "segments": segments,
                "actuals": [dict(row) for row in actuals],
                "capacities": [dict(row) for row in capacities],
                "profiles": [dict(row) for row in profiles],
                "block_groups": block_groups,
                "catalog": catalog["available"],
                "planned": catalog["planned"],
                "planning_cards": planning_cards,
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
        queue_position = int(data.get("queue_position") or 0)
        if queue_position <= 0:
            queue_position = 1 + int(one(con.execute("SELECT COALESCE(MAX(queue_position), 0) AS mx FROM run_block WHERE machine_id = ?", (machine_id,)))["mx"] or 0)
        block_cur = con.execute(
            """
            INSERT INTO run_block (
              operation_id, machine_id, queue_position, scheduled_qty, include_setup, status, planning_status, execution_status,
              anchor_datetime, calculated_start_datetime, calculated_end_datetime, actual_good_qty, actual_reject_qty, remarks, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', 0, 0, ?, CURRENT_TIMESTAMP)
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
                compact_text(data.get("anchor_datetime")),
                compact_text(data.get("remarks")),
            ),
        )
        recalculate_machine(con, machine_id)
        return jsonify({"ok": True, "operation_id": operation_id, "block": trial_block_payload(trial_block_row(con, block_cur.lastrowid))})


@trial_bp.post("/api/trial/catalog/combine")
def api_trial_combine_catalog_ops():
    data = request.get_json(force=True, silent=True) or {}
    with db() as con:
        try:
            card = create_planning_card(con, data.get("ps_id"), data.get("ops") or [], data.get("target_qty"))
            return jsonify({"ok": True, "card": card})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400


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
    queue_position = int(data.get("queue_position") or 0)
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
            return jsonify({"error": "Combined op card not found"}), 404
        if compact_text(card["planning_status"]).upper() == "SCHEDULED" or int(card["scheduled_block_group_id"] or 0) > 0:
            return jsonify({"error": "This combined op card is already scheduled. Remove it from the machine schedule first."}), 400
        con.execute("DELETE FROM planning_card WHERE card_id = ?", (int(card_id),))
        return jsonify({"ok": True, "card_id": int(card_id)})


@trial_bp.put("/api/trial/blocks/<int:block_id>")
def api_trial_update_block(block_id):
    data = request.get_json(force=True, silent=True) or {}
    with db() as con:
        block = trial_block_row(con, block_id)
        if not block:
            return jsonify({"error": "Run block not found"}), 404
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
            block_updates["queue_position"] = max(1, int(data.get("queue_position") or block["queue_position"]))
        if "scheduled_qty" in data:
            block_updates["scheduled_qty"] = max(0.0, parse_number(data.get("scheduled_qty"), block["scheduled_qty"]))
        if "include_setup" in data:
            block_updates["include_setup"] = 1 if data.get("include_setup") else 0
        if "anchor_datetime" in data:
            block_updates["anchor_datetime"] = compact_text(data.get("anchor_datetime"))
        if "actual_good_qty" in data:
            block_updates["actual_good_qty"] = max(0.0, parse_number(data.get("actual_good_qty"), block["actual_good_qty"]))
        if "actual_reject_qty" in data:
            block_updates["actual_reject_qty"] = max(0.0, parse_number(data.get("actual_reject_qty"), block["actual_reject_qty"]))
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
        return jsonify({"ok": True, "block": trial_block_payload(trial_block_row(con, block_id))})


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
        if split_qty >= float(block["scheduled_qty"] or 0):
            return jsonify({"error": "Split quantity must be smaller than the scheduled quantity"}), 400
        remaining = float(block["scheduled_qty"] or 0) - split_qty
        max_position = int(one(con.execute("SELECT COALESCE(MAX(queue_position), 0) AS mx FROM run_block WHERE machine_id = ?", (int(block["machine_id"]),)))["mx"] or 0)
        con.execute("UPDATE run_block SET scheduled_qty = ?, updated_at = CURRENT_TIMESTAMP WHERE block_id = ?", (split_qty, block_id))
        planning_status, execution_status = normalize_block_status_inputs(
            {"planning_status": block["planning_status"], "execution_status": block["execution_status"], "status": block["status"]},
            default_planning=compact_text(block["planning_status"]) or "PLANNED",
            default_execution=compact_text(block["execution_status"] or block["status"]) or "NOT_STARTED",
        )
        cur = con.execute(
            """
            INSERT INTO run_block (
              operation_id, machine_id, queue_position, scheduled_qty, include_setup, status, planning_status, execution_status,
              anchor_datetime, calculated_start_datetime, calculated_end_datetime, actual_good_qty, actual_reject_qty, remarks, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '', '', 0, 0, ?, CURRENT_TIMESTAMP)
            """,
            (
                int(block["operation_id"]),
                int(block["machine_id"]),
                max_position + 1,
                remaining,
                int(block["include_setup"] or 0),
                execution_status,
                planning_status,
                execution_status,
                compact_text(block["remarks"]),
            ),
        )
        recalculate_machine(con, int(block["machine_id"]))
        return jsonify({"ok": True, "block": trial_block_payload(trial_block_row(con, block_id)), "new_block": trial_block_payload(trial_block_row(con, cur.lastrowid))})


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
                (machine_id, idx, ordered_block_id),
            )
        for affected_machine_id in affected_machine_ids:
            recalculate_machine(con, affected_machine_id)
        return jsonify({"ok": True})


@trial_bp.post("/api/trial/blocks/<int:block_id>/combine")
def api_trial_combine_blocks(block_id):
    return jsonify({"error": "Scheduled blocks cannot be combined. Combine operations inside the PS list before scheduling."}), 400


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

        affected_machine_ids = {machine_id}
        affected_operation_ids = {operation_id}

        if group_id:
            group_blocks = rows(
                con.execute(
                    """
                    SELECT block_id, operation_id, machine_id
                    FROM run_block
                    WHERE group_id = ?
                    """,
                    (group_id,),
                )
            )
            affected_machine_ids.update(int(row["machine_id"]) for row in group_blocks if int(row["machine_id"] or 0))
            affected_operation_ids.update(int(row["operation_id"]) for row in group_blocks if int(row["operation_id"] or 0))
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

        for op_id in affected_operation_ids:
            remaining = one(con.execute("SELECT COUNT(*) AS cnt FROM run_block WHERE operation_id = ?", (int(op_id),)))
            if int((remaining or {})["cnt"] if remaining else 0) <= 0:
                con.execute("DELETE FROM operation WHERE operation_id = ?", (int(op_id),))

        for mid in affected_machine_ids:
            if mid:
                recalculate_machine(con, int(mid))
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
        existing = one(
            con.execute(
                """
                SELECT *
                FROM production_actual
                WHERE segment_id = ?
                """,
                (int(segment_id),),
            )
        )

        output_provided = "output_qty" in data
        reject_provided = "reject_qty" in data
        remarks_provided = "remarks" in data

        output_qty = parse_nullable_number(data.get("output_qty")) if output_provided else None
        reject_qty = parse_nullable_number(data.get("reject_qty")) if reject_provided else None
        remarks = compact_text(data.get("remarks")) if remarks_provided else None
        stored_target_qty = existing["target_qty_at_report"] if existing and existing["target_qty_at_report"] is not None else None
        target_qty = float(stored_target_qty if stored_target_qty is not None else segment["qty_done"] or 0)
        old_output_qty = existing["output_qty"] if existing else None
        old_output_diff = 0.0 if old_output_qty is None else float(old_output_qty) - target_qty
        new_output_diff = (float(output_qty) - target_qty) if output_provided and output_qty is not None else old_output_diff
        output_delta = new_output_diff - old_output_diff
        rework_source = find_rework_source_for_reject(con, block_id) if reject_provided and reject_qty and reject_qty > 0 else None
        output_adjustment = {"changed": False, "applied_qty": 0.0}
        if output_provided and output_delta != 0:
            output_adjustment = apply_output_delta_to_block_tail(con, block_id, report_date, output_delta)

        if existing:
            updates = []
            params = []
            if output_provided:
                updates.append("output_qty = ?")
                params.append(output_qty)
            if reject_provided:
                updates.append("reject_qty = ?")
                params.append(reject_qty)
            if remarks_provided:
                updates.append("remarks = ?")
                params.append(remarks)
            updates.append("target_qty_at_report = COALESCE(target_qty_at_report, ?)")
            params.append(target_qty)
            updates.append("reported_at = CURRENT_TIMESTAMP")
            con.execute(
                f"""
                UPDATE production_actual
                SET {", ".join(updates)}
                WHERE actual_id = ?
                """,
                (*params, int(existing["actual_id"])),
            )
        else:
            con.execute(
                """
                INSERT INTO production_actual (
                  segment_id, block_id, report_date, output_qty, reject_qty, remarks, target_qty_at_report, reported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    int(segment_id),
                    block_id,
                    report_date,
                    output_qty if output_provided else None,
                    reject_qty if reject_provided else None,
                    remarks if remarks_provided else "",
                    target_qty,
                ),
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
                "block": trial_block_payload(trial_block_row(con, block_id)),
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
        daily_actuals = data.get("daily_actuals") or []
        for report_date in delete_dates:
            con.execute("DELETE FROM production_actual WHERE block_id = ? AND report_date = ?", (int(block_id), report_date))
        for row in daily_actuals:
            report_date = compact_text(row.get("report_date"))
            if not report_date:
                continue
            output_value = parse_number(row.get("output_qty") if "output_qty" in row else row.get("actual_good_qty"), 0)
            reject_value = parse_number(row.get("reject_qty") if "reject_qty" in row else row.get("actual_reject_qty"), 0)
            existing = one(
                con.execute(
                    """
                    SELECT actual_id
                    FROM production_actual
                    WHERE block_id = ? AND report_date = ?
                    ORDER BY CASE WHEN segment_id IS NULL THEN 1 ELSE 0 END, actual_id
                    LIMIT 1
                    """,
                    (int(block_id), report_date),
                )
            )
            if existing:
                con.execute(
                    """
                    UPDATE production_actual
                    SET output_qty = ?, reject_qty = ?, remarks = ?, target_qty_at_report = COALESCE(target_qty_at_report, ?), reported_at = CURRENT_TIMESTAMP
                    WHERE actual_id = ?
                    """,
                    (
                        output_value,
                        reject_value,
                        compact_text(row.get("remarks")),
                        parse_number(block["scheduled_qty"], 0),
                        int(existing["actual_id"]),
                    ),
                )
            else:
                con.execute(
                    """
                    INSERT INTO production_actual (segment_id, block_id, report_date, output_qty, reject_qty, remarks, target_qty_at_report, reported_at)
                    VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        int(block_id),
                        report_date,
                        output_value,
                        reject_value,
                        compact_text(row.get("remarks")),
                        parse_number(block["scheduled_qty"], 0),
                    ),
                )
        refresh_block_actual_status(con, block_id)
        recalculate_machine(con, int(block["machine_id"]))
        actuals = rows(
            con.execute(
                """
                SELECT actual_id, segment_id, block_id, report_date,
                       output_qty, reject_qty, target_qty_at_report,
                       remarks, reported_at
                FROM production_actual
                WHERE block_id = ?
                ORDER BY report_date, actual_id
                """,
                (int(block_id),),
            )
        )
        return jsonify({"ok": True, "block": trial_block_payload(trial_block_row(con, block_id)), "actuals": [dict(r) for r in actuals]})


@trial_bp.post("/api/trial/recalc")
def api_trial_recalc():
    with db() as con:
        recalculate_all(con)
        return jsonify({"ok": True})
