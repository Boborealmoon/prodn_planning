from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT / "planner.db"


TABLE_RENAMES = [
    ("capacity_profile", "capacity_profile"),
    ("machine_capacity_day", "machine_capacity_day"),
    ("trial_public_holiday", "public_holiday"),
    ("trial_bom_material", "bom_material"),
    ("material_requirement", "material_requirement"),
    ("trial_operation", "operation"),
    ("trial_planning_card", "planning_card"),
    ("trial_planning_card_operation", "planning_card_operation"),
    ("trial_production_actual", "production_actual"),
    ("trial_run_block", "run_block"),
    ("trial_run_block_group", "run_block_group"),
    ("trial_run_block_segment", "run_block_segment"),
    ("part_flow_header", "bom_variation"),
    ("part_flow_steps", "operation_seq"),
]


COLUMN_RENAMES = [
    ("parts", "part_name", "part_no"),
    ("process_sheet", "inv_code", "part_no"),
    ("process_sheet", "inv_desc", "part_desc"),
    ("process_sheet", "selected_flow_id", "selected_bom_id"),
    ("bom_variation", "flow_id", "bom_id"),
    ("bom_variation", "flow_code", "bom_code"),
    ("bom_variation", "flow_name", "bom_desc"),
    ("operation_seq", "step_id", "op_seq_id"),
    ("operation_seq", "flow_id", "bom_id"),
    ("operation", "source_step_id", "source_op_seq_id"),
    ("production_actual", "actual_good_qty", "output_qty"),
    ("production_actual", "actual_reject_qty", "reject_qty"),
]


OPTIONAL_DROP_COLUMNS = [
    ("production_actual", "reported_by"),
    ("material_requirement", "updated_by"),
]


LEGACY_TRIGGERS = [
    "flows_insert",
    "flows_update",
    "flow_steps_insert",
    "flow_steps_update",
    "flow_steps_delete",
    "process_sheets_insert",
    "process_sheets_update",
    "materials_insert",
    "materials_update",
    "materials_delete",
    "parts_insert",
    "parts_update",
    "parts_delete",
]


LEGACY_VIEWS = [
    "flows",
    "flow_steps",
    "process_sheets",
    "materials",
    "planning_rows",
    "planning_blocks",
    "history",
    "capacity_profile",
    "machine_capacity_day",
    "trial_public_holiday",
    "trial_bom_material",
    "material_requirement",
    "trial_operation",
    "trial_planning_card",
    "trial_planning_card_operation",
    "trial_production_actual",
    "trial_run_block",
    "trial_run_block_group",
    "trial_run_block_segment",
]


KEY_TABLES = [
    "production_actual",
    "run_block",
    "operation",
    "run_block_segment",
    "bom_variation",
    "operation_seq",
    "process_sheet",
    "parts",
    "planning_card",
    "planning_card_operation",
    "capacity_profile",
    "machine_capacity_day",
    "material_requirement",
    "bom_material",
]


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def object_type(con: sqlite3.Connection, name: str) -> str | None:
    row = con.execute(
        """
        SELECT type
        FROM sqlite_master
        WHERE name = ?
          AND type IN ('table', 'view', 'trigger', 'index')
        """,
        (name,),
    ).fetchone()
    return row["type"] if row else None


def table_columns(con: sqlite3.Connection, name: str) -> list[str]:
    if object_type(con, name) not in {"table", "view"}:
        return []
    return [
        row["name"]
        for row in con.execute(f"PRAGMA table_info({quote_ident(name)})")
    ]


def table_row_count(con: sqlite3.Connection, name: str) -> int | None:
    if object_type(con, name) != "table":
        return None
    try:
        row = con.execute(f"SELECT COUNT(*) AS cnt FROM {quote_ident(name)}").fetchone()
        return int(row["cnt"] or 0)
    except sqlite3.Error:
        return None


