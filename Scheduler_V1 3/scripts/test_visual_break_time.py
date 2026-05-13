from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scheduler_app.visual_time import (
    add_productive_minutes_visual,
    break_windows_for_date,
    visual_timing_for_segment,
    stored_datetime_to_visual_datetime,
)


def parse_dt(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("T", " "))


def assert_visual_end(start_text: str, minutes: int, work_date: str, profile_name: str, expected_end: str) -> None:
    start_dt = parse_dt(start_text)
    windows = break_windows_for_date(datetime.fromisoformat(work_date).date(), profile_name=profile_name)
    visual_end = add_productive_minutes_visual(start_dt, minutes, windows)
    assert visual_end is not None, f"visual end missing for {start_text}"
    actual = visual_end.strftime("%Y-%m-%d %H:%M")
    assert actual == expected_end, f"expected {expected_end}, got {actual}"


def assert_grouped_block_end(setup_end: str, production_end: str, expected_end: str) -> None:
    setup_dt = parse_dt(setup_end)
    production_dt = parse_dt(production_end)
    grouped_end = max(setup_dt, production_dt)
    actual = grouped_end.strftime("%Y-%m-%d %H:%M")
    assert actual == expected_end, f"expected grouped end {expected_end}, got {actual}"


def main() -> None:
    monday_windows = break_windows_for_date(datetime.fromisoformat("2026-05-11").date(), profile_name="")
    full_24h_windows = break_windows_for_date(datetime.fromisoformat("2026-05-11").date(), profile_name="FULL_24H")

    assert stored_datetime_to_visual_datetime(parse_dt("2026-05-11 11:01:00"), monday_windows).strftime("%Y-%m-%d %H:%M") == "2026-05-11 11:01"
    assert stored_datetime_to_visual_datetime(parse_dt("2026-05-11 12:01:00"), monday_windows).strftime("%Y-%m-%d %H:%M") == "2026-05-11 12:46"
    assert stored_datetime_to_visual_datetime(parse_dt("2026-05-11 14:10:00"), monday_windows).strftime("%Y-%m-%d %H:%M") == "2026-05-11 14:55"
    assert stored_datetime_to_visual_datetime(parse_dt("2026-05-04 10:30:00"), monday_windows).strftime("%Y-%m-%d %H:%M") == "2026-05-04 10:30"
    assert stored_datetime_to_visual_datetime(parse_dt("2026-05-04 15:10:00"), monday_windows).strftime("%Y-%m-%d %H:%M") == "2026-05-04 15:55"

    assert_visual_end("2026-05-11 11:30:00", 90, "2026-05-11", "", "2026-05-11 13:45")
    assert_visual_end("2026-05-11 15:50:00", 30, "2026-05-11", "", "2026-05-11 16:35")
    assert_visual_end("2026-05-16 11:30:00", 90, "2026-05-16", "", "2026-05-16 13:45")
    assert_visual_end("2026-05-16 15:50:00", 30, "2026-05-16", "", "2026-05-16 16:20")
    assert_visual_end("2026-05-11 11:30:00", 90, "2026-05-11", "FULL_24H", "2026-05-11 13:00")

    start_a = parse_dt("2026-05-11 11:01:00")
    end_a = parse_dt("2026-05-11 12:01:00")
    start_b = parse_dt("2026-05-11 12:01:00")
    end_b = parse_dt("2026-05-11 14:10:00")
    visual_start_a = stored_datetime_to_visual_datetime(start_a, monday_windows)
    visual_end_a = add_productive_minutes_visual(start_a, 60, monday_windows)
    visual_start_b = stored_datetime_to_visual_datetime(start_b, monday_windows)
    visual_end_b = add_productive_minutes_visual(start_b, 130, monday_windows)
    assert visual_start_a.strftime("%Y-%m-%d %H:%M") == "2026-05-11 11:01"
    assert visual_end_a.strftime("%Y-%m-%d %H:%M") == "2026-05-11 12:46"
    assert visual_start_b.strftime("%Y-%m-%d %H:%M") == "2026-05-11 12:46"
    assert visual_end_b.strftime("%Y-%m-%d %H:%M") == "2026-05-11 14:55"
    assert visual_end_a == visual_start_b

    grouped_setup = visual_timing_for_segment(
        parse_dt("2026-05-04 10:30:00"),
        280,
        end_dt=parse_dt("2026-05-04 15:10:00"),
        work_date=datetime.fromisoformat("2026-05-04").date(),
        profile_name="",
        shift_profile="",
        segment_type="setup",
    )
    grouped_production = visual_timing_for_segment(
        parse_dt("2026-05-04 15:10:00"),
        170,
        end_dt=parse_dt("2026-05-04 18:00:00"),
        work_date=datetime.fromisoformat("2026-05-04").date(),
        profile_name="",
        shift_profile="",
        segment_type="production",
    )
    assert grouped_setup["visual_end_datetime"] == "2026-05-04 15:55:00"
    assert grouped_production["visual_start_datetime"] == "2026-05-04 15:55:00"
    assert grouped_setup["visual_end_datetime"] == grouped_production["visual_start_datetime"]

    visual_start_24h = stored_datetime_to_visual_datetime(parse_dt("2026-05-11 11:30:00"), full_24h_windows)
    assert visual_start_24h.strftime("%Y-%m-%d %H:%M") == "2026-05-11 11:30"
    print("visual break timing ok")


if __name__ == "__main__":
    main()
