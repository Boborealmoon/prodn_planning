from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app import create_app
from scheduler_app.db import db, ensure_db, one, rows


SMOKE_PARTIAL_A = "__SMOKE_A"
SMOKE_PARTIAL_B = "__SMOKE_B"


def fail(message):
    print(f"FAIL: {message}")
    return 1


def pass_msg(message):
    print(f"PASS: {message}")


def _get_json(client, path):
    res = client.get(path)
    if res.status_code != 200:
        raise RuntimeError(f"GET {path} returned {res.status_code}: {res.get_data(as_text=True)}")
    return res.get_json() or {}


def _table_columns(con, table_name):
    return [str(row["name"]) for row in rows(con.execute(f"PRAGMA table_info({table_name})"))]


def _cleanup_smoke_rows():
    with db() as con:
        smoke_ps_ids = [str(row["ps_id"]) for row in rows(con.execute("SELECT ps_id FROM process_sheet WHERE ps_id LIKE ?", (f"%::{SMOKE_PARTIAL_A}",)))]
        smoke_ps_ids.extend(str(row["ps_id"]) for row in rows(con.execute("SELECT ps_id FROM process_sheet WHERE ps_id LIKE ?", (f"%::{SMOKE_PARTIAL_B}",))))
        smoke_op_rows = rows(
            con.execute(
                """
                SELECT operation_id
                FROM operation
                WHERE pp_partial_no IN (?, ?)
                """,
                (SMOKE_PARTIAL_A, SMOKE_PARTIAL_B),
            )
        )
        smoke_operation_ids = [int(row["operation_id"]) for row in smoke_op_rows if int(row["operation_id"] or 0)]
        smoke_block_ids = [int(row["block_id"]) for row in rows(con.execute(
            f"""
            SELECT block_id
            FROM run_block
            WHERE operation_id IN ({",".join("?" for _ in smoke_operation_ids)})""" if smoke_operation_ids else "SELECT block_id FROM run_block WHERE 0",
            smoke_operation_ids if smoke_operation_ids else [],
        ))]
        if smoke_block_ids:
            placeholders = ",".join("?" for _ in smoke_block_ids)
            con.execute(f"DELETE FROM planning_schedule_segment WHERE block_id IN ({placeholders})", smoke_block_ids)
            con.execute(f"DELETE FROM run_block WHERE block_id IN ({placeholders})", smoke_block_ids)
        con.execute(
            "DELETE FROM planner_opn_state WHERE COALESCE(pp_partial_no, '') IN (?, ?)",
            (SMOKE_PARTIAL_A, SMOKE_PARTIAL_B),
        )
        if smoke_operation_ids:
            placeholders = ",".join("?" for _ in smoke_operation_ids)
            con.execute(f"DELETE FROM operation WHERE operation_id IN ({placeholders})", smoke_operation_ids)
        if smoke_ps_ids:
            placeholders = ",".join("?" for _ in smoke_ps_ids)
            con.execute(f"DELETE FROM planning_process_sheet_state WHERE ps_id IN ({placeholders})", smoke_ps_ids)
            con.execute(f"DELETE FROM process_sheet WHERE ps_id IN ({placeholders})", smoke_ps_ids)


