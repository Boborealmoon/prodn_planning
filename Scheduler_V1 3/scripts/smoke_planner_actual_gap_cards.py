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
                SELECT m.machine_id
                FROM machines m
                LEFT JOIN run_block b
                  ON b.machine_id = m.machine_id
                 AND COALESCE(b.active, 1) = 1
                WHERE COALESCE(m.active, 1) = 1
                GROUP BY m.machine_id
                ORDER BY COUNT(b.block_id) ASC,
                         CASE WHEN COALESCE(m.shift_profile, '') = '24HR' THEN 1 ELSE 0 END,
                         m.machine_id
                LIMIT 1
                """
            )
        )
    return int(row["machine_id"]) if row else 0


def _create_block(client, machine_id: int, label: str, queue_position: float) -> int:
    resp = client.post(
        "/api/trial/operations",
        json={
            "job_no": f"SMOKE-GAP-{label}-{uuid4().hex[:6]}",
            "operation_name": f"Gap Smoke {label}",
            "total_qty": 10,
            "scheduled_qty": 10,
            "setup_minutes": 0,
            "cycle_minutes_per_qty": 30,
            "machine_id": machine_id,
            "queue_position": queue_position,
            "planned_start_at": "2099-05-20 08:30:00",
            "planned_end_at": "2099-05-20 13:30:00",
            "allow_pull_forward": 1,
            "active": 1,
            "is_fresh_monday_item": 0,
            "planning_status": "PLANNED",
            "execution_status": "NOT_STARTED",
            "include_setup": 1,
        },
    )
    if resp.status_code != 200:
        raise RuntimeError(f"create block failed: {resp.status_code} {resp.get_data(as_text=True)}")
    data = resp.get_json() or {}
    block = data.get("block") or {}
    block_id = int(block.get("block_id") or 0)
    if not block_id:
        raise RuntimeError("block creation returned no block_id")
    return block_id


def _recalc(client):
    res = client.post("/api/trial/planner/recalculate", json={"reason": "SMOKE_GAP"})
    if res.status_code != 200:
        raise RuntimeError(f"planner recalculate failed: {res.status_code} {res.get_data(as_text=True)}")


def _schedule(client):
    res = client.get("/api/trial/planner/schedule")
    if res.status_code != 200:
        raise RuntimeError(f"planner schedule failed: {res.status_code} {res.get_data(as_text=True)}")
    return res.get_json() or {}


def _insert_actual(con, block_id: int, machine_id: int, actual_dt: datetime, output_qty: float):
    cur = con.execute(
        """
        INSERT INTO production_actual (
          segment_id, block_id, machine_id, report_date, remarks, reported_at,
          output_qty, reject_qty, target_qty_at_report, status, entry_type,
          correction_of_actual_id, good_qty_at_report, created_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 'REPORT', ?, ?, ?)
        """,
        (
            None,
            int(block_id),
            int(machine_id),
            _fmt_dt(actual_dt),
            "SMOKE GAP",
            _fmt_dt(actual_dt),
            float(output_qty or 0),
            0.0,
            10.0,
            None,
            max(0.0, float(output_qty or 0)),
            "smoke",
        ),
    )
    return int(cur.lastrowid)


def _fetch_block(schedule_json, block_id: int):
    for block in schedule_json.get("blocks") or []:
        if int(block.get("block_id") or 0) == int(block_id):
            return block
    return {}


def main():
    try:
        ensure_db()
    except Exception as exc:
        return fail(f"ensure_db failed: {exc}")

    app = create_app()
    client = app.test_client()
    machine_id = _pick_machine_id()
    if not machine_id:
        return fail("no active machine found")

    created_ids: list[int] = []
    try:
        b1 = _create_block(client, machine_id, "A", 10)
        b2 = _create_block(client, machine_id, "B", 20)
        created_ids = [b1, b2]

        _recalc(client)
        with db() as con:
            row = one(
                con.execute(
                    "SELECT planned_start_at, planned_end_at FROM run_block WHERE block_id = ?",
                    (b1,),
                )
            )
            if not row or not str(row["planned_start_at"] or "").strip() or not str(row["planned_end_at"] or "").strip():
                return fail("initial recalc did not populate planned dates")
            b1_planned_start = _parse_dt(str(row["planned_start_at"]))
            b1_planned_end = _parse_dt(str(row["planned_end_at"]))
            row2 = one(
                con.execute(
                    "SELECT planned_start_at, planned_end_at FROM run_block WHERE block_id = ?",
                    (b2,),
                )
            )
            if not row2:
                return fail("missing block 2 after recalc")
            b2_planned_start = _parse_dt(str(row2["planned_start_at"]))

        # Move block 2 later in the same day so the gap spans actual working time.
        same_day_anchor = _fmt_dt(b1_planned_start.replace(hour=14, minute=30, second=0))
        res = client.put(
            f"/api/trial/blocks/{b2}",
            json={
                "anchor_datetime": same_day_anchor,
                "planned_start_at": same_day_anchor,
                "allow_pull_forward": 0,
            },
        )
        if res.status_code != 200:
            return fail(f"failed to anchor block 2 for same-day gap test: {res.status_code}")
        _recalc(client)
        payload = _schedule(client)
        block2 = _fetch_block(payload, b2)
        b2_planned_start = _parse_dt(str(block2.get("planned_start_at") or same_day_anchor))

        # Scenario 1: actual end exists and gap should be between actual end and planned start.
        actual_end_1 = b1_planned_start + timedelta(hours=1)
        with db() as con:
            con.execute("DELETE FROM production_actual WHERE block_id = ?", (b1,))
            _insert_actual(con, b1, machine_id, actual_end_1, 10.0)
        payload = _schedule(client)
        block1 = _fetch_block(payload, b1)
        block2 = _fetch_block(payload, b2)
        gap = block1.get("gap_after") or {}
        if not gap:
            return fail("gap card was not created when previous actual_end_at exists")
        if str(gap.get("start_at") or "").strip() != _fmt_dt(actual_end_1):
            return fail("gap start did not use previous actual_end_at")
        if str(gap.get("end_at") or "").strip() != str(block2.get("planned_start_at") or "").strip():
            return fail("gap end did not use next planned_start_at")
        if str(gap.get("start_at") or "").strip() == str(block1.get("planned_end_at") or "").strip():
            return fail("gap start incorrectly used previous planned_end_at")
        pass_msg("gap uses previous actual_end_at and next planned_start_at")

        # Scenario 2: no actual end -> no gap.
        with db() as con:
            con.execute("DELETE FROM production_actual WHERE block_id = ?", (b1,))
        payload = _schedule(client)
        block1 = _fetch_block(payload, b1)
        if block1.get("gap_after"):
            return fail("gap should not exist when previous actual_end_at is missing")
        pass_msg("no actual_end_at means no gap")

        # Scenario 3: previous actual end later than next planned start -> no gap.
        late_actual_end = _parse_dt(str(block2.get("planned_start_at") or "")) + timedelta(hours=1)
        with db() as con:
            con.execute("DELETE FROM production_actual WHERE block_id = ?", (b1,))
            _insert_actual(con, b1, machine_id, late_actual_end, 10.0)
        payload = _schedule(client)
        block1 = _fetch_block(payload, b1)
        if block1.get("gap_after"):
            return fail("gap should not exist when previous actual_end_at is later than next planned_start_at")
        pass_msg("later actual end does not create a gap")

        # Scenario 4: same datetime -> no gap.
        with db() as con:
            con.execute("DELETE FROM production_actual WHERE block_id = ?", (b1,))
            _insert_actual(con, b1, machine_id, _parse_dt(str(block2.get("planned_start_at") or "")), 10.0)
        payload = _schedule(client)
        block1 = _fetch_block(payload, b1)
        if block1.get("gap_after"):
            return fail("gap should not exist when previous actual_end_at equals next planned_start_at")
        pass_msg("same-time end/start does not create a gap")

        # Scenario 5: next-day gap uses working calendar minutes, not raw wall-clock.
        next_day_start = _parse_dt(str(block2.get("planned_start_at") or "")) + timedelta(days=1)
        next_day_start = next_day_start.replace(hour=8, minute=30, second=0)
        res = client.put(
            f"/api/trial/blocks/{b2}",
            json={
                "anchor_datetime": _fmt_dt(next_day_start),
                "planned_start_at": _fmt_dt(next_day_start),
                "allow_pull_forward": 0,
            },
        )
        if res.status_code != 200:
            return fail(f"failed to anchor block 2 for next-day gap test: {res.status_code}")
        _recalc(client)
        with db() as con:
            con.execute("DELETE FROM production_actual WHERE block_id = ?", (b1,))
            _insert_actual(con, b1, machine_id, _parse_dt(str(block1.get("planned_start_at") or "")) + timedelta(hours=1), 10.0)
        payload = _schedule(client)
        block1 = _fetch_block(payload, b1)
        gap = block1.get("gap_after") or {}
        if not gap:
            return fail("next-day gap was not created")
        available_minutes = float(gap.get("available_minutes") or 0)
        if available_minutes <= 0:
            return fail("next-day gap available_minutes should be positive")
        wall_clock_minutes = (_parse_dt(str(gap.get("end_at"))) - _parse_dt(str(gap.get("start_at")))).total_seconds() / 60.0
        if available_minutes >= wall_clock_minutes:
            return fail("gap available_minutes should use working minutes, not full wall-clock minutes")
        pass_msg("next-day gap uses working minutes rather than raw wall-clock duration")

        # Scenario 6: lane order save selector only targets real planner blocks.
        with open(ROOT / "scheduler_app" / "templates" / "planner_baseline.html", "r", encoding="utf-8") as fh:
            html = fh.read()
        start = html.find("async function plannerSaveMachineLaneOrder")
        end = html.find("async function loadPlannerBaseline")
        snippet = html[start:end] if start >= 0 and end > start else html
        selector_lines = [line.strip() for line in snippet.splitlines() if "querySelectorAll" in line]
        if not any("planner-block" in line for line in selector_lines):
            return fail("planner lane order save selector no longer targets real planner blocks")
        if any("planner-gap-card" in line for line in selector_lines):
            return fail("planner gap cards appear to be included in lane order save logic")
        pass_msg("lane order save still targets real blocks only")

        print("PASS: smoke_planner_actual_gap_cards completed successfully")
        return 0
    except Exception as exc:
        return fail(str(exc))
    finally:
        for block_id in reversed(created_ids):
            try:
                client.delete(f"/api/trial/blocks/{block_id}")
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