def list_objects(con: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (row["name"], row["type"])
        for row in con.execute(
            """
            SELECT name, type
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        )
    ]


def print_objects(title: str, objects: list[tuple[str, str]]) -> None:
    print(f"\n{title} ({len(objects)}):")
    if not objects:
        print("  (none)")
        return
    for name, typ in objects:
        print(f"  - {name} [{typ}]")


def print_table_info(con: sqlite3.Connection, table_name: str) -> None:
    if object_type(con, table_name) not in {"table", "view"}:
        print(f"\nPRAGMA table_info({table_name}): missing")
        return

    print(f"\nPRAGMA table_info({table_name}):")
    for row in con.execute(f"PRAGMA table_info({quote_ident(table_name)})"):
        print(
            f"  - cid={row['cid']} "
            f"name={row['name']} "
            f"type={row['type']} "
            f"notnull={row['notnull']} "
            f"dflt={row['dflt_value']} "
            f"pk={row['pk']}"
        )


def backup_database(db_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = db_path.with_name(
        f"{db_path.name}.backup-before-new-schema-{timestamp}"
    )
    shutil.copy2(db_path, backup_path)
    return backup_path


def schema_refs(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        con.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE sql IS NOT NULL
              AND (
                   sql LIKE '%OLD.flow_id%'
                OR sql LIKE '%NEW.flow_id%'
                OR sql LIKE '%flow_id%'
                OR sql LIKE '%step_id%'
                OR sql LIKE '%part_flow_header%'
                OR sql LIKE '%part_flow_steps%'
                OR sql LIKE '%selected_flow_id%'
                OR sql LIKE '%trial_run_block%'
                OR sql LIKE '%trial_operation%'
                OR sql LIKE '%trial_production_actual%'
              )
            ORDER BY type, name
            """
        )
    )


def drop_trigger_if_exists(con: sqlite3.Connection, name: str, dry_run: bool) -> bool:
    if object_type(con, name) != "trigger":
        return False
    print(f"  drop trigger {name}")
    if not dry_run:
        con.execute(f"DROP TRIGGER IF EXISTS {quote_ident(name)}")
    return True


def drop_view_if_exists(con: sqlite3.Connection, name: str, dry_run: bool) -> bool:
    typ = object_type(con, name)
    if typ == "view":
        print(f"  drop view {name}")
        if not dry_run:
            con.execute(f"DROP VIEW IF EXISTS {quote_ident(name)}")
        return True
    if typ == "table":
        print(f"  keep table {name} [not dropping table]")
    return False


def drop_legacy_compat_objects(con: sqlite3.Connection, dry_run: bool) -> None:
    print("\nLegacy schema objects referencing old names/columns:")
    refs = schema_refs(con)
    if not refs:
        print("  (none)")
    else:
        for row in refs:
            sql_preview = " ".join((row["sql"] or "").split())
            if len(sql_preview) > 160:
                sql_preview = sql_preview[:157] + "..."
            print(f"  - {row['type']} {row['name']} on {row['tbl_name']}: {sql_preview}")

    print("\nDropping listed legacy triggers:")
    dropped_any = False
    for name in LEGACY_TRIGGERS:
        dropped_any = drop_trigger_if_exists(con, name, dry_run) or dropped_any
    if not dropped_any:
        print("  (none)")

    print("\nDropping listed legacy views:")
    dropped_any = False
    for name in LEGACY_VIEWS:
        dropped_any = drop_view_if_exists(con, name, dry_run) or dropped_any
    if not dropped_any:
        print("  (none)")

    # Extra safety: drop any remaining trigger/view that references the exact
    # stale flow column syntax that breaks ALTER TABLE RENAME COLUMN.
    print("\nDropping remaining trigger/view objects with stale SQL references:")
    refs = schema_refs(con)
    dropped_any = False
    for row in refs:
        typ = row["type"]
        name = row["name"]
        if typ == "trigger":
            dropped_any = drop_trigger_if_exists(con, name, dry_run) or dropped_any
        elif typ == "view":
            dropped_any = drop_view_if_exists(con, name, dry_run) or dropped_any
    if not dropped_any:
        print("  (none)")


def rename_table(con: sqlite3.Connection, old_name: str, new_name: str, dry_run: bool) -> None:
    old_type = object_type(con, old_name)
    new_type = object_type(con, new_name)

    if old_type != "table":
        return

    if new_type == "table":
        old_count = table_row_count(con, old_name)
        new_count = table_row_count(con, new_name)
        print(
            f"  skip rename {old_name} -> {new_name}: "
            f"{new_name} table already exists "
            f"(old rows={old_count}, new rows={new_count})"
        )
        return

    if new_type == "view":
        print(f"  drop view {new_name}")
        if not dry_run:
            con.execute(f"DROP VIEW IF EXISTS {quote_ident(new_name)}")

    print(f"  rename table {old_name} -> {new_name}")
    if not dry_run:
        con.execute(
            f"ALTER TABLE {quote_ident(old_name)} "
            f"RENAME TO {quote_ident(new_name)}"
        )


