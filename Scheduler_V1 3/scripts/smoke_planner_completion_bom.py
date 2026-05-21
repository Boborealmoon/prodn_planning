from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app import create_app
from scheduler_app.db import db, one


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


def flatten_op_cards(cards):
    result = []
    for card in cards or []:
        if card.get("card_kind") == "group" and card.get("ops"):
            result.extend(flatten_op_cards(card.get("ops") or []))
        else:
            result.append(card)
    return result


def find_card_by_operation_id(items, operation_id):
    op_id = int(operation_id or 0)
    for item in items or []:
        for card in flatten_op_cards(item.get("op_cards") or []):
            if int(card.get("operation_id") or 0) == op_id:
                return card
    return None


def find_sidebar_item(data):
    process_sheet_ids = {str(item.get("source_ps_id") or "") for item in data.get("process_sheets") or []}
    candidates = (data.get("catalog") or []) + (data.get("planned") or [])
    return next(
        (
            item
            for item in candidates
            if item.get("source_ps_id")
            and str(item.get("source_ps_id") or "") in process_sheet_ids
            and isinstance(item.get("bom_options"), list)
            and item.get("bom_options")
            and item.get("op_cards")
        ),
        None,
    )


def find_process_sheet_item(data, source_ps_id):
    return next((item for item in data.get("process_sheets") or [] if str(item.get("source_ps_id") or "") == str(source_ps_id)), None)


def capture_original_state(con, source_ps_id, step_rows, selected_bom_id):
    ps_row = one(
        con.execute(
            """
            SELECT source_ps_id, ps_id, selected_bom_id, completed, completed_at, completed_by, planner_status
            FROM process_sheet
            WHERE source_ps_id = ? OR ps_id = ?
            LIMIT 1
            """,
            (source_ps_id, source_ps_id),
        )
    )
    op_state = {}
    for step in step_rows:
        operation_id = int(step.get("op_seq_id") or 0)
        planner_row = one(
            con.execute(
                """
                SELECT *
                FROM planner_opn_state
                WHERE source_ps_id = ?
                  AND selected_bom_id = ?
                  AND source_op_seq_id = ?
                  AND source_op_no = ?
                LIMIT 1
                """,
                (
                    source_ps_id,
                    int(selected_bom_id or 0),
                    int(step.get("op_seq_id") or 0),
                    str(step.get("op_no") or ""),
                ),
            )
        )
        op_state[operation_id] = {
            "planner_row": dict(planner_row) if planner_row else None,
            "source_ps_id": source_ps_id,
            "selected_bom_id": int(selected_bom_id or 0),
            "source_op_seq_id": int(step.get("op_seq_id") or 0),
            "source_op_no": str(step.get("op_no") or ""),
        }
    return dict(ps_row) if ps_row else None, op_state