def _clone_process_sheet_pair(client):
    planner = _get_json(client, "/api/trial/planner/schedule")
    planning_run = planner.get("planning_run") or {}
    planning_run_id = int(planning_run.get("planning_run_id") or 0)
    base_item = next(
        (
            item
            for item in planner.get("process_sheets") or []
            if int(item.get("selected_bom_id") or 0) > 0 and (item.get("op_cards") or [])
        ),
        None,
    )
    if not planning_run_id or not base_item:
        return None

    base_ps_id = str(base_item.get("ps_id") or "").strip()
    if not base_ps_id:
        return None

    with db() as con:
        base = one(
            con.execute(
                """
                SELECT *
                FROM process_sheet
                WHERE ps_id = ?
                LIMIT 1
                """,
                (base_ps_id,),
            )
        )
        if not base:
            return None
        base = dict(base)
        source_ps_id = str(base.get("source_ps_id") or base_item.get("source_ps_id") or "").strip() or str(base.get("ps_id") or "").split("::", 1)[0]
        if not source_ps_id:
            return None

        _cleanup_smoke_rows()

        process_cols = _table_columns(con, "process_sheet")
        state_cols = _table_columns(con, "planning_process_sheet_state")
        clone_specs = [
            (SMOKE_PARTIAL_A, f"{source_ps_id}::__SMOKE_A"),
            (SMOKE_PARTIAL_B, f"{source_ps_id}::__SMOKE_B"),
        ]
        created = []

        for partial_no, clone_ps_id in clone_specs:
            ps_row = dict(base)
            ps_row["ps_id"] = clone_ps_id
            ps_row["source_ps_id"] = source_ps_id
            ps_row["pp_partial_no"] = partial_no
            ps_row["completed"] = 0
            ps_row["completed_at"] = ""
            ps_row["completed_by"] = ""
            ps_row["planner_status"] = "ACTIVE"
            ps_row["status"] = "ACTIVE"
            ps_row["updated_at"] = ps_row.get("updated_at") or ""
            insert_cols = [col for col in process_cols if col in ps_row]
            con.execute(
                f"""
                INSERT INTO process_sheet ({", ".join(insert_cols)})
                VALUES ({", ".join("?" for _ in insert_cols)})
                """,
                [ps_row[col] for col in insert_cols],
            )

            state_row = {
                "ps_id": clone_ps_id,
                "planning_run_id": planning_run_id,
                "expected_start_at": str(base_item.get("expected_start_at") or ""),
                "expected_end_at": str(base_item.get("expected_end_at") or ""),
                "planned_qty": float(base_item.get("partial_qty") or base_item.get("planned_qty") or base.get("planned_qty") or base.get("total_qty") or 0),
                "planned_minutes": float(base_item.get("planned_minutes") or base.get("planned_minutes") or 0),
                "updated_at": base.get("updated_at") or "",
            }
            insert_state_cols = [col for col in state_cols if col in state_row]
            con.execute(
                f"""
                INSERT INTO planning_process_sheet_state ({", ".join(insert_state_cols)})
                VALUES ({", ".join("?" for _ in insert_state_cols)})
                """,
                [state_row[col] for col in insert_state_cols],
            )
            created.append((clone_ps_id, partial_no))

        return {
            "source_ps_id": source_ps_id,
            "created": created,
            "base": base,
        }


def _find_partial_rows(planner_data, source_ps_id, partial_a, partial_b):
    rows_out = (planner_data.get("catalog") or []) + (planner_data.get("planned") or []) + (planner_data.get("process_sheets") or [])
    row_a = next(
        (
            row
            for row in rows_out
            if str(row.get("source_ps_id") or "").strip() == source_ps_id
            and str(row.get("pp_partial_no") or row.get("partial_no") or "").strip() == partial_a
        ),
        None,
    )
    row_b = next(
        (
            row
            for row in rows_out
            if str(row.get("source_ps_id") or "").strip() == source_ps_id
            and str(row.get("pp_partial_no") or row.get("partial_no") or "").strip() == partial_b
        ),
        None,
    )
    return row_a, row_b


def _shared_op_key(row_a, row_b):
    cards_a = [card for card in (row_a.get("op_cards") or []) if int(card.get("operation_id") or 0) >= 0]
    cards_b = {
        (
            int(card.get("source_op_seq_id") or 0),
            str(card.get("source_op_no") or "").strip(),
        ): card
        for card in (row_b.get("op_cards") or [])
    }
    for card_a in cards_a:
        key = (int(card_a.get("source_op_seq_id") or 0), str(card_a.get("source_op_no") or "").strip())
        card_b = cards_b.get(key)
        if card_b:
            return card_a, card_b
    return None, None


