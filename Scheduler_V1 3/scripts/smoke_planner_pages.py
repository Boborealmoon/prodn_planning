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


def _body_text(client, path):
    res = client.get(path)
    body = res.get_data(as_text=True)
    return res.status_code, body


def main():
    app = create_app()
    client = app.test_client()

    checks = [
        ("/planner", ["Planner", "Planning efficiency", "Planned", "Actual", "Show completed", "pl2-sidebar-title", "pl2-ps-card", "pl2-ps-header", "pl2-ps-body", "pl2-timeline", "Partial No", "Part No", "Part Desc", "Total Qty", "Partial Qty", "OPN", "Execution", "BOM", "Machine", "planner-grid", "planner-sidebar-list"], ["Planner Baseline", "Submit Actual", "Correct Actual", "Reject Entry", "Save Actuals", "Actual Entry", "trial-catalog"]),
        ("/planner-baseline", ["Planner", "Planning efficiency", "Planned", "Actual", "Show completed", "pl2-sidebar-title", "pl2-ps-card", "pl2-ps-header", "pl2-ps-body", "pl2-timeline", "Partial No", "Part No", "Part Desc", "Total Qty", "Partial Qty", "OPN", "Execution", "BOM", "Machine", "planner-grid", "planner-sidebar-list"], ["Planner Baseline", "Submit Actual", "Correct Actual", "Reject Entry", "Save Actuals", "Actual Entry", "trial-catalog"]),
        ("/trial", ["Actual Production", "trial-grid"], ["Planner Baseline", "Available PS / Ops", "Combine ops inside the PS", "Process Sheets", "planner-sidebar-list"]),
        ("/actual-production", ["Actual Production", "trial-grid"], ["Planner Baseline", "Available PS / Ops", "Combine ops inside the PS", "Process Sheets", "planner-sidebar-list"]),
    ]

    for path, required, forbidden in checks:
        status, body = _body_text(client, path)
        if status != 200:
            return fail(f"{path} returned {status}")
        for text in required:
            if text not in body:
                return fail(f"{path} missing expected text: {text}")
        for text in forbidden:
            if text in body:
                return fail(f"{path} unexpectedly contains text: {text}")
        pass_msg(f"{path} returns 200 with expected content")

    pass_msg("smoke_planner_pages completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
