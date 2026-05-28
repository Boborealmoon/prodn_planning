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
    res = client.get("/planner")
    if res.status_code != 200:
        return fail(f"/planner returned {res.status_code}")

    body = res.get_data(as_text=True)
    required = [
        "Available next",
        "plannerShortDateTime",
        "plannerDateOnly",
        "planner-compare",
        "PS ",
        "Partial ",
        "Planned",
        "Forecast",
        "Actual",
        "plannerIsActualStartLater",
        "plannerIsActualEndLater",
    ]
    forbidden = [
        "Total planned minutes",
        "Queue #",
        "Queue position",
        "Earlier than planned",
        "planner-compare-forecast",
    ]

    for text in required:
        if text not in body:
            return fail(f"/planner missing expected text: {text}")
    for text in forbidden:
        if text in body:
            return fail(f"/planner unexpectedly contains text: {text}")

    pass_msg("/planner contains the expected Planner card UI text and helpers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
