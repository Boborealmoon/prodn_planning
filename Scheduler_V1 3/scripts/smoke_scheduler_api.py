from __future__ import annotations

import sys
from uuid import uuid4
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app.factory import create_app
from scheduler_app.db import db, ensure_db, one


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


def main():
    try:
        ensure_db()
        ensure_db()
        pass_msg("ensure_db() is idempotent")
    except Exception as exc:
        return fail(f"ensure_db() failed: {exc}")

    app = create_app()
    app.testing = True
    client = app.test_client()

    with db() as con:
        machine = one(con.execute("SELECT machine_id FROM machines WHERE active = 1 ORDER BY machine_id LIMIT 1"))
        if not machine:
            return fail("no active machine found")
        machine_id = int(machine["machine_id"])

    planned_start_at = "2099-01-05 08:30:00"
    planned_end_at = "2099-01-05 12:00:00"
    run_tag = uuid4().hex[:8]

    create_resp = client.post(
        "/api/trial/operations",
        json={
            "job_no": f"SMOKE-API-{run_tag}",
            "operation_name": f"Smoke Operation {run_tag}",
            "total_qty": 10,
            "scheduled_qty": 10,
            "setup_minutes": 15,
            "cycle_minutes_per_qty": 2,
            "compatible_machine_group": "SMOKE",
            "source_ps_id": "SMOKE-PS::1",
            "source_op_seq_id": 1,
            "source_op_no": "10",
            "machine_id": machine_id,
            "queue_position": 0,
            "planned_start_at": planned_start_at,
            "planned_end_at": planned_end_at,
            "allow_pull_forward": 1,
            "active": 1,
            "is_fresh_monday_item": 0,
            "planning_status": "PLANNED",
            "execution_status": "NOT_STARTED",
            "include_setup": 1,
        },
    )
    if create_resp.status_code != 200:
        return fail(f"create operation failed: {create_resp.status_code} {create_resp.get_data(as_text=True)}")
    create_data = create_resp.get_json() or {}
    block = create_data.get("block") or {}
    block_id = int(block.get("block_id") or 0)
    if not block_id:
        return fail("create operation returned no block_id")
    if float(block.get("queue_position") or 0) <= 0:
        return fail("queue_position was not assigned on create")
    if str(block.get("planned_start_at") or "") != planned_start_at:
        return fail("planned_start_at was not preserved on create")
    if int(block.get("allow_pull_forward") or 0) != 1:
        return fail("allow_pull_forward default is wrong")
    pass_msg("planner create operation returns planner-intent fields")

    update_resp = client.put(
        f"/api/trial/blocks/{block_id}",
        json={
            "queue_position": 12.5,
            "planned_start_at": planned_start_at,
            "planned_end_at": planned_end_at,
            "allow_pull_forward": 0,
            "is_fresh_monday_item": 1,
            "active": 1,
            "scheduler_note": "API smoke",
        },
    )
    if update_resp.status_code != 200:
        return fail(f"update block failed: {update_resp.status_code} {update_resp.get_data(as_text=True)}")
    update_data = update_resp.get_json() or {}
    updated = update_data.get("block") or {}
    if float(updated.get("queue_position") or 0) != 12.5:
        return fail("queue_position did not preserve decimal midpoint")
    if str(updated.get("planned_start_at") or "") != planned_start_at:
        return fail("planned_start_at was overwritten by update")
    if str(updated.get("planned_end_at") or "") != planned_end_at:
        return fail("planned_end_at was overwritten by update")
    if updated.get("allow_pull_forward") is None or int(updated.get("allow_pull_forward")) != 0:
        return fail("allow_pull_forward update did not stick")
    if int(updated.get("is_fresh_monday_item") or 0) != 1:
        return fail("is_fresh_monday_item update did not stick")
    if "alerts" not in updated or not isinstance(updated.get("alerts"), list):
        return fail("block payload is missing alerts list")
    pass_msg("planner update preserves intent fields and decimal queue positions")

    with db() as con:
        row = one(con.execute("SELECT block_id, schedule_run_id FROM machine_queue_state WHERE block_id = ?", (block_id,)))
        if not row:
            return fail("machine_queue_state row missing for created block")
        if int(row["schedule_run_id"] or 0) <= 0:
            return fail("machine_queue_state missing schedule_run_id")
    pass_msg("machine_queue_state is populated for the created block")

    delete_resp = client.delete(f"/api/trial/blocks/{block_id}")
    if delete_resp.status_code != 200:
        return fail(f"delete block failed: {delete_resp.status_code} {delete_resp.get_data(as_text=True)}")
    with db() as con:
        deleted = one(con.execute("SELECT block_id FROM run_block WHERE block_id = ?", (block_id,)))
        if deleted:
            return fail("block still exists after delete")
        deleted_state = one(con.execute("SELECT block_id FROM machine_queue_state WHERE block_id = ?", (block_id,)))
        if deleted_state:
            return fail("machine_queue_state row still exists after delete")
        deleted_alert = one(con.execute("SELECT alert_id FROM schedule_alert WHERE block_id = ?", (block_id,)))
        if deleted_alert:
            return fail("schedule_alert row still exists after delete")
    pass_msg("planner delete removes clean planned rows")

    gantt_resp = client.get("/api/trial/gantt")
    if gantt_resp.status_code != 200:
        return fail(f"gantt api failed: {gantt_resp.status_code}")
    gantt_data = gantt_resp.get_json() or {}
    if "calendar_windows" not in gantt_data:
        return fail("gantt api missing calendar_windows")
    if "blocks" not in gantt_data:
        return fail("gantt api missing blocks")
    if not isinstance(gantt_data.get("blocks"), list):
        return fail("gantt api blocks payload is not a list")
    pass_msg("gantt api exposes calendar_windows separately from production blocks")

    print("PASS: smoke_scheduler_api completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
