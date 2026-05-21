from __future__ import annotations

import math
from datetime import datetime

from .db import one

DEFAULT_PLANNING_EFFICIENCY = 0.85
DEFAULT_PLANNING_CALENDAR_POLICY = "MON_FRI_ONLY"
DEFAULT_PLANNING_START_TIME = "08:30"


def _clamp_efficiency(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return DEFAULT_PLANNING_EFFICIENCY
    if not math.isfinite(parsed):
        return DEFAULT_PLANNING_EFFICIENCY
    return max(0.1, min(1.5, parsed))


def get_planning_setting(con, key, default=None):
    row = one(
        con.execute(
            """
            SELECT setting_value
            FROM planning_setting
            WHERE setting_key = ?
            """,
            (str(key),),
        )
    )
    if not row:
        return default
    value = row["setting_value"]
    return value if value is not None and str(value).strip() != "" else default


def set_planning_setting(con, key, value):
    con.execute(
        """
        INSERT INTO planning_setting (setting_key, setting_value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(setting_key) DO UPDATE SET
          setting_value = excluded.setting_value,
          updated_at = CURRENT_TIMESTAMP
        """,
        (str(key), str(value)),
    )


def get_planning_efficiency(con):
    return _clamp_efficiency(get_planning_setting(con, "planning_efficiency", DEFAULT_PLANNING_EFFICIENCY))


def get_planning_calendar_policy(con):
    return str(get_planning_setting(con, "planning_calendar_policy", DEFAULT_PLANNING_CALENDAR_POLICY) or DEFAULT_PLANNING_CALENDAR_POLICY).upper()


def get_planning_start_time_text(con):
    value = str(get_planning_setting(con, "planning_start_time", DEFAULT_PLANNING_START_TIME) or DEFAULT_PLANNING_START_TIME).strip()
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError:
        return DEFAULT_PLANNING_START_TIME
    return value


def get_planning_start_minute(con):
    text = get_planning_start_time_text(con)
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError:
        return 8 * 60 + 30
    return parsed.hour * 60 + parsed.minute

