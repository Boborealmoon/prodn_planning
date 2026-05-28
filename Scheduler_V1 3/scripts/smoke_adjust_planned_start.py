"""
PRD Section 8.3 — Adjust Planned Start Smoke Test

Verifies:
- Block A planned end May 20, block A current/actual end May 23
- Block B planned start May 20
- Button condition evaluates true (prev current end > current planned start by ≥1 day)
- Action: set block B anchor_datetime = prev current/actual end
- Action: set allow_pull_forward = 0
- After recalculation: block B planned_start = max(prev planned_end, anchor)
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app import create_app
from scheduler_app.db import db, ensure_db, one
from scheduler_app.planning_scheduler import recalculate_planning_all


def fail(msg):
    print(f"FAIL: {msg}")
    return 1


def pass_msg(msg):
    print(f"PASS: {msg}")


def _parse_dt(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"blank datetime: {value!r}")
    return datetime.fromisoformat(text.replace(" ", "T"))


def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _date_later_by_day(later_str: str, earlier_str: str) -> bool:
    later = _parse_dt(later_str)
    earlier = _parse_dt(earlier_str)
    ld = datetime(later.year, later.month, later.day)
    ed = datetime(earlier.year, earlier.month, earlier.day)
    return (ld - ed) >= timedelta(days=1)


def _pick_machine() -> tuple[int, str]:
    with db() as con:
        row = one(con.execute(
            "SELECT machine_id, machine_code FROM machines WHERE COALESCE(active,1)=1 ORDER BY machine_id LIMIT 1"
        ))
    if not row:
        raise RuntimeError("no active machine")
    return int(row["machine_id"]), str(row["machine_code"] or "")


def _create_block(client, machine_id: int, label: str, queue_pos: float) -> int:
    res = client.post("/api/trial/operations", json={
        "job_no": f"SMOKE-ADJSTART-{label}-{uuid4().hex[:6]}",
        "operation_name": f"AdjStart Smoke {label}",
        "total_qty": 10,
        "scheduled_qty": 10,
        "setup_minutes": 0,
        "cycle_minutes_per_qty": 30,
        "machine_id": machine_id,
        "queue_position": queue_pos,
        "include_setup": 0,
        "active": 1,
    })
    if res.status_code != 200:
        raise RuntimeError(f"create {label} failed: {res.status_code} {res.get_data(as_text=True)[:200]}")
    block_id = int(((res.get_json() or {}).get("block") or {}).get("block_id") or 0)
    if not block_id:
        raise RuntimeError(f"create {label}: no block_id")
    return block_id


def _fetch(con, block_id: int) -> dict:
    row = one(con.execute(
        "SELECT block_id, planned_start_at, planned_end_at, anchor_datetime, allow_pull_forward, "
        "calculated_start_datetime, calculated_end_datetime FROM run_block WHERE block_id = ?",
        (int(block_id),),
    ))
    return dict(row) if row else {}


def main():
    try:
        ensure_db()
    except Exception as exc:
        return fail(f"ensure_db failed: {exc}")

    machine_id, machine_code = _pick_machine()
    app = create_app()
    client = app.test_client()
    created: list[int] = []

    try:
        created.append(_create_block(client, machine_id, "A", 10))
        created.append(_create_block(client, machine_id, "B", 20))
        ba_id, bb_id = created

        with db() as con:
            recalculate_planning_all(con, reason="SMOKE_ADJUST_INIT")
            ba = _fetch(con, ba_id)
            bb = _fetch(con, bb_id)

        if not str(ba.get("planned_end_at") or "").strip():
            return fail("block A planned_end_at is blank after initial recalc")
        if not str(bb.get("planned_start_at") or "").strip():
            return fail("block B planned_start_at is blank after initial recalc")

        ba_planned_end = str(ba["planned_end_at"]).strip()
        bb_planned_start = str(bb["planned_start_at"]).strip()

        # Verify initial chain: B starts where A ends
        if _parse_dt(bb_planned_start) != _parse_dt(ba_planned_end):
            return fail(f"initial chain broken: B start ({bb_planned_start}) != A end ({ba_planned_end})")
        pass_msg(f"initial chain: B planned_start == A planned_end ({ba_planned_end})")

        # Simulate block A finishing 3 days later than planned (the drift scenario)
        late_actual_end = _fmt_dt(_parse_dt(ba_planned_end) + timedelta(days=3))
        with db() as con:
            con.execute(
                "UPDATE run_block SET calculated_end_datetime = ?, updated_at = CURRENT_TIMESTAMP WHERE block_id = ?",
                (late_actual_end, ba_id),
            )
            ba_after = _fetch(con, ba_id)

        prev_current_end = str(ba_after.get("calculated_end_datetime") or "").strip()
        if not prev_current_end:
            return fail("failed to set block A calculated_end_datetime for drift simulation")

        # PRD button condition: prev current end later than B planned start by at least 1 day
        if not _date_later_by_day(prev_current_end, bb_planned_start):
            return fail(
                f"button condition not triggered: prev current end ({prev_current_end}) "
                f"is not later than B planned start ({bb_planned_start}) by ≥1 day"
            )
        pass_msg(f"button condition met: prev current end ({prev_current_end}) > B planned start ({bb_planned_start})")

        # PRD click action: set anchor_datetime = prev current end, allow_pull_forward = 0
        res = client.put(f"/api/trial/blocks/{bb_id}", json={
            "anchor_datetime": prev_current_end,
            "planned_start_at": prev_current_end,
            "allow_pull_forward": 0,
        })
        if res.status_code != 200:
            return fail(f"adjust planned start PUT failed: {res.status_code} {res.get_data(as_text=True)[:200]}")

        block_after = (res.get_json() or {}).get("block") or {}
        if str(block_after.get("anchor_datetime") or "").strip() != prev_current_end:
            return fail(f"anchor_datetime not set: got {block_after.get('anchor_datetime')!r}, expected {prev_current_end!r}")
        if int(block_after.get("allow_pull_forward") if block_after.get("allow_pull_forward") is not None else 1) != 0:
            return fail("allow_pull_forward was not set to 0")
        pass_msg(f"anchor set to {prev_current_end}, allow_pull_forward=0")

        # After recalculation: B planned_start = max(A planned_end, anchor)
        with db() as con:
            recalculate_planning_all(con, reason="SMOKE_ADJUST_AFTER")
            ba_recalc = _fetch(con, ba_id)
            bb_recalc = _fetch(con, bb_id)

        ba_end_recalc = str(ba_recalc.get("planned_end_at") or "").strip()
        bb_start_recalc = str(bb_recalc.get("planned_start_at") or "").strip()
        if not ba_end_recalc or not bb_start_recalc:
            return fail("missing planned dates after recalculation")

        expected_bb_start = max(_parse_dt(ba_end_recalc), _parse_dt(prev_current_end))
        if _parse_dt(bb_start_recalc) != expected_bb_start:
            return fail(
                f"B planned_start after recalc ({bb_start_recalc}) != "
                f"max(A planned_end={ba_end_recalc}, anchor={prev_current_end}) = {_fmt_dt(expected_bb_start)}"
            )
        pass_msg(f"B planned_start after recalc = max(A planned_end, anchor) = {bb_start_recalc}")

        # Block B must now be anchored (orange in UI)
        bb_anchor = str(bb_recalc.get("anchor_datetime") or "").strip()
        bb_apf = int(bb_recalc.get("allow_pull_forward") if bb_recalc.get("allow_pull_forward") is not None else 1)
        if not bb_anchor and bb_apf != 0:
            return fail("block B should be anchored (anchor_datetime set or allow_pull_forward=0)")
        pass_msg("block B is anchored after adjust planned start")

        print("PASS: smoke_adjust_planned_start completed")
        return 0

    except Exception as exc:
        return fail(f"smoke failed: {exc}")
    finally:
        for bid in reversed(created):
            try:
                client.delete(f"/api/trial/blocks/{bid}")
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
