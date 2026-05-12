from __future__ import annotations

import sqlite3
from pathlib import Path


DB_PATH = Path(__file__).resolve().parents[1] / "TRIAL" / "trial.db"


TABLES = [
    "operation",
    "run_block",
    "run_block_segment",
    "production_actual",
    "planning_card",
    "planning_card_operation",
    "trial_operation",
    "trial_run_block",
    "trial_run_block_segment",
    "trial_production_actual",
]


def count_rows(con: sqlite3.Connection, table: str) -> int:
    try:
        return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)
    except sqlite3.Error as exc:
        print(f"{table}: ERROR ({exc})")
        return -1


def main() -> int:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        print(f"Database: {DB_PATH}")
        print("Row counts:")
        for table in TABLES:
            print(f"  {table}: {count_rows(con, table)}")

        print("\nExpected planner sources:")
        print("  run_block -> machine schedule and block metadata")
        print("  run_block_segment -> visible Gantt/planner segments")
        print("  production_actual -> block actuals")
        print("  planning_card / planning_card_operation -> combined planner cards")
        print("  operation -> operation metadata")

        print("\nSQLite object types:")
        for name in TABLES:
            row = con.execute(
                "SELECT type, name FROM sqlite_master WHERE name = ?",
                (name,),
            ).fetchone()
            if row:
                print(f"  {name}: {row['type']}")
            else:
                print(f"  {name}: missing")

        shadowed = []
        for legacy, modern in (
            ("trial_run_block", "run_block"),
            ("trial_run_block_segment", "run_block_segment"),
            ("trial_production_actual", "production_actual"),
            ("trial_operation", "operation"),
        ):
            legacy_row = con.execute("SELECT type FROM sqlite_master WHERE name = ?", (legacy,)).fetchone()
            modern_row = con.execute("SELECT type FROM sqlite_master WHERE name = ?", (modern,)).fetchone()
            if legacy_row and modern_row and legacy_row["type"] == "table" and modern_row["type"] == "table":
                shadowed.append(legacy)

        print("\nShadowing check:")
        if shadowed:
            print("  Old physical tables still exist alongside the new tables:")
            for name in shadowed:
                print(f"    - {name}")
            print("  Planner code should ignore these legacy tables and read the new tables directly.")
        else:
            print("  No legacy table shadowing detected for the main planner paths.")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
