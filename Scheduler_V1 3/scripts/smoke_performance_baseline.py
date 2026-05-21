from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app import create_app
from scheduler_app.blocks import recalculate_all
from scheduler_app.db import db
from scheduler_app.planning_scheduler import recalculate_planning_all


def main():
    started = time.perf_counter()
    app = create_app()
    create_app_secs = time.perf_counter() - started
    print(f"PASS: create_app completed in {create_app_secs:.3f}s")

    client = app.test_client()

    started = time.perf_counter()
    res = client.get("/planner")
    planner_page_secs = time.perf_counter() - started
    print(f"PASS: /planner completed in {planner_page_secs:.3f}s ({res.status_code})")

    started = time.perf_counter()
    res = client.get("/actual-production")
    actual_page_secs = time.perf_counter() - started
    print(f"PASS: /actual-production completed in {actual_page_secs:.3f}s ({res.status_code})")

    started = time.perf_counter()
    res = client.get("/api/trial/planner/schedule")
    planner_api_secs = time.perf_counter() - started
    print(f"PASS: /api/trial/planner/schedule completed in {planner_api_secs:.3f}s ({res.status_code})")

    started = time.perf_counter()
    res = client.get("/api/trial/schedule")
    actual_api_secs = time.perf_counter() - started
    print(f"PASS: /api/trial/schedule completed in {actual_api_secs:.3f}s ({res.status_code})")

    with db() as con:
        started = time.perf_counter()
        recalculate_planning_all(con, reason="SMOKE_PERF")
        planning_recalc_secs = time.perf_counter() - started
        print(f"PASS: recalculate_planning_all completed in {planning_recalc_secs:.3f}s")

    with db() as con:
        started = time.perf_counter()
        recalculate_all(con)
        live_recalc_secs = time.perf_counter() - started
        print(f"PASS: recalculate_all completed in {live_recalc_secs:.3f}s")

    print("PASS: smoke_performance_baseline completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
