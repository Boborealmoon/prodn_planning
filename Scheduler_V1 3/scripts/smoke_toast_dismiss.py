#!/usr/bin/env python3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def main() -> None:
    js = read(ROOT / "static/js/utils.js")
    css = read(ROOT / "static/css/main.css")

    checks = [
        ("toast dismiss key", "toastDismissKey" in js),
        ("toast close button", "toast-dismiss" in js),
        ("toast localStorage read", "toastReadDismissal" in js),
        ("toast localStorage write", "toastWriteDismissal" in js),
        ("toast stays until dismissed", "duration = 0" in js),
        ("toast close style", ".toast-dismiss" in css),
        ("toast closing animation", ".toast.is-closing" in css),
    ]

    failed = [name for name, ok in checks if not ok]
    if failed:
        raise SystemExit("Missing toast helpers: " + ", ".join(failed))

    print("toast dismiss smoke passed")


if __name__ == "__main__":
    main()
