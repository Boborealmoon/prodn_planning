from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


def _find_sample_context(con):
    ps_row = con.execute(
        """
        SELECT ps.ps_id, ps.source_ps_id, ps.pp_partial_no, ps.selected_bom_id, ps.part_id, COUNT(s.op_seq_id) AS op_count
        FROM process_sheet ps
        JOIN operation_seq s ON s.bom_id = ps.selected_bom_id
        WHERE COALESCE(ps.selected_bom_id, 0) > 0
          AND COALESCE(ps.status, '') <> 'COMPLETED'
          AND COALESCE(ps.planner_status, '') <> 'COMPLETED'
        GROUP BY ps.ps_id, ps.source_ps_id, ps.pp_partial_no, ps.selected_bom_id, ps.part_id
        HAVING COUNT(s.op_seq_id) > 0
        ORDER BY ps.ps_id
        LIMIT 1
        """
    ).fetchone()
    if not ps_row:
        return None
    machine_row = con.execute(
        """
        SELECT machine_id, machine_code, machine_category
        FROM machines
        WHERE active = 1
        ORDER BY machine_id
        LIMIT 1
        """
    ).fetchone()
    if not machine_row:
        return None
    return {
        "source_ps_id": str(ps_row["source_ps_id"] or ps_row["ps_id"] or "").strip(),
        "pp_partial_no": str(ps_row["pp_partial_no"] or "").strip(),
        "bom_id": int(ps_row["selected_bom_id"] or 0),
        "machine_id": int(machine_row["machine_id"] or 0),
        "machine_code": str(machine_row["machine_code"] or "").strip(),
        "machine_category": str(machine_row["machine_category"] or "").strip(),
    }


def _find_history_operation(con, source_ps_id, pp_partial_no, bom_id):
    row = con.execute(
        """
        SELECT o.operation_id
        FROM operation o
        LEFT JOIN run_block b
          ON b.operation_id = o.operation_id
         AND COALESCE(b.active, 1) = 1
        LEFT JOIN production_actual a
          ON a.block_id = b.block_id
         AND COALESCE(a.status, 'ACTIVE') = 'ACTIVE'
        WHERE COALESCE(o.source_ps_id, '') = ?
          AND COALESCE(o.pp_partial_no, '') = ?
          AND COALESCE(o.selected_bom_id, 0) = ?
        GROUP BY o.operation_id
        HAVING COUNT(b.block_id) > 0 OR COUNT(a.actual_id) > 0
        ORDER BY o.operation_id
        LIMIT 1
        """,
        (source_ps_id, pp_partial_no, int(bom_id or 0)),
    ).fetchone()
    if row:
        return int(row["operation_id"] or 0)
    row = con.execute(
        """
        SELECT operation_id
        FROM operation
        WHERE COALESCE(source_ps_id, '') = ?
          AND COALESCE(pp_partial_no, '') = ?
          AND COALESCE(selected_bom_id, 0) = ?
        ORDER BY operation_id
        LIMIT 1
        """,
        (source_ps_id, pp_partial_no, int(bom_id or 0)),
    ).fetchone()
    return int(row["operation_id"] or 0) if row else 0


def _operation_ids_from_response(payload):
    return [int(row.get("operation_id") or 0) for row in (payload.get("operations") or []) if int(row.get("operation_id") or 0) > 0]


