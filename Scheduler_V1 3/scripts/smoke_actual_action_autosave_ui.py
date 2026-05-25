from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app import create_app
from scheduler_app.db import ensure_db


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


def main():
    try:
        ensure_db()
    except Exception as exc:
        return fail(f"ensure_db() failed: {exc}")

    app = create_app()
    client = app.test_client()

    res = client.get("/actual-production")
    if res.status_code != 200:
        return fail(f"GET /actual-production returned {res.status_code}")
    html = res.get_data(as_text=True)

    required = [
        "trialAutosaveActualRow",
        "trialActualFieldBlur",
        "trialActualInputKeydown",
        "trialDeleteActualDailyRow",
        'onfocus="trialActualFieldFocus(',
        'onblur="trialActualFieldBlur(',
        'onkeydown="trialActualInputKeydown(',
        "Delete Date",
    ]
    forbidden = [
        "Save Actual",
        "Save actuals",
        "Save rows",
        "trialSaveActualDailyRows(",
        "trialCollectActualDailyRows(",
        'oninput="trialSyncActualDailyRowDraft(',
        'onchange="trialSyncActualDailyRowDraft(',
        "No actual rows to save.",
    ]

    for needle in required:
        if needle not in html:
            return fail(f"missing required UI hook: {needle}")

    for needle in forbidden:
        if needle in html:
            return fail(f"found forbidden bulk-save UI hook: {needle}")

    pass_msg("actual page exposes row-level autosave hooks and no bulk-save button")
    print("PASS: smoke_actual_action_autosave_ui completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
