"""
web/routes/coach.py — AI Coach Blueprint
=========================================
Routes:
  GET  /coach              — Coach page
  POST /api/coach-advice   — AI advice proxy (multi-provider)
  POST /api/detect-role    — Detect/save role for a date
"""

import json
import logging
from datetime import date
from flask import Blueprint, render_template, request, jsonify

from daemon.db import query
from web.utils import load_config, format_duration
from web.ai_client import call_ai

logger = logging.getLogger(__name__)
bp = Blueprint("coach", __name__)


@bp.route("/coach")
def coach():
    config = load_config()
    try:
        from daemon.analyzer import analyze_productivity_patterns
        analysis = analyze_productivity_patterns(days=30)
    except Exception as e:
        logger.error(f"Coach analysis failed: {e}")
        analysis = {"error": "analysis_failed", "message": str(e),
                    "days_tracked": 0, "avg_score": 0, "trend": "no_data",
                    "best_day": None, "worst_day": None, "scores": []}

    recent_roles = query("""
        SELECT date, role_name, emoji, color
        FROM daily_roles ORDER BY date DESC LIMIT 14
    """)
    daily_scores = query("""
        SELECT date, AVG(productivity_score) as score
        FROM productivity WHERE date >= date('now','-30 days')
        GROUP BY date ORDER BY date ASC
    """)
    return render_template("coach.html",
        config=config, analysis=analysis,
        recent_roles=recent_roles, daily_scores=daily_scores,
        format_duration=format_duration,
    )


@bp.route("/api/coach-advice", methods=["POST"])
def api_coach_advice():
    """
    AI advice proxy — routes to the correct AI provider based on settings.
    NEVER calls any AI API from the browser (CORS). Always server-side.
    """
    data   = request.get_json(silent=True) or {}
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"ok": False, "error": "No prompt provided"})

    cfg    = load_config()
    result = call_ai(prompt, cfg)

    # If no_api_key, add helpful message for the UI
    if not result.get("ok") and result.get("error") == "no_api_key":
        result["message"] = result.get("message", "No API key set.")

    return jsonify(result)


@bp.route("/api/detect-role", methods=["POST"])
def api_detect_role():
    """Detect and save role for a date (called on-demand from Roles page)."""
    data        = request.get_json(silent=True) or {}
    target_date = data.get("date", str(date.today()))
    try:
        from daemon.analyzer import save_daily_role
        role = save_daily_role(target_date)
        return jsonify({"ok": True, "role": role})
    except Exception as e:
        logger.error(f"detect_role failed for {target_date}: {e}")
        return jsonify({"ok": False, "error": str(e)})
