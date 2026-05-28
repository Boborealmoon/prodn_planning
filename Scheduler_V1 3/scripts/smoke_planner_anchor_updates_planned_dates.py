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
                "SELECT machine_id FROM machines WHERE COALESCE(active, 1) = 1 ORDER BY machine_id LIMIT 1"
            )
        )
    return int(row["machine_id"]) if row else 0


def _get_schedule(client):
    res = client.get("/api/trial/planner/schedule")
    if res.status_code != 200:
        raise RuntimeError(f"GET /api/trial/planner/schedule returned {res.status_code}: {res.get_data(as_text=True)}")
    return res.get_json() or {}


def _create_temp_block(client, machine_id: int, suffix: str, queue_position: float):
    seed_start = "2099-05-26 08:30:00"
    seed_end = "2099-05-26 12:30:00"
    resp = client.post(
        "/api/trial/operations",
        json={
            "job_no": f"SMOKE-ANCHOR-{suffix}-{uuid4().hex[:6]}",
            "operation_name": f"Anchor Smoke {suffix}",
            "total_qty": 10,
            "scheduled_qty": 10,
            "setup_minutes": 0,
            "cycle_minutes_per_qty": 2,
            "machine_id": machine_id,
            "queue_position": queue_position,
            "planned_start_at": seed_start,
            "planned_end_at": seed_end,
            "allow_pull_forward": 1,
            "active": 1,
            "is_fresh_monday_item": 0,
            "planning_status": "PLANNED",
            "execution_status": "NOT_STARTED",
            "include_setup": 1,
        },
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to create temp block: {resp.status_code} {resp.get_data(as_text=True)}")
    data = resp.get_json() or {}
    block = data.get("block") or {}
    if not int(block.get("block_id") or 0):
        raise RuntimeError("Temp block creation returned no block")
    return block


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

    created_ids = []
    try:
        first = _create_temp_block(client, machine_id, "A", 10)
        second = _create_temp_block(client, machine_id, "B", 20)
        created_ids = [int(first.get("block_id") or 0), int(second.get("block_id") or 0)]

        block_id = int(second.get("block_id") or 0)
        original_start = str(second.get("planned_start_at") or "").strip()
        original_end = str(second.get("planned_end_at") or "").strip()
        original_anchor = str(second.get("anchor_datetime") or "").strip()
        original_allow_pull_forward = int(second.get("allow_pull_forward") if second.get("allow_pull_forward") is not None else 1)

        if not original_start or not original_end:
          return fail("temp anchor block did not receive planned dates")

        anchor_dt = _fmt_dt(_parse_dt(original_start) + timedelta(days=7))

        update_resp = client.put(
            f"/api/trial/blocks/{block_id}",
            json={
                "anchor_datetime": anchor_dt,
                "planned_start_at": anchor_dt,
                "allow_pull_forward": 0,
            },
        )
        if update_resp.status_code != 200:
            return fail(f"anchor update returned {update_resp.status_code}: {update_resp.get_data(as_text=True)}")
        update_data = update_resp.get_json() or {}
        block_after = update_data.get("block") or {}
        if str(block_after.get("anchor_datetime") or "").strip() != anchor_dt:
            return fail("anchor_datetime was not saved")
        if int(block_after.get("allow_pull_forward") if block_after.get("allow_pull_forward") is not None else 1) != 0:
            return fail("allow_pull_forward was not set to 0")
        if str(block_after.get("planned_start_at") or "").strip() != anchor_dt:
            return fail("planned_start_at did not move to the anchor datetime")
        if not str(block_after.get("planned_end_at") or "").strip():
            return fail("planned_end_at is blank after anchoring")
        if str(block_after.get("planned_end_at") or "").strip() == original_end:
            return fail("planned_end_at was not recalculated after anchoring")
        pass_msg("anchor update rewrites the block's planned dates")

        with db() as con:
            db_row = one(
                con.execute(
                    """
                    SELECT anchor_datetime, planned_start_at, planned_end_at, allow_pull_forward
                    FROM run_block
                    WHERE block_id = ?
                    """,
                    (block_id,),
                )
            ) or {}
            if str(db_row.get("anchor_datetime") or "").strip() != anchor_dt:
                return fail("database anchor_datetime mismatch after update")
            if str(db_row.get("planned_start_at") or "").strip() != anchor_dt:
                return fail("database planned_start_at mismatch after update")
            if not str(db_row.get("planned_end_at") or "").strip():
                return fail("database planned_end_at missing after update")
            if int(db_row.get("allow_pull_forward") if db_row.get("allow_pull_forward") is not None else 1) != 0:
                return fail("database allow_pull_forward mismatch after update")
        pass_msg("database row reflects the anchored planned dates")

        planner = _get_schedule(client)
        found = next((item for item in planner.get("blocks") or [] if int(item.get("block_id") or 0) == block_id), None)
        if not found:
            return fail("anchored block missing from planner schedule")
        if str(found.get("planned_start_at") or "").strip() != anchor_dt:
            return fail("planner schedule did not reflect anchored planned_start_at")
        if not str(found.get("planned_end_at") or "").strip():
            return fail("planner schedule returned blank planned_end_at")
        if str(found.get("planned_end_at") or "").strip() == original_end:
            return fail("planner schedule still shows the stale planned_end_at")
        previous = next((item for item in planner.get("blocks") or [] if int(item.get("machine_id") or 0) == machine_id and int(item.get("block_id") or 0) == int(first.get("block_id") or 0)), None)
        if previous:
            prev_end = str(previous.get("planned_end_at") or previous.get("calculated_end_datetime") or "").strip()
            if prev_end and prev_end >= anchor_dt:
                return fail("anchor did not create an idle window before the anchored block")
        pass_msg("planner schedule shows the anchored planned dates and an idle window before the block")

        clear_resp = client.put(
            f"/api/trial/blocks/{block_id}",
            json={
                "anchor_datetime": "",
                "allow_pull_forward": 1,
            },
        )
        if clear_resp.status_code != 200:
            return fail(f"clear anchor returned {clear_resp.status_code}: {clear_resp.get_data(as_text=True)}")
        clear_data = clear_resp.get_json() or {}
        block_clear = clear_data.get("block") or {}
        if str(block_clear.get("anchor_datetime") or "").strip():
            return fail("anchor_datetime was not cleared")
        if int(block_clear.get("allow_pull_forward") if block_clear.get("allow_pull_forward") is not None else 0) != 1:
            return fail("allow_pull_forward was not restored to 1")
        if not str(block_clear.get("planned_start_at") or "").strip():
            return fail("planned_start_at is blank after clearing anchor")
        if not str(block_clear.get("planned_end_at") or "").strip():
            return fail("planned_end_at is blank after clearing anchor")
        if str(block_clear.get("planned_start_at") or "").strip() == anchor_dt:
            return fail("planned_start_at did not move back after clearing anchor")
        pass_msg("clearing anchor restores the block back to scheduler-planned dates")

        planner = _get_schedule(client)
        found = next((item for item in planner.get("blocks") or [] if int(item.get("block_id") or 0) == block_id), None)
        if not found:
            return fail("cleared block missing from planner schedule")
        if str(found.get("anchor_datetime") or "").strip():
            return fail("planner schedule still shows an anchor after clearing it")
        if not str(found.get("planned_start_at") or "").strip():
            return fail("planner schedule planned_start_at is blank after clearing")
        if not str(found.get("planned_end_at") or "").strip():
            return fail("planner schedule planned_end_at is blank after clearing")
        pass_msg("planner schedule reflects the cleared anchor state")

        print("PASS: smoke_planner_anchor_updates_planned_dates completed successfully")
        return 0
    finally:
        for block_id in created_ids[::-1]:
            if not block_id:
                continue
            try:
                client.delete(f"/api/trial/blocks/{block_id}")
            except Exception:
                pass
        if created_ids:
            try:
                restore = {
                    "anchor_datetime": original_anchor if 'original_anchor' in locals() and original_anchor else "",
                    "allow_pull_forward": original_allow_pull_forward if 'original_allow_pull_forward' in locals() else 1,
                }
                if 'original_start' in locals() and original_start:
                    restore["planned_start_at"] = original_start
                if 'original_end' in locals() and original_end:
                    restore["planned_end_at"] = original_end
                client.put(f"/api/trial/blocks/{block_id}", json=restore)
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
