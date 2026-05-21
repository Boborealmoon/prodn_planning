from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app import create_app
from scheduler_app.db import db, ensure_db, one, rows
from scheduler_app.planning_scheduler import recalculate_planning_all as recalculate_planning_all_baseline


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


def _get_json(client, path):
    res = client.get(path)
    if res.status_code != 200:
        raise RuntimeError(f"GET {path} returned {res.status_code}")
    return res.get_json() or {}


def _op_key(card):
    return (
        int(card.get("source_op_seq_id") or 0),
        str(card.get("source_op_no") or "").strip(),
    )


def _find_candidate(planner_data):
    buckets = defaultdict(list)
    for ps in planner_data.get("process_sheets") or []:
        source_ps_id = str(ps.get("source_ps_id") or "").strip()
        partial_no = str(ps.get("pp_partial_no") or ps.get("partial_no") or "").strip()
        if not source_ps_id or not partial_no:
            continue
        buckets[source_ps_id].append(ps)
    for source_ps_id, rows in buckets.items():
        if len(rows) < 2:
            continue
        rows = sorted(rows, key=lambda item: str(item.get("pp_partial_no") or item.get("partial_no") or ""))
        for idx, row_a in enumerate(rows):
            bom_id = int(row_a.get("selected_bom_id") or 0)
            op_cards_a = [card for card in (row_a.get("op_cards") or []) if int(card.get("operation_id") or 0)]
            if not op_cards_a:
                continue
            for row_b in rows[idx + 1:]:
                if int(row_b.get("selected_bom_id") or 0) != bom_id:
                    continue
                cards_b = {
                    _op_key(card): card
                    for card in (row_b.get("op_cards") or [])
                    if int(card.get("operation_id") or 0)
                }
                for card_a in op_cards_a:
                    if bool(card_a.get("is_completed")):
                        continue
                    key = _op_key(card_a)
                    if key in cards_b:
                        return {
                            "source_ps_id": source_ps_id,
                            "partial_a": str(row_a.get("pp_partial_no") or row_a.get("partial_no") or "").strip(),
                            "partial_b": str(row_b.get("pp_partial_no") or row_b.get("partial_no") or "").strip(),
                            "bom_id": bom_id,
                            "card_a": card_a,
                            "card_b": cards_b[key],
                        }
    return None


