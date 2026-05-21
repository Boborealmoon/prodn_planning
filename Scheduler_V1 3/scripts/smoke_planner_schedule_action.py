from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app import create_app


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


def _find_op_card(process_sheets):
    preferred_cards = []
    for ps in process_sheets:
        for card in ps.get("op_cards") or []:
            try:
                target_qty = float(card.get("target_qty") or 0)
                remaining_qty = float(card.get("remaining_qty") or 0)
                planned_qty = float(card.get("planned_qty") or 0)
            except (TypeError, ValueError):
                continue
            if (
                str(card.get("planning_status") or "").upper() != "FULLY_PLANNED"
                and bool(card.get("can_drag", True))
                and target_qty > 1
                and remaining_qty > 1
                and card.get("source_ps_id")
                and int(card.get("source_op_seq_id") or 0) > 0
                and str(card.get("source_op_no") or "").strip()
            ):
                source_ps_id = str(card.get("source_ps_id") or ps.get("source_ps_id") or "").strip()
                if source_ps_id == "SMOKEPLANOPN::1":
                    return ps, card, planned_qty, target_qty, remaining_qty
                if source_ps_id.startswith("SMOKEPLANOPN"):
                    preferred_cards.append((ps, card, planned_qty, target_qty, remaining_qty))
                elif not preferred_cards:
                    preferred_cards.append((ps, card, planned_qty, target_qty, remaining_qty))
    if preferred_cards:
        return preferred_cards[0]
    return None, None, 0.0, 0.0, 0.0


def _find_matching_card(process_sheets, source_ps_id, source_op_seq_id, source_op_no):
    for ps in process_sheets:
        if str(ps.get("source_ps_id") or "").strip() != source_ps_id:
            continue
        for card in ps.get("op_cards") or []:
            if (
                int(card.get("source_op_seq_id") or 0) == int(source_op_seq_id or 0)
                and str(card.get("source_op_no") or "").strip() == str(source_op_no or "").strip()
            ):
                return ps, card
    return None, None


def _assert_close(a, b, label, tolerance=0.001):
    if not math.isclose(float(a or 0), float(b or 0), rel_tol=0, abs_tol=tolerance):
        raise AssertionError(f"{label}: expected {b}, got {a}")


