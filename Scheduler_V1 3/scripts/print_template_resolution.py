from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scheduler_app import create_app


def main() -> None:
    repo_root = REPO_ROOT
    app = create_app()

    print(f"app.root_path: {app.root_path}")
    print(f"app.template_folder: {app.template_folder}")

    loader = app.jinja_loader
    searchpath = getattr(loader, "searchpath", None)
    if searchpath is not None:
        print("jinja_loader.searchpath:")
        for path in searchpath:
            print(f"  - {path}")

    print("blueprints:")
    for name, blueprint in app.blueprints.items():
        template_folder = getattr(blueprint, "template_folder", None)
        if template_folder:
            print(f"  - {name}: template_folder={template_folder}")
        else:
            print(f"  - {name}: template_folder=<none>")

    print("process_sheets.html files:")
    files = sorted(repo_root.rglob("process_sheets.html"))
    for path in files:
        if any(part in {".git", "__pycache__", ".venv", "venv", "node_modules"} for part in path.parts):
            continue
        print(f"  - {path.relative_to(repo_root)}")

    active = repo_root / "scheduler_app" / "templates" / "process_sheets.html"
    legacy = repo_root / "templates" / "process_sheets.html"
    print(f"expected active template: {active.relative_to(repo_root) if active.exists() else '<missing>'}")
    print(f"legacy duplicate: {legacy.relative_to(repo_root) if legacy.exists() else '<missing>'}")


if __name__ == "__main__":
    main()
