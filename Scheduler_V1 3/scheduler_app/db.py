from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from .constants import CAPACITY_PROFILES, TRIAL_MACHINES

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("SCHEDULER_DB_PATH", ROOT / "planner.db"))


class RowMap(dict):
    __slots__ = ("_values",)

    def __init__(self, keys, values):
        super().__init__(zip(keys, values))
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        return dict.get(self, key, default)


def row_factory(cursor, row):
    return RowMap([col[0] for col in cursor.description], row)


def one(cursor):
    return cursor.fetchone()


def rows(cursor):
    return cursor.fetchall()


def dt_now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def date_text(d: date):
    return d.strftime("%Y-%m-%d")


def parse_dt_text(value):
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("T", " ")
    try:
        return datetime.fromisoformat(text[:19])
    except ValueError:
        return None


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = row_factory
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def table_columns(con, table_name):
    try:
        return {row["name"] for row in rows(con.execute(f"PRAGMA table_info({table_name})"))}
    except sqlite3.Error:
        return set()


def _ensure_index(con, table_name, index_name, sql):
    indexes = {row["name"] for row in rows(con.execute(f"PRAGMA index_list({table_name})"))}
    if index_name not in indexes:
        con.execute(sql)


def ensure_db():
    print(f"Scheduler DB: {DB_PATH}")
    with db() as con:
        _ensure_index(
            con,
            "machines",
            "idx_machines_machine_code_unique",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_machines_machine_code_unique
            ON machines(machine_code)
            """,
        )
        _ensure_index(
            con,
            "capacity_profile",
            "idx_capacity_profile_profile_name_unique",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_capacity_profile_profile_name_unique
            ON capacity_profile(profile_name)
            """,
        )

        if table_columns(con, "capacity_profile"):
            existing_profiles = {row["profile_name"] for row in rows(con.execute("SELECT profile_name FROM capacity_profile"))}
            for profile_name, capacity_minutes, start_minute, note in CAPACITY_PROFILES:
                if profile_name not in existing_profiles:
                    con.execute(
                        """
                        INSERT INTO capacity_profile (profile_name, capacity_minutes, start_minute, note)
                        VALUES (?, ?, ?, ?)
                        """,
                        (profile_name, capacity_minutes, start_minute, note),
                    )
                else:
                    con.execute(
                        """
                        UPDATE capacity_profile
                        SET capacity_minutes = ?, start_minute = ?, note = ?
                        WHERE profile_name = ?
                          AND (capacity_minutes <> ? OR start_minute <> ? OR COALESCE(note, '') <> COALESCE(?, ''))
                        """,
                        (capacity_minutes, start_minute, note, profile_name, capacity_minutes, start_minute, note),
                    )

        if table_columns(con, "machines"):
            existing_machines = {row["machine_code"] for row in rows(con.execute("SELECT machine_code FROM machines"))}
            for machine_code, machine_category, shift_profile in TRIAL_MACHINES:
                if machine_code not in existing_machines:
                    con.execute(
                        """
                        INSERT INTO machines (machine_code, machine_category, shift_profile, active)
                        VALUES (?, ?, ?, 1)
                        """,
                        (machine_code, machine_category, shift_profile),
                    )


def ensure_actual_schema(con):
    return None


def ensure_rework_schema(con):
    return None


def ensure_group_schema(con):
    return None


def ensure_planning_card_schema(con):
    return None


def ensure_v2_compat_views(con):
    return None


def ensure_v2_compat_columns(con):
    return None


def ensure_material_requirement_schema(con):
    return None