def _find_candidate(planner_data):
    buckets = {}
    for ps in (planner_data.get("catalog") or []) + (planner_data.get("planned") or []) + (planner_data.get("process_sheets") or []):
        source_ps_id = str(ps.get("source_ps_id") or "").strip()
        partial_no = str(ps.get("pp_partial_no") or ps.get("partial_no") or "").strip()
        if not source_ps_id or not partial_no:
            continue
        buckets.setdefault(source_ps_id, []).append(ps)
    for source_ps_id, items in buckets.items():
        if len(items) < 2:
            continue
        items = sorted(items, key=lambda item: str(item.get("pp_partial_no") or item.get("partial_no") or ""))
        for idx, row_a in enumerate(items):
            if bool(row_a.get("is_completed")) or not bool(row_a.get("can_drag", True)):
                continue
            bom_id = int(row_a.get("selected_bom_id") or 0)
            cards_a = {
                (
                    int(card.get("source_op_seq_id") or 0),
                    str(card.get("source_op_no") or "").strip(),
                ): card
                for card in (row_a.get("op_cards") or [])
                if int(card.get("operation_id") or 0) >= 0
            }
            if not cards_a:
                continue
            for row_b in items[idx + 1:]:
                if int(row_b.get("selected_bom_id") or 0) != bom_id:
                    continue
                if bool(row_b.get("is_completed")) or not bool(row_b.get("can_drag", True)):
                    continue
                if float(row_b.get("planned_qty") or 0) != 0:
                    continue
                cards_b = {
                    (
                        int(card.get("source_op_seq_id") or 0),
                        str(card.get("source_op_no") or "").strip(),
                    ): card
                    for card in (row_b.get("op_cards") or [])
                    if int(card.get("operation_id") or 0) >= 0
                }
                if not cards_b:
                    continue
                for key, card_a in cards_a.items():
                    card_b = cards_b.get(key)
                    if not card_b:
                        continue
                    if bool(card_a.get("is_completed")) or not bool(card_a.get("can_drag", True)):
                        continue
                    return {
                        "source_ps_id": source_ps_id,
                        "partial_a": str(row_a.get("pp_partial_no") or row_a.get("partial_no") or "").strip(),
                        "partial_b": str(row_b.get("pp_partial_no") or row_b.get("partial_no") or "").strip(),
                        "bom_id": bom_id,
                        "card_a": card_a,
                        "card_b": card_b,
                    }
    return None


