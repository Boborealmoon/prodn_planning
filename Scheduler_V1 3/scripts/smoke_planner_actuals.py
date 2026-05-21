from __future__ import annotations

import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app import create_app
from scheduler_app.db import db, ensure_db, one, rows
from scheduler_app.planning_scheduler import recalculate_planning_all as recalculate_planning_all_baseline


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


def _get_schedule(client):
    res = client.get("/api/trial/planner/schedule")
    if res.status_code != 200:
        raise RuntimeError(f"GET /api/trial/planner/schedule returned {res.status_code}")
    return res.get_json() or {}


def _find_block(data, block_id):
    for block in data.get("blocks") or []:
        if int(block.get("block_id") or 0) == int(block_id):
            return block
    return None


def _find_ps(data, ps_id):
    for ps in data.get("process_sheets") or []:
        if str(ps.get("ps_id") or "") == str(ps_id):
            return ps
    return None


def _create_fixture():
    token = uuid.uuid4().hex[:8]
    temp_ps_id = f"PLAN-ACTUAL-{token}::1"
    temp_part_no = f"PLAN-ACTUAL-PART-{token}"
    temp_bom_code = f"PLAN-ACTUAL-BOM-{token}"
    temp_job_no = f"PA-{token.upper()}"

    with db() as con:
        machine = one(
            con.execute(
                """
                SELECT *
                FROM machines
                WHERE active = 1
                ORDER BY machine_id
                LIMIT 1
                """
            )
        )
        if not machine:
            raise RuntimeError("no active machine found for planner actual smoke")

        part = one(
            con.execute(
                """
                INSERT INTO parts (part_no, part_desc)
                VALUES (?, ?)
                RETURNING part_id
                """,
                (temp_part_no, f"Planner actual smoke part {token}"),
            )
        )
        bom = one(
            con.execute(
                """
                INSERT INTO bom_variation (part_id, bom_code, bom_desc, is_default)
                VALUES (?, ?, ?, 1)
                RETURNING bom_id
                """,
                (int(part["part_id"]), temp_bom_code, f"Planner actual smoke bom {token}"),
            )
        )

        seq_rows = []
        for seq_no, op_no, op_type, cycle_time, setup_time in [
            (10, "10", "CUT", 10, 30),
            (20, "20", "PACK", 8, 20),
            (30, "30", "SHIP", 6, 15),
        ]:
            seq_rows.append(
                one(
                    con.execute(
                        """
                        INSERT INTO operation_seq (
                          bom_id, seq_no, op_no, op_type, machine_category, cycle_time, setup_time, preferred_machine, is_last_op
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        RETURNING op_seq_id
                        """,
                        (
                            int(bom["bom_id"]),
                            seq_no,
                            op_no,
                            op_type,
                            str(machine["machine_category"] or "PLAN"),
                            cycle_time,
                            setup_time,
                            str(machine["machine_code"] or ""),
                            1 if seq_no == 30 else 0,
                        ),
                    )
                )
            )

        con.execute(
            """
            INSERT INTO process_sheet (
              ps_id, part_id, part_no, part_desc, order_date, due_date, total_qty, planned_qty,
              finished_qty, selected_bom_id, planner_status, status, source_ps_id, pp_partial_no
            ) VALUES (?, ?, ?, ?, date('now'), date('now', '+14 day'), ?, 0, 0, ?, 'UNPLANNED', 'ACTIVE', ?, '1')
            """,
            (
                temp_ps_id,
                int(part["part_id"]),
                temp_part_no,
                f"Planner actual smoke part {token}",
                18,
                int(bom["bom_id"]),
                temp_ps_id,
            ),
        )

        block_specs = [
            (seq_rows[0]["op_seq_id"], 3, 10, 3),
            (seq_rows[1]["op_seq_id"], 4, 20, 4),
            (seq_rows[2]["op_seq_id"], 12, 30, 12),
        ]
        for idx, (op_seq_id, scheduled_qty, queue_position, total_qty) in enumerate(block_specs, start=1):
            op_row = one(
                con.execute(
                    """
                    INSERT INTO operation (
                      job_no, operation_name, total_qty, setup_minutes, cycle_minutes_per_qty, compatible_machine_group,
                      source_ps_id, source_op_seq_id, source_op_no, status, remarks, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', '', CURRENT_TIMESTAMP)
                    RETURNING operation_id
                    """,
                    (
                        temp_job_no,
                        f"OP{idx * 10}",
                        total_qty,
                        20 if idx < 3 else 15,
                        10 if idx == 1 else (8 if idx == 2 else 6),
                        str(machine["machine_category"] or "PLAN"),
                        temp_ps_id,
                        int(op_seq_id),
                        str(idx * 10),
                    ),
                )
            )
            con.execute(
                """
                INSERT INTO run_block (
                  operation_id, machine_id, queue_position, scheduled_qty, include_setup, status, planning_status,
                  execution_status, anchor_datetime, planned_start_at, planned_end_at, allow_pull_forward,
                  active, is_fresh_monday_item, calculated_start_datetime, calculated_end_datetime,
                  actual_good_qty, actual_reject_qty, remarks, updated_at
                ) VALUES (?, ?, ?, ?, 1, 'PLANNED', 'PLANNED', 'NOT_STARTED', '', '', '', 1, 1, 0, '', '', 0, 0, '', CURRENT_TIMESTAMP)
                """,
                (
                    int(op_row["operation_id"]),
                    int(machine["machine_id"]),
                    float(queue_position),
                    float(scheduled_qty),
                ),
            )

        recalculate_planning_all_baseline(con, reason="SMOKE_PLANNER_ACTUALS")
    return {
        "ps_id": temp_ps_id,
        "part_no": temp_part_no,
        "bom_code": temp_bom_code,
        "job_no": temp_job_no,
    }


