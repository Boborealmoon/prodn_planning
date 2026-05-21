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

    res = client.get("/actual-production")
    if res.status_code != 200:
        return fail(f"/actual-production returned {res.status_code}")

    body = res.get_data(as_text=True)

    required = [
        ".trial-lane",
        "overflow-y: auto",
        "padding-bottom: 10px",
        ".trial-block-card",
        "flex: 0 0 auto",
        "height: auto",
        ".trial-op-card-actions",
        "flex-wrap: wrap",
    ]
    forbidden = [
        ".trial-block-card { flex: 1",
        ".trial-block-card { height: 100%",
    ]

    for needle in required:
        if needle not in body:
            return fail(f"missing expected CSS/text: {needle}")

    for needle in forbidden:
        if needle in body:
            return fail(f"found forbidden CSS/text: {needle}")

    pass_msg("/actual-production exposes the expected Actual card layout CSS")
    print("PASS: smoke_actual_card_layout completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
