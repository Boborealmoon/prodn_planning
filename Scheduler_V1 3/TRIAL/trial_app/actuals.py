from __future__ import annotations

from .db import one


def actual_totals_for_block(con, block_id):
    row = one(
        con.execute(
            """
            SELECT
              COALESCE(SUM(COALESCE(output_qty, 0)), 0) AS output_qty,
              COALESCE(SUM(COALESCE(reject_qty, 0)), 0) AS reject_qty,
              SUM(CASE WHEN output_qty IS NOT NULL THEN 1 ELSE 0 END) AS output_reports,
              SUM(CASE WHEN reject_qty IS NOT NULL THEN 1 ELSE 0 END) AS reject_reports
            FROM production_actual
            WHERE block_id = ?
            """,
            (int(block_id),),
        )
    )
    return {
        "output_qty": float(row["output_qty"] or 0),
        "reject_qty": float(row["reject_qty"] or 0),
        "output_reports": int(row["output_reports"] or 0),
        "reject_reports": int(row["reject_reports"] or 0),
    }


def refresh_block_actual_status(con, block_id):
    block = one(
        con.execute(
            """
            SELECT *
            FROM run_block
            WHERE block_id = ?
            """,
            (int(block_id),),
        )
    )
    if not block:
        return

    totals = actual_totals_for_block(con, block_id)
    output_qty = totals["output_qty"]
    reject_qty = totals["reject_qty"]
    scheduled_qty = float(block["scheduled_qty"] or 0)
    effective_output_qty = max(0.0, output_qty - reject_qty)

    if totals["output_reports"] <= 0 and totals["reject_reports"] <= 0:
        status = "NOT_STARTED"
    elif effective_output_qty >= scheduled_qty:
        status = "DONE"
    else:
        status = "IN_PROGRESS"

    con.execute(
        """
        UPDATE run_block
        SET actual_good_qty = ?, actual_reject_qty = ?, execution_status = ?, status = ?, updated_at = CURRENT_TIMESTAMP
        WHERE block_id = ?
        """,
        (output_qty, reject_qty, status, status, int(block_id)),
    )
