from __future__ import annotations

from datetime import date, datetime, timedelta

from .constants import TRIAL_MACHINES
from .db import one, rows
from .utils import compact_text


STANDARD_WORK_START_MINUTE = 8 * 60 + 30
STANDARD_WORK_END_MINUTE = 16 * 60 + 30
SATURDAY_WORK_END_MINUTE = 16 * 60 + 15
WEEKDAY_LUNCH_START_MINUTE = 12 * 60
WEEKDAY_LUNCH_END_MINUTE = 12 * 60 + 45
WEEKDAY_COFFEE_START_MINUTE = 16 * 60
WEEKDAY_COFFEE_END_MINUTE = 16 * 60 + 15
DAY_END_MINUTE = 24 * 60


def default_profile_for_weekday(weekday, shift_profile="STANDARD"):
    if weekday == 6:
        return "OFF"
    if weekday == 5:
        return "SATURDAY"
    if compact_text(shift_profile).upper() == "24HR":
        return "FULL_24H"
    return "NORMAL_DAY_NIGHT"


def minutes_to_time_text(minutes):
    value = max(0, min(DAY_END_MINUTE, int(round(float(minutes or 0)))))
    if value == DAY_END_MINUTE:
        return "24:00"
    return f"{value // 60:02d}:{value % 60:02d}"


def datetime_for_date_minute(date_iso, minute):
    work_day = date.fromisoformat(str(date_iso))
    minute_value = max(0, min(DAY_END_MINUTE, int(round(float(minute or 0)))))
    return datetime.combine(work_day, datetime.min.time()) + timedelta(minutes=minute_value)


def _machine_shift_profile(con, machine_id):
    row = one(con.execute("SELECT machine_id, shift_profile FROM machines WHERE machine_id = ?", (int(machine_id),)))
    return compact_text(row["shift_profile"]) if row else ""


def machine_capacity_details_for_date(con, machine_id, work_date):
    work_day = work_date
    row = one(
        con.execute(
            """
            SELECT d.day_id, d.machine_id, d.work_date, d.profile_id, d.capacity_minutes, d.start_minute, d.note,
                   p.profile_name
            FROM machine_capacity_day d
            JOIN capacity_profile p ON p.profile_id = d.profile_id
            WHERE d.machine_id = ? AND d.work_date = ?
            """,
            (int(machine_id), work_day.strftime("%Y-%m-%d")),
        )
    )
    if row:
        return {
            "profile_name": row["profile_name"],
            "capacity_minutes": int(row["capacity_minutes"] or 0),
            "start_minute": int(row["start_minute"] or 0),
            "note": row["note"] or "",
            "has_capacity_day_override": True,
        }

    if work_day.weekday() == 6 or is_public_holiday(con, work_day):
        profile = one(con.execute("SELECT * FROM capacity_profile WHERE profile_name = 'OFF'"))
        if profile:
            return {
                "profile_name": profile["profile_name"],
                "capacity_minutes": int(profile["capacity_minutes"] or 0),
                "start_minute": int(profile["start_minute"] or 0),
                "note": profile["note"] or "",
                "has_capacity_day_override": False,
            }
        return {
            "profile_name": "OFF",
            "capacity_minutes": 0,
            "start_minute": 0,
            "note": "",
            "has_capacity_day_override": False,
        }

    machine_shift_profile = _machine_shift_profile(con, machine_id) or "STANDARD"
    profile_name = default_profile_for_weekday(work_day.weekday(), machine_shift_profile)
    profile = one(con.execute("SELECT * FROM capacity_profile WHERE profile_name = ?", (profile_name,)))
    if not profile:
        profile = one(con.execute("SELECT * FROM capacity_profile ORDER BY profile_id LIMIT 1"))
    if not profile:
        return {
            "profile_name": profile_name,
            "capacity_minutes": 0,
            "start_minute": 0,
            "note": "",
            "has_capacity_day_override": False,
        }
    return {
        "profile_name": profile["profile_name"],
        "capacity_minutes": int(profile["capacity_minutes"] or 0),
        "start_minute": int(profile["start_minute"] or 0),
        "note": profile["note"] or "",
        "has_capacity_day_override": False,
    }


def machine_capacity_for_date(con, machine_id, work_date):
    return machine_capacity_details_for_date(con, machine_id, work_date)


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