def main():
    app = create_app()
    client = app.test_client()

    res = client.get("/api/trial/planner/schedule")
    if res.status_code != 200:
        return fail(f"GET /api/trial/planner/schedule returned {res.status_code}")
    payload = res.get_json() or {}
    process_sheets = payload.get("process_sheets") or []
    machines = payload.get("machines") or []
    if not process_sheets:
        return fail("planner schedule returned no process sheets")
    if not machines:
        return fail("planner schedule returned no machines")

    source_ps, card, initial_planned, target_qty, initial_remaining = _find_op_card(process_sheets)
    if not card:
        return fail("no draggable OPN with enough remaining qty found")

    machine = machines[0]
    source_ps_id = str(card.get("source_ps_id") or source_ps.get("source_ps_id") or "").strip()
    source_op_seq_id = int(card.get("source_op_seq_id") or 0)
    source_op_no = str(card.get("source_op_no") or "").strip()
    selected_bom_id = int(card.get("selected_bom_id") or source_ps.get("selected_bom_id") or 0)
    machine_id = int(machine.get("machine_id") or 0)
    if not source_ps_id or not source_op_seq_id or not source_op_no or not machine_id:
        return fail("missing scheduling identifiers from planner schedule payload")

    first_add = round(max(0.5, float(initial_remaining) / 10.0), 3)
    if first_add <= 0 or first_add >= float(initial_remaining):
        return fail("unable to derive a valid partial quantity for the first schedule action")

    first_payload = {
        "source_ps_id": source_ps_id,
        "source_op_seq_id": source_op_seq_id,
        "source_op_no": source_op_no,
        "selected_bom_id": selected_bom_id,
        "machine_id": machine_id,
        "queue_position": 10,
        "scheduled_qty": first_add,
    }
    res = client.post("/api/trial/planner/schedule-opn", json=first_payload)
    if res.status_code != 200:
        return fail(f"first POST /api/trial/planner/schedule-opn returned {res.status_code}: {(res.get_json() or {}).get('error')}")
    first_block_id = int((res.get_json() or {}).get("block_id") or 0)
    if not first_block_id:
        return fail("first schedule-opn response missing block_id")
    pass_msg("first partial schedule created a block")

    res = client.get("/api/trial/planner/schedule")
    if res.status_code != 200:
        return fail(f"second GET /api/trial/planner/schedule returned {res.status_code}")
    payload = res.get_json() or {}
    process_sheets = payload.get("process_sheets") or []
    ps_after_first, card_after_first = _find_matching_card(process_sheets, source_ps_id, source_op_seq_id, source_op_no)
    if not card_after_first:
        return fail("scheduled OPN not found after first partial planning")
    if str(card_after_first.get("planning_status") or "").upper() != "PARTIALLY_PLANNED":
        return fail(f"expected PARTIALLY_PLANNED after first schedule, got {card_after_first.get('planning_status')}")
    if float(card_after_first.get("planned_qty") or 0) <= float(initial_planned):
        return fail("planned qty did not increase after the first schedule")
    if float(card_after_first.get("remaining_qty") or 0) >= float(initial_remaining):
        return fail("remaining qty did not decrease after the first schedule")
    if not bool(card_after_first.get("can_drag")):
        return fail("partially planned OPN should still be draggable")
    pass_msg("first schedule leaves the OPN partially planned and draggable")

    second_add = float(card_after_first.get("remaining_qty") or 0)
    if second_add <= 0:
        return fail("no remaining qty available for the second schedule step")
    second_payload = dict(first_payload)
    second_payload["queue_position"] = 20
    second_payload["scheduled_qty"] = second_add
    res = client.post("/api/trial/planner/schedule-opn", json=second_payload)
    if res.status_code != 200:
        return fail(f"second POST /api/trial/planner/schedule-opn returned {res.status_code}: {(res.get_json() or {}).get('error')}")
    second_block_id = int((res.get_json() or {}).get("block_id") or 0)
    if not second_block_id:
        return fail("second schedule-opn response missing block_id")
    pass_msg("second schedule created the remaining block")

    res = client.get("/api/trial/planner/schedule")
    if res.status_code != 200:
        return fail(f"third GET /api/trial/planner/schedule returned {res.status_code}")
    payload = res.get_json() or {}
    process_sheets = payload.get("process_sheets") or []
    _, card_after_second = _find_matching_card(process_sheets, source_ps_id, source_op_seq_id, source_op_no)
    if not card_after_second:
        return fail("scheduled OPN not found after second planning")
    if str(card_after_second.get("planning_status") or "").upper() != "FULLY_PLANNED":
        return fail(f"expected FULLY_PLANNED after second schedule, got {card_after_second.get('planning_status')}")
    if float(card_after_second.get("remaining_qty") or 0) != 0:
        return fail(f"remaining qty after second schedule should be 0, got {card_after_second.get('remaining_qty')}")
    if float(card_after_second.get("planned_qty") or 0) < float(card_after_first.get("planned_qty") or 0):
        return fail("planned qty regressed after second schedule")
    if bool(card_after_second.get("can_drag")):
        return fail("fully planned OPN should not be draggable")
    pass_msg("second schedule fills the OPN and disables dragging")

    res = client.post("/api/trial/planner/schedule-opn", json=second_payload)
    if res.status_code != 409:
        return fail(f"third schedule should be rejected with 409, got {res.status_code}")
    error_text = str((res.get_json() or {}).get("error") or "")
    if "already fully planned" not in error_text.lower():
        return fail(f"third schedule returned an unexpected error: {error_text}")
    pass_msg("third schedule is rejected once the OPN is fully planned")

    for block_id in {first_block_id, second_block_id}:
        cleanup = client.delete(f"/api/trial/blocks/{block_id}")
        if cleanup.status_code not in (200, 404):
            return fail(f"cleanup delete for block {block_id} returned {cleanup.status_code}")
    pass_msg("temporary planned blocks cleaned up")

    print("PASS: smoke_planner_schedule_action completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
