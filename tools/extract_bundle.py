from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "All code.txt"


def write_range(path, start, end):
    lines = SRC.read_text(encoding="utf-8", errors="replace").splitlines()
    text = "\n".join(lines[start - 1:end]).rstrip() + "\n"
    out = ROOT / path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")


FILES = [
    ("templates/base.html", 1, 74),
    ("templates/index.html", 103, 107),
    ("static/css/main.css", 138, 712),
    ("static/js/utils.js", 743, 867),
    ("templates/process_sheets.html", 897, 1454),
    ("templates/parts_flows.html", 1488, 1818),
    ("templates/machines.html", 1849, 1963),
    ("templates/calendar.html", 1993, 2133),
    ("templates/materials.html", 2162, 2330),
    ("templates/staffing.html", 2363, 2510),
    ("templates/summary.html", 2543, 2611),
    ("templates/gantt.html", 2646, 2991),
    ("templates/history.html", 3021, 3069),
    ("requirements.txt", 3101, 3101),
    ("INSTALL_DEPS.bat", 3134, 3173),
    ("RUN_APP.bat", 3207, 3242),
    ("RESET_DEMO_DB.bat", 3276, 3323),
    ("ENVIRONMENT_NOTES.txt", 3357, 3427),
    ("README.md", 3461, 3642),
]


for file_name, start, end in FILES:
    write_range(file_name, start, end)

print(f"Extracted {len(FILES)} files.")
