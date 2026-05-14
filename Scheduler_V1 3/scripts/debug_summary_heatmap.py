from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import app


def iso_date(value: date) -> str:
    return value.strftime("%Y-%m-%d")


def parse_iso_date(text: str) -> date:
    return date.fromisoformat(str(text).strip()[:10])


def monday_saturday_for_today() -> tuple[str, str]:
    today = date.today()
    weekday = today.weekday()  # Monday=0
    monday = today - timedelta(days=weekday)
    saturday = monday + timedelta(days=5)
    return iso_date(monday), iso_date(saturday)


def main() -> int:
    parser = argparse.ArgumentParser(description="Debug Summary heatmap cells")
    parser.add_argument("--from", dest="from_date", help="Range start YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", help="Range end YYYY-MM-DD")
    parser.add_argument("--category", default="all", help="Machine category filter")
    args = parser.parse_args()

    if args.from_date and args.to_date:
        start = args.from_date
        end = args.to_date
    else:
        start, end = monday_saturday_for_today()

    params = {
        "from": start,
        "to": end,
        "view": "machines",
    }
    if args.category and args.category != "all":
        params["category"] = args.category

    with app.test_client() as client:
        response = client.get("/api/trial/summary", query_string=params)
        if response.status_code != 200:
            print(f"HTTP {response.status_code}")
            print(response.get_data(as_text=True))
            return 1
        payload = response.get_json(force=True) or {}

    heatmap = payload.get("heatmap") or []
    print(f"Range: {start} -> {end}")
    print(f"Heatmap cells: {len(heatmap)}")
    print()
    print("machine_code\tdate\tplanned_minutes\tcapacity_minutes\traw_load_pct\tstatus")
    for cell in heatmap:
        print(
            f"{cell.get('machine_code','')}\t"
            f"{cell.get('plan_date','')}\t"
            f"{float(cell.get('planned_minutes') or 0):.2f}\t"
            f"{int(cell.get('capacity_minutes') or 0)}\t"
            f"{float(cell.get('raw_load_pct') or 0):.1f}\t"
            f"{cell.get('status','')}"
        )

    suspicious = []
    for cell in heatmap:
        planned = float(cell.get("planned_minutes") or 0)
        capacity = int(cell.get("capacity_minutes") or 0)
        raw = float(cell.get("raw_load_pct") or 0)
        reason = []
        if planned > 0 and capacity == 0:
            reason.append("planned>0 capacity=0")
        if raw > 100:
            reason.append("raw_load>100")
        if planned > 1440:
            reason.append("planned_minutes>1440")
        if capacity < 60 and planned > 60:
            reason.append("capacity<60 planned>60")
        if planned > 0 and 0 < capacity < 60 and planned < 12:
            reason.append("possible hours/minutes mismatch")
        if reason:
            suspicious.append((cell, reason))

    if suspicious:
        print()
        print("Suspicious cells:")
        for cell, reason in suspicious:
            print(
                f"- {cell.get('machine_code','')} {cell.get('plan_date','')}: "
                f"planned={float(cell.get('planned_minutes') or 0):.2f} min, "
                f"capacity={int(cell.get('capacity_minutes') or 0)} min, "
                f"raw={float(cell.get('raw_load_pct') or 0):.1f}%, "
                f"status={cell.get('status','')} :: {', '.join(reason)}"
            )
    else:
        print()
        print("No suspicious cells found.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
