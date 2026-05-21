from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app import create_app


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def pass_msg(message: str) -> None:
    print(f"PASS: {message}")


def main() -> int:
    app = create_app()
    client = app.test_client()

    res = client.get("/planner")
    body = res.get_data(as_text=True)
    if res.status_code != 200:
        return fail(f"/planner returned {res.status_code}")

    required = [
        'data-accordion-trigger="1"',
        "pl2-ps-action-row",
        "bindPlannerSidebarAccordion",
        "togglePlannerPsCompletion",
        "togglePlannerOpnCompletion",
    ]
    forbidden = [
        '<button class="pl2-ps-header"',
        'onclick="togglePlannerPsCard(',
    ]

    for text in required:
        if text not in body:
            return fail(f"/planner missing expected text: {text}")

    for text in forbidden:
        if text in body:
            return fail(f"/planner unexpectedly contains text: {text}")

    pass_msg("/planner sidebar DOM markup looks valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
