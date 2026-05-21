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


def _current_start(block):
    return (
        block.get("actual_start_at")
        or block.get("expected_start_at")
        or block.get("predicted_start_at")
        or block.get("calculated_start_datetime")
        or ""
    )


def _current_end(block):
    if block.get("actual_end_at"):
        return block.get("actual_end_at")
    return (
        block.get("expected_end_at")
        or block.get("predicted_end_at")
        or block.get("calculated_end_datetime")
        or ""
    )


def _current_status(block):
    if block.get("actual_end_at"):
        return "Completed"
    if block.get("actual_start_at"):
        return "In progress"
    return "Forecast"


def main():
    app = create_app()
    client = app.test_client()

    res = client.get("/planner")
    if res.status_code != 200:
        return fail(f"/planner returned {res.status_code}")
    body = res.get_data(as_text=True)
    if "Planned" not in body or "Current" not in body:
        return fail("/planner does not contain Planned and Current labels")
    if 'planner-date-label">Expected' in body or 'planner-date-label">Actual' in body:
        return fail("/planner still shows separate main Expected/Actual labels")
    pass_msg("/planner exposes Planned and Current only in the main card labels")

    res = client.get("/api/trial/planner/schedule")
    if res.status_code != 200:
        return fail(f"GET /api/trial/planner/schedule returned {res.status_code}")
    payload = res.get_json() or {}
    blocks = payload.get("blocks") or []
    if not blocks:
        return fail("planner schedule returned no blocks")
    sample = blocks[0]
    for field in ("planned_start_at", "planned_end_at", "expected_start_at", "expected_end_at", "actual_start_at", "actual_end_at"):
        if field not in sample:
            return fail(f"planner schedule block missing {field}")
    pass_msg("planner schedule still returns planned, expected, and actual timing fields")

    no_actual = {
        "expected_start_at": "2026-01-01 08:30:00",
        "expected_end_at": "2026-01-01 17:00:00",
    }
    start_only = {
        "actual_start_at": "2026-01-02 09:00:00",
        "expected_start_at": "2026-01-02 09:00:00",
        "expected_end_at": "2026-01-02 17:00:00",
    }
    complete = {
        "actual_start_at": "2026-01-03 09:00:00",
        "actual_end_at": "2026-01-03 12:00:00",
    }

    if _current_start(no_actual) != "2026-01-01 08:30:00" or _current_end(no_actual) != "2026-01-01 17:00:00" or _current_status(no_actual) != "Forecast":
        return fail("Current helper fallback failed for the no-actuals case")
    if _current_start(start_only) != "2026-01-02 09:00:00" or _current_end(start_only) != "2026-01-02 17:00:00" or _current_status(start_only) != "In progress":
        return fail("Current helper fallback failed for the in-progress case")
    if _current_start(complete) != "2026-01-03 09:00:00" or _current_end(complete) != "2026-01-03 12:00:00" or _current_status(complete) != "Completed":
        return fail("Current helper fallback failed for the completed case")
    pass_msg("Current helper logic matches the intended forecast/actual fallback")

    print("PASS: smoke_planner_timing_ui completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
