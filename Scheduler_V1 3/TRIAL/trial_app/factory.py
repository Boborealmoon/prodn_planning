from __future__ import annotations

from pathlib import Path

from flask import Flask

from .db import ensure_db
from .blocks import recalculate_all
from .db import db
from .routes_data_mgmt import data_mgmt_bp
from .routes_flows import flows_bp, trial_prefixed_flows_bp
from .routes_gantt import trial_gantt_bp
from .routes_pages import pages_bp
from .routes_materials import materials_bp
from .routes_process_sheets import process_sheets_bp
from .routes_summary import trial_summary_bp
from .routes_trial import trial_bp


def create_app():
    trial_dir = Path(__file__).resolve().parent.parent
    app = Flask(__name__, template_folder=str(trial_dir / "templates"), static_folder=str(trial_dir / "static"))
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
    with db() as con:
        recalculate_all(con)
    return app
