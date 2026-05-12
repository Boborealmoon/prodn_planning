from __future__ import annotations

from flask import Blueprint, render_template

from .db import db
from .imports import trial_data_management_stats

pages_bp = Blueprint("pages", __name__)


@pages_bp.get("/")
@pages_bp.get("/trial")
def trial_page():
    return render_template("trial.html")


@pages_bp.get("/data-management")
@pages_bp.get("/data-management/")
def data_management_page():
    with db() as con:
        stats = trial_data_management_stats(con)
    return render_template("data_management.html", stats=stats)


@pages_bp.get("/materials")
def materials_page():
    return render_template("materials.html")
