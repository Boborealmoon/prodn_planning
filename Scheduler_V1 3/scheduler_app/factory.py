from __future__ import annotations

import os
from pathlib import Path

from flask import Flask

from .db import ensure_db
from .blocks import recalculate_all
from .db import db
from .routes.pages import pages_bp
from .routes.data_mgmt import data_mgmt_bp
from .routes.flows import flows_bp, trial_prefixed_flows_bp
from .routes.gantt import trial_gantt_bp
from .routes.materials import materials_bp
from .routes.process_sheets import process_sheets_bp
from .routes.summary import trial_summary_bp
from .routes.planner import trial_bp


def create_app():
    package_dir = Path(__file__).resolve().parent
    app = Flask(__name__, template_folder=str(package_dir / "templates"), static_folder=str(package_dir / "static"))
    app.register_blueprint(pages_bp)
    app.register_blueprint(data_mgmt_bp)
    app.register_blueprint(flows_bp)
    app.register_blueprint(trial_prefixed_flows_bp)
    app.register_blueprint(materials_bp)
    app.register_blueprint(process_sheets_bp)
    app.register_blueprint(trial_gantt_bp)
    app.register_blueprint(trial_summary_bp)
    app.register_blueprint(trial_bp)
    ensure_db()
    if os.environ.get("SCHEDULER_RECALCULATE_ON_STARTUP") == "1":
        with db() as con:
            recalculate_all(con)
    return app
