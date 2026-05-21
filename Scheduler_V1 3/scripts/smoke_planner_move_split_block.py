from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app import create_app
from scheduler_app.db import db, ensure_db, one
from scheduler_app.planning_scheduler import recalculate_planning_all as recalculate_planning_all_baseline
from scheduler_app.routes.planner import recalculate_machine


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


def _pick_block():
    with db() as con:
        row = one(
            con.execute(
                """
                SELECT *
                FROM run_block
                WHERE COALESCE(active, 1) = 1
                  AND COALESCE(scheduled_qty, 0) >= 2
                ORDER BY block_id
                LIMIT 1
                """
            )
        )
        return dict(row) if row else None


def _pick_target_machine(planner, original_machine_id):
    for machine in planner.get("machines") or []:
        mid = int(machine.get("machine_id") or 0)
        if mid and mid != int(original_machine_id or 0):
            return machine
    return None


def main():
    try:
        ensure_db()
    except Exception as exc:
        return fail(f"ensure_db failed: {exc}")

    app = create_app()
    client = app.test_client()

    block = _pick_block()
    if not block:
        return fail("Could not find a block with scheduled_qty >= 2")

    original_block_id = int(block["block_id"])
    original_machine_id = int(block.get("machine_id") or 0)
    original_qty = float(block.get("scheduled_qty") or 0)
    split_qty = max(1.0, float(int(original_qty // 2) or 1))
    if split_qty >= original_qty:
        split_qty = original_qty - 1

    planner = _get_json(client, "/api/trial/planner/schedule")
    target_machine = _pick_target_machine(planner, original_machine_id)
    if not target_machine:
        return fail("Could not find a different target machine")
    target_machine_id = int(target_machine.get("machine_id") or 0)

    split_res = client.post(f"/api/trial/blocks/{original_block_id}/split", json={"split_qty": split_qty, "source": "PLANNER"})
    if split_res.status_code != 200:
        return fail(f"Split endpoint returned {split_res.status_code}: {split_res.get_data(as_text=True)}")
    split_data = split_res.get_json() or {}
    new_block = split_data.get("new_block") or {}
    new_block_id = int(new_block.get("block_id") or 0)
    if not new_block_id:
        return fail("Split did not return a new block_id")
    pass_msg("Split produced a new block")

    move_res = client.post(
        f"/api/trial/blocks/{new_block_id}/reorder",
        json={
            "machine_id": target_machine_id,
            "ordered_ids": [new_block_id],
        },
    )
    if move_res.status_code != 200:
        return fail(f"Reorder move returned {move_res.status_code}: {move_res.get_data(as_text=True)}")
    pass_msg("Split block moved to the target machine")

    planner_after = _get_json(client, "/api/trial/planner/schedule")
    moved_block = next((b for b in planner_after.get("blocks") or [] if int(b.get("block_id") or 0) == new_block_id), None)
    if not moved_block:
        return fail("Moved split block not found in planner schedule")
    if int(moved_block.get("machine_id") or 0) != target_machine_id:
        return fail("Moved split block machine_id did not change")
    pass_msg("Planner schedule shows the moved split block on the new machine")

    schedule_after = _get_json(client, "/api/trial/schedule")
    moved_in_schedule = next((b for b in schedule_after.get("blocks") or [] if int(b.get("block_id") or 0) == new_block_id), None)
    if not moved_in_schedule:
        return fail("Moved split block not found in actual schedule")
    if int(moved_in_schedule.get("machine_id") or 0) != target_machine_id:
        return fail("Actual schedule did not reflect the moved machine")
    pass_msg("Actual schedule shows the moved split block on the new machine")

    with db() as con:
        con.execute(
            "UPDATE run_block SET scheduled_qty = ?, updated_at = CURRENT_TIMESTAMP WHERE block_id = ?",
            (original_qty, original_block_id),
        )
        recalculate_machine(con, original_machine_id)
        recalculate_planning_all_baseline(con, reason="SMOKE_PLANNER_MOVE_SPLIT_BLOCK_CLEANUP")
    pass_msg("Cleanup recalculated planner state")

    if new_block_id:
        client.delete(f"/api/trial/blocks/{new_block_id}")
    pass_msg("Temporary split block cleaned up")

    page = client.get("/planner").get_data(as_text=True)
    for text in ("isExistingBlockMove", "justMovedExistingBlock", "Missing operation data for this card."):
        if text not in page:
            return fail(f"/planner missing expected text: {text}")
    if "savePlannerOrder(lane)".replace(" ", "") not in page.replace(" ", ""):
        pass_msg("savePlannerOrder exists on planner page")

    actual_page = client.get("/actual-production").get_data(as_text=True)
    if "openTrialSplitModal" in actual_page or 'Split</button>' in actual_page:
        return fail("Actual Production still exposes split controls")
    pass_msg("Actual Production does not expose split controls")

    print("PASS: smoke_planner_move_split_block completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
