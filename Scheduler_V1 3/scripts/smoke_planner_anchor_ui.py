from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app import create_app
from scheduler_app.db import ensure_db


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
    payload = _get_json(client, "/api/trial/planner/schedule")
    for block in payload.get("blocks") or []:
        if int(block.get("block_id") or 0) > 0:
            return block
    return None


def _pick_machine(client):
    payload = _get_json(client, "/api/trial/planner/schedule")
    for machine in payload.get("machines") or []:
        if int(machine.get("machine_id") or 0) > 0:
            return machine
    return None


def main():
    try:
        ensure_db()
    except Exception as exc:
        return fail(f"ensure_db failed: {exc}")

    app = create_app()
    client = app.test_client()

    created_temp_block_id = 0
    block = _pick_block(client)
    if not block:
        machine = _pick_machine(client)
        if not machine:
            return fail("planner schedule returned no machines to seed a temporary block")
        create_res = client.post(
            "/api/trial/operations",
            json={
                "job_no": "SMOKE-ANCHOR",
                "operation_name": "Anchor Smoke",
                "total_qty": 2,
                "scheduled_qty": 2,
                "setup_minutes": 0,
                "cycle_minutes_per_qty": 1,
                "machine_id": int(machine.get("machine_id") or 0),
                "queue_position": 0,
                "include_setup": 1,
                "status": "ACTIVE",
                "planning_status": "PLANNED",
                "execution_status": "NOT_STARTED",
                "remarks": "SMOKE_ANCHOR_TEMP",
            },
        )
        if create_res.status_code != 200:
            return fail(f"Could not seed a temporary block: {create_res.status_code} {create_res.get_data(as_text=True)}")
        block = (create_res.get_json() or {}).get("block") or {}
        created_temp_block_id = int(block.get("block_id") or 0)
        if not created_temp_block_id:
            return fail("Temporary anchor block was created without a block_id")

    block_id = int(block.get("block_id") or 0)
    original_anchor = str(block.get("anchor_datetime") or "")
    original_allow_pull_forward = int(block.get("allow_pull_forward") if block.get("allow_pull_forward") is not None else 1)
    original_planned_start = str(block.get("planned_start_at") or "")

    anchor_dt = "2099-03-13 08:30:00"

    try:
        res = client.put(
            f"/api/trial/blocks/{block_id}",
            json={
                "anchor_datetime": anchor_dt,
                "planned_start_at": anchor_dt,
                "allow_pull_forward": 0,
            },
        )
        if res.status_code != 200:
            return fail(f"PUT anchor update returned {res.status_code}: {res.get_data(as_text=True)}")
        data = res.get_json() or {}
        block_after = data.get("block") or {}
        if str(block_after.get("anchor_datetime") or "") != anchor_dt:
            return fail("anchor_datetime was not saved on the block")
        if int(block_after.get("allow_pull_forward") if block_after.get("allow_pull_forward") is not None else 1) != 0:
            return fail("allow_pull_forward was not set to 0")
        pass_msg("block anchor can be saved through Planner/API")

        planner = _get_json(client, "/api/trial/planner/schedule")
        found = next((item for item in planner.get("blocks") or [] if int(item.get("block_id") or 0) == block_id), None)
        if not found:
          return fail("updated block missing from planner schedule")
        if str(found.get("anchor_datetime") or "") != anchor_dt:
            return fail("planner schedule did not reflect anchor_datetime")
        pass_msg("planner schedule includes anchor fields after update")

        res = client.put(
            f"/api/trial/blocks/{block_id}",
            json={
                "anchor_datetime": "",
                "allow_pull_forward": 1,
            },
        )
        if res.status_code != 200:
            return fail(f"PUT clear anchor returned {res.status_code}: {res.get_data(as_text=True)}")
        data = res.get_json() or {}
        block_after = data.get("block") or {}
        if str(block_after.get("anchor_datetime") or "") not in ("", "None"):
            return fail("anchor_datetime was not cleared")
        if int(block_after.get("allow_pull_forward") if block_after.get("allow_pull_forward") is not None else 0) != 1:
            return fail("allow_pull_forward was not restored to 1")
        pass_msg("block anchor can be cleared")

        planner = _get_json(client, "/api/trial/planner/schedule")
        found = next((item for item in planner.get("blocks") or [] if int(item.get("block_id") or 0) == block_id), None)
        if not found:
            return fail("cleared block missing from planner schedule")
        if str(found.get("anchor_datetime") or "") not in ("", "None"):
            return fail("planner schedule did not clear anchor_datetime")
        pass_msg("planner schedule reflects the cleared anchor state")

        planner_page = client.get("/planner").get_data(as_text=True)
        if "openPlannerAnchorModal" not in planner_page or "submitPlannerAnchor" not in planner_page or "clearPlannerAnchor" not in planner_page:
            return fail("Planner page is missing anchor controls")
        if "Set Anchor" not in planner_page and "Edit Anchor" not in planner_page:
            return fail("Planner page is missing anchor action text")
        actual_page = client.get("/actual-production").get_data(as_text=True)
        for text in ("openTrialAnchorModal", "submitTrialAnchor", "Set Anchor", "Clear Anchor", "trial-anchor-datetime", "trial-edit-anchor-datetime", "trial-anchor-input"):
            if text in actual_page:
                return fail(f"Actual Production still exposes anchor control text: {text}")
        pass_msg("Planner owns anchor controls and Actual Production does not")

        print("PASS: smoke_planner_anchor_ui completed successfully")
        return 0
    finally:
        try:
            if created_temp_block_id:
                client.delete(f"/api/trial/blocks/{created_temp_block_id}")
            restore_payload = {"allow_pull_forward": original_allow_pull_forward}
            if original_anchor:
                restore_payload["anchor_datetime"] = original_anchor
                if original_planned_start:
                    restore_payload["planned_start_at"] = original_planned_start
            else:
                restore_payload["anchor_datetime"] = ""
            client.put(f"/api/trial/blocks/{block_id}", json=restore_payload)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