def capacity_minutes_for_machine_day(con, machine_id, work_date):
    cap = machine_capacity_for_date(con, machine_id, work_date)
    return {
        "profile_name": cap["profile_name"],
        "capacity_minutes": int(cap["capacity_minutes"] or 0),
        "start_minute": int(cap["start_minute"] or 0),
        "note": cap.get("note", ""),
        "has_capacity_day_override": bool(cap.get("has_capacity_day_override")),
    }


def shift_windows_for_machine_day(machine, date_iso, con=None):
    work_day = date.fromisoformat(str(date_iso))
    machine = machine or {}
    shift_profile = compact_text(machine.get("shift_profile") or "")
    machine_id = int(machine.get("machine_id") or 0)
    cap = machine_capacity_details_for_date(con, machine_id, work_day) if con and machine_id else None
    if cap and bool(cap.get("has_capacity_day_override")) and int(cap.get("capacity_minutes") or 0) > 0:
        start_minute = int(cap.get("start_minute") or 0)
        end_minute = min(DAY_END_MINUTE, start_minute + int(cap.get("capacity_minutes") or 0))
        if end_minute > start_minute:
            return [
                {
                    "kind": "override",
                    "start_minute": start_minute,
                    "end_minute": end_minute,
                    "start_datetime": datetime_for_date_minute(date_iso, start_minute).strftime("%Y-%m-%d %H:%M:%S"),
                    "end_datetime": datetime_for_date_minute(date_iso, end_minute).strftime("%Y-%m-%d %H:%M:%S"),
                    "reason": "Capacity override",
                }
            ]
        return []

    if work_day.weekday() == 6:
        return []
    if shift_profile == "24HR":
        return [
            {
                "kind": "full",
                "start_minute": 0,
                "end_minute": DAY_END_MINUTE,
                "start_datetime": datetime_for_date_minute(date_iso, 0).strftime("%Y-%m-%d %H:%M:%S"),
                "end_datetime": datetime_for_date_minute(date_iso, DAY_END_MINUTE).strftime("%Y-%m-%d %H:%M:%S"),
                "reason": "24HR shift",
            }
        ]
    if work_day.weekday() == 5:
        return [
            {
                "kind": "standard",
                "start_minute": STANDARD_WORK_START_MINUTE,
                "end_minute": WEEKDAY_LUNCH_START_MINUTE,
                "start_datetime": datetime_for_date_minute(date_iso, STANDARD_WORK_START_MINUTE).strftime("%Y-%m-%d %H:%M:%S"),
                "end_datetime": datetime_for_date_minute(date_iso, WEEKDAY_LUNCH_START_MINUTE).strftime("%Y-%m-%d %H:%M:%S"),
                "reason": "Saturday shift",
            },
            {
                "kind": "standard",
                "start_minute": WEEKDAY_LUNCH_END_MINUTE,
                "end_minute": SATURDAY_WORK_END_MINUTE,
                "start_datetime": datetime_for_date_minute(date_iso, WEEKDAY_LUNCH_END_MINUTE).strftime("%Y-%m-%d %H:%M:%S"),
                "end_datetime": datetime_for_date_minute(date_iso, SATURDAY_WORK_END_MINUTE).strftime("%Y-%m-%d %H:%M:%S"),
                "reason": "Saturday shift",
            },
        ]
    return [
        {
            "kind": "standard",
            "start_minute": STANDARD_WORK_START_MINUTE,
            "end_minute": WEEKDAY_LUNCH_START_MINUTE,
            "start_datetime": datetime_for_date_minute(date_iso, STANDARD_WORK_START_MINUTE).strftime("%Y-%m-%d %H:%M:%S"),
            "end_datetime": datetime_for_date_minute(date_iso, WEEKDAY_LUNCH_START_MINUTE).strftime("%Y-%m-%d %H:%M:%S"),
            "reason": "Weekday shift",
        },
        {
            "kind": "standard",
            "start_minute": WEEKDAY_LUNCH_END_MINUTE,
            "end_minute": WEEKDAY_COFFEE_START_MINUTE,
            "start_datetime": datetime_for_date_minute(date_iso, WEEKDAY_LUNCH_END_MINUTE).strftime("%Y-%m-%d %H:%M:%S"),
            "end_datetime": datetime_for_date_minute(date_iso, WEEKDAY_COFFEE_START_MINUTE).strftime("%Y-%m-%d %H:%M:%S"),
            "reason": "Weekday shift",
        },
        {
            "kind": "standard",
            "start_minute": WEEKDAY_COFFEE_END_MINUTE,
            "end_minute": STANDARD_WORK_END_MINUTE,
            "start_datetime": datetime_for_date_minute(date_iso, WEEKDAY_COFFEE_END_MINUTE).strftime("%Y-%m-%d %H:%M:%S"),
            "end_datetime": datetime_for_date_minute(date_iso, STANDARD_WORK_END_MINUTE).strftime("%Y-%m-%d %H:%M:%S"),
            "reason": "Weekday shift",
        },
    ]


