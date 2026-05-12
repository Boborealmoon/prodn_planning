from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "planner.db"

REQUIRED_TABLES = {
    "machines",
    "parts",
    "part_flow_header",
    "part_flow_steps",
    "process_sheet",
    "capacity_profile",
    "machine_capacity_day",
    "trial_public_holiday",
    "trial_operation",
    "trial_run_block",
    "trial_run_block_segment",
    "trial_run_block_group",
    "trial_planning_card",
    "trial_planning_card_operation",
    "trial_production_actual",
    "trial_bom_material",
    "material_requirement",
    "data_import_log",
}


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def schema_objects(con: sqlite3.Connection, object_type: str) -> list[sqlite3.Row]:
    return list(
        con.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE type = ?
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """,
            (object_type,),
        )
    )


def table_row_count(con: sqlite3.Connection, table_name: str) -> int:
    return int(con.execute(f"SELECT COUNT(*) FROM {quote_ident(table_name)}").fetchone()[0])


def references_any_table(sql: str | None, table_names: set[str]) -> bool:
    if not sql:
        return False
    for table_name in table_names:
        escaped = re.escape(table_name)
        patterns = (
            rf'(?i)(?<![\w]){escaped}(?![\w])',
            rf'(?i)"{re.escape(table_name.replace(chr(34), chr(34) * 2))}"',
            rf"(?i)'{re.escape(table_name)}'",
            rf"(?i)`{re.escape(table_name)}`",
            rf"(?i)\[{re.escape(table_name)}\]",
        )
        if any(re.search(pattern, sql) for pattern in patterns):
            return True
    return False


def dependent_views_and_triggers(
    con: sqlite3.Connection,
    dropped_tables: set[str],
) -> tuple[list[str], list[str]]:
    views = [
        row["name"]
        for row in schema_objects(con, "view")
        if references_any_table(row["sql"], dropped_tables)
    ]
    triggers = [
        row["name"]
        for row in schema_objects(con, "trigger")
        if row["tbl_name"] in dropped_tables or references_any_table(row["sql"], dropped_tables)
    ]
    return sorted(views), sorted(triggers)


def backup_database(db_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = db_path.with_name(f"{db_path.name}.backup-empty-cleanup-{timestamp}")
    shutil.copy2(db_path, backup_path)
    return backup_path


def print_section(title: str, values: list[str]) -> None:
    print(f"\n{title} ({len(values)}):")
    if values:
        for value in values:
            print(f"  - {value}")
    else:
        print("  (none)")


def analyze(con: sqlite3.Connection) -> dict[str, list[str]]:
    tables = [row["name"] for row in schema_objects(con, "table")]
    row_counts = {table: table_row_count(con, table) for table in tables}
    empty_tables = sorted(table for table, count in row_counts.items() if count == 0)
    preserved_empty_required = sorted(table for table in empty_tables if table in REQUIRED_TABLES)
    dropped_tables = sorted(table for table in empty_tables if table not in REQUIRED_TABLES)
    dropped_views, dropped_triggers = dependent_views_and_triggers(con, set(dropped_tables))
    return {
        "empty_tables": empty_tables,
        "preserved_empty_required": preserved_empty_required,
        "dropped_tables": dropped_tables,
        "dropped_views": dropped_views,
        "dropped_triggers": dropped_triggers,
    }


def execute_cleanup(con: sqlite3.Connection, plan: dict[str, list[str]]) -> list[sqlite3.Row]:
    con.execute("PRAGMA foreign_keys = ON")
    with con:
        for trigger_name in plan["dropped_triggers"]:
            con.execute(f"DROP TRIGGER IF EXISTS {quote_ident(trigger_name)}")
        for view_name in plan["dropped_views"]:
            con.execute(f"DROP VIEW IF EXISTS {quote_ident(view_name)}")
        for table_name in plan["dropped_tables"]:
            con.execute(f"DROP TABLE IF EXISTS {quote_ident(table_name)}")
        violations = list(con.execute("PRAGMA foreign_key_check"))
        if violations:
            raise RuntimeError(f"foreign_key_check failed with {len(violations)} violation(s)")
    con.execute("VACUUM")
    return []


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Drop empty non-required legacy tables from the embedded TRIAL planner database.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"SQLite database path. Defaults to {DEFAULT_DB_PATH}",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually back up and clean the database. Without this flag, only prints a dry-run plan.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = args.db.expanduser().resolve()
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    mode = "EXECUTE" if args.execute else "DRY RUN"
    print(f"Mode: {mode}")
    print(f"Database: {db_path}")

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        plan = analyze(con)
        print_section("All empty tables", plan["empty_tables"])
        print_section("Preserved empty required tables", plan["preserved_empty_required"])
        print_section("Tables to drop", plan["dropped_tables"])
        print_section("Views to drop", plan["dropped_views"])
        print_section("Triggers to drop", plan["dropped_triggers"])

        if not args.execute:
            print("\nDry run only. Re-run with --execute to back up and apply these changes.")
            return 0

        backup_path = backup_database(db_path)
        print(f"\nBackup created: {backup_path}")
        execute_cleanup(con, plan)
        print("foreign_key_check passed.")
        print("VACUUM completed.")
        print("Cleanup completed.")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
