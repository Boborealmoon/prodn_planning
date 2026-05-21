from __future__ import annotations

import sys
from datetime import datetime
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
    for key in ("planning_run", "settings", "machines", "blocks", "segments", "process_sheets", "catalog", "planned", "planning_cards"):
        if key not in data:
            return fail(f"planner schedule missing key: {key}")
    machines = data.get("machines") or []
    blocks = data.get("blocks") or []
    segments = data.get("segments") or []
    process_sheets = data.get("process_sheets") or []
    catalog = data.get("catalog") or []
    planned = data.get("planned") or []
    if not machines:
        return fail("planner schedule returned no machines")
    if not blocks:
        return fail("planner schedule returned no blocks")
    if not process_sheets:
        return fail("planner schedule returned no process sheets")
    if not any((item.get("op_cards") or []) for item in catalog + planned):
        return fail("planner schedule returned no sidebar op cards")
    sidebar_items = [item for item in (catalog + planned + process_sheets) if item]
    sample_item = next((item for item in sidebar_items if item.get("source_ps_id")), None)
    if not sample_item:
        return fail("planner schedule returned no process sheet/sidebar sample item")
    for field in ("source_ps_id", "partial_no", "pp_partial_no", "part_no", "part_desc", "total_qty", "partial_qty", "opn_count", "active_opn_count", "completed_opn_count", "is_completed", "completed_at", "execution_label", "selected_bom_id", "default_bom_id", "bom_options"):
        if field not in sample_item:
            return fail(f"planner sidebar item missing {field}")
    if not str(sample_item.get("source_ps_id") or "").strip():
        return fail("planner sidebar item source_ps_id is empty")
    if not isinstance(sample_item.get("opn_count"), int):
        return fail("planner sidebar item opn_count must be numeric")
    if not isinstance(sample_item.get("active_opn_count"), int):
        return fail("planner sidebar item active_opn_count must be numeric")
    if not isinstance(sample_item.get("completed_opn_count"), int):
        return fail("planner sidebar item completed_opn_count must be numeric")
    if not isinstance(sample_item.get("total_qty"), (int, float)):
        return fail("planner sidebar item total_qty must be numeric")
    if not isinstance(sample_item.get("partial_qty"), (int, float)):
        return fail("planner sidebar item partial_qty must be numeric")
    if not isinstance(sample_item.get("bom_options"), list):
        return fail("planner sidebar item bom_options must be a list")
    sample_op_card = next((card for card in (sample_item.get("op_cards") or []) if card), None)
    if not sample_op_card:
        return fail("planner sidebar item missing op_cards")
    for field in ("operation_id", "source_ps_id", "source_op_seq_id", "source_op_no", "opn_label", "is_completed", "completed_at", "execution_label"):
        if field not in sample_op_card:
            return fail(f"planner op card missing {field}")
    for block in blocks:
        for field in ("expected_start_at", "expected_end_at", "actual_start_at", "actual_end_at", "planned_minutes"):
            if field not in block:
                return fail(f"block payload missing {field}")
    for ps in process_sheets:
        for field in ("expected_start_at", "expected_end_at", "actual_start_at", "actual_end_at", "planned_qty", "planned_minutes"):
            if field not in ps:
                return fail(f"process sheet payload missing {field}")
        for field in ("source_ps_id", "partial_no", "pp_partial_no", "part_no", "part_desc", "total_qty", "partial_qty", "opn_count", "active_opn_count", "completed_opn_count", "is_completed", "completed_at", "execution_label", "selected_bom_id", "default_bom_id", "bom_options"):
            if field not in ps:
                return fail(f"process sheet payload missing {field}")
    for seg in segments:
        seg_date = str(seg.get("segment_date") or "")
        if seg_date:
            weekday = datetime.fromisoformat(f"{seg_date} 00:00:00").date().weekday()
            if weekday >= 5:
                return fail(f"planner schedule contains weekend segment: {seg_date}")
    pass_msg("planner workspace API exposes sidebar and lane data")

    res = client.get("/api/trial/planner/settings")
    if res.status_code != 200:
        return fail(f"GET /api/trial/planner/settings returned {res.status_code}")
    settings = (res.get_json() or {}).get("settings") or {}
    if "planning_efficiency" not in settings:
        return fail("planner settings missing planning_efficiency")
    pass_msg("planner workspace settings are available")

    print("PASS: smoke_planner_workspace completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
