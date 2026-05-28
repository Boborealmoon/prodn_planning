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
    for path in ("/planner", "/planner-baseline"):
        res = client.get(path)
        if res.status_code != 200:
            return fail(f"{path} returned {res.status_code}")
        body = res.get_data(as_text=True)
        for text in ("planner-grid", "planner-sidebar-list"):
            if text not in body:
                return fail(f"{path} missing expected text: {text}")
    pass_msg("planner pages render for gap-card coverage")
    return 0


if __name__ == "__main__":
    sys.exit(main())
