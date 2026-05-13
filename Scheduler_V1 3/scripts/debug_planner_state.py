from __future__ import annotations

import inspect
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app.db import DB_PATH
from scheduler_app.routes.planner import api_trial_schedule


def main():
    print(f"DB path: {DB_PATH}")
    con = sqlite3.connect(DB_PATH)
    for table in ("operation", "run_block", "run_block_segment", "production_actual"):
        try:
            count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error as exc:
            print(f"{table}: ERROR {exc}")
        else:
            print(f"{table}: {count}")
    print(f"schedule endpoint module: {inspect.getsourcefile(api_trial_schedule) or 'unknown'}")
    print(f"schedule endpoint name: {api_trial_schedule.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