def working_windows_for_machine_day(machine, date_iso, con=None):
    return shift_windows_for_machine_day(machine, date_iso, con)


def break_windows_for_machine_day(machine, date_iso, con=None):
    work_day = date.fromisoformat(str(date_iso))
    machine = machine or {}
    shift_profile = compact_text(machine.get("shift_profile") or "")
    machine_id = int(machine.get("machine_id") or 0)
    cap = machine_capacity_details_for_date(con, machine_id, work_day) if con and machine_id else None
    if shift_profile == "24HR":
        return []
    if work_day.weekday() == 6:
        return []
    if work_day.weekday() == 5:
        windows = [(WEEKDAY_LUNCH_START_MINUTE, WEEKDAY_LUNCH_END_MINUTE, "Lunch break", "lunch")]
    else:
        windows = [
            (WEEKDAY_LUNCH_START_MINUTE, WEEKDAY_LUNCH_END_MINUTE, "Lunch break", "lunch"),
            (WEEKDAY_COFFEE_START_MINUTE, WEEKDAY_COFFEE_END_MINUTE, "Coffee break", "coffee"),
        ]

    work_windows = shift_windows_for_machine_day(machine, date_iso, con)
    if not work_windows:
        return []
    work_start = min(int(window["start_minute"]) for window in work_windows)
    work_end = max(int(window["end_minute"]) for window in work_windows)
    result = []
    for start_minute, end_minute, reason, kind in windows:
        if end_minute <= work_start or start_minute >= work_end:
            continue
        result.append(
            {
                "kind": kind,
                "start_minute": start_minute,
                "end_minute": end_minute,
                "start_datetime": datetime_for_date_minute(date_iso, start_minute).strftime("%Y-%m-%d %H:%M:%S"),
                "end_datetime": datetime_for_date_minute(date_iso, end_minute).strftime("%Y-%m-%d %H:%M:%S"),
                "reason": reason,
            }
        )
    return result


