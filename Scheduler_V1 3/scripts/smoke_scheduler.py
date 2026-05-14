from __future__ import annotations

import sys
from pathlib import Path
from datetime import date

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app.actuals import actual_totals_for_block
from scheduler_app.blocks import recalculate_all
from scheduler_app.db import db, ensure_db, one


TABLES = [
    "schedule_run",
    "machine_queue_state",
    "process_sheet_operation_state",
    "process_sheet_state",
    "schedule_alert",
    "machine_calendar_window",
    "rework_link",
]


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


def _qty_tuple(row):
    if not row:
        return (0.0, 0.0, 0.0)
    return (
        float(row["output_qty"] or 0),
        float(row["reject_qty"] or 0),
        float(row["good_qty"] or 0),
    )


def main():
    try:
        ensure_db()
        ensure_db()
        pass_msg("ensure_db() is idempotent")
    except Exception as exc:
        return fail(f"ensure_db() failed: {exc}")

    with db() as con:
        try:
            recalculate_all(con)
            pass_msg("recalculate_all() completed")
        except Exception as exc:
            return fail(f"recalculate_all() failed: {exc}")

        for table in TABLES:
            exists = one(con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)))
            if not exists:
                return fail(f"missing table: {table}")
        pass_msg("scheduler tables exist")

        schedule_run = one(con.execute("SELECT COUNT(*) AS c FROM schedule_run"))
        if int(schedule_run["c"] or 0) <= 0:
            return fail("schedule_run has no rows after recalculation")
        pass_msg("schedule_run has rows")

        mq = one(con.execute("SELECT COUNT(*) AS c FROM machine_queue_state"))
        if int(mq["c"] or 0) <= 0:
            return fail("machine_queue_state is empty")
        pass_msg("machine_queue_state is queryable")

        block = one(con.execute("SELECT block_id, machine_id FROM run_block WHERE COALESCE(active, 1) = 1 ORDER BY block_id LIMIT 1"))
        if not block:
            return fail("no active run_block rows found for actuals smoke test")

        block_id = int(block["block_id"])
        baseline = actual_totals_for_block(con, block_id)
        baseline_view = one(
            con.execute(
                """
                SELECT output_qty, reject_qty, good_qty
                FROM v_block_actual_totals
                WHERE block_id = ?
                """,
                (block_id,),
            )
        )
        baseline_api = one(
            con.execute(
                """
                SELECT
                    block_id,
                    COALESCE(SUM(COALESCE(output_qty, 0)), 0) AS output_qty,
                    COALESCE(SUM(COALESCE(reject_qty, 0)), 0) AS reject_qty,
                    COALESCE(SUM(COALESCE(output_qty, 0) - COALESCE(reject_qty, 0)), 0) AS good_qty
                FROM production_actual
                WHERE block_id = ?
                  AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
                GROUP BY block_id
                """,
                (block_id,),
            )
        )
        con.execute("SAVEPOINT smoke_voided_actual")
        con.execute(
            """
            INSERT INTO production_actual (
              block_id, machine_id, report_date, remarks, reported_at,
              output_qty, reject_qty, target_qty_at_report, status, entry_type, created_by
            ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, 'VOIDED', 'VOID', '')
            """,
            (
                block_id,
                int(block["machine_id"] or 0),
                date.today().isoformat(),
                "smoke",
                123.0,
                45.0,
                0.0,
            ),
        )
        after = actual_totals_for_block(con, block_id)
        after_view = one(
            con.execute(
                """
                SELECT output_qty, reject_qty, good_qty
                FROM v_block_actual_totals
                WHERE block_id = ?
                """,
                (block_id,),
            )
        )
        after_api = one(
            con.execute(
                """
                SELECT
                    block_id,
                    COALESCE(SUM(COALESCE(output_qty, 0)), 0) AS output_qty,
                    COALESCE(SUM(COALESCE(reject_qty, 0)), 0) AS reject_qty,
                    COALESCE(SUM(COALESCE(output_qty, 0) - COALESCE(reject_qty, 0)), 0) AS good_qty
                FROM production_actual
                WHERE block_id = ?
                  AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
                GROUP BY block_id
                """,
                (block_id,),
            )
        )
        con.execute("ROLLBACK TO smoke_voided_actual")
        con.execute("RELEASE smoke_voided_actual")
        if baseline != after:
            return fail("VOIDED actual rows affected totals")
        pass_msg("VOIDED actual rows are ignored")
        if _qty_tuple(baseline_view) != _qty_tuple(after_view):
            return fail("v_block_actual_totals changed after VOIDED insert")
        pass_msg("v_block_actual_totals ignores VOIDED rows")
        if _qty_tuple(baseline_api) != _qty_tuple(after_api):
            return fail("API-style aggregation changed after VOIDED insert")
        pass_msg("API-style actual aggregation ignores VOIDED rows")

    pass_msg("smoke checks completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