def _cleanup_fixture(fixture):
    ps_id = fixture["ps_id"]
    with db() as con:
        block_ids = [
            row["block_id"]
            for row in rows(
                con.execute(
                    """
                    SELECT b.block_id
                    FROM run_block b
                    JOIN operation o ON o.operation_id = b.operation_id
                    WHERE COALESCE(o.source_ps_id, '') = ?
                    ORDER BY b.block_id
                    """,
                    (ps_id,),
                )
            )
        ]
        if block_ids:
            q = ",".join("?" for _ in block_ids)
            con.execute(f"DELETE FROM production_actual WHERE block_id IN ({q})", block_ids)
            con.execute(f"DELETE FROM run_block WHERE block_id IN ({q})", block_ids)
        con.execute("DELETE FROM operation WHERE source_ps_id = ?", (ps_id,))
        con.execute("DELETE FROM operation_seq WHERE bom_id IN (SELECT bom_id FROM bom_variation WHERE bom_code = ?)", (fixture["bom_code"],))
        con.execute("DELETE FROM process_sheet WHERE ps_id = ?", (ps_id,))
        con.execute("DELETE FROM bom_variation WHERE bom_code = ?", (fixture["bom_code"],))
        con.execute("DELETE FROM parts WHERE part_no = ?", (fixture["part_no"],))


