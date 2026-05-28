"""
PRD Section 8.1 — Planner Planned Start Order Smoke Test

Verifies:
- One machine, three unanchored blocks
- After recalculation, block 2 planned_start == block 1 planned_end
- After recalculation, block 3 planned_start == block 2 planned_end
- Anchor block 2 later → block 2 planned_start == anchor (max rule)
- Anchor block 2 earlier → block 2 planned_start == block 1 planned_end (clamped, not pulled back)
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
        raise ValueError(f"missing datetime: {value!r}")
    return datetime.fromisoformat(text.replace(" ", "T"))


def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _pick_machine():
    with db() as con:
        row = one(con.execute(
            "SELECT machine_id, machine_code FROM machines WHERE COALESCE(active,1)=1 ORDER BY machine_id LIMIT 1"
        ))
    if not row:
        raise RuntimeError("no active machine found")
    return int(row["machine_id"]), str(row["machine_code"] or "")


def _create_block(client, machine_id: int, label: str, queue_pos: float) -> int:
    res = client.post("/api/trial/operations", json={
        "job_no": f"SMOKE-ORDER-{label}-{uuid4().hex[:6]}",
        "operation_name": f"Order Smoke {label}",
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
        raise RuntimeError(f"create block {label} failed: {res.status_code} {res.get_data(as_text=True)[:200]}")
    data = res.get_json() or {}
    block_id = int((data.get("block") or {}).get("block_id") or 0)
    if not block_id:
        raise RuntimeError(f"create block {label}: no block_id in response")
    return block_id


def _fetch(con, block_id: int) -> dict:
    row = one(con.execute(
        "SELECT block_id, planned_start_at, planned_end_at, anchor_datetime, allow_pull_forward FROM run_block WHERE block_id = ?",
        (int(block_id),),
    ))
    return dict(row) if row else {}


def _recalc(con) -> int:
    return recalculate_planning_all(con, reason="SMOKE_PLANNER_ORDER")


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
        created.append(_create_block(client, machine_id, "C", 30))
        b1_id, b2_id, b3_id = created

        with db() as con:
            _recalc(con)
            b1 = _fetch(con, b1_id)
            b2 = _fetch(con, b2_id)
            b3 = _fetch(con, b3_id)

        for idx, blk in enumerate([b1, b2, b3], 1):
            if not str(blk.get("planned_start_at") or "").strip():
                return fail(f"block {idx} planned_start_at is blank after initial recalc")
            if not str(blk.get("planned_end_at") or "").strip():
                return fail(f"block {idx} planned_end_at is blank after initial recalc")

        b1_end = _parse_dt(b1["planned_end_at"])
        b2_start = _parse_dt(b2["planned_start_at"])
        b2_end = _parse_dt(b2["planned_end_at"])
        b3_start = _parse_dt(b3["planned_start_at"])

        if b2_start != b1_end:
            return fail(f"block 2 planned_start ({b2_start}) != block 1 planned_end ({b1_end})")
        pass_msg(f"block 2 planned_start == block 1 planned_end ({b1_end})")

        if b3_start != b2_end:
            return fail(f"block 3 planned_start ({b3_start}) != block 2 planned_end ({b2_end})")
        pass_msg(f"block 3 planned_start == block 2 planned_end ({b2_end})")

        # Anchor block 2 LATER than block 1's end — block 2 should start at anchor
        late_anchor = _fmt_dt(b1_end + timedelta(days=3))
        res = client.put(f"/api/trial/blocks/{b2_id}", json={
            "anchor_datetime": late_anchor,
            "planned_start_at": late_anchor,
            "allow_pull_forward": 0,
        })
        if res.status_code != 200:
            return fail(f"late anchor PUT failed: {res.status_code}")
        with db() as con:
            b2_late = _fetch(con, b2_id)
        if _parse_dt(b2_late["planned_start_at"]) != _parse_dt(late_anchor):
            return fail(f"late anchor: block 2 planned_start ({b2_late['planned_start_at']}) != anchor ({late_anchor})")
        pass_msg(f"late anchor: block 2 starts at anchor {late_anchor}")

        # Anchor block 2 EARLIER than block 1's end — block 2 must be clamped to block 1's end
        early_anchor = _fmt_dt(b1_end - timedelta(days=1))
        res = client.put(f"/api/trial/blocks/{b2_id}", json={
            "anchor_datetime": early_anchor,
            "planned_start_at": early_anchor,
            "allow_pull_forward": 0,
        })
        if res.status_code != 200:
            return fail(f"early anchor PUT failed: {res.status_code}")
        with db() as con:
            _recalc(con)
            b1_now = _fetch(con, b1_id)
            b2_early = _fetch(con, b2_id)
        b1_end_now = _parse_dt(b1_now["planned_end_at"])
        b2_early_start = _parse_dt(b2_early["planned_start_at"])
        if b2_early_start != b1_end_now:
            return fail(
                f"early anchor: block 2 planned_start ({b2_early_start}) != block 1 planned_end ({b1_end_now}); "
                "anchor earlier than previous end must not pull block backward"
            )
        pass_msg(f"early anchor clamped to block 1 planned_end ({b1_end_now})")

        print("PASS: smoke_planner_planned_start_order completed")
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
