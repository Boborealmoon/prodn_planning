from __future__ import annotations

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


def main():
    app = create_app()
    client = app.test_client()

    res = client.get("/api/trial/planner/schedule")
    if res.status_code != 200:
        return fail(f"GET /api/trial/planner/schedule returned {res.status_code}")

    data = res.get_json() or {}
    process_sheets = data.get("process_sheets") or []
    if not process_sheets:
        return fail("planner schedule returned no process sheets")

    for idx, ps in enumerate(process_sheets[:20], 1):
        op_cards = ps.get("op_cards") or []
        selected_bom_id = int(ps.get("selected_bom_id") or 0)
        default_bom_id = int(ps.get("default_bom_id") or 0)
        bom_options = ps.get("bom_options") or []
        opn_count = int(ps.get("opn_count") or 0)
        active_opn_count = int(ps.get("active_opn_count") or 0)
        completed_opn_count = int(ps.get("completed_opn_count") or 0)
        print(
            f"{idx}. source_ps_id={ps.get('source_ps_id') or ''} "
            f"selected_bom_id={selected_bom_id} default_bom_id={default_bom_id} "
            f"bom_options={len(bom_options)} opn_count={opn_count} "
            f"active_opn_count={active_opn_count} completed_opn_count={completed_opn_count} "
            f"op_cards={len(op_cards)}"
        )
        if (selected_bom_id or default_bom_id) and bom_options and opn_count > 0 and not len(op_cards):
            return fail(
                "BOM-backed process sheet missing op_cards: "
                f"source_ps_id={ps.get('source_ps_id') or ''}, selected_bom_id={selected_bom_id}, "
                f"default_bom_id={default_bom_id}, opn_count={opn_count}"
            )

    pass_msg("planner sidebar debug passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