def main():
    try:
        ensure_db()
        ensure_db()
        pass_msg("ensure_db() is idempotent")
    except Exception as exc:
        return fail(f"ensure_db() failed: {exc}")

    fixture = None
    try:
        fixture = _create_fixture()
        app = create_app()
        client = app.test_client()

        data = _get_schedule(client)
        block_ids = [block["block_id"] for block in data.get("blocks") or [] if str(block.get("source_ps_id") or "") == fixture["ps_id"]]
        if len(block_ids) < 3:
            return fail("fixture blocks missing from planner schedule")
        block_ids = sorted(int(b) for b in block_ids)
        block1, block2, block3 = block_ids[:3]

        block1_payload = _find_block(data, block1)
        block2_payload = _find_block(data, block2)
        block3_payload = _find_block(data, block3)
        ps_payload = _find_ps(data, fixture["ps_id"])
        if not block1_payload or not block2_payload or not block3_payload or not ps_payload:
            return fail("fixture payloads missing from planner schedule")

        if block1_payload["actual_start_at"] or block1_payload["actual_end_at"]:
            return fail("no-actuals block should have empty actual fields")
        if ps_payload["actual_start_at"] or ps_payload["actual_end_at"]:
            return fail("no-actuals PS should have empty actual fields")
        pass_msg("no actuals means actual_start_at and actual_end_at are empty")

        with db() as con:
            con.execute(
                """
                INSERT INTO production_actual (
                  block_id, machine_id, report_date, remarks, reported_at,
                  output_qty, reject_qty, target_qty_at_report, status, entry_type, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'VOIDED', 'VOID', '')
                """,
                (block1, block1_payload["machine_id"], "2099-01-01", "voided-smoke", "2099-01-01 08:00:00", 99.0, 0.0, block1_payload["planned_qty"]),
            )
        data = _get_schedule(client)
        block1_payload = _find_block(data, block1)
        ps_payload = _find_ps(data, fixture["ps_id"])
        if block1_payload["actual_start_at"] or block1_payload["actual_end_at"] or float(block1_payload["actual_good_qty"] or 0) != 0:
            return fail("VOIDED actuals should be ignored")
        if ps_payload["actual_start_at"] or ps_payload["actual_end_at"]:
            return fail("VOIDED actuals should not affect PS actual fields")
        pass_msg("VOIDED actuals are ignored")

        with db() as con:
            con.execute(
                """
                INSERT INTO production_actual (
                  block_id, machine_id, report_date, remarks, reported_at,
                  output_qty, reject_qty, target_qty_at_report, status, entry_type, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 'REPORT', '')
                """,
                (block1, block1_payload["machine_id"], "2099-01-01", "partial", "2099-01-01 08:30:00", 1.0, 0.0, block1_payload["planned_qty"]),
            )
        data = _get_schedule(client)
        block1_payload = _find_block(data, block1)
        ps_payload = _find_ps(data, fixture["ps_id"])
        if not block1_payload["actual_start_at"]:
            return fail("first actual did not populate block actual_start_at")
        if block1_payload["actual_end_at"]:
            return fail("incomplete block should keep actual_end_at empty")
        if float(block1_payload["actual_good_qty"] or 0) != 1.0:
            return fail("actual_good_qty should reflect ACTIVE good qty")
        if not ps_payload["actual_start_at"]:
            return fail("first actual did not populate PS actual_start_at")
        if ps_payload["actual_end_at"]:
            return fail("PS actual_end_at should stay empty until all blocks complete")
        pass_msg("first actual populates actual_start_at and keeps actual_end_at empty")

        with db() as con:
            con.execute(
                """
                INSERT INTO production_actual (
                  block_id, machine_id, report_date, remarks, reported_at,
                  output_qty, reject_qty, target_qty_at_report, status, entry_type, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 'REPORT', '')
                """,
                (block3, block3_payload["machine_id"], "2099-01-01", "reject-check", "2099-01-01 09:00:00", 10.0, 2.0, block3_payload["planned_qty"]),
            )
        data = _get_schedule(client)
        block3_payload = _find_block(data, block3)
        if float(block3_payload["actual_good_qty"] or 0) != 8.0:
            return fail("reject qty did not reduce good qty")
        if block3_payload["actual_end_at"]:
            return fail("incomplete block should still have empty actual_end_at after reject row")
        pass_msg("reject qty reduces actual good qty")

        with db() as con:
            con.execute(
                """
                INSERT INTO production_actual (
                  block_id, machine_id, report_date, remarks, reported_at,
                  output_qty, reject_qty, target_qty_at_report, status, entry_type, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 'REPORT', '')
                """,
                (block1, block1_payload["machine_id"], "2099-01-01", "complete-1", "2099-01-01 10:00:00", 2.0, 0.0, block1_payload["planned_qty"]),
            )
            con.execute(
                """
                INSERT INTO production_actual (
                  block_id, machine_id, report_date, remarks, reported_at,
                  output_qty, reject_qty, target_qty_at_report, status, entry_type, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 'REPORT', '')
                """,
                (block2, block2_payload["machine_id"], "2099-01-01", "complete-2", "2099-01-01 11:00:00", block2_payload["planned_qty"], 0.0, block2_payload["planned_qty"]),
            )
            con.execute(
                """
                INSERT INTO production_actual (
                  block_id, machine_id, report_date, remarks, reported_at,
                  output_qty, reject_qty, target_qty_at_report, status, entry_type, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 'REPORT', '')
                """,
                (block3, block3_payload["machine_id"], "2099-01-01", "complete-3", "2099-01-01 12:00:00", 4.0, 0.0, block3_payload["planned_qty"]),
            )

        data = _get_schedule(client)
        block1_payload = _find_block(data, block1)
        block2_payload = _find_block(data, block2)
        block3_payload = _find_block(data, block3)
        ps_payload = _find_ps(data, fixture["ps_id"])
        if not block1_payload["actual_end_at"]:
            return fail("completed block should populate actual_end_at")
        if not block2_payload["actual_end_at"]:
            return fail("completed block should populate actual_end_at")
        if not block3_payload["actual_end_at"]:
            return fail("completed block should populate actual_end_at")
        if not ps_payload["actual_end_at"]:
            return fail("PS actual_end_at should populate when all blocks are complete")
        pass_msg("completed operations show actual end")
        pass_msg("process sheet actual end appears when all planned blocks are complete")

        return 0
    finally:
        if fixture:
            _cleanup_fixture(fixture)
            with db() as con:
                recalculate_planning_all_baseline(con, reason="SMOKE_PLANNER_ACTUALS_CLEANUP")


if __name__ == "__main__":
    sys.exit(main())
