from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app import create_app
from scheduler_app.blocks import recalculate_machine
from scheduler_app.db import db, ensure_db, one, rows


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


def _parse_dt(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("missing datetime")
    return datetime.fromisoformat(text.replace(" ", "T"))


def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _pick_machine_id():
    with db() as con:
        row = one(
            con.execute(
                """
                SELECT m.machine_id, m.machine_code, COUNT(b.block_id) AS active_blocks
                FROM machines m
                LEFT JOIN run_block b
                  ON b.machine_id = m.machine_id
                 AND COALESCE(b.active, 1) = 1
                WHERE COALESCE(m.active, 1) = 1
                GROUP BY m.machine_id
                ORDER BY active_blocks ASC, m.machine_id ASC
                LIMIT 1
                """
            )
        )
    if not row:
        return 0, ""
    return int(row["machine_id"]), str(row["machine_code"] or "")


def _create_block(client, machine_id: int, label: str, queue_position: float):
    res = client.post(
        "/api/trial/operations",
        json={
            "job_no": f"SMOKE-PLANNER-{label}-{uuid4().hex[:8]}",
            "operation_name": f"Planner Smoke {label}",
            "total_qty": 10,
            "scheduled_qty": 10,
            "setup_minutes": 0,
            "cycle_minutes_per_qty": 10,
            "machine_id": machine_id,
            "queue_position": queue_position,
            "include_setup": 1,
            "active": 1,
            "planning_status": "PLANNED",
            "execution_status": "NOT_STARTED",
        },
    )
    if res.status_code != 200:
        raise RuntimeError(f"POST /api/trial/operations failed: {res.status_code} {res.get_data(as_text=True)}")
    data = res.get_json() or {}
    block = data.get("block") or {}
    block_id = int(block.get("block_id") or 0)
    if not block_id:
        raise RuntimeError("operation creation returned no block_id")
    return block_id


def _fetch_block(con, block_id: int):
    row = one(
        con.execute(
            """
            SELECT block_id, machine_id, queue_position,
                   planned_start_at, planned_end_at,
                   calculated_start_datetime, calculated_end_datetime,
                   anchor_datetime, allow_pull_forward
            FROM run_block
            WHERE block_id = ?
            """,
            (int(block_id),),
        )
    )
    return dict(row) if row else None


def _date_later_by_day(later_value: str, earlier_value: str) -> bool:
    later = _parse_dt(later_value)
    earlier = _parse_dt(earlier_value)
    later_day = datetime(later.year, later.month, later.day)
    earlier_day = datetime(earlier.year, earlier.month, earlier.day)
    return (later_day - earlier_day) >= timedelta(days=1)


def _assert_chain(blocks, label):
    b1, b2, b3 = blocks
    for idx, block in enumerate(blocks, start=1):
        if not str(block.get("planned_start_at") or "").strip():
            raise AssertionError(f"{label}: block {idx} planned_start_at is blank")
        if not str(block.get("planned_end_at") or "").strip():
            raise AssertionError(f"{label}: block {idx} planned_end_at is blank")
    if _parse_dt(b2["planned_start_at"]) != _parse_dt(b1["planned_end_at"]):
        raise AssertionError(f"{label}: block 2 start is not block 1 end")
    if _parse_dt(b3["planned_start_at"]) != _parse_dt(b2["planned_end_at"]):
        raise AssertionError(f"{label}: block 3 start is not block 2 end")


def main():
    try:
        ensure_db()
    except Exception as exc:
        return fail(f"ensure_db failed: {exc}")

    machine_id, machine_code = _pick_machine_id()
    if not machine_id:
        return fail("no active machine found")

    app = create_app()
    client = app.test_client()
    created_ids: list[int] = []

    try:
        created_ids.append(_create_block(client, machine_id, "A", 10))
        created_ids.append(_create_block(client, machine_id, "B", 20))
        created_ids.append(_create_block(client, machine_id, "C", 30))

        with db() as con:
            recalculate_machine(con, machine_id, reason="SMOKE_PLANNER_PLANNED_START_AND_ADJUST")
            rows_now = [_fetch_block(con, bid) for bid in created_ids]
        if any(row is None for row in rows_now):
            return fail("one or more blocks missing after initial recalc")
        _assert_chain(rows_now, f"machine {machine_code or machine_id} initial chain")
        pass_msg("unanchored lane is chained by previous planned end")

        b1_end = _parse_dt(rows_now[0]["planned_end_at"])
        late_anchor = _fmt_dt(b1_end + timedelta(days=7))
        res = client.put(
            f"/api/trial/blocks/{created_ids[1]}",
            json={
                "anchor_datetime": late_anchor,
                "planned_start_at": late_anchor,
                "allow_pull_forward": 0,
            },
        )
        if res.status_code != 200:
            return fail(f"late anchor update failed: {res.status_code} {res.get_data(as_text=True)}")
        block_after = (res.get_json() or {}).get("block") or {}
        if str(block_after.get("anchor_datetime") or "").strip() != late_anchor:
            return fail("late anchor was not saved")
        if int(block_after.get("allow_pull_forward") if block_after.get("allow_pull_forward") is not None else 1) != 0:
            return fail("allow_pull_forward was not set to 0 on late anchor")

        with db() as con:
            rows_now = [_fetch_block(con, bid) for bid in created_ids]
        if any(row is None for row in rows_now):
            return fail("one or more blocks missing after late anchor")
        if _parse_dt(rows_now[1]["planned_start_at"]) != _parse_dt(late_anchor):
            return fail("block 2 did not start at the late anchor")
        if _parse_dt(rows_now[2]["planned_start_at"]) != _parse_dt(rows_now[1]["planned_end_at"]):
            return fail("block 3 did not follow block 2 after late anchor")
        pass_msg("later anchor pushes block 2 forward and preserves downstream chaining")

        early_anchor = _fmt_dt(b1_end - timedelta(days=1))
        res = client.put(
            f"/api/trial/blocks/{created_ids[1]}",
            json={
                "anchor_datetime": early_anchor,
                "planned_start_at": early_anchor,
                "allow_pull_forward": 0,
            },
        )
        if res.status_code != 200:
            return fail(f"early anchor update failed: {res.status_code} {res.get_data(as_text=True)}")
        block_after = (res.get_json() or {}).get("block") or {}
        if str(block_after.get("anchor_datetime") or "").strip() != early_anchor:
            return fail("early anchor was not saved")
        if int(block_after.get("allow_pull_forward") if block_after.get("allow_pull_forward") is not None else 1) != 0:
            return fail("allow_pull_forward was not set to 0 on early anchor")

        with db() as con:
            rows_now = [_fetch_block(con, bid) for bid in created_ids]
        if any(row is None for row in rows_now):
            return fail("one or more blocks missing after early anchor")
        if _parse_dt(rows_now[1]["planned_start_at"]) != _parse_dt(rows_now[0]["planned_end_at"]):
            return fail("early anchor moved block 2 backwards instead of clamping to block 1 end")
        if _parse_dt(rows_now[2]["planned_start_at"]) != _parse_dt(rows_now[1]["planned_end_at"]):
            return fail("block 3 did not follow block 2 after early anchor")
        pass_msg("earlier anchor is clamped to the previous planned end")

        # Simulate a previous job that completed late so the manual adjust action has a real trigger.
        with db() as con:
            current = _fetch_block(con, created_ids[0])
            current_b2 = _fetch_block(con, created_ids[1])
            if not current or not current_b2:
                return fail("unable to fetch blocks for drift simulation")
            simulated_prev_end = _fmt_dt(_parse_dt(current_b2["planned_start_at"]) + timedelta(days=2))
            con.execute(
                """
                UPDATE run_block
                SET calculated_start_datetime = ?,
                    calculated_end_datetime = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE block_id = ?
                """,
                (
                    _fmt_dt(_parse_dt(simulated_prev_end) - timedelta(hours=2)),
                    simulated_prev_end,
                    created_ids[0],
                ),
            )
            current = _fetch_block(con, created_ids[0])
            current_b2 = _fetch_block(con, created_ids[1])
        if not current or not current_b2:
            return fail("drift simulation failed")
        if not _date_later_by_day(current["calculated_end_datetime"], current_b2["planned_start_at"]):
            return fail("drift simulation did not create a late previous end relative to block 2 planned start")

        res = client.put(
            f"/api/trial/blocks/{created_ids[1]}",
            json={
                "anchor_datetime": current["calculated_end_datetime"],
                "planned_start_at": current["calculated_end_datetime"],
                "allow_pull_forward": 0,
            },
        )
        if res.status_code != 200:
            return fail(f"adjust planned start endpoint failed: {res.status_code} {res.get_data(as_text=True)}")
        block_after = (res.get_json() or {}).get("block") or {}
        if str(block_after.get("anchor_datetime") or "").strip() != str(current["calculated_end_datetime"]).strip():
            return fail("adjust planned start did not set the anchor_datetime")
        if int(block_after.get("allow_pull_forward") if block_after.get("allow_pull_forward") is not None else 1) != 0:
            return fail("adjust planned start did not set allow_pull_forward to 0")

        with db() as con:
            recalculate_machine(con, machine_id, reason="SMOKE_PLANNER_PLANNED_START_AND_ADJUST")
            rows_now = [_fetch_block(con, bid) for bid in created_ids]
        if any(row is None for row in rows_now):
            return fail("one or more blocks missing after adjust recalculation")
        if _parse_dt(rows_now[1]["planned_start_at"]) != max(_parse_dt(rows_now[0]["planned_end_at"]), _parse_dt(current["calculated_end_datetime"])):
            return fail("adjust planned start did not respect max(previous planned end, anchor)")
        if _parse_dt(rows_now[2]["planned_start_at"]) != _parse_dt(rows_now[1]["planned_end_at"]):
            return fail("block 3 did not follow block 2 after adjust planned start")
        pass_msg("adjust planned start anchors the block to the previous job's late end")

        print("PASS: smoke_planner_planned_start_and_adjust completed successfully")
        return 0
    finally:
        for block_id in created_ids[::-1]:
            try:
                client.delete(f"/api/trial/blocks/{block_id}")
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
