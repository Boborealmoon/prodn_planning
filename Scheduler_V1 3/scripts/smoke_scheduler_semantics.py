from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app.blocks import recalculate_all, recalculate_machine, trial_block_payload, trial_block_row
from scheduler_app.actuals import actual_totals_for_block
from scheduler_app.db import db, ensure_db, one, parse_dt_text


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


@contextmanager
def savepoint(con, name):
    con.execute(f"SAVEPOINT {name}")
    try:
        yield
    finally:
        con.execute(f"ROLLBACK TO {name}")
        con.execute(f"RELEASE {name}")


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
        block = one(
            con.execute(
                """
                SELECT block_id, machine_id
                FROM run_block
                WHERE COALESCE(active, 1) = 1
                ORDER BY block_id
                LIMIT 1
                """
            )
        )
        if not block:
            return fail("no active run_block rows found")

        schedule_block = one(
            con.execute(
                """
                SELECT b.block_id, b.machine_id
                FROM run_block b
                LEFT JOIN production_actual a
                  ON a.block_id = b.block_id
                 AND COALESCE(a.status, 'ACTIVE') = 'ACTIVE'
                WHERE COALESCE(b.active, 1) = 1
                  AND a.actual_id IS NULL
                ORDER BY b.block_id
                LIMIT 1
                """
            )
        )
        if not schedule_block:
            return fail("no active block without actual history found for scheduling smoke")

        block_id = int(block["block_id"])
        machine_id = int(block["machine_id"] or 0)
        schedule_block_id = int(schedule_block["block_id"])
        schedule_machine_id = int(schedule_block["machine_id"] or 0)

        try:
            with savepoint(con, "smoke_planned_start"):
                planned_start = "2099-01-02 08:30:00"
                con.execute(
                    """
                    UPDATE run_block
                    SET planned_start_at = ?, allow_pull_forward = 0
                    WHERE block_id = ?
                    """,
                    (planned_start, schedule_block_id),
                )
                recalculate_machine(con, schedule_machine_id)
                updated = trial_block_row(con, schedule_block_id)
                if not updated:
                    return fail("trial_block_row missing after recalc_machine()")
                actual_start = parse_dt_text(updated["calculated_start_datetime"])
                planned_start_dt = parse_dt_text(updated["planned_start_at"])
                if not planned_start_dt or not actual_start or actual_start < planned_start_dt:
                    return fail("planned_start_at was overwritten or pull-forward was not clamped")
            pass_msg("planned_start_at is preserved and pull-forward is clamped")
        except Exception as exc:
            return fail(f"planned_start_at smoke failed: {exc}")

        try:
            with savepoint(con, "smoke_queue_position"):
                con.execute(
                    """
                    UPDATE run_block
                    SET queue_position = ?
                    WHERE block_id = ?
                    """,
                    (12.5, block_id),
                )
                payload = trial_block_payload(trial_block_row(con, block_id), con)
                if float(payload.get("queue_position") or 0) != 12.5:
                    return fail("queue_position did not preserve decimal precision")
            pass_msg("queue_position decimal survives payloads")
        except Exception as exc:
            return fail(f"queue_position smoke failed: {exc}")

        try:
            with savepoint(con, "smoke_active_filter"):
                con.execute(
                    """
                    UPDATE run_block
                    SET active = 0
                    WHERE block_id = ?
                    """,
                    (block_id,),
                )
                active_count = one(
                    con.execute(
                        """
                        SELECT COUNT(*) AS c
                        FROM run_block
                        WHERE block_id = ?
                          AND COALESCE(active, 1) = 1
                        """,
                        (block_id,),
                    )
                )
                if int(active_count["c"] or 0) != 0:
                    return fail("inactive block still appears in active query")
            pass_msg("inactive run_block rows are excluded from active queries")
        except Exception as exc:
            return fail(f"active filter smoke failed: {exc}")

        try:
            with savepoint(con, "smoke_voided_view"):
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
                con.execute(
                    """
                    INSERT INTO production_actual (
                      block_id, machine_id, report_date, remarks, reported_at,
                      output_qty, reject_qty, target_qty_at_report, status, entry_type, created_by
                    ) VALUES (?, ?, date('now'), 'semantics-smoke', CURRENT_TIMESTAMP, ?, ?, ?, 'VOIDED', 'VOID', '')
                    """,
                    (block_id, machine_id, 9.0, 2.0, 0.0),
                )
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
                if _qty_tuple(baseline_view) != _qty_tuple(after_view):
                    return fail("VOIDED rows affected v_block_actual_totals")
            pass_msg("VOIDED actual rows do not affect v_block_actual_totals")
        except Exception as exc:
            return fail(f"VOIDED view smoke failed: {exc}")

        try:
            with savepoint(con, "smoke_segment_actual_edit"):
                seg_actual = one(
                    con.execute(
                        """
                        SELECT a.actual_id, a.segment_id, a.block_id, a.report_date, a.remarks, a.target_qty_at_report,
                               a.output_qty, a.reject_qty, COALESCE(a.machine_id, b.machine_id) AS machine_id
                        FROM production_actual a
                        JOIN run_block b ON b.block_id = a.block_id
                        WHERE segment_id IS NOT NULL
                          AND COALESCE(a.status, 'ACTIVE') = 'ACTIVE'
                        ORDER BY actual_id
                        LIMIT 1
                        """
                    )
                )
                if not seg_actual:
                    return fail("no segment-backed actual found for edit smoke")
                con.execute(
                    "UPDATE production_actual SET status = 'VOIDED' WHERE actual_id = ?",
                    (int(seg_actual["actual_id"]),),
                )
                con.execute(
                    """
                    INSERT INTO production_actual (
                      segment_id, block_id, machine_id, report_date, remarks, reported_at,
                      output_qty, reject_qty, target_qty_at_report, status, entry_type,
                      correction_of_actual_id, good_qty_at_report, created_by
                    ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, 'ACTIVE', 'CORRECTION', ?, ?, '')
                    """,
                    (
                        int(seg_actual["segment_id"]),
                        int(seg_actual["block_id"]),
                        int(seg_actual["machine_id"] or 0),
                        str(seg_actual["report_date"] or ""),
                        str(seg_actual["remarks"] or ""),
                        seg_actual["output_qty"],
                        seg_actual["reject_qty"],
                        seg_actual["target_qty_at_report"],
                        int(seg_actual["actual_id"]),
                        None if seg_actual["output_qty"] is None or seg_actual["reject_qty"] is None else max(0.0, float(seg_actual["output_qty"] or 0) - float(seg_actual["reject_qty"] or 0)),
                    ),
                )
            pass_msg("segment actuals can be edited via append/void correction")
        except Exception as exc:
            return fail(f"segment actual edit smoke failed: {exc}")

        try:
            with savepoint(con, "smoke_recalc_states"):
                recalculate_all(con)
                schedule_run = one(con.execute("SELECT COUNT(*) AS c FROM schedule_run"))
                if int(schedule_run["c"] or 0) <= 0:
                    return fail("schedule_run missing after recalculate_all()")
                mq = one(con.execute("SELECT COUNT(*) AS c FROM machine_queue_state"))
                if int(mq["c"] or 0) <= 0:
                    return fail("machine_queue_state missing after recalculate_all()")
            pass_msg("recalculate_all() refreshes state tables")
        except Exception as exc:
            return fail(f"state refresh smoke failed: {exc}")

    pass_msg("scheduler semantics smoke completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