def rename_column(con: sqlite3.Connection, table: str, old_name: str, new_name: str, dry_run: bool) -> None:
    if object_type(con, table) != "table":
        return

    cols = table_columns(con, table)
    if old_name not in cols:
        return

    if new_name in cols:
        print(f"  skip rename {table}.{old_name} -> {new_name}: {new_name} already exists")
        return

    print(f"  rename column {table}.{old_name} -> {new_name}")
    if not dry_run:
        con.execute(
            f"ALTER TABLE {quote_ident(table)} "
            f"RENAME COLUMN {quote_ident(old_name)} TO {quote_ident(new_name)}"
        )


def drop_optional_column(con: sqlite3.Connection, table: str, col: str, dry_run: bool) -> None:
    if object_type(con, table) != "table":
        return

    cols = table_columns(con, table)
    if col not in cols:
        return

    print(f"  drop column {table}.{col}")
    if dry_run:
        return

    try:
        con.execute(
            f"ALTER TABLE {quote_ident(table)} "
            f"DROP COLUMN {quote_ident(col)}"
        )
    except sqlite3.OperationalError as exc:
        print(f"    skip drop column {table}.{col}: {exc}")


def warn_old_physical_tables(con: sqlite3.Connection) -> None:
    print("\nOld physical trial_* / flow tables still present:")
    found = False
    old_names = [old for old, _ in TABLE_RENAMES]
    for name in old_names:
        if object_type(con, name) == "table":
            count = table_row_count(con, name)
            print(f"  - {name} [table], rows={count}")
            found = True
    if not found:
        print("  (none)")


def validate_key_tables(con: sqlite3.Connection) -> None:
    print("\nKey table row counts:")
    for table_name in KEY_TABLES:
        typ = object_type(con, table_name)
        if typ != "table":
            print(f"  - {table_name}: missing")
            continue
        count = table_row_count(con, table_name)
        print(f"  - {table_name}: {count} rows")


def migrate(con: sqlite3.Connection, dry_run: bool) -> None:
    print_objects("Current objects", list_objects(con))

    # Critical: must happen before flow_id -> bom_id rename.
    drop_legacy_compat_objects(con, dry_run)

    print("\nTable renames:")
    did_any = False
    for old_name, new_name in TABLE_RENAMES:
        before = list_objects(con)
        rename_table(con, old_name, new_name, dry_run)
        after = list_objects(con)
        did_any = did_any or before != after
    if not did_any:
        print("  (no table renames applied or all skipped)")

    print("\nColumn renames:")
    did_any = False
    for table, old_name, new_name in COLUMN_RENAMES:
        before_cols = table_columns(con, table)
        rename_column(con, table, old_name, new_name, dry_run)
        after_cols = table_columns(con, table) if not dry_run else before_cols
        did_any = did_any or before_cols != after_cols
    if not did_any:
        print("  (no column renames applied or all skipped)")

    print("\nOptional column drops:")
    did_any = False
    for table, col in OPTIONAL_DROP_COLUMNS:
        before_cols = table_columns(con, table)
        drop_optional_column(con, table, col, dry_run)
        after_cols = table_columns(con, table) if not dry_run else before_cols
        did_any = did_any or before_cols != after_cols
    if not did_any:
        print("  (no optional columns dropped or all skipped)")

    warn_old_physical_tables(con)
    validate_key_tables(con)

    if dry_run:
        print("\nDry run only. Re-run with --execute to apply the migration.")
        return

    con.commit()

    violations = list(con.execute("PRAGMA foreign_key_check"))
    if violations:
        print("\nforeign_key_check violations:")
        for row in violations:
            print(f"  - {tuple(row)}")
        raise RuntimeError(f"foreign_key_check failed with {len(violations)} violation(s)")

    print("\nforeign_key_check passed.")

    # VACUUM must not run inside an active transaction.
    con.commit()
    con.execute("VACUUM")
    con.commit()
    print("VACUUM completed.")

    print_objects("Final objects", list_objects(con))
    for table_name in KEY_TABLES:
        print_table_info(con, table_name)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate planner.db to the new non-prefixed schema."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="SQLite database path",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the migration instead of dry-running it",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = args.db.expanduser().resolve()

    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    print(f"Database: {db_path}")
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    try:
        # Keep SQLite from trying to enforce foreign keys mid-rename.
        # We validate with PRAGMA foreign_key_check after all renames finish.
        con.execute("PRAGMA foreign_keys = OFF")

        if args.execute:
            backup_path = backup_database(db_path)
            print(f"Backup created: {backup_path}")

        migrate(con, dry_run=not args.execute)
        return 0

    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
