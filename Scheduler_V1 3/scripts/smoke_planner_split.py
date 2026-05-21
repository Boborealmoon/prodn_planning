from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app import create_app
from scheduler_app.db import db, ensure_db, one, rows


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


def _get_json(client, path):
    res = client.get(path)
    if res.status_code != 200:
        raise RuntimeError(f"GET {path} returned {res.status_code}: {res.get_data(as_text=True)}")
    return res.get_json() or {}


def _pick_block(client):
    planner = _get_json(client, "/api/trial/planner/schedule")
    for block in planner.get("blocks") or []:
        scheduled_qty = float(block.get("scheduled_qty") or 0)
        if scheduled_qty >= 2 and int(block.get("block_id") or 0) > 0:
            return block
    return None


def _pick_any_block(client):
    planner = _get_json(client, "/api/trial/planner/schedule")
    for block in planner.get("blocks") or []:
        if int(block.get("block_id") or 0) > 0:
            return block
    return None


def _block_ids(payload):
    return {int(block.get("block_id") or 0) for block in payload.get("blocks") or [] if int(block.get("block_id") or 0) > 0}


def main():
    try:
        ensure_db()
    except Exception as exc:
        return fail(f"ensure_db failed: {exc}")

    app = create_app()
    client = app.test_client()

    block = _pick_block(client)
    temp_block_id = 0
    if not block:
        seed = _pick_any_block(client)
        if not seed:
            return fail("Could not find a seed block to create a temporary split candidate")
        create_res = client.post(
            "/api/trial/blocks",
            json={
                "job_no": seed.get("job_no") or seed.get("source_ps_id") or "",
                "operation_name": seed.get("operation_name") or "",
                "total_qty": 2,
                "scheduled_qty": 2,
                "cycle_minutes_per_qty": float(seed.get("cycle_minutes_per_qty") or 0),
                "setup_minutes": float(seed.get("setup_minutes") or 0),
                "compatible_machine_group": seed.get("compatible_machine_group") or "",
                "source_ps_id": seed.get("source_ps_id") or "",
                "source_op_seq_id": int(seed.get("source_op_seq_id") or 0),
                "source_op_no": seed.get("source_op_no") or "",
                "machine_id": int(seed.get("machine_id") or 0),
                "queue_position": float(seed.get("queue_position") or 0) + 0.1,
                "include_setup": 1,
                "status": seed.get("status") or "NOT_STARTED",
                "planning_status": seed.get("planning_status") or "PLANNED",
                "execution_status": seed.get("execution_status") or seed.get("status") or "NOT_STARTED",
                "active": 1,
                "remarks": "SMOKE_TEMP_SPLIT",
            },
        )
        if create_res.status_code != 200:
            return fail(f"Could not create temporary split candidate: {create_res.status_code} {create_res.get_data(as_text=True)}")
        block = (create_res.get_json() or {}).get("block") or {}
        temp_block_id = int(block.get("block_id") or 0)
        if not temp_block_id:
            return fail("Temporary split candidate was created without a block_id")

    block_id = int(block["block_id"])
    original_qty = float(block.get("scheduled_qty") or 0)
    split_qty = max(1.0, float(int(original_qty // 2) or 1))
    if split_qty >= original_qty:
        split_qty = original_qty - 1
    split_result = {}

    res = client.post(f"/api/trial/blocks/{block_id}/split", json={"split_qty": split_qty, "source": "PLANNER"})
    if res.status_code != 200:
        return fail(f"Split endpoint returned {res.status_code}: {res.get_data(as_text=True)}")
    data = res.get_json() or {}
    split_result = data
    if not data.get("ok"):
        return fail("Split response missing ok=true")
    if int(data.get("planning_run_id") or 0) <= 0:
        return fail("Split response missing planning_run_id")

    original = data.get("block") or {}
    new_block = data.get("new_block") or {}
    if int(original.get("block_id") or 0) != block_id:
        return fail("Split response original block_id changed unexpectedly")
    if abs(float(original.get("scheduled_qty") or 0) - float(split_qty)) > 1e-6:
        return fail("Original block scheduled_qty was not updated to split_qty")
    if abs(float(new_block.get("scheduled_qty") or 0) - float(original_qty - split_qty)) > 1e-6:
        return fail("New block scheduled_qty does not match remainder")
    if int(new_block.get("machine_id") or 0) != int(block.get("machine_id") or 0):
        return fail("New block machine_id changed unexpectedly")
    if float(new_block.get("queue_position") or 0) <= float(original.get("queue_position") or 0):
        return fail("New block queue_position was not placed after the original")
    if str(new_block.get("source_ps_id") or "") != str(original.get("source_ps_id") or ""):
        return fail("New block source_ps_id changed unexpectedly")
    pass_msg("Split endpoint preserves block identity and queue placement")

    planner_after = _get_json(client, "/api/trial/planner/schedule")
    planner_ids = _block_ids(planner_after)
    if block_id not in planner_ids or int(new_block.get("block_id") or 0) not in planner_ids:
        return fail("Planner schedule does not include both split blocks")
    pass_msg("Planner schedule includes both split blocks")

    schedule_after = _get_json(client, "/api/trial/schedule")
    schedule_ids = _block_ids(schedule_after)
    if block_id not in schedule_ids or int(new_block.get("block_id") or 0) not in schedule_ids:
        return fail("Actual schedule does not include both split blocks")
    pass_msg("Actual schedule includes both split blocks")

    planner_page = client.get("/planner").get_data(as_text=True)
    if "openPlannerSplitModal" not in planner_page or "submitPlannerSplit" not in planner_page:
        return fail("Planner page is missing split controls")
    actual_page = client.get("/actual-production").get_data(as_text=True)
    if "openTrialSplitModal" in actual_page or 'onclick="openTrialSplitModal' in actual_page:
        return fail("Actual Production still exposes split controls")
    pass_msg("Planner owns split controls and Actual Production does not")

    with db() as con:
        new_block_id = int(new_block.get("block_id") or 0)
        if new_block_id:
            client.delete(f"/api/trial/blocks/{new_block_id}")
        if temp_block_id:
            client.delete(f"/api/trial/blocks/{block_id}")
        if split_result and not temp_block_id:
            con.execute(
                "UPDATE run_block SET scheduled_qty = ?, updated_at = CURRENT_TIMESTAMP WHERE block_id = ?",
                (float(original_qty), block_id),
            )
    client.post("/api/trial/planner/recalculate", json={"reason": "SMOKE_PLANNER_SPLIT_CLEANUP"})

    print("PASS: smoke_planner_split completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
