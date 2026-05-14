from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from .actuals import actual_totals_for_block
from .db import date_text, one, parse_dt_text, rows
from .machines import capacity_minutes_for_machine_day, machine_work_intervals_for_day
from .scheduler_state import (
    create_schedule_run,
    refresh_operation_state,
    refresh_process_sheet_state,
    refresh_states_for_machine,
    refresh_machine_queue_state,
    resolve_schedule_alert,
    upsert_schedule_alert,
)
from .utils import compact_text, format_qty


def trial_block_row(con, block_id):
    return one(
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
            WHERE b.block_id = ?
            """,
            (int(block_id),),
        )
    )


def _queue_state_for_block(con, block_id):
    if not con:
        return None
    return one(
        con.execute(
            """
            SELECT *
            FROM machine_queue_state
            WHERE block_id = ?
            """,
            (int(block_id),),
        )
    )


def trial_block_payload(block, con=None):
    if not block:
        return None
    planning_status = block.get("planning_status", "UNPLANNED") or "UNPLANNED"
    execution_status = block.get("execution_status", block.get("status", "NOT_STARTED")) or "NOT_STARTED"
    queue_state = _queue_state_for_block(con, block["block_id"]) if con else None
    predicted_start_at = (queue_state["predicted_start_at"] if queue_state and queue_state["predicted_start_at"] else block.get("calculated_start_datetime") or block.get("planned_start_at") or "")
    predicted_end_at = (queue_state["predicted_end_at"] if queue_state and queue_state["predicted_end_at"] else block.get("calculated_end_datetime") or block.get("planned_end_at") or "")
    remaining_qty = float(queue_state["remaining_qty"] if queue_state and queue_state["remaining_qty"] is not None else max(0.0, float(block["scheduled_qty"] or 0) - max(0.0, float(block["actual_good_qty"] or 0))))
    good_qty = float(queue_state["good_qty"] if queue_state and queue_state["good_qty"] is not None else float(block["actual_good_qty"] or 0))
    reject_qty = float(queue_state["reject_qty"] if queue_state and queue_state["reject_qty"] is not None else float(block["actual_reject_qty"] or 0))
    schedule_status = queue_state["schedule_status"] if queue_state and queue_state["schedule_status"] else planning_status
    return {
        "block_id": int(block["block_id"]),
        "operation_id": int(block["operation_id"]),
        "machine_id": int(block["machine_id"]),
        "queue_position": float(block["queue_position"] or 0),
        "scheduled_qty": float(block["scheduled_qty"] or 0),
        "include_setup": int(block["include_setup"] or 0),
        "status": execution_status,
        "planning_status": planning_status,
        "execution_status": execution_status,
        "anchor_datetime": block["anchor_datetime"] or "",
        "planned_start_at": block.get("planned_start_at") or "",
        "planned_end_at": block.get("planned_end_at") or "",
        "allow_pull_forward": int(block.get("allow_pull_forward") if block.get("allow_pull_forward") is not None else 1),
        "active": int(block.get("active") if block.get("active") is not None else 1),
        "is_fresh_monday_item": int(block.get("is_fresh_monday_item") or 0),
        "last_schedule_run_id": int(block.get("last_schedule_run_id") or 0),
        "planned_qty_original": float(block.get("planned_qty_original") or 0),
        "split_from_block_id": int(block.get("split_from_block_id") or 0),
        "scheduler_note": block.get("scheduler_note") or "",
        "calculated_start_datetime": block["calculated_start_datetime"] or "",
        "calculated_end_datetime": block["calculated_end_datetime"] or "",
        "predicted_start_at": predicted_start_at,
        "predicted_end_at": predicted_end_at,
        "actual_good_qty": good_qty,
        "actual_reject_qty": reject_qty,
        "good_qty": good_qty,
        "reject_qty": reject_qty,
        "remaining_qty": remaining_qty,
        "schedule_status": schedule_status,
        "remarks": block["remarks"] or "",
        "job_no": block["job_no"] or "",
        "operation_name": block["operation_name"] or "",
        "total_qty": float(block["total_qty"] or 0),
        "setup_minutes": float(block["setup_minutes"] or 0),
        "cycle_minutes_per_qty": float(block["cycle_minutes_per_qty"] or 0),
        "compatible_machine_group": block["compatible_machine_group"] or "",
        "source_ps_id": block["source_ps_id"] or "",
        "source_op_seq_id": int(block["source_op_seq_id"] or 0),
        "source_op_no": block["source_op_no"] or "",
        "machine_code": block["machine_code"] or "",
        "machine_category": block["machine_category"] or "",
        "shift_profile": block["shift_profile"] or "",
        "block_type": block.get("block_type", "ORIGINAL") or "ORIGINAL",
        "source_reject_block_id": int(block.get("source_reject_block_id") or 0),
        "source_reject_segment_id": int(block.get("source_reject_segment_id") or 0),
        "group_id": int(block.get("group_id") or 0),
        "group_label": block.get("group_label") or "",
        "group_type": block.get("group_type") or "",
    }


def schedule_signature_for_machine(con, machine_id):
    return [
        (
            int(row["block_id"]),
            row["calculated_start_datetime"] or "",
            row["calculated_end_datetime"] or "",
        )
        for row in rows(
            con.execute(
                """
                SELECT block_id, calculated_start_datetime, calculated_end_datetime
                FROM run_block
                WHERE machine_id = ?
                  AND COALESCE(active, 1) = 1
                ORDER BY queue_position, block_id
                """,
                (int(machine_id),),
            )
        )
    ]


def next_capacity_date_for_machine(con, machine_id, after_date: date):
    probe = after_date + timedelta(days=1)
    for _ in range(370):
        cap = capacity_minutes_for_machine_day(con, machine_id, probe)
        if int(cap["capacity_minutes"] or 0) > 0:
            return probe, cap
        probe += timedelta(days=1)
    cap = capacity_minutes_for_machine_day(con, machine_id, probe)
    return probe, cap


def dependency_finish_for_block(con, block):
    source_ps_id = compact_text(block.get("source_ps_id") or "")
    source_op_seq_id = int(block.get("source_op_seq_id") or 0)
    if not source_ps_id or not source_op_seq_id:
        return None
    current_step = one(
        con.execute(
            """
            SELECT seq_no
            FROM operation_seq
            WHERE op_seq_id = ?
            """,
            (source_op_seq_id,),
        )
    )
    if not current_step:
        return None
    prev_row = one(
        con.execute(
            """
            SELECT MAX(COALESCE(q.predicted_end_at, b.calculated_end_datetime, b.planned_end_at, b.anchor_datetime)) AS dependency_finish
            FROM run_block b
            JOIN operation o ON o.operation_id = b.operation_id
            JOIN operation_seq s ON s.op_seq_id = o.source_op_seq_id
            LEFT JOIN machine_queue_state q ON q.block_id = b.block_id
            WHERE COALESCE(o.source_ps_id, '') = ?
              AND COALESCE(b.active, 1) = 1
              AND s.seq_no < ?
            """,
            (source_ps_id, int(current_step["seq_no"] or 0)),
        )
    )
    finish_text = compact_text(prev_row["dependency_finish"] if prev_row else "")
    return parse_dt_text(finish_text) if finish_text else None


def add_future_segments_after_date(con, block_id, after_date: date, qty_to_add, schedule_run_id=None):
    block = trial_block_row(con, block_id)
    if not block:
        return False

    machine_id = int(block["machine_id"])
    cycle_time = max(0.0, float(block["cycle_minutes_per_qty"] or 0))
    if cycle_time <= 0:
        return False

    remaining_qty = float(qty_to_add or 0)
    if remaining_qty <= 0:
        return False

    work_date = after_date + timedelta(days=1)
    changed = False
    safety = 0

    while remaining_qty > 0 and safety < 370:
        safety += 1
        intervals = machine_work_intervals_for_day(con, machine_id, work_date)
        if not intervals:
            work_date += timedelta(days=1)
            continue
        for interval_start, interval_end in intervals:
            if remaining_qty <= 0:
                break
            available_minutes = max(0.0, (interval_end - interval_start).total_seconds() / 60.0)
            max_qty_today = math.floor(available_minutes / cycle_time)
            if max_qty_today <= 0:
                continue
            qty_today = min(remaining_qty, max_qty_today)
            minutes_used = qty_today * cycle_time
            end_dt = interval_start + timedelta(minutes=minutes_used)
            con.execute(
                """
                INSERT INTO run_block_segment (
                  block_id, machine_id, schedule_run_id, segment_date, segment_type,
                  qty_done, planned_qty, minutes_used, planned_minutes, segment_status, start_datetime, end_datetime, is_actual
                ) VALUES (?, ?, ?, ?, 'production', ?, ?, ?, ?, 'PLANNED', ?, ?, 0)
                """,
                (
                    int(block_id),
                    machine_id,
                    int(schedule_run_id) if schedule_run_id is not None else None,
                    date_text(work_date),
                    qty_today,
                    minutes_used,
                    qty_today,
                    minutes_used,
                    interval_start.strftime("%Y-%m-%d %H:%M:%S"),
                    end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            remaining_qty -= qty_today
            changed = True
        work_date += timedelta(days=1)

    return changed


def add_shortfall_to_tail_with_capacity(con, block_id, actual_date, qty_to_add):
    block = trial_block_row(con, block_id)
    if not block:
        return False

    machine_id = int(block["machine_id"])
    cycle_time = max(0.0, float(block["cycle_minutes_per_qty"] or 0))
    if cycle_time <= 0:
        return False

    remaining_to_add = float(qty_to_add or 0)
    if remaining_to_add <= 0:
        return False

    changed = False
    tail = one(
        con.execute(
            """
            SELECT *
            FROM run_block_segment
            WHERE block_id = ?
              AND segment_type = 'production'
              AND segment_date > ?
              AND segment_id NOT IN (
                SELECT segment_id
                FROM production_actual
                WHERE segment_id IS NOT NULL
                  AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
              )
            ORDER BY segment_date DESC, end_datetime DESC, segment_id DESC
            LIMIT 1
            """,
            (int(block_id), date_text(actual_date)),
        )
    )

    if tail:
        tail_date_text = compact_text(tail["segment_date"])
        tail_date = parse_dt_text(tail_date_text).date() if tail_date_text else actual_date
        cap = capacity_minutes_for_machine_day(con, machine_id, tail_date)
        capacity_minutes = int(cap["capacity_minutes"] or 0)
        max_qty_for_tail_day = math.floor(capacity_minutes / cycle_time) if cycle_time > 0 else 0
        current_qty = float(tail["qty_done"] or 0)
        available_qty_on_tail = max(0.0, max_qty_for_tail_day - current_qty)
        add_to_tail = min(remaining_to_add, available_qty_on_tail)

        if add_to_tail > 0:
            new_qty = current_qty + add_to_tail
            new_minutes = new_qty * cycle_time
            start_dt = parse_dt_text(tail["start_datetime"])
            end_dt = start_dt + timedelta(minutes=new_minutes) if start_dt else parse_dt_text(tail["end_datetime"])
            con.execute(
                """
                UPDATE run_block_segment
                SET qty_done = ?, minutes_used = ?, end_datetime = ?
                WHERE segment_id = ?
                """,
                (
                    new_qty,
                    new_minutes,
                    end_dt.strftime("%Y-%m-%d %H:%M:%S") if end_dt else tail["end_datetime"],
                    int(tail["segment_id"]),
                ),
            )
            remaining_to_add -= add_to_tail
            changed = True

        if remaining_to_add > 0:
            changed = add_future_segments_after_date(con, block_id, tail_date, remaining_to_add) or changed
    else:
        changed = add_future_segments_after_date(con, block_id, actual_date, remaining_to_add)

    return changed


def refresh_block_schedule_bounds(con, block_id):
    block = trial_block_row(con, block_id)
    if not block:
        return
    bounds = one(
        con.execute(
            """
            SELECT
              MIN(start_datetime) AS start_datetime,
              MAX(end_datetime) AS end_datetime
            FROM run_block_segment
            WHERE block_id = ?
            """,
            (int(block_id),),
        )
    ) or {}

    future = one(
        con.execute(
            """
            SELECT COALESCE(SUM(qty_done), 0) AS future_qty
            FROM run_block_segment
            WHERE block_id = ?
              AND segment_type = 'production'
              AND segment_id NOT IN (
                SELECT segment_id
                FROM production_actual
                WHERE segment_id IS NOT NULL
                  AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
              )
            """,
            (int(block_id),),
        )
    ) or {}

    totals = actual_totals_for_block(con, block_id)
    required_qty = float(block["scheduled_qty"] or 0)
    valid_done = max(0.0, float(totals["output_qty"] or 0) - float(totals["reject_qty"] or 0))
    remaining_required = max(0.0, required_qty - valid_done)
    future_qty = float(future.get("future_qty") or 0)

    if remaining_required <= 0:
        planning_status = "PLANNED"
    elif future_qty <= 0:
        planning_status = "UNPLANNED"
    elif future_qty + 1e-9 >= remaining_required:
        planning_status = "PLANNED"
    else:
        planning_status = "PARTIALLY_PLANNED"

    con.execute(
        """
        UPDATE run_block
        SET calculated_start_datetime = ?, calculated_end_datetime = ?,
            planning_status = ?, updated_at = CURRENT_TIMESTAMP
        WHERE block_id = ?
        """,
        (
            compact_text(bounds.get("start_datetime")),
            compact_text(bounds.get("end_datetime")),
            planning_status,
            int(block_id),
        ),
    )


def apply_output_delta_to_block_tail(con, block_id, actual_date_text, delta_qty):
    delta_qty = float(delta_qty or 0)
    if delta_qty == 0:
        return {"changed": False, "applied_qty": 0.0}

    block = trial_block_row(con, block_id)
    if not block:
        return {"changed": False, "applied_qty": 0.0}

    actual_date = parse_dt_text(actual_date_text).date() if actual_date_text else date.today()

    if delta_qty < 0:
        return {"changed": False, "applied_qty": abs(delta_qty)}

    remaining_to_shave = float(delta_qty)
    if remaining_to_shave <= 0:
        return {"changed": False, "applied_qty": 0.0}

    cycle_time = max(0.0, float(block["cycle_minutes_per_qty"] or 0))
    if cycle_time <= 0:
        return {"changed": False, "applied_qty": 0.0}

    future_segments = rows(
        con.execute(
            """
            SELECT *
            FROM run_block_segment
            WHERE block_id = ?
              AND segment_type = 'production'
              AND segment_date > ?
              AND segment_id NOT IN (
                SELECT segment_id
                FROM production_actual
                WHERE segment_id IS NOT NULL
                  AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
              )
            ORDER BY segment_date DESC, end_datetime DESC, segment_id DESC
            """,
            (int(block_id), date_text(actual_date)),
        )
    )

    changed = False
    for seg in future_segments:
        if remaining_to_shave <= 0:
            break

        seg_qty = float(seg["qty_done"] or 0)
        shave = min(seg_qty, remaining_to_shave)
        new_qty = seg_qty - shave

        if new_qty <= 0:
            con.execute("DELETE FROM run_block_segment WHERE segment_id = ?", (int(seg["segment_id"]),))
        else:
            new_minutes = new_qty * cycle_time
            start_dt = parse_dt_text(seg["start_datetime"])
            end_dt = start_dt + timedelta(minutes=new_minutes) if start_dt else parse_dt_text(seg["end_datetime"])
            con.execute(
                """
                UPDATE run_block_segment
                SET qty_done = ?, minutes_used = ?, end_datetime = ?
                WHERE segment_id = ?
                """,
                (
                    new_qty,
                    new_minutes,
                    end_dt.strftime("%Y-%m-%d %H:%M:%S") if end_dt else seg["end_datetime"],
                    int(seg["segment_id"]),
                ),
            )

        remaining_to_shave -= shave
        changed = True

    return {"changed": changed, "applied_qty": delta_qty}


def find_rework_source_for_reject(con, reject_block_id):
    reject_block = trial_block_row(con, reject_block_id)
    if not reject_block:
        return None

    source_ps_id = compact_text(reject_block["source_ps_id"])
    source_op_seq_id = int(reject_block["source_op_seq_id"] or 0)
    reject_done = compact_text(reject_block["execution_status"] or reject_block["status"]).upper() == "DONE"
    if not source_ps_id or not source_op_seq_id:
        return reject_block if reject_done else None

    reject_step = one(con.execute("SELECT bom_id AS bom_id, seq_no FROM operation_seq WHERE op_seq_id = ?", (source_op_seq_id,)))
    if not reject_step:
        return reject_block if reject_done else None

    affected_rows = rows(
        con.execute(
            """
            SELECT b.*
            FROM run_block b
            JOIN operation o ON o.operation_id = b.operation_id
            JOIN operation_seq pfs ON pfs.op_seq_id = o.source_op_seq_id
            WHERE o.source_ps_id = ?
              AND COALESCE(b.block_type, 'ORIGINAL') = 'ORIGINAL'
              AND pfs.bom_id = ?
              AND pfs.seq_no <= ?
              AND COALESCE(b.execution_status, b.status, '') = 'DONE'
            ORDER BY pfs.seq_no, pfs.op_seq_id, b.block_id
            """,
            (source_ps_id, int(reject_step["bom_id"] or 0), int(reject_step["seq_no"] or 0)),
        )
    )
    return affected_rows[0] if affected_rows else None


def rework_op_for_block(con, block_id):
    block = trial_block_row(con, block_id)
    if not block:
        return None

    source_ps_id = compact_text(block["source_ps_id"])
    source_op_seq_id = int(block["source_op_seq_id"] or 0)
    step = None
    if source_op_seq_id:
        step = one(con.execute("SELECT op_seq_id AS op_seq_id, bom_id AS bom_id, seq_no, op_no, op_type, machine_category, preferred_machine, cycle_time, setup_time, is_last_op FROM operation_seq WHERE op_seq_id = ?", (source_op_seq_id,)))
    if not step and source_ps_id:
        step = one(
            con.execute(
                """
                SELECT pfs.*
                FROM process_sheet ps
                JOIN operation_seq pfs ON pfs.bom_id = ps.selected_bom_id
                WHERE ps.ps_id = ?
                ORDER BY pfs.seq_no, pfs.op_seq_id
                LIMIT 1
                """,
                (source_ps_id,),
            )
        )
    if not step:
        return {
            "source_ps_id": source_ps_id,
            "source_op_seq_id": source_op_seq_id,
            "source_op_no": block["source_op_no"] or "",
            "operation_name": f"{block['operation_name'] or ''} REWORK".strip(),
            "machine_id": int(block["machine_id"] or 0),
            "machine_category": block["compatible_machine_group"] or block["machine_category"] or "",
            "cycle_minutes_per_qty": float(block["cycle_minutes_per_qty"] or 0),
            "setup_minutes": float(block["setup_minutes"] or 0),
        }

    machine = None
    preferred_machine = compact_text(step["preferred_machine"])
    if preferred_machine:
        machine = one(
            con.execute(
                "SELECT * FROM machines WHERE machine_code = ? AND active = 1",
                (preferred_machine,),
            )
        )

    if not machine:
        machine = one(
            con.execute(
                """
                SELECT *
                FROM machines
                WHERE machine_category = ? AND active = 1
                ORDER BY machine_id
                LIMIT 1
                """,
                (compact_text(step["machine_category"]) or "UNKNOWN",),
            )
        )

    if not machine:
        machine = one(con.execute("SELECT * FROM machines WHERE active = 1 ORDER BY machine_id LIMIT 1"))

    return {
        "source_ps_id": source_ps_id,
        "source_op_seq_id": int(step["op_seq_id"] or 0),
        "source_op_no": step["op_no"] or "",
        "operation_name": f"{step['op_no'] or ''} {step['op_type'] or ''} REWORK".strip(),
        "machine_id": int(machine["machine_id"]) if machine else 0,
        "machine_category": step["machine_category"] or "",
        "cycle_minutes_per_qty": float(step["cycle_time"] or 0),
        "setup_minutes": float(step["setup_time"] or 0),
    }


def preserved_actual_bounds_for_block(con, block_id):
    row = one(
        con.execute(
            """
            SELECT
              MIN(s.start_datetime) AS start_datetime,
              MAX(s.end_datetime) AS end_datetime,
              COUNT(CASE WHEN a.actual_id IS NOT NULL AND (a.output_qty IS NOT NULL OR a.reject_qty IS NOT NULL) THEN 1 END) AS actual_count
            FROM run_block_segment s
            LEFT JOIN production_actual a
              ON a.segment_id = s.segment_id
             AND COALESCE(a.status, 'ACTIVE') = 'ACTIVE'
            WHERE s.block_id = ?
              AND (
                (
                  a.actual_id IS NOT NULL
                  AND (a.output_qty IS NOT NULL OR a.reject_qty IS NOT NULL)
                )
                OR (
                  s.segment_type = 'setup'
                  AND s.block_id IN (
                    SELECT DISTINCT block_id
                    FROM production_actual
                    WHERE COALESCE(status, 'ACTIVE') = 'ACTIVE'
                      AND (output_qty IS NOT NULL OR reject_qty IS NOT NULL)
                  )
                )
              )
            """,
            (int(block_id),),
        )
    ) or {}

    start_dt = parse_dt_text(row.get("start_datetime"))
    end_dt = parse_dt_text(row.get("end_datetime"))
    actual_count = int(row.get("actual_count") or 0)
    if actual_count <= 0 or not start_dt or not end_dt:
        return None
    return {"start_datetime": start_dt, "end_datetime": end_dt}


def latest_actual_date_for_block(con, block_id):
    row = one(
        con.execute(
            """
            SELECT MAX(s.segment_date) AS latest_actual_date
            FROM run_block_segment s
            JOIN production_actual a
              ON a.segment_id = s.segment_id
            WHERE s.block_id = ?
              AND COALESCE(a.status, 'ACTIVE') = 'ACTIVE'
              AND (a.output_qty IS NOT NULL OR a.reject_qty IS NOT NULL)
            """,
            (int(block_id),),
        )
    ) or {}
    text = compact_text(row.get("latest_actual_date"))
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def delete_rework_from_reject_segment(con, reject_segment_id):
    rework_rows = rows(
        con.execute(
            """
            SELECT block_id, operation_id, machine_id
            FROM run_block
            WHERE block_type = 'REWORK' AND source_reject_segment_id = ?
            """,
            (int(reject_segment_id),),
        )
    )
    machine_ids = set()
    for row in rework_rows:
        machine_ids.add(int(row["machine_id"] or 0))
        con.execute("DELETE FROM run_block WHERE block_id = ?", (int(row["block_id"]),))
        remaining = one(con.execute("SELECT COUNT(*) AS cnt FROM run_block WHERE operation_id = ?", (int(row["operation_id"]),)))
        if int((remaining or {})["cnt"] if remaining else 0) <= 0:
            con.execute("DELETE FROM operation WHERE operation_id = ?", (int(row["operation_id"]),))
    return {machine_id for machine_id in machine_ids if machine_id}


def create_rework_from_reject(con, rework_source_block_id, reject_segment_id, reject_qty):
    reject_qty = max(0.0, float(reject_qty or 0))
    if reject_qty <= 0:
        return {"created": False, "machine_id": 0}

    rework_source_block = trial_block_row(con, rework_source_block_id)
    if not rework_source_block:
        return {"created": False, "machine_id": 0}

    existing_rework = one(
        con.execute(
            """
            SELECT b.block_id, b.machine_id, b.operation_id
            FROM run_block b
            WHERE b.block_type = 'REWORK' AND b.source_reject_segment_id = ?
            LIMIT 1
            """,
            (int(reject_segment_id),),
        )
    )
    if existing_rework:
        con.execute(
            """
            UPDATE run_block
            SET scheduled_qty = ?, updated_at = CURRENT_TIMESTAMP
            WHERE block_id = ?
            """,
            (reject_qty, int(existing_rework["block_id"])),
        )
        con.execute(
            """
            UPDATE operation
            SET total_qty = ?, updated_at = CURRENT_TIMESTAMP
            WHERE operation_id = ?
            """,
            (reject_qty, int(existing_rework["operation_id"])),
        )
        return {"created": False, "machine_id": int(existing_rework["machine_id"] or 0)}

    first_op = rework_op_for_block(con, rework_source_block_id)
    if not first_op or not first_op["machine_id"]:
        return {"created": False, "machine_id": 0}

    machine_id = int(first_op["machine_id"])
    queue_position = 1 + float(
        one(
            con.execute(
                "SELECT COALESCE(MAX(queue_position), 0) AS mx FROM run_block WHERE machine_id = ?",
                (machine_id,),
            )
        )["mx"]
        or 0
    )

    op_cur = con.execute(
        """
        INSERT INTO operation (
          job_no, operation_name, total_qty, setup_minutes, cycle_minutes_per_qty, compatible_machine_group,
          source_ps_id, source_op_seq_id, source_op_no, status, remarks, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, CURRENT_TIMESTAMP)
        """,
        (
            rework_source_block["job_no"],
            first_op["operation_name"],
            reject_qty,
            first_op["setup_minutes"],
            first_op["cycle_minutes_per_qty"],
            first_op["machine_category"],
            first_op["source_ps_id"],
            first_op["source_op_seq_id"],
            first_op["source_op_no"],
            f"REWORK from reject on block {rework_source_block_id}",
        ),
    )

    operation_id = int(op_cur.lastrowid)
    block_cur = con.execute(
        """
        INSERT INTO run_block (
          operation_id, machine_id, queue_position, scheduled_qty, include_setup, status, planning_status, execution_status,
          anchor_datetime, calculated_start_datetime, calculated_end_datetime, actual_good_qty, actual_reject_qty, remarks,
          block_type, source_reject_block_id, source_reject_segment_id, updated_at
        ) VALUES (?, ?, ?, ?, 1, 'NOT_STARTED', 'PLANNED', 'NOT_STARTED', '', '', '', 0, 0, ?, 'REWORK', ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            operation_id,
            machine_id,
            queue_position,
            reject_qty,
            f"REWORK qty {format_qty(reject_qty)} created from reject on {rework_source_block['job_no']}",
            int(rework_source_block_id),
            int(reject_segment_id),
        ),
    )
    rework_block_id = int(block_cur.lastrowid)
    source_actual = one(
        con.execute(
            """
            SELECT actual_id
            FROM production_actual
            WHERE segment_id = ?
              AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
            ORDER BY actual_id DESC
            LIMIT 1
            """,
            (int(reject_segment_id),),
        )
    )
    con.execute(
        """
        INSERT OR IGNORE INTO rework_link (
          source_actual_id, source_block_id, rework_block_id, reject_qty, created_at
        ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            int(source_actual["actual_id"]) if source_actual else None,
            int(rework_source_block_id),
            rework_block_id,
            reject_qty,
        ),
    )
    return {"created": True, "machine_id": machine_id}


def _next_interval_after(con, machine_id, current_dt):
    probe = current_dt.date()
    safety = 0
    while safety < 365:
        safety += 1
        intervals = machine_work_intervals_for_day(con, machine_id, probe)
        for start_dt, end_dt in intervals:
            if end_dt <= current_dt:
                continue
            if start_dt >= current_dt:
                return start_dt, end_dt
            if start_dt <= current_dt < end_dt:
                return current_dt, end_dt
        probe += timedelta(days=1)
        current_dt = datetime.combine(probe, datetime.min.time())
    return None, None


def _schedule_setup_across_intervals(con, machine_id, block_id, schedule_run_id, start_dt, remaining_setup):
    current_dt = start_dt
    first_start = None
    end_dt = None
    safety = 0
    remaining = float(remaining_setup or 0)
    while remaining > 0 and safety < 365:
        safety += 1
        interval_start, interval_end = _next_interval_after(con, machine_id, current_dt)
        if not interval_start or not interval_end:
            break
        if current_dt < interval_start:
            current_dt = interval_start
        if first_start is None:
            first_start = current_dt
        available = max(0.0, (interval_end - current_dt).total_seconds() / 60.0)
        if available <= 0:
            current_dt = interval_end
            continue
        use = min(remaining, available)
        seg_end = current_dt + timedelta(minutes=use)
        con.execute(
            """
            INSERT INTO run_block_segment (
              block_id, machine_id, schedule_run_id, segment_date, segment_type, qty_done, planned_qty,
              minutes_used, planned_minutes, segment_status, start_datetime, end_datetime, is_actual
            ) VALUES (?, ?, ?, ?, 'setup', 0, 0, ?, ?, 'PLANNED', ?, ?, 0)
            """,
            (
                int(block_id),
                int(machine_id),
                int(schedule_run_id),
                date_text(current_dt.date()),
                use,
                use,
                current_dt.strftime("%Y-%m-%d %H:%M:%S"),
                seg_end.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        remaining -= use
        current_dt = seg_end
        end_dt = seg_end
    return first_start, current_dt, end_dt, remaining


def _schedule_production_across_intervals(con, machine_id, block, schedule_run_id, start_dt, remaining_qty, cycle_time):
    current_dt = start_dt
    first_start = None
    end_dt = None
    remaining = float(remaining_qty or 0)
    cycle_time = max(0.0, float(cycle_time or 0))
    safety = 0
    while remaining > 0 and safety < 365:
        safety += 1
        interval_start, interval_end = _next_interval_after(con, machine_id, current_dt)
        if not interval_start or not interval_end:
            break
        if current_dt < interval_start:
            current_dt = interval_start
        if first_start is None:
            first_start = current_dt
        available = max(0.0, (interval_end - current_dt).total_seconds() / 60.0)
        if available <= 0:
            current_dt = interval_end
            continue
        if cycle_time <= 0:
            break
        qty = min(remaining, math.floor(available / cycle_time))
        if qty <= 0:
            current_dt = interval_end
            continue
        use = qty * cycle_time
        seg_end = current_dt + timedelta(minutes=use)
        con.execute(
            """
            INSERT INTO run_block_segment (
              block_id, machine_id, schedule_run_id, segment_date, segment_type, qty_done, planned_qty,
              minutes_used, planned_minutes, segment_status, start_datetime, end_datetime, is_actual
            ) VALUES (?, ?, ?, ?, 'production', ?, ?, ?, ?, 'PLANNED', ?, ?, 0)
            """,
            (
                int(block["block_id"]),
                int(machine_id),
                int(schedule_run_id),
                date_text(current_dt.date()),
                qty,
                use,
                use,
                use,
                current_dt.strftime("%Y-%m-%d %H:%M:%S"),
                seg_end.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        remaining -= qty
        current_dt = seg_end
        end_dt = seg_end
    return first_start, current_dt, end_dt, remaining


def _schedule_combined_production_across_intervals(con, machine_id, members, schedule_run_id, start_dt, remaining_qty):
    current_dt = start_dt
    first_start = None
    end_dt = None
    remaining = float(remaining_qty or 0)
    combined_cycle = sum(float(member["cycle_minutes_per_qty"] or 0) for member in members)
    safety = 0
    while remaining > 0 and safety < 365:
        safety += 1
        interval_start, interval_end = _next_interval_after(con, machine_id, current_dt)
        if not interval_start or not interval_end:
            break
        if current_dt < interval_start:
            current_dt = interval_start
        if first_start is None:
            first_start = current_dt
        available = max(0.0, (interval_end - current_dt).total_seconds() / 60.0)
        if available <= 0:
            current_dt = interval_end
            continue
        if combined_cycle <= 0:
            break
        qty = min(remaining, math.floor(available / combined_cycle))
        if qty <= 0:
            current_dt = interval_end
            continue
        group_use = qty * combined_cycle
        seg_end = current_dt + timedelta(minutes=group_use)
        for idx, member in enumerate(members):
            member_cycle = max(0.0, float(member["cycle_minutes_per_qty"] or 0))
            member_minutes = qty * member_cycle
            member_end = current_dt + timedelta(minutes=member_minutes)
            con.execute(
                """
                INSERT INTO run_block_segment (
                  block_id, machine_id, schedule_run_id, segment_date, segment_type, qty_done, planned_qty,
                  minutes_used, planned_minutes, segment_status, start_datetime, end_datetime, is_actual
                ) VALUES (?, ?, ?, ?, 'production', ?, ?, ?, ?, 'PLANNED', ?, ?, 0)
                """,
                (
                    int(member["block_id"]),
                    int(machine_id),
                    int(schedule_run_id),
                    date_text(current_dt.date()),
                    qty,
                    member_minutes,
                    member_minutes,
                    member_minutes,
                    current_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    member_end.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            if idx == len(members) - 1:
                end_dt = seg_end
        remaining -= qty
        current_dt = seg_end
    return first_start, current_dt, end_dt, remaining


def recalculate_machine(con, machine_id, reason="PLANNER_CHANGE", schedule_run_id=None):
    if schedule_run_id is None:
        schedule_run_id = create_schedule_run(con, reason=reason, scope_type="MACHINE", machine_id=int(machine_id), notes=f"Recalculate machine {machine_id}")
    blocks = rows(
        con.execute(
            """
            SELECT b.*, o.job_no, o.operation_name, o.total_qty, o.setup_minutes, o.cycle_minutes_per_qty,
                   o.compatible_machine_group, o.source_ps_id, o.source_op_seq_id AS source_op_seq_id, o.source_op_no,
                   m.machine_code, m.machine_category, m.shift_profile
            FROM run_block b
            JOIN operation o ON o.operation_id = b.operation_id
            JOIN machines m ON m.machine_id = b.machine_id
            WHERE b.machine_id = ?
              AND COALESCE(b.active, 1) = 1
            ORDER BY b.queue_position, b.block_id
            """,
            (int(machine_id),),
        )
    )
    block_ids = [int(b["block_id"]) for b in blocks]
    if block_ids:
        q = ",".join("?" for _ in block_ids)
        con.execute(
            f"""
            DELETE FROM run_block_segment
            WHERE block_id IN ({q})
              AND segment_id NOT IN (
                SELECT segment_id
                FROM production_actual
                WHERE segment_id IS NOT NULL
                  AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
              )
              AND NOT (
                segment_type = 'setup'
                AND block_id IN (
                  SELECT DISTINCT block_id
                  FROM production_actual
                  WHERE COALESCE(status, 'ACTIVE') = 'ACTIVE'
                    AND (output_qty IS NOT NULL OR reject_qty IS NOT NULL)
                )
              )
            """,
            block_ids,
        )

    if not blocks:
        return

    today_start = datetime.combine(date.today(), datetime.min.time()).replace(hour=8, minute=30, second=0, microsecond=0)

    combined_groups = {}
    queue_items = []
    for block in blocks:
        group_id = int(block["group_id"] or 0)
        if group_id > 0:
            combined_groups.setdefault(group_id, []).append(block)
        else:
            queue_items.append({"members": [block], "combined": False})
    for members in combined_groups.values():
        members.sort(key=lambda row: (float(row["queue_position"] or 0), int(row["block_id"] or 0)))
        queue_items.append({"members": members, "combined": len(members) > 1})

    def item_sort_key(item):
        leader = item["members"][0]
        return (
            float(leader["queue_position"] or 0),
            int(leader["block_id"] or 0),
        )

    queue_items.sort(key=item_sort_key)

    anchor_values = [parsed for parsed in (parse_dt_text(item["members"][0]["anchor_datetime"]) for item in queue_items) if parsed]
    actual_start_values = []
    for item in queue_items:
        leader = item["members"][0]
        actual_bounds = preserved_actual_bounds_for_block(con, int(leader["block_id"]))
        if actual_bounds:
            actual_start_values.append(actual_bounds["start_datetime"])
    start_candidates = [today_start, *anchor_values, *actual_start_values]
    current_dt = min(start_candidates) if start_candidates else today_start

    def update_block_schedule_window(block_id, start_dt, end_dt, planning_status=None):
        start_text = start_dt.strftime("%Y-%m-%d %H:%M:%S") if start_dt else ""
        end_text = end_dt.strftime("%Y-%m-%d %H:%M:%S") if end_dt else ""
        con.execute(
            """
            UPDATE run_block
            SET calculated_start_datetime = ?, calculated_end_datetime = ?,
                last_schedule_run_id = ?,
                planned_qty_original = CASE WHEN COALESCE(planned_qty_original, 0) <= 0 THEN scheduled_qty ELSE planned_qty_original END,
                planning_status = COALESCE(?, CASE
                  WHEN COALESCE(planning_status, '') = 'UNPLANNED' THEN 'PLANNED'
                  ELSE planning_status
                END),
                status = COALESCE(execution_status, status, 'NOT_STARTED'),
                updated_at = CURRENT_TIMESTAMP
            WHERE block_id = ?
            """,
            (
                start_text,
                end_text,
                int(schedule_run_id) if schedule_run_id is not None else None,
                planning_status,
                int(block_id),
            ),
        )
        if not start_text or not end_text:
            upsert_schedule_alert(
                con,
                schedule_run_id=schedule_run_id,
                block_id=block_id,
                operation_id=int(block["operation_id"]),
                ps_id=compact_text(block.get("source_ps_id")),
                machine_id=int(machine_id),
                alert_type="NO_CAPACITY",
                severity="WARN",
                message="No capacity found while recalculating.",
                planned_at=block.get("planned_end_at") or block.get("calculated_end_datetime") or "",
                predicted_at="",
                delay_minutes=0,
                status="OPEN",
            )
        else:
            for alert in rows(
                con.execute(
                    """
                    SELECT alert_id
                    FROM schedule_alert
                    WHERE block_id = ?
                      AND alert_type = 'NO_CAPACITY'
                      AND status IN ('OPEN', 'ACKNOWLEDGED')
                    """,
                    (int(block_id),),
                )
            ):
                resolve_schedule_alert(con, int(alert["alert_id"]))
            old_end = compact_text(block.get("planned_end_at") or block.get("calculated_end_datetime"))
            if old_end and old_end != end_text:
                upsert_schedule_alert(
                    con,
                    schedule_run_id=schedule_run_id,
                    block_id=block_id,
                    operation_id=int(block["operation_id"]),
                    ps_id=compact_text(block.get("source_ps_id")),
                    machine_id=int(machine_id),
                    alert_type="PREDICTED_END_CHANGED",
                    severity="INFO",
                    message="Predicted end changed after recalculation.",
                    old_value=old_end,
                    new_value=end_text,
                    planned_at=old_end,
                    predicted_at=end_text,
                    delay_minutes=0,
                    status="OPEN",
                )

    for item in queue_items:
        members = item["members"]
        leader = members[0]
        is_combined = bool(item["combined"])

        if not is_combined:
            block = leader
            planned_start = parse_dt_text(block["planned_start_at"]) or parse_dt_text(block["anchor_datetime"])
            dependency_finish = dependency_finish_for_block(con, block)
            candidate_start = current_dt
            if dependency_finish and dependency_finish > candidate_start:
                candidate_start = dependency_finish
            if planned_start and candidate_start < planned_start and (int(block.get("allow_pull_forward") if block.get("allow_pull_forward") is not None else 1) == 0 or int(block.get("is_fresh_monday_item") or 0) == 1):
                candidate_start = planned_start
            current_dt = candidate_start

            actual_bounds = preserved_actual_bounds_for_block(con, int(block["block_id"]))
            if actual_bounds:
                update_block_schedule_window(
                    block["block_id"],
                    actual_bounds["start_datetime"],
                    actual_bounds["end_datetime"],
                    None,
                )
                totals = actual_totals_for_block(con, block["block_id"])
                reported_output = max(0.0, float(totals["output_qty"] or 0) - float(totals["reject_qty"] or 0))
                scheduled_qty = max(0.0, float(block["scheduled_qty"] or 0))
                remaining_qty = max(0.0, scheduled_qty - reported_output)
                latest_actual_date = latest_actual_date_for_block(con, int(block["block_id"]))
                if remaining_qty > 0 and latest_actual_date:
                    add_future_segments_after_date(con, int(block["block_id"]), latest_actual_date, remaining_qty, schedule_run_id=schedule_run_id)
                refresh_block_schedule_bounds(con, int(block["block_id"]))
                refreshed = trial_block_row(con, int(block["block_id"]))
                refreshed_end = parse_dt_text(refreshed["calculated_end_datetime"]) if refreshed else None
                current_dt = refreshed_end or actual_bounds["end_datetime"]
                continue

            raw_reported_output = max(0.0, float(block["actual_good_qty"] or 0))
            reported_reject = max(0.0, float(block["actual_reject_qty"] or 0))
            reported_output = max(0.0, raw_reported_output - reported_reject)
            scheduled_qty = max(0.0, float(block["scheduled_qty"] or 0))
            remaining_qty = max(0.0, scheduled_qty - reported_output)
            if remaining_qty <= 0:
                remaining_qty = scheduled_qty

            setup_minutes = float(block["setup_minutes"] or 0) if int(block["include_setup"] or 0) == 1 else 0.0
            remaining_setup = 0.0 if (reported_output > 0 or reported_reject > 0) else setup_minutes
            cycle_time = max(0.0, float(block["cycle_minutes_per_qty"] or 0))
            if cycle_time <= 0:
                remaining_qty = 0.0
            start_dt = None
            end_dt = None
            if remaining_setup > 0:
                setup_start, current_dt, setup_end, remaining_setup = _schedule_setup_across_intervals(
                    con,
                    machine_id,
                    int(block["block_id"]),
                    schedule_run_id,
                    current_dt,
                    remaining_setup,
                )
                start_dt = start_dt or setup_start
                end_dt = setup_end or end_dt
            if remaining_qty > 0 and cycle_time > 0:
                prod_start, current_dt, prod_end, remaining_qty = _schedule_production_across_intervals(
                    con,
                    machine_id,
                    block,
                    schedule_run_id,
                    current_dt,
                    remaining_qty,
                    cycle_time,
                )
                start_dt = start_dt or prod_start
                end_dt = prod_end or end_dt

            update_block_schedule_window(block["block_id"], start_dt, end_dt, None)
            continue

        setup_minutes = max((float(member["setup_minutes"] or 0) for member in members), default=0.0)
        combined_cycle = sum(float(member["cycle_minutes_per_qty"] or 0) for member in members)
        scheduled_qty = max((float(member["scheduled_qty"] or 0) for member in members), default=0.0)
        leader_planned_start = parse_dt_text(leader["planned_start_at"]) or parse_dt_text(leader["anchor_datetime"])
        leader_dependency_finish = dependency_finish_for_block(con, leader)
        candidate_start = current_dt
        if leader_dependency_finish and leader_dependency_finish > candidate_start:
            candidate_start = leader_dependency_finish
        if leader_planned_start and candidate_start < leader_planned_start and (int(leader.get("allow_pull_forward") if leader.get("allow_pull_forward") is not None else 1) == 0 or int(leader.get("is_fresh_monday_item") or 0) == 1):
            candidate_start = leader_planned_start
        current_dt = candidate_start

        actual_bounds_by_block = {int(member["block_id"]): preserved_actual_bounds_for_block(con, int(member["block_id"])) for member in members}
        if any(actual_bounds_by_block.values()):
            max_end = None
            for member in members:
                member_id = int(member["block_id"])
                actual_bounds = actual_bounds_by_block.get(member_id)
                if actual_bounds:
                    update_block_schedule_window(member_id, actual_bounds["start_datetime"], actual_bounds["end_datetime"], None)
                totals = actual_totals_for_block(con, member_id)
                reported_output = max(0.0, float(totals["output_qty"] or 0) - float(totals["reject_qty"] or 0))
                member_scheduled_qty = max(0.0, float(member["scheduled_qty"] or 0))
                remaining_qty = max(0.0, member_scheduled_qty - reported_output)
                latest_actual_date = latest_actual_date_for_block(con, member_id)
                if remaining_qty > 0 and latest_actual_date:
                    add_future_segments_after_date(con, member_id, latest_actual_date, remaining_qty, schedule_run_id=schedule_run_id)
                refresh_block_schedule_bounds(con, member_id)
                refreshed = trial_block_row(con, member_id)
                refreshed_end = parse_dt_text(refreshed["calculated_end_datetime"]) if refreshed else None
                if refreshed_end and (max_end is None or refreshed_end > max_end):
                    max_end = refreshed_end
            current_dt = max_end or current_dt
            continue

        remaining_setup = setup_minutes if int(leader["include_setup"] or 0) == 1 else 0.0
        remaining_qty = scheduled_qty
        start_dt = None
        end_dt = None
        if remaining_setup > 0:
            setup_start, current_dt, setup_end, remaining_setup = _schedule_setup_across_intervals(
                con,
                machine_id,
                int(leader["block_id"]),
                schedule_run_id,
                current_dt,
                remaining_setup,
            )
            start_dt = start_dt or setup_start
            end_dt = setup_end or end_dt
        if remaining_qty > 0 and combined_cycle > 0:
            group_start, current_dt, group_end, remaining_qty = _schedule_combined_production_across_intervals(
                con,
                machine_id,
                members,
                schedule_run_id,
                current_dt,
                remaining_qty,
            )
            start_dt = start_dt or group_start
            end_dt = group_end or end_dt

        for member in members[1:]:
            refresh_block_schedule_bounds(con, int(member["block_id"]))
        if start_dt and end_dt:
            update_block_schedule_window(leader["block_id"], start_dt, end_dt, None)
        else:
            refresh_block_schedule_bounds(con, int(leader["block_id"]))
        refreshed_leader = trial_block_row(con, int(leader["block_id"]))
        refreshed_end = parse_dt_text(refreshed_leader["calculated_end_datetime"]) if refreshed_leader else None
        if refreshed_end and refreshed_end > current_dt:
            current_dt = refreshed_end

    refresh_states_for_machine(con, int(machine_id), schedule_run_id=schedule_run_id)


def recalculate_all(con):
    machine_ids = [row["machine_id"] for row in rows(con.execute("SELECT machine_id FROM machines WHERE active = 1 ORDER BY machine_id"))]
    schedule_run_id = create_schedule_run(con, reason="MANUAL_RECALCULATE", scope_type="FULL", machine_id=None, notes="Recalculate all machines")
    for machine_id in machine_ids:
        recalculate_machine(con, machine_id, reason="MANUAL_RECALCULATE", schedule_run_id=schedule_run_id)


def refresh_block_group_label(con, group_id):
    group_id = int(group_id or 0)
    if not group_id:
        return ""
    members = rows(
        con.execute(
            """
            SELECT b.block_id, b.queue_position, o.operation_name, o.source_op_no
            FROM run_block b
            JOIN operation o ON o.operation_id = b.operation_id
            WHERE b.group_id = ?
            ORDER BY b.queue_position, b.block_id
            """,
            (group_id,),
        )
    )
    label = " + ".join(
        compact_text(row["source_op_no"] or row["operation_name"] or f"Block {row['block_id']}")
        for row in members
        if compact_text(row["source_op_no"] or row["operation_name"])
    )
    if " + " in label:
        label = " & ".join(part.strip() for part in label.split(" + ") if part.strip())
    if not label:
        label = "Combined"
    con.execute(
        "UPDATE run_block_group SET group_label = ? WHERE group_id = ?",
        (label, group_id),
    )
    return label
