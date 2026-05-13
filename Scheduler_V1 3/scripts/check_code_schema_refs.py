from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_ROOT = ROOT / "scheduler_app"

FORBIDDEN = [
    "trial_run_block",
    "trial_run_block_segment",
    "trial_operation",
    "trial_production_actual",
    "trial_material_requirement",
    "trial_bom_material",
    "part_flow_header",
    "part_flow_steps",
    "selected_flow_id",
]

ALIAS_OK_PATTERNS = [
    re.compile(r"\bAS\s+flow_id\b", re.IGNORECASE),
    re.compile(r"\bAS\s+flow_code\b", re.IGNORECASE),
    re.compile(r"\bAS\s+flow_name\b", re.IGNORECASE),
    re.compile(r"\bAS\s+step_id\b", re.IGNORECASE),
    re.compile(r"\bAS\s+source_step_id\b", re.IGNORECASE),
]


def main():
    problems = []
    warnings = []
    for path in ACTIVE_ROOT.rglob("*"):
        if path.suffix.lower() not in {".py", ".html", ".sql", ".js", ".css"}:
            continue
        text = path.read_text(errors="ignore")
        for needle in FORBIDDEN:
            if needle in text:
                problems.append(f"{path}: contains forbidden reference {needle}")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "flow_id" in line or "step_id" in line:
                if any(pattern.search(line) for pattern in ALIAS_OK_PATTERNS):
                    continue
                if "selected_flow_id" in line:
                    problems.append(f"{path}:{lineno}: uses forbidden selected_flow_id")
                elif "flow_id" in line:
                    warnings.append(f"{path}:{lineno}: flow_id should only appear as an API alias")
                elif "step_id" in line:
                    warnings.append(f"{path}:{lineno}: step_id should only appear as an API alias")

    for line in problems:
        print(line)
    for line in warnings:
        print("WARN:", line)

    if problems:
        return 1
    print("Code schema reference check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