def main():
    try:
        ensure_db()
        pass_msg("ensure_db() is idempotent")
    except Exception as exc:
        return fail(f"ensure_db() failed: {exc}")

    app = create_app()
    client = app.test_client()
    candidate = None
    op_completed = False
    ps_completed = False

    try:
        planner = _get_json(client, "/api/trial/planner/schedule")
        candidate = _find_candidate(planner)
        if not candidate:
            return fail("Could not find a source_ps_id with two partials and a shared planned OPN")

        card_a = candidate["card_a"]
        card_b = candidate["card_b"]
        source_ps_id = candidate["source_ps_id"]
        partial_a = candidate["partial_a"]
        partial_b = candidate["partial_b"]
        bom_id = int(candidate["bom_id"] or 0)

        op_res = client.post(
            f"/api/trial/planner/opn/{int(card_a['operation_id'])}/completion",
            json={
                "completed": True,
                "source_ps_id": source_ps_id,
                "pp_partial_no": partial_a,
                "source_op_seq_id": int(card_a.get("source_op_seq_id") or 0),
                "source_op_no": str(card_a.get("source_op_no") or ""),
                "bom_id": bom_id,
            },
        )
        if op_res.status_code != 200:
            return fail(f"OPN completion returned {op_res.status_code}: {op_res.get_data(as_text=True)}")
        op_completed = True

        planner_after_op = _get_json(client, "/api/trial/planner/schedule")
        rows = planner_after_op.get("process_sheets") or []
        row_a = next(
            (row for row in rows if str(row.get("source_ps_id") or "") == source_ps_id and str(row.get("pp_partial_no") or row.get("partial_no") or "") == partial_a),
            None,
        )
        row_b = next(
            (row for row in rows if str(row.get("source_ps_id") or "") == source_ps_id and str(row.get("pp_partial_no") or row.get("partial_no") or "") == partial_b),
            None,
        )
        if not row_a or not row_b:
            return fail("Expected both partial rows to be present after OPN completion")
        op_key = _op_key(card_a)
        card_a_after = next((card for card in row_a.get("op_cards") or [] if _op_key(card) == op_key), None)
        card_b_after = next((card for card in row_b.get("op_cards") or [] if _op_key(card) == op_key), None)
        if not card_a_after or not card_b_after:
            return fail("Expected the same OPN to exist on both partial rows")
        if not bool(card_a_after.get("is_completed")):
            return fail("Partial A OPN was not marked completed")
        if bool(card_b_after.get("is_completed")):
            return fail("Partial B OPN incorrectly became completed")
        pass_msg("OPN completion stayed scoped to the selected partial")

        ps_res = client.post(
            f"/api/trial/planner/source-ps/{source_ps_id}/completion",
            json={
                "pp_partial_no": partial_a,
                "completed": True,
            },
        )
        if ps_res.status_code != 200:
            return fail(f"PS completion returned {ps_res.status_code}: {ps_res.get_data(as_text=True)}")
        ps_completed = True

        planner_after_ps = _get_json(client, "/api/trial/planner/schedule")
        row_a = next(
            (row for row in planner_after_ps.get("process_sheets") or [] if str(row.get("source_ps_id") or "") == source_ps_id and str(row.get("pp_partial_no") or row.get("partial_no") or "") == partial_a),
            None,
        )
        row_b = next(
            (row for row in planner_after_ps.get("process_sheets") or [] if str(row.get("source_ps_id") or "") == source_ps_id and str(row.get("pp_partial_no") or row.get("partial_no") or "") == partial_b),
            None,
        )
        if not row_a or not row_b:
            return fail("Expected both partial rows to remain queryable after PS completion")
        if not bool(row_a.get("is_completed")):
            return fail("Partial A PS was not marked completed")
        if bool(row_b.get("is_completed")):
            return fail("Partial B PS was incorrectly marked completed")
        pass_msg("PS completion stayed scoped to the selected partial")

        ps_active_res = client.post(
            f"/api/trial/planner/source-ps/{source_ps_id}/completion",
            json={
                "pp_partial_no": partial_a,
                "completed": False,
            },
        )
        if ps_active_res.status_code != 200:
            return fail(f"PS active toggle returned {ps_active_res.status_code}: {ps_active_res.get_data(as_text=True)}")
        ps_completed = False

        planner_after_active = _get_json(client, "/api/trial/planner/schedule")
        row_a = next(
            (row for row in planner_after_active.get("process_sheets") or [] if str(row.get("source_ps_id") or "") == source_ps_id and str(row.get("pp_partial_no") or row.get("partial_no") or "") == partial_a),
            None,
        )
        row_b = next(
            (row for row in planner_after_active.get("process_sheets") or [] if str(row.get("source_ps_id") or "") == source_ps_id and str(row.get("pp_partial_no") or row.get("partial_no") or "") == partial_b),
            None,
        )
        if not row_a or not row_b:
            return fail("Expected both partial rows after re-activating PS")
        if bool(row_a.get("is_completed")):
            return fail("Partial A PS remained completed after re-activating it")
        if bool(row_b.get("is_completed")):
            return fail("Partial B PS changed while toggling partial A")
        pass_msg("PS active toggle only affected the selected partial")

        print("PASS: smoke_planner_completion_scope completed successfully")
        return 0
    finally:
        if candidate:
            try:
                if ps_completed:
                    client.post(
                        f"/api/trial/planner/source-ps/{candidate['source_ps_id']}/completion",
                        json={"pp_partial_no": candidate["partial_a"], "completed": False},
                    )
                if op_completed:
                    client.post(
                        f"/api/trial/planner/opn/{int(candidate['card_a']['operation_id'])}/completion",
                        json={
                            "completed": False,
                            "source_ps_id": candidate["source_ps_id"],
                            "pp_partial_no": candidate["partial_a"],
                            "source_op_seq_id": int(candidate["card_a"].get("source_op_seq_id") or 0),
                            "source_op_no": str(candidate["card_a"].get("source_op_no") or ""),
                            "bom_id": int(candidate["bom_id"] or 0),
                        },
                    )
            except Exception as exc:
                print(f"WARN: cleanup failed: {exc}")


if __name__ == "__main__":
    sys.exit(main())
