from __future__ import annotations

import json

from flask import Blueprint, jsonify, request

from ..db import db
from ..imports import (
    trial_apply_active_sheet,
    trial_data_management_stats,
    trial_import_workbook,
    trial_log_import,
)
from ..utils import compact_text, load_trial_active_sheet

data_mgmt_bp = Blueprint("data_mgmt", __name__)


@data_mgmt_bp.post("/api/data-management/upload-workbook")
def upload_workbook():
    file = request.files.get("file")
    if not file or not getattr(file, "filename", ""):
        return jsonify({"error": "Choose a workbook first"}), 400
    if not file.filename.lower().endswith((".xlsx", ".xlsm", ".xltx", ".xltm")):
        return jsonify({"error": "Upload an .xlsx or .xlsm workbook"}), 400
    with db() as con:
        try:
            result = trial_import_workbook(con, file)
            stats = trial_data_management_stats(con)
            return jsonify({"ok": True, "result": result, "stats": stats})
        except Exception as exc:
            return jsonify({"error": f"Workbook import failed: {exc}"}), 400


@data_mgmt_bp.post("/api/data-management/upload-active-sheet")
def upload_active_sheet():
    file = request.files.get("file")
    if not file or not getattr(file, "filename", ""):
        return jsonify({"error": "Choose an active sheet workbook first"}), 400
    if not file.filename.lower().endswith((".xlsx", ".xlsm", ".xltx", ".xltm")):
        return jsonify({"error": "Upload an .xlsx or .xlsm workbook"}), 400
    with db() as con:
        try:
            rows_data = load_trial_active_sheet(file)
            completed = trial_apply_active_sheet(con, rows_data)
            trial_log_import(
                con,
                "active-sheet",
                workbook_name=compact_text(getattr(file, "filename", "")) or "active.xlsx",
                active_sheet_name="Active Orders",
                status="SUCCESS",
                message=json.dumps({"completed": completed}),
            )
            stats = trial_data_management_stats(con)
            return jsonify({"ok": True, "completed": completed, "stats": stats})
        except Exception as exc:
            return jsonify({"error": f"Active sheet import failed: {exc}"}), 400
