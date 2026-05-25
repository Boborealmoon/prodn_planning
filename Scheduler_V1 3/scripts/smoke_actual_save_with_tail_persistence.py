from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smoke_actual_save_only_persistence import _cleanup_fixture, _create_fixture, _schedule_block, fail, pass_msg
from scheduler_app import create_app
from scheduler_app.db import db, ensure_db, one, rows


def main():
    try:
        ensure_db()
        pass_msg("ensure_db() is idempotent")
    except Exception as exc:
        return fail(f"ensure_db() failed: {exc}")

    app = create_app()
    client = app.test_client()
    fixture = None

    try:
        fixture = _create_fixture()

        report_date = "2099-04-02"
        res1 = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={
                "apply_tail_adjustments": True,
                "daily_actuals": [
                    {
                        "report_date": report_date,
                        "target_qty": "50",
                        "output_qty": "1",
                        "reject_qty": "0",
                        "remarks": "tail smoke",
                    }
                ],
            },
        )
        if res1.status_code != 200:
            return fail(f"save returned {res1.status_code}: {res1.get_data(as_text=True)}")
        data1 = res1.get_json() or {}
        if int(data1.get("saved_count") or 0) != 1:
            return fail(f"saved_count expected 1, got {data1.get('saved_count')!r}")
        if len(data1.get("debug_actual_save", {}).get("inserted_actual_ids") or []) != 1:
            return fail("inserted_actual_ids should contain one row")

        with db() as con:
            active = one(
                con.execute(
                    """
                    SELECT *
                    FROM production_actual
                    WHERE block_id = ?
                      AND report_date = ?
                      AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
                    ORDER BY actual_id DESC
                    LIMIT 1
                    """,
                    (fixture["block_id"], report_date),
                )
            )
        if not active:
            return fail("ACTIVE production_actual row not found after tail-enabled save")
        pass_msg("tail-enabled save persisted the actual row")

        schedule = _schedule_block(client, fixture["block_id"])
        rows_in_schedule = schedule.get("actual_daily_rows") or []
        if not any(str(row.get("report_date") or "") == report_date for row in rows_in_schedule):
            return fail("schedule actual_daily_rows does not include the saved actual")
        pass_msg("schedule reflects the tail-enabled save")

        res2 = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={
                "apply_tail_adjustments": True,
                "daily_actuals": [
                    {
                        "report_date": report_date,
                        "target_qty": "50",
                        "output_qty": "2",
                        "reject_qty": "0",
                        "remarks": "tail smoke update",
                    }
                ],
            },
        )
        if res2.status_code != 200:
            return fail(f"update returned {res2.status_code}: {res2.get_data(as_text=True)}")
        data2 = res2.get_json() or {}
        if int(data2.get("saved_count") or 0) != 1:
            return fail(f"update saved_count expected 1, got {data2.get('saved_count')!r}")
        with db() as con:
            active_rows = rows(
                con.execute(
                    """
                    SELECT *
                    FROM production_actual
                    WHERE block_id = ?
                      AND report_date = ?
                      AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
                    ORDER BY actual_id DESC
                    """,
                    (fixture["block_id"], report_date),
                )
            )
        if len(active_rows) != 1 or float(active_rows[0]["output_qty"] or 0) != 2.0:
            return fail(f"tail-enabled update did not persist the corrected row: {active_rows}")
        pass_msg("tail-enabled correction persisted the updated row")

        res3 = client.post(
            f"/api/trial/blocks/{fixture['block_id']}/actual",
            json={
                "apply_tail_adjustments": True,
                "delete_actual_dates": [report_date],
            },
        )
        if res3.status_code != 200:
            return fail(f"delete returned {res3.status_code}: {res3.get_data(as_text=True)}")
        with db() as con:
            final_active = one(
                con.execute(
                    """
                    SELECT *
                    FROM production_actual
                    WHERE block_id = ?
                      AND report_date = ?
                      AND COALESCE(status, 'ACTIVE') = 'ACTIVE'
                    """,
                    (fixture["block_id"], report_date),
                )
            )
        if final_active:
            return fail("ACTIVE production_actual row still exists after tail-enabled delete")
        pass_msg("tail-enabled delete removed the active actual row")

        return 0
    except Exception as exc:
        return fail(f"smoke failed: {exc}")
    finally:
        if fixture:
            _cleanup_fixture(fixture)


if __name__ == "__main__":
    sys.exit(main())
