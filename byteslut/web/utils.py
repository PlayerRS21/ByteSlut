"""
web/utils.py — Shared utilities for all route modules
======================================================
Imported by every Blueprint. Contains:
  - Config load/save
  - Format helpers (duration, bytes)
  - Date range helper
  - is_system_process filter

MODULARIZATION NOTE:
  app.py imports from here. All blueprints in web/routes/ import from here.
  If you need to change config handling, edit ONLY this file.
"""

import os
import json
import logging
from pathlib import Path
from datetime import date, timedelta

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

def _config_path() -> Path:
    """
    Always resolves relative to THIS file's directory.
    web/utils.py → web/../config/settings.json
    Works regardless of working directory or systemd service location.
    """
    return Path(__file__).parent.parent / "config" / "settings.json"


def load_config() -> dict:
    """
    Load settings.json and deep-merge with defaults.
    Your saved values always win. New keys get their default values.
    Safe to call on every request — reads from disk but is fast.
    """
    DEFAULTS = {
        "app_name":                    "ByteSlut",
        "cli_command":                 "byteslut",
        "dashboard_port":              6969,
        "collection_interval_seconds": 30,
        "idle_threshold_seconds":      300,
        "version":                     "5.1.0",
        "ui_theme":                    "default",
        "ui_layout":                   "default",   # default|glass|minimal|compact
        "accent_color":                "",          # hex override e.g. "#00b4d8", "" = theme default
        "ui_background":               "",          # background color override for glass theme
        "anthropic_api_key":           "",
        "ai_coach": {
            "enabled":        False,
            "ai_model":       "claude",
            "consent_given":  False,
            "custom_api_url": "",
        },
        "privacy": {
            "track_keystrokes":      True,
            "track_browser_history": True,
            "track_notifications":   True,
            "track_commands":        True,
            "track_typed_words":     True,
        },
        "daily_report": {
            "enabled":                     True,
            "time":                        "18:30",
            "delay_if_cpu_above_percent":  30,
            "delay_if_temp_above_celsius": 75,
            "delay_check_interval_seconds": 120,
            "max_delay_minutes":           60,
        },
    }

    def _deep_merge(defaults: dict, saved: dict) -> dict:
        result = dict(defaults)
        for key, saved_val in saved.items():
            if key in result and isinstance(result[key], dict) and isinstance(saved_val, dict):
                result[key] = _deep_merge(result[key], saved_val)
            else:
                result[key] = saved_val
        return result

    path = _config_path()
    saved = {}
    try:
        with open(path) as f:
            saved = json.load(f)
    except FileNotFoundError:
        logger.warning(f"settings.json not found at {path} — using defaults")
    except json.JSONDecodeError as e:
        logger.error(f"settings.json corrupt: {e} — using defaults")
    except Exception as e:
        logger.error(f"Could not load settings: {e} — using defaults")

    return _deep_merge(DEFAULTS, saved)


def save_config(config: dict) -> bool:
    """
    Atomically write settings.json.
    Writes to .tmp first, then renames — prevents corruption on crash.
    Returns True on success.
    """
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(config, f, indent=2)
        tmp.replace(path)
        logger.info(f"Settings saved to {path}")
        return True
    except Exception as e:
        logger.error(f"Failed to save settings: {e}")
        return False


# ─────────────────────────────────────────────────────────────
# FORMAT HELPERS
# ─────────────────────────────────────────────────────────────

def format_duration(seconds: int) -> str:
    """Convert seconds to human-readable string: 1h 23m, 45m 12s, 8s"""
    if not seconds or seconds < 0:
        return "0s"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}h {m}m" if m else f"{h}h"


def format_bytes(byte_count) -> str:
    """Convert bytes to human-readable string: 1.2 GB, 345 MB, 12 KB"""
    if not byte_count:
        return "0 B"
    b = float(byte_count)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {unit}" if unit != "B" else f"{int(b)} B"
        b /= 1024
    return f"{b:.1f} PB"


# ─────────────────────────────────────────────────────────────
# DATE RANGE
# ─────────────────────────────────────────────────────────────

def get_date_range(period: str, reference_date: str = None):
    """
    Convert a period name to (start_date, end_date) strings.
    period: 'today' | 'yesterday' | 'week' | 'month' | 'year' | 'YYYY-MM-DD'
    """
    ref = date.fromisoformat(reference_date) if reference_date else date.today()

    if period == "today":
        return str(ref), str(ref)
    elif period == "yesterday":
        d = ref - timedelta(days=1)
        return str(d), str(d)
    elif period == "week":
        start = ref - timedelta(days=ref.weekday())
        return str(start), str(ref)
    elif period == "month":
        return f"{ref.year}-{ref.month:02d}-01", str(ref)
    elif period == "year":
        return f"{ref.year}-01-01", str(ref)
    else:
        try:
            d = date.fromisoformat(period)
            return str(d), str(d)
        except ValueError:
            return str(ref), str(ref)


# ─────────────────────────────────────────────────────────────
# THEME SYSTEM — folder-based
# ─────────────────────────────────────────────────────────────

REQUIRED_THEME_SELECTORS = [
    ".card", ".card-header", ".card-body", ".card-red",
    ".sidebar", ".sidebar-brand",
    ".nav-link", ".nav-link.active",
    ".main-content",
    ".btn", ".tab", ".tab.active",
    ".tbl", ".bar", ".bar-fill",
    ".stat-val", ".stat-lbl",
    ".field-input", ".badge",
    "::-webkit-scrollbar",
]


def get_themes_dir() -> Path:
    """Return the path to web/themes/ directory."""
    return Path(__file__).parent / "themes"


def validate_theme_css(css_path: str) -> list:
    """
    Check that a theme style.css covers all required selectors.
    Returns list of missing selectors (empty = theme is valid).
    """
    try:
        with open(css_path) as f:
            content = f.read()
    except FileNotFoundError:
        return [f"FILE NOT FOUND: {css_path}"]
    return [s for s in REQUIRED_THEME_SELECTORS if s not in content]


def get_available_themes() -> dict:
    """
    Scan web/themes/ for theme folders.
    Each valid folder contains theme.json + style.css.
    Returns:
        {
          "glass": {
            "name": "Glass", "description": "...", "author": "...",
            "accent_default": "#ff4444",
            "css_path": "/abs/path/to/style.css",
            "valid": True,
            "missing": [],
          }, ...
        }
    """
    themes_dir = get_themes_dir()
    result = {}
    if not themes_dir.exists():
        return result

    for folder in sorted(themes_dir.iterdir()):
        # Skip non-directories, hidden folders, and the _example template
        if not folder.is_dir() or folder.name.startswith("_"):
            continue

        css_path  = folder / "style.css"
        json_path = folder / "theme.json"

        if not css_path.exists():
            continue  # not a theme folder

        meta = {}
        try:
            with open(json_path) as f:
                meta = json.load(f)
        except Exception:
            meta = {"name": folder.name.title(), "description": ""}

        missing = validate_theme_css(str(css_path))
        result[folder.name] = {
            "name":           meta.get("name", folder.name.title()),
            "description":    meta.get("description", ""),
            "author":         meta.get("author", ""),
            "version":        meta.get("version", "1.0"),
            "accent_default": meta.get("accent_default", ""),
            "preview_colors": meta.get("preview_colors", []),
            "css_path":       str(css_path),
            "valid":          len(missing) == 0,
            "missing":        missing,
        }
    return result
