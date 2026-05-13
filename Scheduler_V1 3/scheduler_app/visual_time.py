from __future__ import annotations

from datetime import date, datetime, timedelta


def _normalize_profile_text(profile_name="", shift_profile=""):
    return f"{profile_name or ''} {shift_profile or ''}".strip().upper()


def is_24hr_profile(profile_name="", shift_profile=""):
    text = _normalize_profile_text(profile_name, shift_profile)
    return "24HR" in text or "FULL_24H" in text


def _break_duration_minutes(window):
    start = int(window.get("start_minute") or 0)
    end = int(window.get("end_minute") or 0)
    return max(0, end - start)


def _coerce_datetime(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("T", " "))
        except ValueError:
            return None
    return None


def break_windows_for_date(work_date, profile_name="", shift_profile=""):
    if work_date is None or is_24hr_profile(profile_name, shift_profile):
        return []
    if isinstance(work_date, datetime):
        work_day = work_date.date()
    elif isinstance(work_date, date):
        work_day = work_date
    else:
        work_day = date.fromisoformat(str(work_date))

    weekday = work_day.weekday()
    if weekday == 6:
        return []

    windows = [("Lunch", 12 * 60, 12 * 60 + 45)]
    if weekday <= 4:
        windows.append(("Coffee", 16 * 60, 16 * 60 + 15))

    result = []
    for label, start_minute, end_minute in windows:
        start_dt = datetime.combine(work_day, datetime.min.time()) + timedelta(minutes=start_minute)
        end_dt = datetime.combine(work_day, datetime.min.time()) + timedelta(minutes=end_minute)
        result.append(
            {
                "kind": "break",
                "label": label,
                "start_datetime": start_dt.isoformat(sep=" ", timespec="seconds"),
                "end_datetime": end_dt.isoformat(sep=" ", timespec="seconds"),
                "start_minute": start_minute,
                "end_minute": end_minute,
            }
        )
    return result


def stored_datetime_to_visual_datetime(dt, break_windows):
    dt = _coerce_datetime(dt)
    if dt is None:
        return None
    offset_minutes = 0
    minute_of_day = dt.hour * 60 + dt.minute
    for window in sorted(break_windows or [], key=lambda item: int(item.get("start_minute") or 0)):
        start_minute = int(window.get("start_minute") or 0)
        if minute_of_day > start_minute:
            offset_minutes += _break_duration_minutes(window)
    return dt + timedelta(minutes=offset_minutes)


def add_productive_minutes_visual(start_dt, productive_minutes, break_windows):
    if start_dt is None:
        return None
    remaining = max(0.0, float(productive_minutes or 0))
    cursor = start_dt
    for window in sorted(break_windows or [], key=lambda item: item.get("start_datetime") or ""):
        if remaining <= 0:
            break
        window_start = datetime.fromisoformat(str(window.get("start_datetime") or "").replace("T", " "))
        window_end = datetime.fromisoformat(str(window.get("end_datetime") or "").replace("T", " "))
        if window_end <= cursor:
            continue
        if cursor < window_start:
            gap_minutes = max(0.0, (window_start - cursor).total_seconds() / 60.0)
            if remaining <= gap_minutes:
                return cursor + timedelta(minutes=remaining)
            remaining -= gap_minutes
            cursor = window_end
            continue
        if cursor < window_end:
            cursor = window_end
    if remaining > 0:
        cursor = cursor + timedelta(minutes=remaining)
    return cursor


def visual_parts_for_segment(start_dt, productive_minutes, break_windows, segment_type="production", visual_start_dt=None):
    start_dt = _coerce_datetime(start_dt)
    visual_start_dt = _coerce_datetime(visual_start_dt) or (stored_datetime_to_visual_datetime(start_dt, break_windows) if start_dt is not None else None)
    if start_dt is None or visual_start_dt is None:
        return []
    remaining = max(0.0, float(productive_minutes or 0))
    raw_cursor = start_dt
    visual_cursor = visual_start_dt
    parts = []
    window_list = sorted(break_windows or [], key=lambda item: item.get("start_datetime") or "")
    for window in window_list:
        if remaining <= 0:
            break
        window_start = datetime.fromisoformat(str(window.get("start_datetime") or "").replace("T", " "))
        window_end = datetime.fromisoformat(str(window.get("end_datetime") or "").replace("T", " "))
        if window_end <= raw_cursor:
            continue
        if raw_cursor < window_start:
            work_end = min(window_start, raw_cursor + timedelta(minutes=remaining))
            if work_end > raw_cursor:
                work_minutes = max(0.0, (work_end - raw_cursor).total_seconds() / 60.0)
                parts.append(
                    {
                        "kind": "productive",
                        "label": segment_type or "production",
                        "start_datetime": visual_cursor.isoformat(sep=" ", timespec="seconds"),
                        "end_datetime": (visual_cursor + timedelta(minutes=work_minutes)).isoformat(sep=" ", timespec="seconds"),
                        "minutes": work_minutes,
                    }
                )
                remaining -= work_minutes
                raw_cursor = work_end
                visual_cursor = visual_cursor + timedelta(minutes=work_minutes)
            if remaining <= 0:
                break
        if raw_cursor < window_end and remaining > 0:
            break_start = max(raw_cursor, window_start)
            break_end = window_end
            break_minutes = max(0.0, (break_end - break_start).total_seconds() / 60.0)
            parts.append(
                {
                    "kind": "break",
                    "label": window.get("label") or "Break",
                    "start_datetime": break_start.isoformat(sep=" ", timespec="seconds"),
                    "end_datetime": break_end.isoformat(sep=" ", timespec="seconds"),
                    "minutes": break_minutes,
                }
            )
            raw_cursor = break_end
            visual_cursor = visual_cursor + timedelta(minutes=break_minutes)
    if remaining > 0:
        work_end = raw_cursor + timedelta(minutes=remaining)
        parts.append(
            {
                "kind": "productive",
                "label": segment_type or "production",
                "start_datetime": visual_cursor.isoformat(sep=" ", timespec="seconds"),
                "end_datetime": (visual_cursor + timedelta(minutes=remaining)).isoformat(sep=" ", timespec="seconds"),
                "minutes": remaining,
            }
        )
    return parts


def visual_timing_for_segment(start_dt, productive_minutes, end_dt=None, work_date=None, profile_name="", shift_profile="", segment_type="production"):
    start_dt = _coerce_datetime(start_dt)
    end_dt = _coerce_datetime(end_dt)
    if start_dt is None:
        return {
            "visual_start_datetime": "",
            "visual_end_datetime": "",
            "visual_parts": [],
            "break_windows": [],
        }
    if work_date is None:
        work_date = start_dt.date()
    windows = break_windows_for_date(work_date, profile_name=profile_name, shift_profile=shift_profile)
    visual_start_dt = stored_datetime_to_visual_datetime(start_dt, windows)
    if end_dt is not None:
        visual_end_dt = stored_datetime_to_visual_datetime(end_dt, windows)
    else:
        visual_end_dt = add_productive_minutes_visual(visual_start_dt, productive_minutes, windows)
    return {
        "visual_start_datetime": visual_start_dt.isoformat(sep=" ", timespec="seconds") if visual_start_dt else "",
        "visual_end_datetime": visual_end_dt.isoformat(sep=" ", timespec="seconds") if visual_end_dt else "",
        "visual_parts": visual_parts_for_segment(start_dt, productive_minutes, windows, segment_type=segment_type, visual_start_dt=visual_start_dt),
        "break_windows": windows,
    }