def main():
    try:
        ensure_db()
    except Exception as exc:
        return fail(f"ensure_db failed: {exc}")

    app = create_app()
    client = app.test_client()
    block_ids_to_cleanup = []
    candidate = None

    try:
        planner = _get_json(client, "/api/trial/planner/schedule")
        candidate = _find_candidate(planner)
        if not candidate:
            return fail("Could not find two partial rows with a shared unplanned OPN")
        source_ps_id = candidate["source_ps_id"]
        partial_a = candidate["partial_a"]
        partial_b = candidate["partial_b"]
        bom_id = int(candidate["bom_id"] or 0)
        card_a = dict(candidate["card_a"])
        card_b = dict(candidate["card_b"])
        machine = next((item for item in planner.get("machines") or [] if int(item.get("machine_id") or 0) > 0), None)
        if not machine:
            return fail("No machine found in planner schedule")
        machine_id = int(machine.get("machine_id") or 0)

        payload_a = {
            "source_ps_id": source_ps_id,
            "pp_partial_no": partial_a,
            "source_op_seq_id": int(card_a.get("source_op_seq_id") or 0),
            "source_op_no": str(card_a.get("source_op_no") or ""),
            "selected_bom_id": bom_id,
            "machine_id": machine_id,
            "queue_position": 10,
            "scheduled_qty": max(1, int(card_a.get("remaining_qty") or card_a.get("target_qty") or 1)),
        }
        res_a = client.post("/api/trial/planner/schedule-opn", json=payload_a)
        if res_a.status_code != 200:
            return fail(f"Scheduling partial A failed: {res_a.status_code} {res_a.get_data(as_text=True)}")
        data_a = res_a.get_json() or {}
        if int(data_a.get("block_id") or 0):
            block_ids_to_cleanup.append(int(data_a.get("block_id") or 0))
        pass_msg("Scheduling partial A succeeded")

        planner_after_a = _get_json(client, "/api/trial/planner/schedule")
        row_a, row_b = _find_partial_rows(planner_after_a, source_ps_id, partial_a, partial_b)
        if not row_a or not row_b:
            return fail("Could not find both partial rows after scheduling partial A")
        card_a_after, card_b_after = _shared_op_key(row_a, row_b)
        if not card_a_after or not card_b_after:
            return fail("Could not find the shared OPN after scheduling partial A")
        initial_b_planned_qty = float(card_b.get("planned_qty") or 0)
        if float(card_a_after.get("planned_qty") or 0) <= 0:
            return fail("Partial A planned qty did not increase after scheduling")
        if float(card_b_after.get("planned_qty") or 0) != initial_b_planned_qty:
            return fail("Partial B planned qty changed when only partial A was scheduled")
        if str(card_b_after.get("pp_partial_no") or "").strip() != partial_b:
            return fail("Partial B identity changed when scheduling partial A")
        pass_msg("Partial A planning stayed scoped and partial B remained independent")

        payload_b = {
            "source_ps_id": source_ps_id,
            "pp_partial_no": partial_b,
            "source_op_seq_id": int(card_b_after.get("source_op_seq_id") or 0),
            "source_op_no": str(card_b_after.get("source_op_no") or ""),
            "selected_bom_id": bom_id,
            "machine_id": machine_id,
            "queue_position": 20,
            "scheduled_qty": max(1, int(card_b_after.get("remaining_qty") or card_b_after.get("target_qty") or 1)),
        }
        res_b = client.post("/api/trial/planner/schedule-opn", json=payload_b)
        if res_b.status_code != 200:
            return fail(f"Scheduling partial B failed: {res_b.status_code} {res_b.get_data(as_text=True)}")
        data_b = res_b.get_json() or {}
        if int(data_b.get("block_id") or 0):
            block_ids_to_cleanup.append(int(data_b.get("block_id") or 0))
        pass_msg("Scheduling partial B succeeded")

        planner_after_b = _get_json(client, "/api/trial/planner/schedule")
        row_a, row_b = _find_partial_rows(planner_after_b, source_ps_id, partial_a, partial_b)
        if not row_a or not row_b:
            return fail("Could not find both partial rows after scheduling partial B")
        card_a_after, card_b_after = _shared_op_key(row_a, row_b)
        if not card_a_after or not card_b_after:
            return fail("Could not find the shared OPN after scheduling partial B")
        if float(card_a_after.get("planned_qty") or 0) <= 0:
            return fail("Partial A planned qty disappeared after scheduling partial B")
        if float(card_b_after.get("planned_qty") or 0) <= initial_b_planned_qty:
            return fail("Partial B planned qty did not increase after scheduling")
        if str(card_a_after.get("pp_partial_no") or "").strip() == str(card_b_after.get("pp_partial_no") or "").strip():
            return fail("Partial identities collapsed after scheduling both partials")
        pass_msg("Each partial keeps an independent planned qty and identity")

        with db() as con:
            op_rows = rows(
                con.execute(
                    """
                    SELECT operation_id, pp_partial_no, selected_bom_id, source_op_seq_id, source_op_no
                    FROM operation
                    WHERE source_ps_id = ?
                      AND COALESCE(pp_partial_no, '') IN (?, ?)
                      AND COALESCE(selected_bom_id, 0) = ?
                      AND COALESCE(source_op_seq_id, 0) = ?
                      AND COALESCE(source_op_no, '') = ?
                    ORDER BY operation_id
                    """,
                    (
                        source_ps_id,
                        partial_a,
                        partial_b,
                        int(row_a.get("selected_bom_id") or 0),
                        int(card_a_after.get("source_op_seq_id") or 0),
                        str(card_a_after.get("source_op_no") or ""),
                    ),
                )
            )
            if len(op_rows) < 2:
                return fail("Expected separate operation rows for the two partials")
            if len({str(row["pp_partial_no"] or "") for row in op_rows}) < 2:
                return fail("Operation rows did not preserve distinct partial numbers")
        pass_msg("Operation rows are scoped per partial")

        print("PASS: smoke_planner_partial_identity completed successfully")
        return 0
    finally:
        if block_ids_to_cleanup:
            for block_id in reversed(block_ids_to_cleanup):
                try:
                    client.delete(f"/api/trial/blocks/{int(block_id)}")
                except Exception as exc:
                    print(f"WARN: cleanup failed for block {block_id}: {exc}")
        _cleanup_smoke_rows()


if __name__ == "__main__":
    sys.exit(main())