def off_windows_for_machine_day(machine, date_iso, con=None):
    work_day = date.fromisoformat(str(date_iso))
    machine = machine or {}
    shift_profile = compact_text(machine.get("shift_profile") or "")
    machine_id = int(machine.get("machine_id") or 0)
    cap = machine_capacity_details_for_date(con, machine_id, work_day) if con and machine_id else None
    if cap and bool(cap.get("has_capacity_day_override")) and int(cap.get("capacity_minutes") or 0) <= 0:
        kind = "override"
        reason = "Capacity override off"
        return [
            {
                "kind": kind,
                "start_minute": 0,
                "end_minute": DAY_END_MINUTE,
                "start_datetime": datetime_for_date_minute(date_iso, 0).strftime("%Y-%m-%d %H:%M:%S"),
                "end_datetime": datetime_for_date_minute(date_iso, DAY_END_MINUTE).strftime("%Y-%m-%d %H:%M:%S"),
                "reason": reason,
                "title": reason,
            }
        ]
    if cap and int(cap.get("capacity_minutes") or 0) <= 0:
        kind = "sunday" if work_day.weekday() == 6 else "override"
        reason = "Sunday off" if kind == "sunday" else "Capacity override off"
        return [
            {
                "kind": kind,
                "start_minute": 0,
                "end_minute": DAY_END_MINUTE,
                "start_datetime": datetime_for_date_minute(date_iso, 0).strftime("%Y-%m-%d %H:%M:%S"),
                "end_datetime": datetime_for_date_minute(date_iso, DAY_END_MINUTE).strftime("%Y-%m-%d %H:%M:%S"),
                "reason": reason,
                "title": reason,
            }
        ]

    if work_day.weekday() == 6 and not (cap and bool(cap.get("has_capacity_day_override"))):
        return [
            {
                "kind": "sunday",
                "start_minute": 0,
                "end_minute": DAY_END_MINUTE,
                "start_datetime": datetime_for_date_minute(date_iso, 0).strftime("%Y-%m-%d %H:%M:%S"),
                "end_datetime": datetime_for_date_minute(date_iso, DAY_END_MINUTE).strftime("%Y-%m-%d %H:%M:%S"),
                "reason": "Sunday off",
                "title": "Sunday off",
            }
        ]

    work_windows = shift_windows_for_machine_day(machine, date_iso, con)
    if not work_windows:
        if shift_profile == "24HR":
            return []
        return [
            {
                "kind": "override" if cap and bool(cap.get("has_capacity_day_override")) else "sunday",
                "start_minute": 0,
                "end_minute": DAY_END_MINUTE,
                "start_datetime": datetime_for_date_minute(date_iso, 0).strftime("%Y-%m-%d %H:%M:%S"),
                "end_datetime": datetime_for_date_minute(date_iso, DAY_END_MINUTE).strftime("%Y-%m-%d %H:%M:%S"),
                "reason": "Capacity override off" if cap and bool(cap.get("has_capacity_day_override")) else "Sunday off",
                "title": "Capacity override off" if cap and bool(cap.get("has_capacity_day_override")) else "Sunday off",
            }
        ]

    blocks = []
    work_start = min(int(window["start_minute"]) for window in work_windows)
    work_end = max(int(window["end_minute"]) for window in work_windows)
    if work_start > 0:
        blocks.append(
            {
                "kind": "off_shift",
                "start_minute": 0,
                "end_minute": work_start,
                "start_datetime": datetime_for_date_minute(date_iso, 0).strftime("%Y-%m-%d %H:%M:%S"),
                "end_datetime": datetime_for_date_minute(date_iso, work_start).strftime("%Y-%m-%d %H:%M:%S"),
                "reason": "Before shift",
                "title": f"Off shift: 00:00-{minutes_to_time_text(work_start)}",
            }
        )

    for break_window in break_windows_for_machine_day(machine, date_iso, con):
        blocks.append(
            {
                "kind": break_window["kind"],
                "start_minute": int(break_window["start_minute"] or 0),
                "end_minute": int(break_window["end_minute"] or 0),
                "start_datetime": break_window["start_datetime"],
                "end_datetime": break_window["end_datetime"],
                "reason": break_window["reason"],
                "title": f"{break_window['reason']}: {minutes_to_time_text(break_window['start_minute'])}-{minutes_to_time_text(break_window['end_minute'])}",
            }
        )

    if work_end < DAY_END_MINUTE:
        blocks.append(
            {
                "kind": "off_shift",
                "start_minute": work_end,
                "end_minute": DAY_END_MINUTE,
                "start_datetime": datetime_for_date_minute(date_iso, work_end).strftime("%Y-%m-%d %H:%M:%S"),
                "end_datetime": datetime_for_date_minute(date_iso, DAY_END_MINUTE).strftime("%Y-%m-%d %H:%M:%S"),
                "reason": "After shift",
                "title": f"Off shift: {minutes_to_time_text(work_end)}-24:00",
            }
        )

    return blocks


def gantt_off_time_blocks(machines, dates, con=None):
    blocks = []
    for machine in machines or []:
        machine_id = int(machine.get("machine_id") or 0)
        if not machine_id:
            continue
        machine_code = compact_text(machine.get("machine_code") or "")
        for work_date_text in dates or []:
            for window in off_windows_for_machine_day(machine, work_date_text, con):
                blocks.append(
                    {
                        "machine_id": machine_id,
                        "machine_code": machine_code,
                        "date": work_date_text,
                        "start_min": int(window.get("start_minute") or 0),
                        "end_min": int(window.get("end_minute") or 0),
                        "start_datetime": window.get("start_datetime") or "",
                        "end_datetime": window.get("end_datetime") or "",
                        "reason": window.get("reason") or "",
                        "kind": window.get("kind") or "",
                        "title": window.get("title") or "",
                    }
                )
    return blocks


def fetch_machines(con):
    return rows(con.execute("SELECT machine_id, machine_code, machine_category, shift_profile, active FROM machines WHERE active = 1 ORDER BY machine_id"))


def fetch_profiles(con):
    return rows(con.execute("SELECT profile_id, profile_name, capacity_minutes, start_minute, note FROM capacity_profile ORDER BY profile_id"))