def main():
    src_db = ROOT / "planner.db"
    if not src_db.exists():
        return fail(f"Missing database file: {src_db}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="planner_bom_editor_"))
    tmp_db = tmp_dir / "planner.db"
    shutil.copy2(src_db, tmp_db)
    os.environ["SCHEDULER_DB_PATH"] = str(tmp_db)

    from scheduler_app import create_app
    from scheduler_app.db import db

    app = create_app()
    client = app.test_client()

    with db() as con:
        ctx = _find_sample_context(con)
        if not ctx:
            return fail("No process sheet/BOM context found for operation editor smoke.")
        source_ps_id = ctx["source_ps_id"]
        partial_no = ctx["pp_partial_no"]
        bom_id = ctx["bom_id"]
        machine_id = ctx["machine_id"]
        machine_code = ctx["machine_code"]
        machine_category = ctx["machine_category"]

    encoded_ps = quote(source_ps_id, safe="")

    res = client.get(
        f"/api/trial/planner/source-ps/{encoded_ps}/operations?pp_partial_no={quote(partial_no, safe='')}&bom_id={bom_id}"
    )
    if res.status_code != 200:
        return fail(f"GET operations returned {res.status_code}")
    data = res.get_json() or {}
    ops = data.get("operations") or []
    if not ops:
        return fail("GET operations returned no operations")
    pass_msg("GET editable operations returns an operations array")

    baseline_schedule = client.get("/api/trial/planner/schedule")
    if baseline_schedule.status_code != 200:
        return fail(f"GET /api/trial/planner/schedule returned {baseline_schedule.status_code}")
    baseline_schedule_data = baseline_schedule.get_json() or {}

    existing_op_nos = []
    for op in ops:
        try:
            existing_op_nos.append(float(str(op.get("op_no") or "0").replace(",", "")))
        except ValueError:
            continue
    next_op_no = str(int(max(existing_op_nos) + 10) if existing_op_nos else 10)

    add_payload = {
        "pp_partial_no": partial_no,
        "bom_id": bom_id,
        "source_op_no": next_op_no,
        "operation_name": "Test Operation",
        "setup_minutes": 15,
        "cycle_minutes_per_qty": 2.5,
        "machine_category": machine_category or "UNKNOWN",
        "preferred_machine": machine_code,
        "machine_ids": [machine_id],
    }
    add_res = client.post(f"/api/trial/planner/source-ps/{encoded_ps}/operations", json=add_payload)
    if add_res.status_code != 200:
        return fail(f"POST add operation returned {add_res.status_code}: {add_res.get_data(as_text=True)}")
    add_data = add_res.get_json() or {}
    if int(add_data.get("planning_run_id") or 0) <= 0:
        return fail("Add operation did not return a planning_run_id")
    pass_msg("Add operation saves and triggers planner recalculation")

    res = client.get(
        f"/api/trial/planner/source-ps/{encoded_ps}/operations?pp_partial_no={quote(partial_no, safe='')}&bom_id={bom_id}"
    )
    data = res.get_json() or {}
    ops_after_add = data.get("operations") or []
    added_op = next(
        (
            op
            for op in ops_after_add
            if str(op.get("source_op_no") or "") == next_op_no
            and "Test Operation" in str(op.get("operation_name") or "")
            and float(op.get("setup_minutes") or 0) == 15
            and float(op.get("cycle_minutes_per_qty") or 0) == 2.5
        ),
        None,
    )
    if not added_op:
        return fail("Added operation did not appear in editor GET response")
    added_op_id = int(added_op.get("operation_id") or 0)
    if not added_op_id:
        return fail("Added operation GET response missing operation_id")
    pass_msg("Added operation appears in editor GET response")

    schedule_after_add = client.get("/api/trial/planner/schedule")
    if schedule_after_add.status_code != 200:
        return fail(f"GET /api/trial/planner/schedule after add returned {schedule_after_add.status_code}")
    schedule_after_add_data = schedule_after_add.get_json() or {}
    op_found_in_schedule = False
    for item in (schedule_after_add_data.get("catalog") or []) + (schedule_after_add_data.get("planned") or []) + (schedule_after_add_data.get("process_sheets") or []):
        for card in item.get("op_cards") or []:
            if int(card.get("operation_id") or 0) == added_op_id:
                op_found_in_schedule = True
                break
        if op_found_in_schedule:
            break
    if not op_found_in_schedule:
        return fail("Added operation did not appear in planner catalog/sidebar data")
    pass_msg("Added operation appears in planner sidebar/catalog data")

    edit_payload = {
        "source_op_no": next_op_no,
        "operation_name": "Test Operation Edited",
        "setup_minutes": 30,
        "cycle_minutes_per_qty": 5,
        "machine_category": machine_category or "UNKNOWN",
        "preferred_machine": machine_code,
        "machine_ids": [machine_id],
        "is_active": True,
    }
    edit_res = client.put(f"/api/trial/planner/operations/{added_op_id}", json=edit_payload)
    if edit_res.status_code != 200:
        return fail(f"PUT update operation returned {edit_res.status_code}: {edit_res.get_data(as_text=True)}")
    edit_data = edit_res.get_json() or {}
    if int(edit_data.get("planning_run_id") or 0) <= 0:
        return fail("Update operation did not return a planning_run_id")
    pass_msg("Edit operation triggers recalculation")

    with db() as con:
        updated = con.execute(
            """
            SELECT operation_id, setup_minutes, cycle_minutes_per_qty, operation_name
            FROM operation
            WHERE COALESCE(source_op_no, '') = ?
              AND ABS(COALESCE(setup_minutes, 0) - ?) < 0.0001
              AND ABS(COALESCE(cycle_minutes_per_qty, 0) - ?) < 0.0001
              AND COALESCE(operation_name, '') LIKE '%Test Operation Edited%'
            ORDER BY operation_id
            LIMIT 1
            """,
            (next_op_no, 30.0, 5.0),
        ).fetchone()
    if not updated:
        return fail("Updated operation values did not persist in DB")
    operation_name = str(updated["operation_name"] or "")
    if operation_name.startswith(f"{next_op_no} "):
        return fail("Updated operation name still includes the op number prefix")
    added_op_id = int(updated["operation_id"] or added_op_id)
    pass_msg("Edit operation persists updated setup/cycle values")
    pass_msg("Edit operation name no longer accumulates the op number prefix")

    validation_res = client.post(
        f"/api/trial/planner/source-ps/{encoded_ps}/operations",
        json={
            "pp_partial_no": partial_no,
            "bom_id": bom_id,
            "source_op_no": "9990",
            "operation_name": "Invalid Operation",
            "setup_minutes": 5,
            "cycle_minutes_per_qty": 0,
            "machine_category": machine_category or "UNKNOWN",
            "preferred_machine": machine_code,
            "machine_ids": [machine_id],
        },
    )
    if validation_res.status_code != 400:
        return fail("cycle_minutes_per_qty = 0 should fail validation")
    pass_msg("Validation rejects zero cycle minutes")

    validation_res = client.post(
        f"/api/trial/planner/source-ps/{encoded_ps}/operations",
        json={
            "pp_partial_no": partial_no,
            "bom_id": bom_id,
            "source_op_no": "9991",
            "operation_name": "Invalid Operation",
            "setup_minutes": -1,
            "cycle_minutes_per_qty": 1,
            "machine_category": machine_category or "UNKNOWN",
            "preferred_machine": machine_code,
            "machine_ids": [machine_id],
        },
    )
    if validation_res.status_code != 400:
        return fail("setup_minutes = -1 should fail validation")
    pass_msg("Validation rejects negative setup minutes")

    reordered_ids = [added_op_id] + [int(op.get("operation_id") or 0) for op in ops_after_add if int(op.get("operation_id") or 0) != added_op_id]
    reorder_res = client.post(
        f"/api/trial/planner/source-ps/{encoded_ps}/operations/reorder",
        json={
            "pp_partial_no": partial_no,
            "bom_id": bom_id,
            "operation_ids": reordered_ids,
        },
    )
    if reorder_res.status_code != 200:
        return fail(f"POST reorder operation returned {reorder_res.status_code}: {reorder_res.get_data(as_text=True)}")
    with db() as con:
        seq_row = con.execute(
            """
            SELECT s.seq_no
            FROM operation o
            JOIN operation_seq s ON s.op_seq_id = o.source_op_seq_id AND s.bom_id = o.selected_bom_id
            WHERE o.operation_id = ?
            LIMIT 1
            """,
            (added_op_id,),
        ).fetchone()
    if not seq_row or int(seq_row["seq_no"] or 0) != 1:
        return fail("Reorder did not move the chosen operation to the front of the sequence")
    pass_msg("Reorder updates the operation order")

    res = client.get(
        f"/api/trial/planner/source-ps/{encoded_ps}/operations?pp_partial_no={quote(partial_no, safe='')}&bom_id={bom_id}"
    )
    if res.status_code != 200:
        return fail(f"GET operations after reorder returned {res.status_code}")
    pass_msg("GET operations still loads after reorder")

    with db() as con:
        target_delete_op_id = _find_history_operation(con, source_ps_id, partial_no, bom_id)
        before_actual_count = int((con.execute("SELECT COUNT(*) AS cnt FROM production_actual").fetchone() or {"cnt": 0})["cnt"])

    if not target_delete_op_id:
        return fail("Could not find an operation to exercise safe delete/deactivate behavior")

    delete_res = client.delete(f"/api/trial/planner/operations/{target_delete_op_id}")
    if delete_res.status_code != 200:
        return fail(f"DELETE operation returned {delete_res.status_code}: {delete_res.get_data(as_text=True)}")
    delete_data = delete_res.get_json() or {}
    if delete_data.get("mode") != "deactivated":
        return fail("Delete endpoint should deactivate operations rather than hard delete")
    if int(delete_data.get("planning_run_id") or 0) <= 0:
        return fail("Delete endpoint did not return a planning_run_id")

    with db() as con:
        op_row = con.execute(
            "SELECT operation_id, status FROM operation WHERE operation_id = ?",
            (int(target_delete_op_id),),
        ).fetchone()
        if not op_row:
            return fail("Deleted operation row was hard-deleted")
        if str(op_row["status"] or "").upper() != "INACTIVE":
            return fail("Deleted operation should be marked INACTIVE")
        after_actual_count = int((con.execute("SELECT COUNT(*) AS cnt FROM production_actual").fetchone() or {"cnt": 0})["cnt"])
        if after_actual_count != before_actual_count:
            return fail("Operation deactivation should not rewrite production_actual rows")
    pass_msg("Delete/deactivate keeps history intact and does not touch actual rows")

    final_schedule = client.get("/api/trial/planner/schedule")
    if final_schedule.status_code != 200:
        return fail(f"GET /api/trial/planner/schedule after edits returned {final_schedule.status_code}")
    final_schedule_data = final_schedule.get_json() or {}
    if int((final_schedule_data.get("planning_run") or {}).get("planning_run_id") or 0) <= int((baseline_schedule_data.get("planning_run") or {}).get("planning_run_id") or 0):
        return fail("Planner recalculation did not advance the planning run after edits")
    pass_msg("Planner recalculation advances after routing edits")

    print("PASS: smoke_bom_operation_editor completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
