from __future__ import annotations

from .constants import CAPACITY_PROFILES, TRIAL_MACHINES
from .db import one, rows
from .utils import compact_text


def default_profile_for_weekday(weekday, shift_profile="STANDARD"):
    if weekday == 6:
        return "OFF"
    if weekday == 5:
        return "SATURDAY"
    if compact_text(shift_profile).upper() == "24HR":
        return "FULL_24H"
    return "NORMAL_DAY_NIGHT"


def is_public_holiday(con, work_date):
    row = one(
        con.execute(
            """
            SELECT holiday_date, note
            FROM public_holiday
            WHERE holiday_date = ?
            """,
            (work_date.strftime("%Y-%m-%d"),),
        )
    )
    return row is not None


def machine_capacity_for_date(con, machine_id, work_date):
    if work_date.weekday() == 6 or is_public_holiday(con, work_date):
        profile = one(con.execute("SELECT * FROM capacity_profile WHERE profile_name = 'OFF'"))
        if profile:
            return {
                "profile_name": profile["profile_name"],
                "capacity_minutes": int(profile["capacity_minutes"] or 0),
                "start_minute": int(profile["start_minute"] or 0),
                "note": profile["note"] or "",
            }
        return {
            "profile_name": "OFF",
            "capacity_minutes": 0,
            "start_minute": 0,
            "note": "",
        }
    row = one(
        con.execute(
            """
            SELECT d.day_id, d.machine_id, d.work_date, d.profile_id, d.capacity_minutes, d.start_minute, d.note,
                   p.profile_name
            FROM machine_capacity_day d
            JOIN capacity_profile p ON p.profile_id = d.profile_id
            WHERE d.machine_id = ? AND d.work_date = ?
            """,
            (int(machine_id), work_date.strftime("%Y-%m-%d")),
        )
    )
    if row:
        return row
    machine = one(con.execute("SELECT shift_profile FROM machines WHERE machine_id = ?", (int(machine_id),)))
    profile_name = default_profile_for_weekday(
        work_date.weekday(),
        machine["shift_profile"] if machine else "STANDARD",
    )
    profile = one(con.execute("SELECT * FROM capacity_profile WHERE profile_name = ?", (profile_name,)))
    if not profile:
        profile = one(con.execute("SELECT * FROM capacity_profile ORDER BY profile_id LIMIT 1"))
    if not profile:
        return {
            "profile_name": profile_name,
            "capacity_minutes": 0,
            "start_minute": 0,
            "note": "",
        }
    return {
        "profile_name": profile["profile_name"],
        "capacity_minutes": int(profile["capacity_minutes"] or 0),
        "start_minute": int(profile["start_minute"] or 0),
        "note": profile["note"] or "",
    }


def capacity_minutes_for_machine_day(con, machine_id, work_date):
    cap = machine_capacity_for_date(con, machine_id, work_date)
    return {
        "profile_name": cap["profile_name"],
        "capacity_minutes": int(cap["capacity_minutes"] or 0),
        "start_minute": int(cap["start_minute"] or 0),
        "note": cap.get("note", ""),
    }


def fetch_machines(con):
    return rows(con.execute("SELECT machine_id, machine_code, machine_category, shift_profile, active FROM machines WHERE active = 1 ORDER BY machine_id"))


def fetch_profiles(con):
    return rows(con.execute("SELECT profile_id, profile_name, capacity_minutes, start_minute, note FROM capacity_profile ORDER BY profile_id"))
