from __future__ import annotations

from .db import one, parse_dt_text, rows


def actual_totals_for_block(con, block_id):
    row = one(
        con.execute(
            """
            SELECT
              COALESCE(SUM(COALESCE(output_qty, 0)), 0) AS output_qty,
              COALESCE(SUM(COALESCE(reject_qty, 0)), 0) AS reject_qty,
              COALESCE(SUM(COALESCE(output_qty, 0) - COALESCE(reject_qty, 0)), 0) AS good_qty,
              SUM(CASE WHEN output_qty IS NOT NULL THEN 1 ELSE 0 END) AS output_reports,
              SUM(CASE WHEN reject_qty IS NOT NULL THEN 1 ELSE 0 END) AS reject_reports
            FROM production_actual
            WHERE block_id = ?
              AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
            """,
            (int(block_id),),
        )
    )
    return {
        "output_qty": float(row["output_qty"] or 0),
        "reject_qty": float(row["reject_qty"] or 0),
        "good_qty": float(row["good_qty"] or 0),
        "output_reports": int(row["output_reports"] or 0),
        "reject_reports": int(row["reject_reports"] or 0),
    }


def actual_summary_for_block(con, block_id, target_qty=None):
    block_id = int(block_id or 0)
    target_qty_value = None if target_qty is None else max(0.0, float(target_qty or 0))
    if target_qty_value is None:
        block_row = one(
            con.execute(
                """
                SELECT scheduled_qty
                FROM run_block
                WHERE block_id = ?
                """,
                (block_id,),
            )
        )
        target_qty_value = max(0.0, float((block_row or {}).get("scheduled_qty") or 0))

    actual_rows = rows(
        con.execute(
            """
            SELECT a.actual_id, a.segment_id, a.block_id, a.output_qty, a.reject_qty,
                   a.reported_at, a.status,
                   COALESCE(a.report_date, s.start_datetime, a.reported_at, '') AS actual_start_at,
                   COALESCE(a.report_date, s.end_datetime, a.reported_at, '') AS actual_end_at
            FROM production_actual a
            LEFT JOIN run_block_segment s ON s.segment_id = a.segment_id
            WHERE a.block_id = ?
              AND COALESCE(a.status, 'ACTIVE') = 'ACTIVE'
              AND (a.output_qty IS NOT NULL OR a.reject_qty IS NOT NULL)
            ORDER BY COALESCE(a.report_date, s.start_datetime, a.reported_at, '') ASC,
                     COALESCE(a.report_date, s.end_datetime, a.reported_at, '') ASC,
                     a.actual_id ASC
            """,
            (block_id,),
        )
    )

    actual_start_at = ""
    actual_end_at = ""
    cumulative_good_qty = 0.0
    actual_row_count = 0
    output_qty_total = 0.0
    reject_qty_total = 0.0
    for row in actual_rows:
        start_text = str(row["actual_start_at"] or row["reported_at"] or "").strip()
        end_text = str(row["actual_end_at"] or row["reported_at"] or "").strip()
        start_dt = parse_dt_text(start_text)
        end_dt = parse_dt_text(end_text)
        output_qty = max(0.0, float(row["output_qty"] or 0))
        reject_qty = max(0.0, float(row["reject_qty"] or 0))
        good_qty = max(0.0, output_qty - reject_qty)
        if output_qty <= 0 and reject_qty <= 0 and not start_dt and not end_dt:
            continue
        actual_row_count += 1
        output_qty_total += output_qty
        reject_qty_total += reject_qty
        if not actual_start_at:
            chosen_start = start_dt or end_dt
            actual_start_at = chosen_start.strftime("%Y-%m-%d %H:%M:%S") if chosen_start else start_text
        cumulative_good_qty += good_qty
        if not actual_end_at and target_qty_value > 0 and cumulative_good_qty + 1e-9 >= target_qty_value:
            chosen_end = end_dt or start_dt
            actual_end_at = chosen_end.strftime("%Y-%m-%d %H:%M:%S") if chosen_end else end_text

    return {
        "actual_start_at": actual_start_at,
        "actual_end_at": actual_end_at,
        "actual_good_qty": cumulative_good_qty,
        "actual_row_count": actual_row_count,
        "planned_qty": target_qty_value,
        "output_qty": output_qty_total,
        "reject_qty": reject_qty_total,
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
    good_qty = totals["good_qty"]
    scheduled_qty = float(block["scheduled_qty"] or 0)

    if totals["output_reports"] <= 0 and totals["reject_reports"] <= 0:
        status = "NOT_STARTED"
    elif good_qty >= scheduled_qty and scheduled_qty > 0:
        status = "DONE"
    else:
        status = "IN_PROGRESS"

    con.execute(
        """
        UPDATE run_block
        SET actual_good_qty = ?, actual_reject_qty = ?, execution_status = ?, status = ?, updated_at = CURRENT_TIMESTAMP
        WHERE block_id = ?
        """,
        (good_qty, reject_qty, status, status, int(block_id)),
    )