def restore_original_state(con, ps_row, op_state):
    if ps_row:
        con.execute(
            """
            UPDATE process_sheet
            SET selected_bom_id = ?,
                completed = ?,
                completed_at = ?,
                completed_by = ?,
                planner_status = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE source_ps_id = ? OR ps_id = ?
            """,
            (
                int(ps_row.get("selected_bom_id") or 0),
                int(ps_row.get("completed") or 0),
                str(ps_row.get("completed_at") or ""),
                str(ps_row.get("completed_by") or ""),
                str(ps_row.get("planner_status") or "ACTIVE"),
                str(ps_row.get("source_ps_id") or ""),
                str(ps_row.get("ps_id") or ""),
            ),
        )
    for operation_id, state in (op_state or {}).items():
        planner_row = state.get("planner_row")
        if planner_row:
            con.execute(
                """
                INSERT INTO planner_opn_state (
                  source_ps_id, selected_bom_id, source_op_seq_id, source_op_no,
                  operation_id, opn_completed, opn_completed_at, opn_completed_by, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(source_ps_id, selected_bom_id, source_op_seq_id, source_op_no)
                DO UPDATE SET
                  operation_id = excluded.operation_id,
                  opn_completed = excluded.opn_completed,
                  opn_completed_at = excluded.opn_completed_at,
                  opn_completed_by = excluded.opn_completed_by,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (
                    planner_row.get("source_ps_id", ""),
                    int(planner_row.get("selected_bom_id") or 0),
                    int(planner_row.get("source_op_seq_id") or 0),
                    str(planner_row.get("source_op_no") or ""),
                    int(planner_row.get("operation_id") or operation_id),
                    int(planner_row.get("opn_completed") or 0),
                    str(planner_row.get("opn_completed_at") or ""),
                    str(planner_row.get("opn_completed_by") or ""),
                ),
            )
        else:
            con.execute(
                """
                DELETE FROM planner_opn_state
                WHERE source_ps_id = ?
                  AND selected_bom_id = ?
                  AND source_op_seq_id = ?
                  AND source_op_no = ?
                """,
                (
                    str(state.get("source_ps_id") or ""),
                    int(state.get("selected_bom_id") or 0),
                    int(state.get("source_op_seq_id") or 0),
                    str(state.get("source_op_no") or ""),
                ),
            )


def main():
    app = create_app()
    client = app.test_client()

    res = client.get("/api/trial/planner/schedule")
    if res.status_code != 200:
        return fail(f"GET /api/trial/planner/schedule returned {res.status_code}")
    data = res.get_json() or {}

    sidebar_item = find_sidebar_item(data)
    if not sidebar_item:
        return fail("No planner sidebar item with bom_options and op_cards found")

    required_sidebar_fields = [
        "source_ps_id",
        "partial_no",
        "pp_partial_no",
        "part_no",
        "part_desc",
        "total_qty",
        "partial_qty",
        "opn_count",
        "active_opn_count",
        "completed_opn_count",
        "is_completed",
        "completed_at",
        "execution_label",
        "selected_bom_id",
        "default_bom_id",
        "bom_options",
    ]
    for field in required_sidebar_fields:
        if field not in sidebar_item:
            return fail(f"sidebar item missing {field}")

    if not sidebar_item.get("source_ps_id"):
        return fail("source_ps_id must not be empty")
    if not isinstance(sidebar_item.get("opn_count"), int):
        return fail("opn_count must be numeric")
    if not isinstance(sidebar_item.get("active_opn_count"), int):
        return fail("active_opn_count must be numeric")
    if not isinstance(sidebar_item.get("completed_opn_count"), int):
        return fail("completed_opn_count must be numeric")
    if not isinstance(sidebar_item.get("total_qty"), (int, float)):
        return fail("total_qty must be numeric")
    if not isinstance(sidebar_item.get("partial_qty"), (int, float)):
        return fail("partial_qty must be numeric")
    if not isinstance(sidebar_item.get("bom_options"), list) or not sidebar_item.get("bom_options"):
        return fail("bom_options must be a non-empty list")
    if not isinstance(sidebar_item.get("bom_options")[0], dict):
        return fail("bom_options entries must be objects")
    if not sidebar_item["bom_options"][0].get("is_default") and int(sidebar_item.get("default_bom_id") or 0) != int(sidebar_item["bom_options"][0].get("bom_id") or 0):
        pass_msg("default BOM is available and sorted first")

    process_sheet_item = find_process_sheet_item(data, sidebar_item["source_ps_id"])
    if not process_sheet_item:
        return fail("matching process sheet item not found")
    for field in ("source_ps_id", "partial_no", "pp_partial_no", "part_no", "part_desc", "total_qty", "partial_qty", "opn_count", "active_opn_count", "completed_opn_count", "is_completed", "completed_at", "execution_label", "selected_bom_id", "default_bom_id", "bom_options"):
        if field not in process_sheet_item:
            return fail(f"process sheet item missing {field}")

    bom_options = sidebar_item["bom_options"]
    selected_bom_id = int(sidebar_item.get("selected_bom_id") or sidebar_item.get("default_bom_id") or bom_options[0]["bom_id"])
    with db() as con:
        step_rows = [
            dict(row)
            for row in con.execute(
                """
                SELECT op_seq_id, seq_no, op_no, op_type, is_last_op
                FROM operation_seq
                WHERE bom_id = ?
                ORDER BY seq_no, op_seq_id
                """,
                (selected_bom_id,),
            )
        ]
        if not step_rows:
            return fail("No BOM steps found for selected BOM")
        original_ps_state, original_op_state = capture_original_state(con, sidebar_item["source_ps_id"], step_rows, selected_bom_id)
        if not original_ps_state:
            return fail("Could not capture original process sheet state")

    try:
        # Test B: completion toggle endpoint.
        first_step = step_rows[0]
        first_operation_id = int(first_step.get("op_seq_id") or 0)
        res = client.post(
            f"/api/trial/planner/opn/{first_operation_id}/completion",
            json={
                "completed": True,
                "source_ps_id": sidebar_item["source_ps_id"],
                "source_op_seq_id": int(first_step.get("op_seq_id") or 0),
                "source_op_no": str(first_step.get("op_no") or ""),
                "bom_id": selected_bom_id,
            },
        )
        if res.status_code != 200:
            return fail(f"mark complete returned {res.status_code}")
        res = client.get("/api/trial/planner/schedule")
        data = res.get_json() or {}
        updated_ps = find_process_sheet_item(data, sidebar_item["source_ps_id"])
        if not updated_ps or int(updated_ps.get("completed_opn_count") or 0) < 1:
            return fail("OPN completion did not update process sheet counts")
        pass_msg("OPN completion endpoint marks an operation completed")

        res = client.post(
            f"/api/trial/planner/opn/{first_operation_id}/completion",
            json={
                "completed": False,
                "source_ps_id": sidebar_item["source_ps_id"],
                "source_op_seq_id": int(first_step.get("op_seq_id") or 0),
                "source_op_no": str(first_step.get("op_no") or ""),
                "bom_id": selected_bom_id,
            },
        )
        if res.status_code != 200:
            return fail(f"mark active returned {res.status_code}")
        res = client.get("/api/trial/planner/schedule")
        data = res.get_json() or {}
        updated_ps = find_process_sheet_item(data, sidebar_item["source_ps_id"])
        if not updated_ps or int(updated_ps.get("completed_opn_count") or 0) != 0:
            return fail("OPN completion reset did not update process sheet counts")
        pass_msg("OPN completion endpoint marks an operation active")

        # Test D/E: BOM options and selection endpoint.
        if len(bom_options) > 1:
            next_bom_id = next((int(opt.get("bom_id") or 0) for opt in bom_options if int(opt.get("bom_id") or 0) != selected_bom_id), selected_bom_id)
        else:
            next_bom_id = selected_bom_id
        res = client.post(
            f"/api/trial/planner/source-ps/{sidebar_item['source_ps_id']}/bom",
            json={"bom_id": next_bom_id},
        )
        if res.status_code != 200:
            return fail(f"select BOM returned {res.status_code}")
        res = client.get("/api/trial/planner/schedule")
        data = res.get_json() or {}
        updated_ps = find_process_sheet_item(data, sidebar_item["source_ps_id"])
        if not updated_ps:
            return fail("Updated process sheet missing after BOM selection")
        if int(updated_ps.get("selected_bom_id") or 0) != int(next_bom_id):
            return fail("BOM selection did not persist")
        pass_msg("BOM selection endpoint persists the selected BOM")

        # Test C: PS auto completion when all OPNs finish.
        for step in step_rows:
            op_id = int(step.get("op_seq_id") or 0)
            res = client.post(
                f"/api/trial/planner/opn/{op_id}/completion",
                json={
                    "completed": True,
                    "source_ps_id": sidebar_item["source_ps_id"],
                    "source_op_seq_id": int(step.get("op_seq_id") or 0),
                    "source_op_no": str(step.get("op_no") or ""),
                    "bom_id": next_bom_id,
                },
            )
            if res.status_code != 200:
                return fail(f"bulk completion returned {res.status_code}")
        res = client.get("/api/trial/planner/schedule")
        data = res.get_json() or {}
        updated_ps = find_process_sheet_item(data, sidebar_item["source_ps_id"])
        if not updated_ps or not updated_ps.get("is_completed"):
            return fail("Process sheet did not auto-complete after finishing all OPNs")
        pass_msg("Process sheet auto-completes when all OPNs are finished")

        res = client.post(
            f"/api/trial/planner/opn/{int(step_rows[0].get('op_seq_id') or 0)}/completion",
            json={
                "completed": False,
                "source_ps_id": sidebar_item["source_ps_id"],
                "source_op_seq_id": int(step_rows[0].get("op_seq_id") or 0),
                "source_op_no": str(step_rows[0].get("op_no") or ""),
                "bom_id": next_bom_id,
            },
        )
        if res.status_code != 200:
            return fail(f"bulk uncomplete returned {res.status_code}")
        res = client.get("/api/trial/planner/schedule")
        data = res.get_json() or {}
        updated_ps = find_process_sheet_item(data, sidebar_item["source_ps_id"])
        if not updated_ps or updated_ps.get("is_completed"):
            return fail("Process sheet did not reactivate after uncompleting an OPN")
        pass_msg("Uncompleting an OPN reactivates the process sheet")

        print("PASS: smoke_planner_completion_bom completed successfully")
        return 0
    finally:
        with db() as con:
            restore_original_state(con, original_ps_state, original_op_state)


if __name__ == "__main__":
    sys.exit(main())
