from __future__ import annotations

from datetime import date, datetime, timedelta

from .db import date_text, one, parse_dt_text, rows
from .machines import machine_work_intervals_for_day
from .planning_settings import (
    get_planning_calendar_policy,
    get_planning_efficiency,
    get_planning_start_minute,
)
from .utils import compact_text

# Planning baseline note:
# - uses run_block as planner input
# - writes planning_schedule_segment and planning_*_state tables
# - Monday-Friday only
# - ignores actuals
# - planning_cycle_minutes = cycle_minutes_per_qty / planning_efficiency
# Example: 10 min/qty / 0.85 = 11.76 min/qty


def _merge_intervals(intervals):
    cleaned = sorted([(start, end) for start, end in intervals if start and end and end > start], key=lambda item: item[0])
    if not cleaned:
        return []
    merged = [cleaned[0]]
    for start, end in cleaned[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _block_row_for_schedule(con, block_id):
    return one(
        con.execute(
            """
            SELECT b.*, o.job_no, o.operation_name, o.total_qty, o.setup_minutes, o.cycle_minutes_per_qty,
                   o.compatible_machine_group, o.source_ps_id, o.source_op_seq_id AS source_op_seq_id, o.source_op_no,
                   o.status AS operation_status,
                   m.machine_code, m.machine_category, m.shift_profile
            FROM run_block b
            JOIN operation o ON o.operation_id = b.operation_id
            JOIN machines m ON m.machine_id = b.machine_id
            WHERE b.block_id = ?
            """,
            (int(block_id),),
        )
    )


def _active_blocks_for_machine(con, machine_id):
    return rows(
        con.execute(
            """
            SELECT b.*, o.job_no, o.operation_name, o.total_qty, o.setup_minutes, o.cycle_minutes_per_qty,
                   o.compatible_machine_group, o.source_ps_id, o.source_op_seq_id AS source_op_seq_id, o.source_op_no,
                   o.status AS operation_status,
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


def _active_blocks_all(con):
    return rows(
        con.execute(
            """
            SELECT b.*, o.job_no, o.operation_name, o.total_qty, o.setup_minutes, o.cycle_minutes_per_qty,
                   o.compatible_machine_group, o.source_ps_id, o.source_op_seq_id AS source_op_seq_id, o.source_op_no,
                   o.status AS operation_status,
                   m.machine_code, m.machine_category, m.shift_profile
            FROM run_block b
            JOIN operation o ON o.operation_id = b.operation_id
            JOIN machines m ON m.machine_id = b.machine_id
            WHERE COALESCE(b.active, 1) = 1
            ORDER BY b.machine_id, b.queue_position, b.block_id
            """
        )
    )


def _insert_planning_segment(
    con,
    planning_run_id,
    block_id,
    operation_id,
    machine_id,
    segment_date,
    segment_type,
    planned_qty,
    planned_minutes,
    start_dt,
    end_dt,
):
    con.execute(
        """
        INSERT INTO planning_schedule_segment (
          planning_run_id, block_id, operation_id, machine_id,
          segment_date, segment_type, planned_qty, planned_minutes,
          start_datetime, end_datetime
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(planning_run_id),
            int(block_id),
            int(operation_id),
            int(machine_id),
            segment_date,
            segment_type,
            float(planned_qty or 0),
            float(planned_minutes or 0),
            start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )


def create_planning_schedule_run(con, reason="PLANNER_RECALCULATE", planning_efficiency=None, calendar_policy="MON_FRI_ONLY", notes=""):
    con.execute(
        """
        UPDATE planning_schedule_run
        SET status = 'ARCHIVED', updated_at = CURRENT_TIMESTAMP
        WHERE status = 'CURRENT'
        """,
    )
    if planning_efficiency is None:
        planning_efficiency = get_planning_efficiency(con)
    cur = con.execute(
        """
        INSERT INTO planning_schedule_run (
          reason, status, planning_efficiency, calendar_policy, generated_at, created_at, updated_at, notes
        ) VALUES (?, 'CURRENT', ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)
        """,
        (
            str(reason or "PLANNER_RECALCULATE"),
            float(planning_efficiency or get_planning_efficiency(con)),
            str(calendar_policy or get_planning_calendar_policy(con) or "MON_FRI_ONLY").upper(),
            str(notes or ""),
        ),
    )
    return int(cur.lastrowid)


def planning_work_intervals_for_day(con, machine_id, work_date):
    work_day = work_date if isinstance(work_date, date) else date.fromisoformat(str(work_date))
    if work_day.weekday() >= 5:
        return []
    intervals = machine_work_intervals_for_day(con, machine_id, work_day)
    start_minute = max(0, min(24 * 60, int(get_planning_start_minute(con) or 0)))
    if start_minute <= 0:
        return intervals
    floor_dt = datetime.combine(work_day, datetime.min.time()) + timedelta(minutes=start_minute)
    trimmed = []
    for start_dt, end_dt in intervals:
        if end_dt <= floor_dt:
            continue
        trimmed.append((max(start_dt, floor_dt), end_dt))
    return _merge_intervals(trimmed)


def next_planning_interval_after(con, machine_id, current_dt):
    probe = current_dt.date()
    safety = 0
    while safety < 370:
        safety += 1
        intervals = planning_work_intervals_for_day(con, machine_id, probe)
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


def previous_operation_step_for_block(con, block):
    source_ps_id = compact_text(block.get("source_ps_id") or "")
    source_op_seq_id = int(block.get("source_op_seq_id") or 0)
    if not source_ps_id or not source_op_seq_id:
        return None
    current_step = one(
        con.execute(
            """
            SELECT op_seq_id, bom_id, seq_no
            FROM operation_seq
            WHERE op_seq_id = ?
            """,
            (source_op_seq_id,),
        )
    )
    if not current_step:
        return None
    return one(
        con.execute(
            """
            SELECT op_seq_id, bom_id, seq_no, op_no, op_type, machine_category, cycle_time, setup_time, preferred_machine, is_last_op
            FROM operation_seq
            WHERE bom_id = ?
              AND seq_no < ?
            ORDER BY seq_no DESC, op_seq_id DESC
            LIMIT 1
            """,
            (int(current_step["bom_id"] or 0), int(current_step["seq_no"] or 0)),
        )
    )


def cumulative_required_qty_for_block(con, block):
    source_ps_id = compact_text(block.get("source_ps_id") or "")
    source_op_seq_id = int(block.get("source_op_seq_id") or 0)
    if not source_ps_id or not source_op_seq_id:
        return max(0.0, float(block.get("scheduled_qty") or 0))

    related_blocks = rows(
        con.execute(
            """
            SELECT b.block_id, b.queue_position, b.calculated_start_datetime, b.planned_start_at, b.anchor_datetime,
                   b.scheduled_qty
            FROM run_block b
            JOIN operation o ON o.operation_id = b.operation_id
            WHERE COALESCE(b.active, 1) = 1
              AND COALESCE(o.source_ps_id, '') = ?
              AND COALESCE(o.source_op_seq_id, 0) = ?
            ORDER BY
              COALESCE(b.calculated_start_datetime, ''),
              COALESCE(b.planned_start_at, ''),
              COALESCE(b.anchor_datetime, ''),
              COALESCE(b.queue_position, 0),
              b.block_id
            """,
            (source_ps_id, source_op_seq_id),
        )
    )
    total = 0.0
    target_block_id = int(block.get("block_id") or 0)
    for row in related_blocks:
        total += float(row["scheduled_qty"] or 0)
        if int(row["block_id"] or 0) == target_block_id:
            break
    if total <= 0:
        total = max(0.0, float(block.get("scheduled_qty") or 0))
    return total


def planning_dependency_finish_for_block(con, block, planning_run_id, operation_expected_end_by_seq=None):
    previous_step = previous_operation_step_for_block(con, block)
    if not previous_step:
        return None
    previous_op_seq_id = int(previous_step["op_seq_id"] or 0)
    if operation_expected_end_by_seq and previous_op_seq_id in operation_expected_end_by_seq:
        return operation_expected_end_by_seq[previous_op_seq_id]
    row = one(
        con.execute(
            """
            SELECT MAX(expected_end_at) AS expected_end_at
            FROM planning_operation_state
            WHERE planning_run_id = ?
              AND operation_id IN (
                SELECT operation_id
                FROM operation
                WHERE COALESCE(source_ps_id, '') = ?
                  AND COALESCE(source_op_seq_id, 0) = ?
              )
            """,
            (int(planning_run_id), compact_text(block.get("source_ps_id") or ""), previous_op_seq_id),
        )
    )
    text = compact_text(row["expected_end_at"] if row else "")
    return parse_dt_text(text) if text else None


def _schedule_setup_across_intervals(con, machine_id, block_id, operation_id, planning_run_id, start_dt, remaining_setup):
    current_dt = start_dt
    first_start = None
    end_dt = None
    remaining = float(remaining_setup or 0)
    safety = 0
    while remaining > 0 and safety < 370:
        safety += 1
        interval_start, interval_end = next_planning_interval_after(con, machine_id, current_dt)
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
        _insert_planning_segment(
            con,
            planning_run_id,
            block_id,
            operation_id,
            machine_id,
            date_text(current_dt.date()),
            "setup",
            0,
            use,
            current_dt,
            seg_end,
        )
        remaining -= use
        current_dt = seg_end
        end_dt = seg_end
    return first_start or start_dt, current_dt, end_dt, remaining


def _schedule_production_across_intervals(con, machine_id, block, planning_run_id, start_dt, remaining_qty, planning_cycle_minutes):
    current_dt = start_dt
    first_start = None
    end_dt = None
    remaining = float(remaining_qty or 0)
    cycle_time = max(0.0, float(planning_cycle_minutes or 0))
    safety = 0
    while remaining > 0 and safety < 370:
        safety += 1
        interval_start, interval_end = next_planning_interval_after(con, machine_id, current_dt)
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
        qty = min(remaining, available / cycle_time)
        if qty <= 0:
            current_dt = interval_end
            continue
        use = qty * cycle_time
        seg_end = current_dt + timedelta(minutes=use)
        _insert_planning_segment(
            con,
            planning_run_id,
            int(block["block_id"]),
            int(block["operation_id"]),
            machine_id,
            date_text(current_dt.date()),
            "production",
            qty,
            use,
            current_dt,
            seg_end,
        )
        remaining -= qty
        current_dt = seg_end
        end_dt = seg_end
    return first_start or start_dt, current_dt, end_dt, remaining


def _upsert_planning_block_state(con, block_id, planning_run_id):
    row = one(
        con.execute(
            """
            SELECT MIN(start_datetime) AS expected_start_at,
                   MAX(end_datetime) AS expected_end_at,
                   COALESCE(SUM(planned_qty), 0) AS planned_qty,
                   COALESCE(SUM(planned_minutes), 0) AS planned_minutes
            FROM planning_schedule_segment
            WHERE planning_run_id = ?
              AND block_id = ?
            """,
            (int(planning_run_id), int(block_id)),
        )
    ) or {}
    block = _block_row_for_schedule(con, block_id)
    con.execute(
        """
        INSERT INTO planning_block_state (
          block_id, planning_run_id, expected_start_at, expected_end_at, planned_qty, planned_minutes, machine_id, operation_id, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(block_id) DO UPDATE SET
          planning_run_id = excluded.planning_run_id,
          expected_start_at = excluded.expected_start_at,
          expected_end_at = excluded.expected_end_at,
          planned_qty = excluded.planned_qty,
          planned_minutes = excluded.planned_minutes,
          machine_id = excluded.machine_id,
          operation_id = excluded.operation_id,
          updated_at = CURRENT_TIMESTAMP
        """,
        (
            int(block_id),
            int(planning_run_id),
            compact_text(row.get("expected_start_at")),
            compact_text(row.get("expected_end_at")),
            float(row.get("planned_qty") or 0),
            float(row.get("planned_minutes") or 0),
            int(block["machine_id"]) if block else None,
            int(block["operation_id"]) if block else None,
        ),
    )


def _upsert_planning_operation_state(con, operation_id, planning_run_id):
    row = one(
        con.execute(
            """
            SELECT MIN(s.start_datetime) AS expected_start_at,
                   MAX(s.end_datetime) AS expected_end_at,
                   COALESCE(SUM(s.planned_qty), 0) AS planned_qty,
                   COALESCE(SUM(s.planned_minutes), 0) AS planned_minutes
            FROM planning_schedule_segment s
            WHERE s.planning_run_id = ?
              AND s.operation_id = ?
            """,
            (int(planning_run_id), int(operation_id)),
        )
    ) or {}
    op = one(con.execute("SELECT operation_id, source_ps_id FROM operation WHERE operation_id = ?", (int(operation_id),)))
    con.execute(
        """
        INSERT INTO planning_operation_state (
          operation_id, planning_run_id, ps_id, expected_start_at, expected_end_at, planned_qty, planned_minutes, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(operation_id) DO UPDATE SET
          planning_run_id = excluded.planning_run_id,
          ps_id = excluded.ps_id,
          expected_start_at = excluded.expected_start_at,
          expected_end_at = excluded.expected_end_at,
          planned_qty = excluded.planned_qty,
          planned_minutes = excluded.planned_minutes,
          updated_at = CURRENT_TIMESTAMP
        """,
        (
            int(operation_id),
            int(planning_run_id),
            compact_text(op["source_ps_id"]) if op else "",
            compact_text(row.get("expected_start_at")),
            compact_text(row.get("expected_end_at")),
            float(row.get("planned_qty") or 0),
            float(row.get("planned_minutes") or 0),
        ),
    )


def _resolve_process_sheet_ps_id(con, ps_id):
    ps_id = compact_text(ps_id)
    if not ps_id:
        return ""
    row = one(
        con.execute(
            """
            SELECT ps_id, source_ps_id
            FROM process_sheet
            WHERE ps_id = ?
               OR source_ps_id = ?
            ORDER BY CASE WHEN ps_id = ? THEN 0 ELSE 1 END, ps_id
            LIMIT 1
            """,
            (ps_id, ps_id, ps_id),
        )
    )
    if not row:
        return ps_id
    return compact_text(row["ps_id"] or ps_id)


def _upsert_planning_process_sheet_state(con, ps_id, planning_run_id):
    source_ps_id = compact_text(ps_id)
    resolved_ps_id = _resolve_process_sheet_ps_id(con, source_ps_id)
    if not resolved_ps_id:
        return
    row = one(
        con.execute(
            """
            SELECT MIN(s.start_datetime) AS expected_start_at,
                   MAX(s.end_datetime) AS expected_end_at,
                   COALESCE(SUM(s.planned_qty), 0) AS planned_qty,
                   COALESCE(SUM(s.planned_minutes), 0) AS planned_minutes
            FROM planning_schedule_segment s
            JOIN operation o ON o.operation_id = s.operation_id
            WHERE s.planning_run_id = ?
              AND (COALESCE(o.source_ps_id, '') = ? OR COALESCE(o.source_ps_id, '') = ?)
            """,
            (int(planning_run_id), source_ps_id, resolved_ps_id),
        )
    ) or {}
    con.execute(
        """
        INSERT INTO planning_process_sheet_state (
          ps_id, planning_run_id, expected_start_at, expected_end_at, planned_qty, planned_minutes, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(ps_id) DO UPDATE SET
          planning_run_id = excluded.planning_run_id,
          expected_start_at = excluded.expected_start_at,
          expected_end_at = excluded.expected_end_at,
          planned_qty = excluded.planned_qty,
          planned_minutes = excluded.planned_minutes,
          updated_at = CURRENT_TIMESTAMP
        """,
        (
            resolved_ps_id,
            int(planning_run_id),
            compact_text(row.get("expected_start_at")),
            compact_text(row.get("expected_end_at")),
            float(row.get("planned_qty") or 0),
            float(row.get("planned_minutes") or 0),
        ),
    )


def _schedule_one_block(con, block, planning_run_id, machine_cursor, operation_expected_end_by_seq):
    efficiency = get_planning_efficiency(con)
    planning_start_minute = get_planning_start_minute(con)
    baseline_start = datetime.combine(date.today(), datetime.min.time()) + timedelta(minutes=planning_start_minute)
    machine_id = int(block["machine_id"])
    current_cursor = machine_cursor.get(machine_id, baseline_start)
    planned_start = parse_dt_text(block.get("anchor_datetime")) or parse_dt_text(block.get("planned_start_at"))
    dependency_finish = planning_dependency_finish_for_block(con, block, planning_run_id, operation_expected_end_by_seq)
    allow_pull_forward = int(block.get("allow_pull_forward") if block.get("allow_pull_forward") is not None else 1)

    candidate_start = current_cursor
    if planned_start and allow_pull_forward == 0 and candidate_start < planned_start:
        candidate_start = planned_start

    setup_minutes = max(0.0, float(block.get("setup_minutes") or 0)) if int(block.get("include_setup") or 0) == 1 else 0.0
    cycle_time = max(0.0, float(block.get("cycle_minutes_per_qty") or 0))
    planning_cycle_minutes = (cycle_time / efficiency) if cycle_time > 0 else 0.0
    remaining_qty = max(0.0, float(block.get("scheduled_qty") or 0))
    start_dt = None
    end_dt = None

    if setup_minutes > 0:
        setup_start, after_setup, setup_end, _ = _schedule_setup_across_intervals(
            con,
            machine_id,
            int(block["block_id"]),
            int(block["operation_id"]),
            planning_run_id,
            candidate_start,
            setup_minutes,
        )
        start_dt = start_dt or setup_start
        end_dt = setup_end or end_dt
        production_start_candidate = after_setup
    else:
        production_start_candidate = candidate_start

    if dependency_finish and production_start_candidate < dependency_finish:
        production_start_candidate = dependency_finish
    if planned_start and allow_pull_forward == 0 and production_start_candidate < planned_start:
        production_start_candidate = planned_start

    if remaining_qty > 0 and planning_cycle_minutes > 0:
        prod_start, after_prod, prod_end, _ = _schedule_production_across_intervals(
            con,
            machine_id,
            block,
            planning_run_id,
            production_start_candidate,
            remaining_qty,
            planning_cycle_minutes,
        )
        start_dt = start_dt or prod_start
        end_dt = prod_end or end_dt
        machine_cursor[machine_id] = after_prod if after_prod else production_start_candidate
    else:
        machine_cursor[machine_id] = production_start_candidate

    block_end = end_dt or start_dt or candidate_start
    op_seq_id = int(block.get("source_op_seq_id") or 0)
    operation_expected_end_by_seq[op_seq_id] = max(operation_expected_end_by_seq.get(op_seq_id, block_end), block_end)
    _upsert_planning_block_state(con, int(block["block_id"]), planning_run_id)
    return block_end


def _schedule_blocks(con, blocks, planning_run_id):
    machine_cursor = {}
    operation_expected_end_by_seq = {}
    pending_by_machine = {}
    machine_order = []
    for block in blocks:
        machine_id = int(block["machine_id"])
        pending_by_machine.setdefault(machine_id, []).append(block)
        if machine_id not in machine_order:
            machine_order.append(machine_id)

    safety = 0
    total_blocks = sum(len(items) for items in pending_by_machine.values())
    scheduled_count = 0
    while scheduled_count < total_blocks and safety < total_blocks * 20 + 50:
        safety += 1
        progress = False
        for machine_id in machine_order:
            queue = pending_by_machine.get(machine_id, [])
            if not queue:
                continue
            block = queue[0]
            previous_step = previous_operation_step_for_block(con, block)
            previous_op_seq_id = int(previous_step["op_seq_id"] or 0) if previous_step else 0
            if previous_step is not None:
                if previous_op_seq_id not in operation_expected_end_by_seq:
                    continue
            _schedule_one_block(con, block, planning_run_id, machine_cursor, operation_expected_end_by_seq)
            queue.pop(0)
            scheduled_count += 1
            progress = True
        if not progress:
            fallback_scheduled = False
            for machine_id in machine_order:
                queue = pending_by_machine.get(machine_id, [])
                if not queue:
                    continue
                _schedule_one_block(con, queue.pop(0), planning_run_id, machine_cursor, operation_expected_end_by_seq)
                scheduled_count += 1
                fallback_scheduled = True
                break
            if not fallback_scheduled:
                break
    return operation_expected_end_by_seq


def refresh_planning_block_state(con, block_id, planning_run_id):
    _upsert_planning_block_state(con, block_id, planning_run_id)
    return one(
        con.execute(
            """
            SELECT *
            FROM planning_block_state
            WHERE block_id = ?
            """,
            (int(block_id),),
        )
    )


def refresh_planning_operation_state(con, operation_id, planning_run_id):
    _upsert_planning_operation_state(con, operation_id, planning_run_id)
    return one(
        con.execute(
            """
            SELECT *
            FROM planning_operation_state
            WHERE operation_id = ?
            """,
            (int(operation_id),),
        )
    )


def refresh_planning_process_sheet_state(con, ps_id, planning_run_id):
    _upsert_planning_process_sheet_state(con, ps_id, planning_run_id)
    return one(
        con.execute(
            """
            SELECT *
            FROM planning_process_sheet_state
            WHERE ps_id = ?
            """,
            (compact_text(ps_id),),
        )
    )


def refresh_planning_states(con, planning_run_id):
    block_ids = [int(row["block_id"]) for row in rows(con.execute("SELECT block_id FROM run_block WHERE COALESCE(active, 1) = 1 ORDER BY block_id"))]
    operation_ids = [int(row["operation_id"]) for row in rows(con.execute("SELECT operation_id FROM operation WHERE COALESCE(status, 'ACTIVE') = 'ACTIVE' ORDER BY operation_id"))]
    ps_ids = [compact_text(row["source_ps_id"]) for row in rows(con.execute("SELECT DISTINCT source_ps_id FROM operation WHERE COALESCE(source_ps_id, '') <> '' ORDER BY source_ps_id"))]

    for block_id in block_ids:
        _upsert_planning_block_state(con, block_id, planning_run_id)
    for operation_id in operation_ids:
        _upsert_planning_operation_state(con, operation_id, planning_run_id)
    for ps_id in ps_ids:
        _upsert_planning_process_sheet_state(con, ps_id, planning_run_id)


def recalculate_planning_machine(con, machine_id, planning_run_id):
    if planning_run_id is None:
        planning_run_id = create_planning_schedule_run(con)
    blocks = _active_blocks_for_machine(con, machine_id)
    _schedule_blocks(con, blocks, planning_run_id)
    refresh_planning_states(con, planning_run_id)
    return planning_run_id


def recalculate_planning_all(con, reason="PLANNER_RECALCULATE"):
    planning_run_id = create_planning_schedule_run(con, reason=reason)
    blocks = _active_blocks_all(con)
    _schedule_blocks(con, blocks, planning_run_id)
    refresh_planning_states(con, planning_run_id)
    return planning_run_id
