from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app import create_app
from scheduler_app.db import db, ensure_db, one
from scheduler_app.planner_actuals import actual_summary_for_block_row, actual_summary_for_process_sheet_rows
from scheduler_app.planning_settings import set_planning_setting


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


def main():
    try:
        ensure_db()
        ensure_db()
        pass_msg("ensure_db() is idempotent")
    except Exception as exc:
        return fail(f"ensure_db() failed: {exc}")

    app = create_app()
    client = app.test_client()

    with db() as con:
        original_row = one(
            con.execute(
                """
                SELECT setting_value
                FROM planning_setting
                WHERE setting_key = 'planning_efficiency'
                """
            )
        )
        original_exists = original_row is not None
        original_value = str(original_row["setting_value"]) if original_row else "0.85"

    try:
        res = client.get("/api/trial/planner/settings")
        if res.status_code != 200:
            return fail(f"GET /api/trial/planner/settings returned {res.status_code}")
        data = res.get_json() or {}
        if "planning_efficiency" not in (data.get("settings") or {}):
            return fail("planner settings response missing planning_efficiency")
        pass_msg("planner settings endpoint returns planning_efficiency")

        res = client.patch("/api/trial/planner/settings", json={"planning_efficiency": 0.85})
        if res.status_code != 200:
            return fail(f"PATCH /api/trial/planner/settings returned {res.status_code}")
        data = res.get_json() or {}
        if abs(float((data.get("settings") or {}).get("planning_efficiency") or 0) - 0.85) > 1e-9:
            return fail("planner settings PATCH did not persist 0.85")
        pass_msg("planner settings PATCH can set planning_efficiency to 0.85")

        res = client.post("/api/trial/planner/recalculate", json={"reason": "SMOKE_PLANNER_API"})
        if res.status_code != 200:
            return fail(f"POST /api/trial/planner/recalculate returned {res.status_code}")
        data = res.get_json() or {}
        planning_run_id = int(data.get("planning_run_id") or 0)
        if not planning_run_id:
            return fail("planner recalculate did not return planning_run_id")
        pass_msg("planner recalculate returns planning_run_id")

        res = client.get("/api/trial/planner/schedule")
        if res.status_code != 200:
            return fail(f"GET /api/trial/planner/schedule returned {res.status_code}")
        data = res.get_json() or {}
        for key in ("planning_run", "settings", "machines", "blocks", "segments", "process_sheets", "catalog", "planned", "planning_cards"):
            if key not in data:
                return fail(f"planner schedule missing key: {key}")
        blocks = data.get("blocks") or []
        process_sheets = data.get("process_sheets") or []
        catalog = data.get("catalog") or []
        planned = data.get("planned") or []
        if not blocks:
            return fail("planner schedule returned no blocks")
        if not process_sheets:
            return fail("planner schedule returned no process sheets")
        if not isinstance(catalog, list) or not isinstance(planned, list):
            return fail("planner schedule catalog/planned payloads must be lists")
        sidebar_sample = next((item for item in catalog + planned + process_sheets if item), None)
        if not sidebar_sample:
            return fail("planner schedule returned no sidebar sample item")
        for field in ("source_ps_id", "partial_no", "pp_partial_no", "part_no", "part_desc", "total_qty", "partial_qty", "opn_count", "active_opn_count", "completed_opn_count", "is_completed", "completed_at", "execution_label", "selected_bom_id", "default_bom_id", "bom_options"):
            if field not in sidebar_sample:
                return fail(f"planner sidebar item missing {field}")
        if not str(sidebar_sample.get("source_ps_id") or "").strip():
            return fail("planner sidebar item source_ps_id is empty")
        if not isinstance(sidebar_sample.get("opn_count"), int):
            return fail("planner sidebar item opn_count must be numeric")
        if not isinstance(sidebar_sample.get("active_opn_count"), int):
            return fail("planner sidebar item active_opn_count must be numeric")
        if not isinstance(sidebar_sample.get("completed_opn_count"), int):
            return fail("planner sidebar item completed_opn_count must be numeric")
        if not isinstance(sidebar_sample.get("total_qty"), (int, float)):
            return fail("planner sidebar item total_qty must be numeric")
        if not isinstance(sidebar_sample.get("partial_qty"), (int, float)):
            return fail("planner sidebar item partial_qty must be numeric")
        if not isinstance(sidebar_sample.get("bom_options"), list):
            return fail("planner sidebar item bom_options must be a list")
        sample_cards = sidebar_sample.get("op_cards") or []
        sample_op_card = next((card for card in sample_cards if card), None)
        if not sample_op_card:
            return fail("planner sidebar item missing op_cards")
        for field in ("operation_id", "source_ps_id", "source_op_seq_id", "source_op_no", "opn_label", "is_completed", "completed_at", "execution_label"):
            if field not in sample_op_card:
                return fail(f"planner op card missing {field}")
        for block in blocks:
            for field in ("actual_start_at", "actual_end_at", "actual_good_qty", "actual_row_count"):
                if field not in block:
                    return fail(f"planner block payload missing {field}")
        for ps in process_sheets:
            for field in ("actual_start_at", "actual_end_at", "actual_good_qty", "actual_block_count", "source_ps_id", "partial_no", "pp_partial_no", "part_no", "part_desc", "total_qty", "partial_qty", "opn_count", "active_opn_count", "completed_opn_count", "is_completed", "completed_at", "execution_label", "selected_bom_id", "default_bom_id", "bom_options"):
                if field not in ps:
                    return fail(f"planner PS payload missing {field}")
        if not any((item.get("op_cards") or []) for item in catalog + planned):
            return fail("planner schedule catalog/planned payloads missing op cards")
        if len({str(ps.get("ps_id") or "").strip() for ps in process_sheets}) != len(process_sheets):
            return fail("planner PS payload contains duplicate ps_id values")
        identity_a = {"ps_id": "A-1", "source_ps_id": "SRC-1", "pp_partial_no": "1", "part_no": "P-1"}
        identity_b = {"ps_id": "A-2", "source_ps_id": "SRC-1", "pp_partial_no": "2", "part_no": "P-1"}
        identity_c = {"ps_id": "", "source_ps_id": "", "pp_partial_no": "3", "part_no": "P-9", "job_no": "JOB-9"}
        def planner_key_py(item):
            direct = str(item.get("ps_id") or item.get("process_sheet_id") or item.get("id") or "").strip()
            if direct:
                return direct
            source = str(item.get("source_ps_id") or item.get("job_no") or "").strip()
            partial = str(item.get("pp_partial_no") or item.get("partial_no") or "").strip()
            part_no = str(item.get("part_no") or "").strip()
            job_no = str(item.get("job_no") or "").strip()
            return "::".join([value for value in (source, partial, part_no, job_no) if value]) or source
        if planner_key_py(identity_a) == planner_key_py(identity_b):
            return fail("planner identity helper should distinguish rows with the same source_ps_id")
        if not planner_key_py(identity_c):
            return fail("planner identity helper should synthesize a fallback key when ids are missing")
        pass_msg("planner schedule returns actual comparison fields")

        segments = data.get("segments") or []
        for seg in segments:
            seg_date = str(seg.get("segment_date") or "")
            if seg_date:
                weekday = datetime.fromisoformat(f"{seg_date} 00:00:00").date().weekday()
                if weekday >= 5:
                    return fail(f"planner schedule contains weekend segment: {seg_date}")
        pass_msg("planner schedule returns expected keys and no weekend segments")

        if blocks and process_sheets:
            pass_msg("planner schedule is populated without requiring actuals")

        with db() as con:
            block_row = one(
                con.execute(
                    """
                    SELECT b.*
                    FROM run_block b
                    LEFT JOIN production_actual a
                      ON a.block_id = b.block_id
                     AND COALESCE(a.status, 'ACTIVE') = 'ACTIVE'
                    WHERE COALESCE(b.active, 1) = 1
                      AND COALESCE(b.scheduled_qty, 0) >= 2
                      AND a.actual_id IS NULL
                    ORDER BY b.block_id
                    LIMIT 1
                    """
                )
            )
            if not block_row:
                return fail("no clean block found for actual comparison smoke")

            block_dict = dict(block_row)
            baseline_summary = actual_summary_for_block_row(con, block_dict)
            if baseline_summary["actual_start_at"] or baseline_summary["actual_end_at"]:
                return fail("no-actuals block should have empty actual start/end")
            pass_msg("no actuals means actual_start_at and actual_end_at are empty")

            scheduled_qty = float(block_dict.get("scheduled_qty") or 0)
            if scheduled_qty < 2:
                return fail("test block scheduled_qty must be at least 2")

            with savepoint(con, "planner_actual_smoke"):
                con.execute(
                    """
                    INSERT INTO production_actual (
                      block_id, machine_id, report_date, remarks, reported_at,
                      output_qty, reject_qty, target_qty_at_report, status, entry_type, created_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'VOIDED', 'VOID', '')
                    """,
                    (
                        int(block_dict["block_id"]),
                        int(block_dict["machine_id"] or 0),
                        "2099-01-01",
                        "voided-smoke",
                        "2099-01-01 08:00:00",
                        99.0,
                        0.0,
                        scheduled_qty,
                    ),
                )
                voided_summary = actual_summary_for_block_row(con, block_dict)
                if voided_summary["actual_start_at"] or voided_summary["actual_end_at"]:
                    return fail("VOIDED actuals should be ignored")
                pass_msg("VOIDED actuals are ignored")

                con.execute(
                    """
                    INSERT INTO production_actual (
                      block_id, machine_id, report_date, remarks, reported_at,
                      output_qty, reject_qty, target_qty_at_report, status, entry_type, created_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 'REPORT', '')
                    """,
                    (
                        int(block_dict["block_id"]),
                        int(block_dict["machine_id"] or 0),
                        "2099-01-01",
                        "active-smoke-1",
                        "2099-01-01 08:30:00",
                        1.0,
                        0.0,
                        scheduled_qty,
                    ),
                )
                partial_summary = actual_summary_for_block_row(con, block_dict)
                if not partial_summary["actual_start_at"]:
                    return fail("first ACTIVE actual did not populate actual_start_at")
                if partial_summary["actual_end_at"]:
                    return fail("incomplete ACTIVE actual should keep actual_end_at empty")
                pass_msg("first actual populates actual_start_at and keeps actual_end_at empty")

                con.execute(
                    """
                    INSERT INTO production_actual (
                      block_id, machine_id, report_date, remarks, reported_at,
                      output_qty, reject_qty, target_qty_at_report, status, entry_type, created_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 'REPORT', '')
                    """,
                    (
                        int(block_dict["block_id"]),
                        int(block_dict["machine_id"] or 0),
                        "2099-01-01",
                        "active-smoke-2",
                        "2099-01-01 09:30:00",
                        max(0.0, scheduled_qty - 1.0),
                        0.0,
                        scheduled_qty,
                    ),
                )
                complete_summary = actual_summary_for_block_row(con, block_dict)
                if not complete_summary["actual_end_at"]:
                    return fail("cumulative good qty did not populate actual_end_at")
                pass_msg("cumulative good_qty >= planned qty populates actual_end_at")

                ps_empty = actual_summary_for_process_sheet_rows(
                    [
                        {
                            "planned_qty": 10,
                            "actual_start_at": complete_summary["actual_start_at"],
                            "actual_end_at": complete_summary["actual_end_at"],
                            "actual_good_qty": complete_summary["actual_good_qty"],
                        },
                        {
                            "planned_qty": 1,
                            "actual_start_at": "",
                            "actual_end_at": "",
                            "actual_good_qty": 0,
                        },
                    ]
                )
                if ps_empty["actual_end_at"]:
                    return fail("PS should stay open until all operations are complete")
                ps_complete = actual_summary_for_process_sheet_rows(
                    [
                        {
                            "planned_qty": 10,
                            "actual_start_at": complete_summary["actual_start_at"],
                            "actual_end_at": complete_summary["actual_end_at"],
                            "actual_good_qty": complete_summary["actual_good_qty"],
                        },
                        {
                            "planned_qty": 1,
                            "actual_start_at": "2099-01-01 10:00:00",
                            "actual_end_at": "2099-01-01 10:15:00",
                            "actual_good_qty": 1,
                        },
                    ]
                )
                if not ps_complete["actual_end_at"]:
                    return fail("PS actual_end_at should appear once all operations are complete")
                pass_msg("process sheet actual aggregation behaves as expected")

    finally:
        with db() as con:
            if original_exists:
                set_planning_setting(con, "planning_efficiency", original_value)
            else:
                con.execute(
                    "DELETE FROM planning_setting WHERE setting_key = 'planning_efficiency'"
                )

    pass_msg("smoke_planner_api completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
