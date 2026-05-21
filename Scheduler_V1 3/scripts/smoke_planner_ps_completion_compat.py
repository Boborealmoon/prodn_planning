from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler_app import create_app
from scheduler_app.db import db, ensure_db, one, rows


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


def _first_candidate_from_schedule_rows(process_sheets):
    buckets = {}
    for ps in process_sheets:
        source_ps_id = str(ps.get("source_ps_id") or "").strip()
        partial_no = str(ps.get("pp_partial_no") or ps.get("partial_no") or "").strip()
        if not source_ps_id or not partial_no:
            continue
        buckets.setdefault(source_ps_id, []).append(ps)
    for source_ps_id, items in buckets.items():
        if len(items) < 2:
            continue
        items = sorted(items, key=lambda item: str(item.get("pp_partial_no") or item.get("partial_no") or ""))
        row_a = next((row for row in items if not bool(row.get("is_completed"))), items[0])
        row_b = next(
            (
                row
                for row in items
                if str(row.get("pp_partial_no") or row.get("partial_no") or "")
                != str(row_a.get("pp_partial_no") or row_a.get("partial_no") or "")
            ),
            None,
        )
        if row_b:
            return source_ps_id, row_a, row_b
    return None


def _table_columns(con, table_name):
    return [str(row["name"]) for row in rows(con.execute(f"PRAGMA table_info({table_name})"))]


def _clone_process_sheet_pair(client):
    planner = _get_json(client, "/api/trial/planner/schedule")
    planning_run = planner.get("planning_run") or {}
    planning_run_id = int(planning_run.get("planning_run_id") or 0)
    base_item = next((item for item in planner.get("process_sheets") or [] if int(item.get("selected_bom_id") or 0) > 0), None)
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

        process_cols = _table_columns(con, "process_sheet")
        state_cols = _table_columns(con, "planning_process_sheet_state")
        clone_specs = [
            ("A", f"{source_ps_id}::__SMOKE_A"),
            ("B", f"{source_ps_id}::__SMOKE_B"),
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
                "planned_qty": float(base_item.get("planned_qty") or base_item.get("partial_qty") or base.get("planned_qty") or base.get("total_qty") or 0),
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


def _fetch_rows_for_source(client, source_ps_id):
    planner = _get_json(client, "/api/trial/planner/schedule")
    rows_out = [
        row
        for row in planner.get("process_sheets") or []
        if str(row.get("source_ps_id") or "").strip() == source_ps_id
    ]
    return rows_out


def main():
    try:
        ensure_db()
    except Exception as exc:
        return fail(f"ensure_db failed: {exc}")

    app = create_app()
    client = app.test_client()

    clone_info = _clone_process_sheet_pair(client)
    if not clone_info:
        return fail("Could not create temporary partial PS rows for completion compatibility smoke")

    source_ps_id = clone_info["source_ps_id"]
    created = clone_info["created"]
    partial_a = created[0][1]
    partial_b = created[1][1]
    clone_ps_id_a = created[0][0]
    clone_ps_id_b = created[1][0]

    try:
        with db() as con:
            con.execute(
                """
                UPDATE process_sheet
                SET completed = 1,
                    planner_status = 'ACTIVE',
                    updated_at = CURRENT_TIMESTAMP
                WHERE ps_id = ?
                """,
                (clone_ps_id_a,),
            )

        planner_after_legacy = _fetch_rows_for_source(client, source_ps_id)
        a = next((ps for ps in planner_after_legacy if str(ps.get("pp_partial_no") or ps.get("partial_no") or "") == partial_a), None)
        b = next((ps for ps in planner_after_legacy if str(ps.get("pp_partial_no") or ps.get("partial_no") or "") == partial_b), None)
        if not a or not b:
            return fail("Expected both partial rows after legacy completion update")
        if not bool(a.get("is_completed")):
            return fail("Legacy completed partial A was not returned as completed")
        if bool(b.get("is_completed")):
            return fail("Partial B was incorrectly marked completed by legacy state")
        pass_msg("Legacy completed=1 rows still hide correctly and remain scoped")

        res = client.post(
            f"/api/trial/planner/source-ps/{source_ps_id}/completion",
            json={"pp_partial_no": partial_b, "completed": True},
        )
        if res.status_code != 200:
            return fail(f"New PS completion endpoint failed: {res.status_code} {res.get_data(as_text=True)}")

        planner_after_new = _fetch_rows_for_source(client, source_ps_id)
        a = next((ps for ps in planner_after_new if str(ps.get("pp_partial_no") or ps.get("partial_no") or "") == partial_a), None)
        b = next((ps for ps in planner_after_new if str(ps.get("pp_partial_no") or ps.get("partial_no") or "") == partial_b), None)
        if not a or not b:
            return fail("Expected both partial rows after scoped completion toggle")
        if not bool(a.get("is_completed")):
            return fail("Partial A lost completed state after toggling partial B")
        if not bool(b.get("is_completed")):
            return fail("Partial B was not marked completed by scoped completion endpoint")
        pass_msg("Scoped completion only affected the selected partial")

        res = client.post(
            f"/api/trial/planner/source-ps/{source_ps_id}/completion",
            json={"pp_partial_no": partial_b, "completed": False},
        )
        if res.status_code != 200:
            return fail(f"Scoped re-activate failed: {res.status_code} {res.get_data(as_text=True)}")

        planner_after_reset = _fetch_rows_for_source(client, source_ps_id)
        a = next((ps for ps in planner_after_reset if str(ps.get("pp_partial_no") or ps.get("partial_no") or "") == partial_a), None)
        b = next((ps for ps in planner_after_reset if str(ps.get("pp_partial_no") or ps.get("partial_no") or "") == partial_b), None)
        if not a or not b:
            return fail("Expected both partial rows after reactivating partial B")
        if not bool(a.get("is_completed")):
            return fail("Partial A lost completed state after reactivating partial B")
        if bool(b.get("is_completed")):
            return fail("Partial B remained completed after reactivation")
        pass_msg("Re-activating one partial did not affect the other")

        print("PASS: smoke_planner_ps_completion_compat completed successfully")
        return 0
    finally:
        with db() as con:
            con.execute(
                "DELETE FROM planner_opn_state WHERE source_ps_id = ? AND COALESCE(pp_partial_no, '') IN (?, ?)",
                (source_ps_id, partial_a, partial_b),
            )
            con.execute(
                "DELETE FROM planning_process_sheet_state WHERE ps_id IN (?, ?)",
                (clone_ps_id_a, clone_ps_id_b),
            )
            con.execute(
                "DELETE FROM process_sheet WHERE ps_id IN (?, ?)",
                (clone_ps_id_a, clone_ps_id_b),
            )


if __name__ == "__main__":
    sys.exit(main())
