"""
web/app.py — ByteSlut Web Dashboard
=====================================
This is the Flask web server that powers the dashboard.
It reads from the SQLite database and serves HTML pages.

FLASK BASICS (for learning):
  Flask is a minimal Python web framework.
  @app.route('/') means "when someone visits /, run this function"
  The function returns HTML (rendered from a template file).
  Templates are in web/templates/ and use Jinja2 syntax ({{ variable }}).

WHY FLASK NOT SOMETHING HEAVIER?
  Flask is tiny (~1MB) vs Django (massive). Perfect for local tools.
  No database migrations, no admin panel bloat. Just routes and templates.
"""

import os
import sys
import json
import time
import logging
import subprocess
from pathlib import Path
from datetime import datetime, date, timedelta
from flask import Flask, render_template, jsonify, request, redirect, url_for, session

# Add parent directory to path so daemon.* imports work
sys.path.insert(0, str(Path(__file__).parent.parent))

from daemon.db import query, execute, get_db_path
# Import shared utilities — all config/format helpers live in utils.py
# If you need to change how settings are loaded, edit web/utils.py ONLY.
from web.utils import (
    load_config, save_config, format_duration, format_bytes,
    get_date_range, _config_path
)

app = Flask(__name__)
app.secret_key = "byteslut_secret_key_change_me"

logger = logging.getLogger(__name__)

# ── Custom Jinja2 filters ──
@app.template_filter("strftime")
def strftime_filter(timestamp, fmt="%H:%M"):
    """
    Convert a Unix timestamp to a formatted time string in templates.
    Usage: {{ cmd.timestamp | strftime('%H:%M') }}
    """
    try:
        return datetime.fromtimestamp(int(timestamp)).strftime(fmt)
    except Exception:
        return ""

# Make 'os' and 'getenv' available inside templates
@app.context_processor
def inject_globals():
    """Inject useful variables into every template context."""
    return {
        "current_year": date.today().year,
        "g_user": os.environ.get("USER", "user"),
    }





@app.route("/")
def index():
    """Dashboard — today's overview with live stats."""
    config     = load_config()
    today      = str(date.today())

    # ── Screen time ──
    screen_time = query("""
        SELECT SUM(CASE WHEN duration_seconds IS NOT NULL THEN duration_seconds
                        ELSE CAST(strftime('%s','now') AS INTEGER) - start_time END) as total,
               COUNT(*) as session_count
        FROM sessions WHERE date(start_time,'unixepoch') = ?
    """, (today,), fetch="one")

    # ── Active time from foreground seconds (accurate on Wayland) ──
    active_data = query("""
        SELECT SUM(foreground_seconds) as active_seconds
        FROM app_usage WHERE date = ? AND foreground_seconds > 0
    """, (today,), fetch="one")

    # ── Top apps today ──
    top_apps = query("""
        SELECT app_name, SUM(foreground_seconds) as fg_time, SUM(background_seconds) as bg_time
        FROM app_usage
        WHERE date = ? AND foreground_seconds > 0
          AND app_name NOT LIKE '(%)' AND app_name NOT IN (
              'sd-pam','accounts-daemon','agetty','rtkit-daemon','polkitd',
              'udisksd','upowerd','colord','avahi-daemon','NetworkManager',
              'wpa_supplicant','bluetoothd','pipewire','wireplumber','pulseaudio',
              'gvfsd','gvfsd-fuse','gvfsd-metadata','tracker-miner','evolution-addressbook',
              'xdg-desktop-portal','xdg-desktop-portal-gtk','xdg-desktop-portal-hyprland',
              'plasmashell','gnome-shell','waybar','swaync','dunst','mako',
              'hyprland','sway','i3','openbox','dialog','tk','sh','bash','zsh','fish','unknown'
          )
        GROUP BY app_name ORDER BY fg_time DESC LIMIT 8
    """, (today,))

    # ── Current system stats ──
    system_now = query("""
        SELECT cpu_percent, ram_percent, cpu_temp
        FROM system_stats ORDER BY timestamp DESC LIMIT 1
    """, fetch="one")

    # ── Today's temps ──
    temps_today = query("""
        SELECT cpu_min, cpu_max, cpu_avg FROM temperature_daily WHERE date = ?
    """, (today,), fetch="one")

    # ── Network today ──
    network_today = query("""
        SELECT COALESCE(SUM(bytes_sent), 0) as sent,
               COALESCE(SUM(bytes_received), 0) as recv
        FROM network_usage WHERE date = ?
    """, (today,), fetch="one")

    # ── Today's productivity ──
    productivity = query("""
        SELECT productivity_score, raw_score_weighted,
               dominant_category, detected_role, sample_count,
               coding_samples, learning_samples, ai_samples,
               entertainment_samples, neutral_samples, total_confidence
        FROM productivity WHERE date = ?
        ORDER BY timestamp DESC LIMIT 1
    """, (today,), fetch="one")

    # ── Recent commands ──
    recent_commands = query("""
        SELECT command, exit_code, working_directory, timestamp
        FROM commands ORDER BY timestamp DESC LIMIT 10
    """)

    # ── Average daily screen time (last 30 days) ──
    avg_screen_time = query("""
        SELECT AVG(daily_total) as avg_seconds FROM (
            SELECT date(start_time,'unixepoch') as day,
                   SUM(CASE WHEN duration_seconds IS NOT NULL THEN duration_seconds
                            ELSE CAST(strftime('%s','now') AS INTEGER) - start_time END) as daily_total
            FROM sessions WHERE start_time > strftime('%s','now','-30 days')
            GROUP BY day
        )
    """, fetch="one")

    # ── Battery ──
    battery = query("""
        SELECT percent, is_plugged, capacity_full_mwh, capacity_design_mwh
        FROM battery_stats ORDER BY timestamp DESC LIMIT 1
    """, fetch="one")

    battery_health = None
    if battery and battery.get("capacity_full_mwh") and battery.get("capacity_design_mwh"):
        battery_health = round(
            (battery["capacity_full_mwh"] / battery["capacity_design_mwh"]) * 100, 1
        )

    return render_template("index.html",
        config=config, today=today,
        screen_time=screen_time,
        active_seconds=active_data["active_seconds"] if active_data else 0,
        top_apps=top_apps, system_now=system_now,
        temps_today=temps_today, network_today=network_today,
        productivity=productivity, recent_commands=recent_commands,
        avg_screen_time=avg_screen_time,
        battery=battery, battery_health=battery_health,
        format_duration=format_duration, format_bytes=format_bytes,
    )



@app.route("/apps")
def apps():
    """All-time app usage — user apps only, deleted apps preserved."""
    config = load_config()
    period = request.args.get("period", "today")
    start_date, end_date = get_date_range(period)

    # Exclude system processes at DB level using NOT LIKE patterns.
    # The is_system check in apps.py prevents NEW system entries but
    # old data from v1 may still have them — this query filters both.
    app_data = query("""
        SELECT
            app_name,
            SUM(foreground_seconds) as fg_total,
            SUM(background_seconds) as bg_total,
            COUNT(DISTINCT date)    as days_used,
            MAX(date)               as last_used,
            MAX(is_flatpak)         as is_flatpak
        FROM app_usage
        WHERE date BETWEEN ? AND ?
          AND foreground_seconds > 0
          AND app_name NOT LIKE '(%)'
          AND app_name NOT IN (
              'sd-pam','accounts-daemon','agetty','rtkit-daemon',
              'polkitd','udisksd','upowerd','colord','avahi-daemon',
              'NetworkManager','wpa_supplicant','bluetoothd',
              'pipewire','wireplumber','pulseaudio',
              'at-spi-bus-laun','at-spi2-registr','xdg-desktop-por',
              'gvfsd','gvfsd-fuse','dconf-service',
              'archlinux-keyri','auto-cpufreq','udev-worker',
              'systemd','dbus-daemon','dbus-broker'
          )
          AND app_name NOT LIKE '%daemon%'
          AND app_name NOT LIKE '%worker%'
          AND app_name NOT LIKE '%-helper'
          AND app_name NOT LIKE 'kworker%'
          AND app_name NOT LIKE 'sd-%'
        GROUP BY app_name
        ORDER BY fg_total DESC
    """, (start_date, end_date))

    # Mark deleted apps from registry
    registry = query("SELECT app_name, is_deleted, first_seen FROM app_registry")
    registry_map = {r["app_name"]: r for r in registry}

    for app_row in app_data:
        reg = registry_map.get(app_row["app_name"], {})
        app_row["is_deleted"] = reg.get("is_deleted", 0)
        app_row["first_seen"] = reg.get("first_seen", "")

    return render_template("apps.html",
        config=config,
        app_data=app_data,
        period=period,
        start_date=start_date,
        end_date=end_date,
        format_duration=format_duration,
    )


@app.route("/commands")
def commands():
    """Terminal command history — recent first, load-more pagination."""
    config = load_config()
    period = request.args.get("period", "today")
    filter_type = request.args.get("filter", "all")
    offset = int(request.args.get("offset", 0))
    limit = 100  # Load 100 at a time

    # For "today" show today; for others use date range
    # But ALWAYS sort newest first so recent commands are at top
    start_date, end_date = get_date_range(period)

    where_extra = ""
    if filter_type == "errors":
        where_extra = "AND exit_code IS NOT NULL AND exit_code != 0"
    elif filter_type == "sudo":
        where_extra = "AND is_sudo = 1"

    cmd_data = query(f"""
        SELECT command, exit_code, working_directory, shell,
               is_sudo, duration_seconds, timestamp, date
        FROM commands
        WHERE date BETWEEN ? AND ? {where_extra}
        ORDER BY timestamp DESC
        LIMIT ? OFFSET ?
    """, (start_date, end_date, limit, offset))

    # Total count for "load more" button
    total_count = query(f"""
        SELECT COUNT(*) as cnt FROM commands
        WHERE date BETWEEN ? AND ? {where_extra}
    """, (start_date, end_date), fetch="one")
    total = total_count["cnt"] if total_count else 0

    # Most used commands
    top_commands = query(f"""
        SELECT command, COUNT(*) as count,
               SUM(CASE WHEN exit_code = 0 THEN 1 ELSE 0 END) as success_count,
               SUM(CASE WHEN exit_code != 0 AND exit_code IS NOT NULL THEN 1 ELSE 0 END) as error_count
        FROM commands
        WHERE date BETWEEN ? AND ? {where_extra}
        GROUP BY command
        ORDER BY count DESC
        LIMIT 20
    """, (start_date, end_date))

    return render_template("commands.html",
        config=config,
        cmd_data=cmd_data,
        top_commands=top_commands,
        period=period,
        filter_type=filter_type,
        offset=offset,
        limit=limit,
        total=total,
        has_more=(offset + limit) < total,
        format_duration=format_duration,
    )


@app.route("/browser")
def browser():
    """Browser history, most visited sites, YouTube watched."""
    config = load_config()
    period = request.args.get("period", "today")
    start_date, end_date = get_date_range(period)

    # Top domains — deduplicated by (domain, hour) to prevent autoplay inflation.
    # Each domain gets max 300s per hour-slot. youtube.com open 8h = max 8×300s = 40min.
    top_domains = query("""
        SELECT domain,
               SUM(slot_count)      as visits,
               SUM(slot_secs)       as total_time
        FROM (
            SELECT domain,
                   strftime('%Y-%m-%d %H', datetime(timestamp,'unixepoch','localtime')) as hour_slot,
                   COUNT(*) as slot_count,
                   MIN(300, SUM(MIN(visit_duration_seconds, 300))) as slot_secs
            FROM browser_history
            WHERE date BETWEEN ? AND ? AND domain != ''
              AND domain NOT LIKE '127.0.0.1%'
              AND domain NOT LIKE 'localhost%'
            GROUP BY domain, hour_slot
        )
        GROUP BY domain
        ORDER BY total_time DESC
        LIMIT 20
    """, (start_date, end_date))

    # YouTube — deduplicated by clean title (strips "(N) " notification prefix).
    # "(2) Gulaab - Amr8..." and "Gulaab - Amr8..." are the same video.
    # Sorted by MAX(timestamp) DESC so the most RECENTLY watched video is first,
    # matching the order of the "Recent Pages" list at the bottom.
    youtube_videos = query("""
        SELECT
            TRIM(
                CASE
                    WHEN COALESCE(youtube_title, title) GLOB '([0-9]*) *'
                    THEN SUBSTR(COALESCE(youtube_title, title),
                                INSTR(COALESCE(youtube_title, title), ' ') + 1)
                    ELSE COALESCE(youtube_title, title)
                END
            ) as video_title,
            MAX(url)                       as url,
            SUM(MIN(visit_duration_seconds, 300))    as total_duration,
            COUNT(*)                       as plays,
            MAX(timestamp)                 as last_played,
            MAX(browser)                   as browser
        FROM browser_history
        WHERE date BETWEEN ? AND ?
          AND is_youtube = 1
          AND (youtube_title != '' OR title != '')
          AND domain NOT IN ('127.0.0.1:6969', 'localhost')
          AND domain NOT LIKE '127.0.0.1%'
          AND domain NOT LIKE 'localhost%'
        GROUP BY TRIM(
            CASE
                WHEN COALESCE(youtube_title, title) GLOB '([0-9]*) *'
                THEN SUBSTR(COALESCE(youtube_title, title),
                            INSTR(COALESCE(youtube_title, title), ' ') + 1)
                ELSE COALESCE(youtube_title, title)
            END
        )
        ORDER BY last_played DESC
        LIMIT 50
    """, (start_date, end_date))

    # Recent history — deduplicated: same URL within 60s = one entry
    recent_history = query("""
        SELECT url, title, domain, browser, timestamp,
               SUM(MIN(visit_duration_seconds, 300)) as visit_duration_seconds,
               is_youtube, youtube_title
        FROM browser_history
        WHERE date BETWEEN ? AND ?
        GROUP BY url, strftime('%Y-%m-%d %H:%M', datetime(timestamp, 'unixepoch'))
        ORDER BY MAX(timestamp) DESC
        LIMIT 100
    """, (start_date, end_date))

    # Stats — use unique page count for visits (not raw row count)
    # ── Browser stats with DEDUPLICATION by domain+hour ─────────────────────
    # WHY DEDUP: YouTube autoplay creates 1 history entry every 2-3 minutes.
    # 716 visits × 300s cap each = 59h which is wrong.
    # Fix: count each (domain, hour) slot at most once (max 300s per hour per domain).
    # This means 1h of YouTube = 1 hour-slot = 300s, not 60 visits × 300s = 5h.
    stats = query("""
        SELECT
            COUNT(DISTINCT domain || is_youtube) as total_visits,
            COUNT(DISTINCT domain)       as unique_domains,
            SUM(CASE WHEN is_youtube = 1 THEN 1 ELSE 0 END) as youtube_visits,
            -- Dedup non-YouTube: one 300s slot per (domain, hour)
            SUM(CASE WHEN is_youtube = 0 THEN slot_secs ELSE 0 END) as total_browsing_time,
            -- Dedup YouTube: one 300s slot per hour
            SUM(CASE WHEN is_youtube = 1 THEN slot_secs ELSE 0 END) as total_youtube_time
        FROM (
            SELECT domain, is_youtube,
                   strftime('%Y-%m-%d %H', datetime(timestamp,'unixepoch','localtime')) as hour_slot,
                   MIN(300, SUM(MIN(visit_duration_seconds, 300))) as slot_secs
            FROM browser_history
            WHERE date BETWEEN ? AND ?
              AND domain NOT LIKE '%127.0.0.1%'
              AND domain NOT LIKE '%localhost%'
            GROUP BY domain, is_youtube, hour_slot
        )
    """, (start_date, end_date), fetch="one")

    return render_template("browser.html",
        config=config,
        top_domains=top_domains,
        youtube_videos=youtube_videos,
        recent_history=recent_history,
        stats=stats,
        period=period,
        format_duration=format_duration,
    )


@app.route("/monthly")
def monthly():
    """Monthly usage overview."""
    config = load_config()
    year = int(request.args.get("year", date.today().year))
    month = int(request.args.get("month", date.today().month))

    start_date = f"{year}-{month:02d}-01"
    # Last day of month
    if month == 12:
        end_date = f"{year}-12-31"
    else:
        end_date = str(date(year, month + 1, 1) - timedelta(days=1))

    # Reusable expression: actual duration (live for running session, stored for ended ones)
    # SQLite doesn't have variables, so we repeat this CASE expression in each query
    _dur = """CASE
        WHEN duration_seconds IS NOT NULL THEN duration_seconds
        ELSE CAST(strftime('%s','now') AS INTEGER) - start_time
    END"""

    # Daily screen time for the month
    daily_screen = query(f"""
        SELECT date(start_time, 'unixepoch') as day,
               SUM({_dur}) as total_seconds,
               SUM({_dur}) - SUM(COALESCE(idle_seconds, 0)) as active_seconds
        FROM sessions
        WHERE day BETWEEN ? AND ?
        GROUP BY day
        ORDER BY day
    """, (start_date, end_date))

    # Monthly totals
    monthly_totals = query(f"""
        SELECT
            SUM({_dur}) as total_screen_time,
            AVG({_dur}) as avg_session,
            COUNT(*) as total_sessions
        FROM sessions
        WHERE date(start_time, 'unixepoch') BETWEEN ? AND ?
    """, (start_date, end_date), fetch="one")

    # Average daily screen time
    avg_daily = query(f"""
        SELECT AVG(daily_total) as avg_seconds FROM (
            SELECT date(start_time, 'unixepoch') as day,
                   SUM({_dur}) as daily_total
            FROM sessions
            WHERE day BETWEEN ? AND ?
            GROUP BY day
        )
    """, (start_date, end_date), fetch="one")

    # Network totals
    network_monthly = query("""
        SELECT SUM(bytes_sent) as total_sent, SUM(bytes_received) as total_recv
        FROM network_usage WHERE date BETWEEN ? AND ?
    """, (start_date, end_date), fetch="one")

    # Top apps this month — user apps only
    top_apps = query("""
        SELECT app_name, SUM(foreground_seconds) as fg_total
        FROM app_usage
        WHERE date BETWEEN ? AND ?
          AND foreground_seconds > 0
          AND app_name NOT LIKE '(%)'
          AND app_name NOT IN (
              'sd-pam','accounts-daemon','agetty','rtkit-daemon',
              'polkitd','udisksd','upowerd','colord','avahi-daemon',
              'NetworkManager','wpa_supplicant','bluetoothd',
              'pipewire','wireplumber','pulseaudio',
              'at-spi-bus-laun','at-spi2-registr','xdg-desktop-por',
              'gvfsd','gvfsd-fuse','dconf-service','archlinux-keyri',
              'auto-cpufreq','udev-worker','systemd','dbus-daemon'
          )
          AND app_name NOT LIKE '%daemon%'
          AND app_name NOT LIKE '%worker%'
          AND app_name NOT LIKE 'kworker%'
          AND app_name NOT LIKE 'sd-%'
        GROUP BY app_name ORDER BY fg_total DESC LIMIT 10
    """, (start_date, end_date))

    # Temperature summary
    temp_summary = query("""
        SELECT AVG(cpu_min) as avg_min, AVG(cpu_max) as avg_max,
               MIN(cpu_min) as abs_min, MAX(cpu_max) as abs_max
        FROM temperature_daily
        WHERE date BETWEEN ? AND ?
    """, (start_date, end_date), fetch="one")

    # Packages installed this month
    packages = query("""
        SELECT action, COUNT(*) as count
        FROM packages WHERE date BETWEEN ? AND ?
        GROUP BY action
    """, (start_date, end_date))

    # Productivity average for this month
    # New schema: no work_seconds/entertainment_seconds — use sample columns instead.
    # coding+learning+ai = productive work samples
    # entertainment_samples = entertainment time
    avg_productivity = query("""
        SELECT AVG(productivity_score)                         as avg_score,
               SUM(coding_samples + learning_samples + ai_samples) as total_work,
               SUM(entertainment_samples)                      as total_entertainment
        FROM productivity WHERE date BETWEEN ? AND ?
    """, (start_date, end_date), fetch="one")

    return render_template("monthly.html",
        config=config,
        year=year, month=month,
        start_date=start_date, end_date=end_date,
        daily_screen=daily_screen,
        monthly_totals=monthly_totals,
        avg_daily=avg_daily,
        network_monthly=network_monthly,
        top_apps=top_apps,
        temp_summary=temp_summary,
        packages=packages,
        avg_productivity=avg_productivity,
        format_duration=format_duration,
        format_bytes=format_bytes,
    )


@app.route("/focus")
def focus():
    """
    Productivity focus breakdown — explains WHY you got your score.
    Shows category breakdown, detected role, top domains, trends.
    """
    config = load_config()
    period = request.args.get("period", "today")
    start_date, end_date = get_date_range(period)

    # Latest score — aggregate across all rows for the period
    latest = query("""
        SELECT
            AVG(productivity_score)                         AS productivity_score,
            AVG(raw_score_weighted)                         AS raw_score_weighted,
            MAX(date)                                       AS date,
            SUM(sample_count)                               AS sample_count,
            SUM(total_confidence)                           AS total_confidence,
            SUM(coding_samples)                             AS coding_samples,
            SUM(learning_samples)                           AS learning_samples,
            SUM(ai_samples)                                 AS ai_samples,
            SUM(neutral_samples)                            AS neutral_samples,
            SUM(entertainment_samples)                      AS entertainment_samples,
            -- dominant_category: most frequent NON-IDLE category
            -- idle rows are old data or lock-screen time, not real work
            (SELECT dominant_category FROM productivity
             WHERE date BETWEEN ? AND ?
               AND dominant_category IS NOT NULL
               AND dominant_category != 'idle'
             GROUP BY dominant_category ORDER BY COUNT(*) DESC LIMIT 1) AS dominant_category,
            -- detected_role: from most recent row
            (SELECT detected_role FROM productivity
             WHERE date BETWEEN ? AND ?
               AND detected_role IS NOT NULL
               AND detected_role != 'unknown'
             ORDER BY timestamp DESC LIMIT 1) AS detected_role
        FROM productivity
        WHERE date BETWEEN ? AND ?
    """, (start_date, end_date,   # dominant_category sub
          start_date, end_date,   # detected_role sub
          start_date, end_date),  # main WHERE
    fetch="one")

    # Daily score trend — average all flush-interval rows per day
    # (collector writes one row per minute, we need one point per day on chart)
    daily_scores = query("""
        SELECT date,
               AVG(productivity_score)                          AS productivity_score,
               SUM(coding_samples + learning_samples + ai_samples) AS coding_samples,
               SUM(learning_samples)                            AS learning_samples,
               SUM(entertainment_samples)                       AS entertainment_samples,
               SUM(neutral_samples)                             AS neutral_samples,
               SUM(ai_samples)                                  AS ai_samples,
               SUM(sample_count)                                AS sample_count,
               -- dominant_category per day = most common
               (SELECT dominant_category FROM productivity p2
                WHERE p2.date = p.date AND dominant_category IS NOT NULL
                GROUP BY dominant_category ORDER BY COUNT(*) DESC LIMIT 1) AS dominant_category
        FROM productivity p
        WHERE date BETWEEN ? AND ?
        GROUP BY date
        ORDER BY date ASC
    """, (start_date, end_date))

    # Aggregate category_breakdown across ALL productivity rows for the period.
    # Each row's category_breakdown is a JSON dict of {category: seconds}.
    # We sum them all to get the true total for the period — not just one 60s window.
    all_rows = query("""
        SELECT category_breakdown, top_domains
        FROM productivity
        WHERE date BETWEEN ? AND ?
          AND category_breakdown IS NOT NULL
    """, (start_date, end_date))

    category_breakdown = {}
    top_domains_agg    = {}
    for row in all_rows:
        try:
            cb = json.loads(row["category_breakdown"] or "{}")
            for k, v in cb.items():
                category_breakdown[k] = category_breakdown.get(k, 0) + v
        except Exception:
            pass
        try:
            td = json.loads(row["top_domains"] or "{}")
            for k, v in td.items():
                top_domains_agg[k] = top_domains_agg.get(k, 0) + v
        except Exception:
            pass

    # Top 5 domains by total seconds
    top_domains = dict(
        sorted(top_domains_agg.items(), key=lambda x: x[1], reverse=True)[:5]
    )

    # ── Derive dominant_category from the fully-aggregated breakdown ──────────
    # This is MORE ACCURATE than the SQL sub-query because:
    #   - The sub-query picks the most frequent dominant_category field per ROW
    #   - This sums actual SECONDS across all rows, then picks the winner
    #   - We skip 'idle' — that's lock-screen/away time, not real work
    #
    # Also derive detected_role from the dominant non-idle category.
    SKIP_CATS = {'idle', 'neutral', 'user_neutral'}  # don't let these be "dominant"
    ROLE_FROM_CAT = {
        'coding_editor':      'developer',
        'terminal':           'developer',
        'localhost_dev':      'developer',
        'dev_tool':           'developer',
        'design_tool':        'designer',
        'writing_docs':       'writer',
        'learning_video':     'learner',
        'productive_site':    'researcher',
        'ai_tool':            'developer',
        'work_comm':          'communicator',
        'entertainment_fg':   'consumer',
        'entertainment_app':  'consumer',
        'social_media':       'consumer',
        'user_productive':    'developer',
        'user_entertainment': 'consumer',
    }
    CAT_LABELS = {
        'coding_editor':      'Code Editor',
        'terminal':           'Terminal',
        'localhost_dev':      'Local Dev Server',
        'dev_tool':           'Dev Tool',
        'design_tool':        'Design Tool',
        'writing_docs':       'Writing / Docs',
        'learning_video':     'Learning Video',
        'productive_site':    'Productive Site',
        'ai_tool':            'AI Tool',
        'work_comm':          'Work Comms',
        'entertainment_fg':   'Entertainment',
        'entertainment_app':  'Entertainment App',
        'social_media':       'Social Media',
        'user_productive':    'You Labelled: Work',
        'user_entertainment': 'You Labelled: Fun',
        'neutral':            'Unknown App/Site',
        'user_neutral':       'You Labelled: Neutral',
        'idle':               'System Idle',
    }

    # Find the dominant non-idle, non-neutral category by seconds
    real_work_cats = {k: v for k, v in category_breakdown.items()
                      if k not in SKIP_CATS and v > 0}

    if real_work_cats:
        derived_dominant = max(real_work_cats, key=real_work_cats.get)
        derived_role     = ROLE_FROM_CAT.get(derived_dominant, 'unknown')
        derived_label    = CAT_LABELS.get(derived_dominant, derived_dominant.replace('_', ' ').title())
    else:
        # All time was idle/neutral — check if latest has anything useful
        all_non_idle = {k: v for k, v in category_breakdown.items() if k != 'idle' and v > 0}
        if all_non_idle:
            derived_dominant = max(all_non_idle, key=all_non_idle.get)
            derived_role     = ROLE_FROM_CAT.get(derived_dominant, 'unknown')
            derived_label    = CAT_LABELS.get(derived_dominant, derived_dominant.replace('_', ' ').title())
        else:
            derived_dominant = None
            derived_role     = None
            derived_label    = None

    # Override latest with our better-derived values
    if latest:
        # Convert sqlite Row to a mutable dict so we can update fields
        latest = dict(latest)
        if derived_dominant:
            latest["dominant_category"] = derived_dominant
            latest["dominant_label"]    = derived_label
        if derived_role:
            latest["detected_role"] = derived_role
    elif derived_dominant:
        # No productivity rows at all — build a minimal latest from app_usage
        latest = {
            "productivity_score":  0,
            "raw_score_weighted":  0,
            "sample_count":        sum(category_breakdown.values()),
            "dominant_category":   derived_dominant,
            "dominant_label":      derived_label,
            "detected_role":       derived_role,
            "total_confidence":    0,
        }

    # Sites to review (unknown sites needing a label)
    sites_to_review = {}
    review_file = Path.home() / ".local" / "share" / "byteslut" / "sites_to_review.json"
    try:
        if review_file.exists():
            with open(review_file) as f:
                raw = json.load(f)
            sites_to_review = dict(
                sorted(raw.items(), key=lambda x: x[1], reverse=True)[:20]
            )
    except Exception:
        pass

    # User-labeled sites
    user_labels = {}
    user_file = Path.home() / ".local" / "share" / "byteslut" / "user_sites.json"
    try:
        if user_file.exists():
            with open(user_file) as f:
                user_labels = json.load(f)
    except Exception:
        pass

    return render_template("focus.html",
        config=config,
        latest=latest,
        daily_scores=daily_scores,
        category_breakdown=category_breakdown,
        top_domains=top_domains,
        sites_to_review=sites_to_review,
        user_labels=user_labels,
        period=period,
        format_duration=format_duration,
    )


@app.route("/api/label-site", methods=["POST"])
def api_label_site():
    """
    Label a site as productive/neutral/entertainment.
    Called from the Focus page when user labels a site to review.
    Writes directly to user_sites.json — no daemon restart needed.
    """
    data   = request.get_json(silent=True) or {}
    domain = (data.get("domain") or "").strip().lower()
    label  = (data.get("label")  or "").strip().lower()

    if not domain:
        return jsonify({"ok": False, "error": "No domain provided"})
    if label not in ("productive", "neutral", "entertainment"):
        return jsonify({"ok": False, "error": "Label must be productive/neutral/entertainment"})

    user_file = Path.home() / ".local" / "share" / "byteslut" / "user_sites.json"
    review_file = Path.home() / ".local" / "share" / "byteslut" / "sites_to_review.json"

    try:
        # Load existing
        sites = {}
        if user_file.exists():
            with open(user_file) as f:
                sites = json.load(f)
        sites[domain] = label
        with open(user_file, "w") as f:
            json.dump(sites, f, indent=2)

        # Remove from review queue
        review = {}
        if review_file.exists():
            with open(review_file) as f:
                review = json.load(f)
        review.pop(domain, None)
        with open(review_file, "w") as f:
            json.dump(review, f, indent=2)

        return jsonify({"ok": True, "domain": domain, "label": label})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/records")
def records():
    """Personal records — all-time bests across every metric."""
    config = load_config()

    # Query all records in one shot — each is a subquery for the best day
    recs = {}

    # Most keystrokes in a single day
    r = query("SELECT date, SUM(keystrokes) as v FROM input_stats GROUP BY date ORDER BY v DESC LIMIT 1", fetch="one")
    recs["most_keystrokes_day"]     = r

    # Fastest WPM ever recorded in a single sample
    r = query("SELECT date, MAX(wpm_sample) as v FROM input_stats WHERE wpm_sample > 0", fetch="one")
    recs["fastest_wpm"]             = r

    # Most mouse distance in a single day
    r = query("SELECT date, SUM(mouse_distance_px) as v FROM input_stats GROUP BY date ORDER BY v DESC LIMIT 1", fetch="one")
    recs["most_mouse_distance_day"] = r

    # Longest single session (lid open → lid close)
    r = query("SELECT date(start_time,'unixepoch') as date, MAX(duration_seconds) as v FROM sessions WHERE duration_seconds > 0", fetch="one")
    recs["longest_session"]         = r

    # Most screen time in a day
    r = query("""
        SELECT date(start_time,'unixepoch') as date,
               SUM(duration_seconds) as v
        FROM sessions
        WHERE duration_seconds > 0
        GROUP BY date(start_time,'unixepoch')
        ORDER BY v DESC LIMIT 1
    """, fetch="one")
    recs["most_screen_time_day"]    = r

    # Best productivity score ever
    r = query("SELECT date, MAX(productivity_score) as v FROM productivity WHERE productivity_score > 0", fetch="one")
    recs["best_productivity_score"] = r

    # Most commands in a single day
    r = query("SELECT date, COUNT(*) as v FROM commands GROUP BY date ORDER BY v DESC LIMIT 1", fetch="one")
    recs["most_commands_day"]       = r

    # Hottest CPU temperature ever
    r = query("SELECT date, MAX(cpu_max) as v FROM temperature_daily WHERE cpu_max > 0", fetch="one")
    recs["hottest_temp"]            = r

    # Most downloaded in a single day
    r = query("SELECT date, SUM(bytes_received) as v FROM network_usage GROUP BY date ORDER BY v DESC LIMIT 1", fetch="one")
    recs["most_downloaded_day"]     = r

    # Most YouTube videos in a day
    r = query("SELECT date, COUNT(*) as v FROM browser_history WHERE is_youtube=1 GROUP BY date ORDER BY v DESC LIMIT 1", fetch="one")
    recs["most_youtube_day"]        = r

    # Most mouse clicks in a day
    r = query("SELECT date, SUM(mouse_clicks) as v FROM input_stats GROUP BY date ORDER BY v DESC LIMIT 1", fetch="one")
    recs["most_clicks_day"]         = r

    # First day of tracking
    r = query("SELECT MIN(date(start_time,'unixepoch')) as v FROM sessions WHERE start_time > 0", fetch="one")
    recs["tracking_since"]          = r

    # Total days tracked
    r = query("SELECT COUNT(DISTINCT date(start_time,'unixepoch')) as v FROM sessions", fetch="one")
    recs["total_days_tracked"]      = r

    # App used most in total (all time)
    r = query("""
        SELECT app_name as date, SUM(foreground_seconds) as v
        FROM app_usage
        WHERE app_name NOT LIKE '(%)'
          AND app_name NOT IN ('sd-pam','accounts-daemon','agetty','pipewire',
              'wireplumber','at-spi-bus-laun','systemd','dbus-daemon')
          AND app_name NOT LIKE '%daemon%'
        GROUP BY app_name ORDER BY v DESC LIMIT 1
    """, fetch="one")
    recs["most_used_app_alltime"]   = r

    return render_template("records.html",
        config=config,
        recs=recs,
        format_duration=format_duration,
        format_bytes=format_bytes,
    )


@app.route("/roles")
def roles():
    """
    Day Roles page — shows what role was detected for each tracked day.
    Role is detected by daemon/analyzer.py based on what you actually did.
    Each day gets one label: Deep Focus Coder, Movie Guy, Grind Day, etc.
    """
    config = load_config()

    # Auto-detect today's role if not yet saved
    try:
        from daemon.analyzer import save_daily_role
        today_str = str(date.today())
        existing = query(
            "SELECT date FROM daily_roles WHERE date = ?",
            (today_str,), fetch="one"
        )
        if not existing:
            save_daily_role(today_str)
    except Exception as e:
        logger.warning(f"Could not auto-detect today's role: {e}")

    # All saved roles, newest first
    all_roles = query("""
        SELECT date, role_name, description, emoji, color,
               role_score, features, alternatives
        FROM daily_roles
        ORDER BY date DESC
        LIMIT 180
    """)

    # Parse JSON fields
    for r in all_roles:
        try:
            r["features_parsed"]    = json.loads(r["features"] or "{}")
        except Exception:
            r["features_parsed"]    = {}
        try:
            r["alternatives_parsed"] = json.loads(r["alternatives"] or "[]")
        except Exception:
            r["alternatives_parsed"] = []

    # Role frequency summary
    role_counts = {}
    for r in all_roles:
        name = r["role_name"] or "Unknown"
        role_counts[name] = role_counts.get(name, 0) + 1
    role_counts_sorted = sorted(role_counts.items(), key=lambda x: x[1], reverse=True)

    # Most productive role (highest avg productivity_score per role)
    role_scores = {}
    for r in all_roles:
        name  = r["role_name"] or "Unknown"
        score = r["features_parsed"].get("productivity_score", 0)
        role_scores.setdefault(name, []).append(score)
    role_avg_scores = {
        name: round(sum(scores) / len(scores), 1)
        for name, scores in role_scores.items()
        if scores
    }

    return render_template("roles.html",
        config=config,
        all_roles=all_roles,
        role_counts=role_counts_sorted,
        role_avg_scores=role_avg_scores,
        format_duration=format_duration,
    )


@app.route("/api/coach-advice", methods=["POST"])
def api_coach_advice():
    """
    AI advice proxy — routes to the correct provider (Claude/GPT/Gemini/Ollama/custom).
    Never calls any AI API from the browser — always server-side (no CORS issues).
    Provider is read from settings.json ai_coach.ai_model field.
    """
    from web.ai_client import call_ai
    data   = request.get_json(silent=True) or {}
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"ok": False, "error": "No prompt provided"})
    result = call_ai(prompt, load_config())
    return jsonify(result)


@app.route("/api/detect-role", methods=["POST"])
def api_detect_role():
    """
    Detect and save role for a specific date on demand.
    Called when user clicks "Detect" on the roles page.
    """
    data = request.get_json(silent=True) or {}
    target_date = data.get("date", str(date.today()))
    try:
        from daemon.analyzer import save_daily_role
        role = save_daily_role(target_date)
        return jsonify({"ok": True, "role": role})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/coach")
def coach():
    """
    Productivity Coach page — analyzes your patterns and gives actionable advice.
    Pulls from local DB data + optionally enriches with web search via Claude API.
    """
    config = load_config()

    try:
        from daemon.analyzer import analyze_productivity_patterns
        analysis = analyze_productivity_patterns(days=30)
    except Exception as e:
        logger.error(f"Coach analysis failed: {e}")
        analysis = {"error": "analysis_failed", "message": str(e)}

    # Recent role history for context
    recent_roles = query("""
        SELECT date, role_name, emoji, color
        FROM daily_roles
        ORDER BY date DESC LIMIT 14
    """)

    # Recent daily scores for the chart
    daily_scores = query("""
        SELECT date, AVG(productivity_score) as score
        FROM productivity
        WHERE date >= date('now', '-30 days')
        GROUP BY date ORDER BY date ASC
    """)

    return render_template("coach.html",
        config=config,
        analysis=analysis,
        recent_roles=recent_roles,
        daily_scores=daily_scores,
        format_duration=format_duration,
    )


@app.route("/yearly")
def yearly():
    """Yearly overview with month-by-month breakdown."""
    config = load_config()
    year = int(request.args.get("year", date.today().year))

    # Monthly screen time averages
    monthly_data = []
    for m in range(1, 13):
        start = f"{year}-{m:02d}-01"
        if m == 12:
            end = f"{year}-12-31"
        else:
            end = str(date(year, m + 1, 1) - timedelta(days=1))

        row = query("""
            SELECT AVG(daily_total) as avg_seconds FROM (
                SELECT date(start_time, 'unixepoch') as day,
                       SUM(duration_seconds) as daily_total
                FROM sessions
                WHERE day BETWEEN ? AND ?
                GROUP BY day
            )
        """, (start, end), fetch="one")

        monthly_data.append({
            "month": m,
            "month_name": datetime(year, m, 1).strftime("%b"),
            "avg_daily_seconds": row["avg_seconds"] if row and row["avg_seconds"] else 0,
        })

    # Yearly totals
    yearly_totals = query("""
        SELECT SUM(duration_seconds) as total_seconds,
               COUNT(*) as total_sessions,
               AVG(duration_seconds) as avg_session
        FROM sessions
        WHERE strftime('%Y', datetime(start_time, 'unixepoch')) = ?
    """, (str(year),), fetch="one")

    # Top apps of the year
    top_apps_year = query("""
        SELECT app_name, SUM(foreground_seconds) as fg_total
        FROM app_usage
        WHERE strftime('%Y', date) = ?
        GROUP BY app_name ORDER BY fg_total DESC LIMIT 15
    """, (str(year),))

    # Network for the year
    network_year = query("""
        SELECT SUM(bytes_sent) as total_sent, SUM(bytes_received) as total_recv
        FROM network_usage
        WHERE strftime('%Y', date) = ?
    """, (str(year),), fetch="one")

    # Packages installed this year
    pkg_year = query("""
        SELECT action, COUNT(*) as count, package_name
        FROM packages WHERE strftime('%Y', date) = ?
        GROUP BY action ORDER BY count DESC
    """, (str(year),))

    return render_template("yearly.html",
        config=config,
        year=year,
        monthly_data=monthly_data,
        yearly_totals=yearly_totals,
        top_apps_year=top_apps_year,
        network_year=network_year,
        pkg_year=pkg_year,
        format_duration=format_duration,
        format_bytes=format_bytes,
    )


@app.route("/notifications")
def notifications():
    """Notification history."""
    config = load_config()
    period = request.args.get("period", "today")
    start_date, end_date = get_date_range(period)

    notif_data = query("""
        SELECT app_name, summary, body, action, urgency, timestamp
        FROM notifications
        WHERE date BETWEEN ? AND ?
        ORDER BY timestamp DESC LIMIT 200
    """, (start_date, end_date))

    # Per-app notification counts
    app_counts = query("""
        SELECT app_name, COUNT(*) as total,
               SUM(CASE WHEN action = 'dismissed' THEN 1 ELSE 0 END) as dismissed,
               SUM(CASE WHEN action = 'clicked' THEN 1 ELSE 0 END) as clicked
        FROM notifications
        WHERE date BETWEEN ? AND ?
        GROUP BY app_name ORDER BY total DESC
    """, (start_date, end_date))

    return render_template("notifications.html",
        config=config,
        notif_data=notif_data,
        app_counts=app_counts,
        period=period,
        format_duration=format_duration,
    )


@app.route("/hardware")
def hardware():
    """CPU, RAM, temperature, battery history."""
    config = load_config()
    period = request.args.get("period", "today")
    start_date, end_date = get_date_range(period)

    # Temperature + CPU/RAM history combined (hourly)
    temp_history = query("""
        SELECT strftime('%Y-%m-%d %H:00', datetime(timestamp, 'unixepoch', 'localtime')) as hour,
               AVG(cpu_temp)    as avg_cpu,
               MAX(cpu_temp)    as max_cpu,
               AVG(gpu_temp)    as avg_gpu,
               AVG(cpu_percent) as avg_cpu_pct,
               AVG(ram_percent) as avg_ram_pct
        FROM system_stats
        WHERE date BETWEEN ? AND ? AND cpu_temp IS NOT NULL
        GROUP BY hour ORDER BY hour
    """, (start_date, end_date))

    # CPU/RAM averages
    perf_averages = query("""
        SELECT AVG(cpu_percent) as avg_cpu, MAX(cpu_percent) as max_cpu,
               AVG(ram_percent) as avg_ram, MAX(ram_percent) as max_ram,
               AVG(swap_percent) as avg_swap
        FROM system_stats WHERE date BETWEEN ? AND ?
    """, (start_date, end_date), fetch="one")

    # Daily temp summary
    daily_temps = query("""
        SELECT date, cpu_min, cpu_max, cpu_avg
        FROM temperature_daily
        WHERE date BETWEEN ? AND ? ORDER BY date DESC
    """, (start_date, end_date))

    # Battery history
    battery_history = query("""
        SELECT strftime('%Y-%m-%d %H:00', datetime(timestamp, 'unixepoch')) as hour,
               AVG(percent) as avg_percent, MAX(is_plugged) as plugged
        FROM battery_stats
        WHERE date BETWEEN ? AND ?
        GROUP BY hour ORDER BY hour
    """, (start_date, end_date))

    return render_template("hardware.html",
        config=config,
        temp_history=temp_history,
        perf_averages=perf_averages,
        daily_temps=daily_temps,
        battery_history=battery_history,
        period=period,
        format_duration=format_duration,
    )


@app.route("/timeline")
def timeline_page():
    """The new hierarchical timeline view."""
    config = load_config()
    selected_date = request.args.get("date", str(date.today()))
    return render_template("timeline.html",
        config=config,
        selected_date=selected_date,
    )


@app.route("/input-stats")
def input_stats():
    """Keystrokes, WPM, mouse activity page."""
    config = load_config()
    period = request.args.get("period", "today")
    start_date, end_date = get_date_range(period)

    # Daily aggregates
    daily_data = query("""
        SELECT date,
               SUM(keystrokes)           as total_keys,
               SUM(mouse_clicks)         as total_clicks,
               SUM(mouse_scroll_events)  as total_scroll,
               SUM(mouse_distance_px)    as total_distance_px,
               AVG(NULLIF(wpm_sample,0)) as avg_wpm
        FROM input_stats
        WHERE date BETWEEN ? AND ?
        GROUP BY date
        ORDER BY date DESC
    """, (start_date, end_date))

    # Overall totals
    totals = query("""
        SELECT SUM(keystrokes)           as total_keys,
               SUM(mouse_clicks)         as total_clicks,
               SUM(mouse_scroll_events)  as total_scroll,
               SUM(mouse_distance_px)    as total_distance_px,
               AVG(NULLIF(wpm_sample,0)) as avg_wpm,
               COUNT(DISTINCT date)      as days_active
        FROM input_stats
        WHERE date BETWEEN ? AND ?
    """, (start_date, end_date), fetch="one")

    return render_template("input_stats.html",
        config=config,
        daily_data=daily_data,
        totals=totals or {},
        period=period,
        start_date=start_date,
        end_date=end_date,
    )


@app.route("/packages")
def packages():
    """Pacman package history."""
    config = load_config()
    period = request.args.get("period", "month")
    start_date, end_date = get_date_range(period)

    pkg_history = query("""
        SELECT action, package_name, old_version, new_version, date, timestamp
        FROM packages WHERE date BETWEEN ? AND ?
        ORDER BY timestamp DESC LIMIT 500
    """, (start_date, end_date))

    pkg_stats = query("""
        SELECT action, COUNT(*) as count
        FROM packages WHERE date BETWEEN ? AND ?
        GROUP BY action
    """, (start_date, end_date))

    # Most installed packages
    most_common = query("""
        SELECT package_name, COUNT(*) as times_installed
        FROM packages WHERE action = 'installed'
        GROUP BY package_name ORDER BY times_installed DESC LIMIT 20
    """)

    return render_template("packages.html",
        config=config,
        pkg_history=pkg_history,
        pkg_stats=pkg_stats,
        most_common=most_common,
        period=period,
    )


@app.route("/settings", methods=["GET", "POST"])
def settings():
    """Settings page — all config in one place, properly persisted."""
    config = load_config()
    message = None

    if request.method == "POST":
        old_command = config.get("cli_command", "byteslut")
        new_name    = (request.form.get("app_name", "ByteSlut").strip() or "ByteSlut")
        new_command = (request.form.get("cli_command", "byteslut")
                       .strip().lower().replace(" ", "_").replace("-", "_") or "byteslut")
        new_port     = int(request.form.get("dashboard_port", 6969) or 6969)
        new_interval = int(request.form.get("collection_interval", 30) or 30)

        config["app_name"]                    = new_name
        config["cli_command"]                 = new_command
        config["dashboard_port"]              = new_port
        config["collection_interval_seconds"] = new_interval
        config["anthropic_api_key"]           = request.form.get("anthropic_api_key", "").strip()
        config["ui_theme"]                    = request.form.get("ui_theme", "default")
        config["ui_layout"]                   = request.form.get("ui_layout", "default")
        # accent_color: save the text field (may be blank = use theme default)
        raw_accent = request.form.get("accent_color_text", "").strip()
        import re as _re
        config["accent_color"] = raw_accent if _re.match(r'^#[0-9a-fA-F]{6}$', raw_accent) else ""

        # Privacy toggles — checkbox present = True, absent = False
        new_privacy = {
            "track_keystrokes":      "track_keystrokes"      in request.form,
            "track_browser_history": "track_browser_history" in request.form,
            "track_notifications":   "track_notifications"   in request.form,
            "track_commands":        "track_commands"        in request.form,
            "track_typed_words":     "track_typed_words"     in request.form,
        }
        config["privacy"] = new_privacy

        # Daily report — saved exactly as entered, no defaults override
        config["daily_report"] = {
            "enabled":                      "report_enabled"       in request.form,
            "time":                         request.form.get("report_time", "18:30"),
            "delay_if_cpu_above_percent":   int(request.form.get("report_cpu_threshold",  30) or 30),
            "delay_if_temp_above_celsius":  int(request.form.get("report_temp_threshold", 75) or 75),
            "delay_check_interval_seconds": 120,
            "max_delay_minutes":            int(request.form.get("report_max_delay",      60) or 60),
        }

        # AI Coach — opt-in only, requires explicit consent checkbox
        config["ai_coach"] = {
            "enabled":        "ai_coach_enabled" in request.form,
            "ai_model":       request.form.get("ai_model", "claude"),
            "consent_given":  "ai_consent"       in request.form,
            "custom_api_url": request.form.get("ai_custom_url", "").strip(),
        }

        save_config(config)

        # Write privacy sentinel so daemon picks up changes within 10s
        # without needing a restart. Daemon's health monitor reads this file.
        try:
            sentinel = Path(__file__).parent.parent / "daemon" / ".privacy_changed"
            sentinel.write_text(json.dumps(new_privacy))
        except Exception as e:
            logger.warning(f"Could not write privacy sentinel: {e}")

        if old_command != new_command:
            _update_shell_alias(old_command, new_command)

        message = "Settings saved."
        config  = load_config()


    # DB stats
    db_path    = get_db_path()
    db_size_mb = round(os.path.getsize(db_path) / (1024 * 1024), 2) if os.path.exists(db_path) else 0

    record_counts = {}
    for table in ["sessions", "app_usage", "browser_history", "commands",
                  "notifications", "system_stats", "network_usage", "input_stats"]:
        row = query(f"SELECT COUNT(*) as cnt FROM {table}", fetch="one")
        record_counts[table] = row["cnt"] if row else 0

    oldest = query("SELECT MIN(date(start_time,'unixepoch')) as oldest FROM sessions", fetch="one")

    # Load available themes from web/themes/ folders
    from web.utils import get_available_themes
    available_themes = get_available_themes()

    return render_template("settings.html",
        config=config,
        message=message,
        db_size_mb=db_size_mb,
        db_path=db_path,
        record_counts=record_counts,
        oldest_date=(oldest["oldest"] if oldest and oldest["oldest"] else "N/A"),
        available_themes=available_themes,
    )


@app.route("/api/validate-themes")
def api_validate_themes():
    """
    Scan web/static/themes/ for layout CSS files and validate each one.
    Returns which themes are valid and which are missing required selectors.
    Called from the settings page to warn about broken themes.
    """
    from web.utils import get_available_themes
    themes = get_available_themes()
    return jsonify({
        "themes": {
            key: {
                "name":    info["name"],
                "valid":   info["valid"],
                "missing": info["missing"],
                "author":  info.get("author",""),
            }
            for key, info in themes.items()
        }
    })


@app.route("/api/save-theme", methods=["POST"])
def api_save_theme():
    """Save color theme or layout immediately when user clicks a card (no Save needed)."""
    data   = request.get_json(silent=True) or {}
    cfg    = load_config()
    saved  = {}

    theme  = data.get("theme", "").strip()
    layout = data.get("layout", "").strip()

    VALID_THEMES  = {"default","cyberpunk","matrix","minimal","blood","aurora",
                     "ocean","sunset","nord","dracula","solarized","gruvbox","catppuccin"}
    VALID_LAYOUTS = {"default","glass","minimal","compact"}

    if theme and theme in VALID_THEMES:
        cfg["ui_theme"] = theme
        saved["theme"]  = theme
    if layout and layout in VALID_LAYOUTS:
        cfg["ui_layout"] = layout
        saved["layout"]  = layout

    if saved:
        save_config(cfg)
        return jsonify({"ok": True, **saved})
    return jsonify({"ok": False, "error": "nothing to save"})



@app.route("/api/vacuum-db", methods=["POST"])
def api_vacuum_db():
    """
    VACUUM the SQLite database to reclaim space from deleted rows.
    Also runs auto-aggregation to roll up old raw data.
    """
    import os
    from daemon.db import get_db_path, get_connection
    db_path = get_db_path()
    size_before = os.path.getsize(db_path) / (1024*1024) if os.path.exists(db_path) else 0
    try:
        conn = get_connection()
        conn.execute("VACUUM")
        conn.execute("PRAGMA optimize")
        conn.close()
        size_after = os.path.getsize(db_path) / (1024*1024) if os.path.exists(db_path) else 0
        return jsonify({
            "ok": True,
            "size_before_mb": round(size_before, 2),
            "size_after_mb":  round(size_after, 2),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/wipe-database", methods=["POST"])
def wipe_database():
    """
    Wipe all collected data from the database.

    SAFETY: requires a one-time token that was generated and shown
    to the user in the Settings page. The token is a random 6-character
    string stored in the Flask session. The user must type it exactly
    to confirm they intend to wipe everything.

    Tables wiped: all data tables (keeps schema so daemon can resume).
    The app_registry is also wiped so app history resets completely.

    Why a token instead of just "type DELETE"?
      A static confirmation word can be muscle-memoried accidentally.
      A random token forces the user to actually read and copy it.
    """
    import json as _json

    data = request.get_json(silent=True) or {}
    submitted_token = data.get("token", "").strip()
    expected_token  = session.get("wipe_token", "")

    if not expected_token:
        return jsonify({"ok": False, "error": "No token generated — refresh Settings and try again"})

    if submitted_token != expected_token:
        return jsonify({"ok": False, "error": "Wrong confirmation code. Check again."})

    # Token matched — wipe all data tables
    tables = [
        "sessions", "app_usage", "system_stats", "temperature_daily",
        "browser_history", "notifications", "commands", "network_usage",
        "battery_stats", "packages", "productivity", "boot_times",
        "app_registry", "input_stats",
    ]

    wiped = {}
    for table in tables:
        try:
            row = query(f"SELECT COUNT(*) as cnt FROM {table}", fetch="one")
            count_before = row["cnt"] if row else 0
            execute(f"DELETE FROM {table}")
            wiped[table] = count_before
        except Exception as e:
            wiped[table] = f"error: {e}"

    # Invalidate the token so it can't be reused
    session.pop("wipe_token", None)

    # Write a reset sentinel file so the running daemon detects the wipe
    # and reinitialises its in-memory state WITHOUT needing a restart.
    # The daemon's health monitor loop checks for this file every 10 seconds.
    # When found: SessionCollector ends the deleted session + starts a new one,
    # AppCollector clears its accumulator, BatchWriter flushes its queue.
    # The file is deleted by the daemon after it handles it.
    try:
        sentinel = Path(__file__).parent.parent / "daemon" / ".db_wiped"
        sentinel.write_text("wiped")
    except Exception as e:
        logger.warning(f"Could not write reset sentinel: {e}")

    total_rows = sum(v for v in wiped.values() if isinstance(v, int))
    import logging as _logging
    _logging.getLogger(__name__).warning(
        f"DATABASE WIPED by user — {total_rows} rows deleted across {len(tables)} tables"
    )

    return jsonify({"ok": True, "wiped": wiped, "total_rows": total_rows})


@app.route("/api/generate-wipe-token", methods=["POST"])
def generate_wipe_token():
    """
    Generate a new random 6-character confirmation token for the database wipe.
    Stored in the Flask session (server-side). Never stored in the DB.
    Returns the token to show to the user in the UI.
    """
    import random, string
    token = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    session["wipe_token"] = token
    return jsonify({"token": token})


# ════════════════════════════════════════
# API ROUTES (used by JavaScript charts)
# ════════════════════════════════════════

@app.route("/api/hourly-apps")
def api_hourly_apps():
    """
    Returns app usage broken down by hour of the day.

    Response format:
    [
        { "hour": 9,  "app": "kitty", "seconds": 2400 },
        { "hour": 10, "app": "brave", "seconds": 3200 },
        ...
    ]

    For each hour, returns the DOMINANT app (most foreground time).
    Hours with no activity are excluded (frontend fills them as empty bars).

    This powers the main hourly chart on the dashboard.
    """
    today = str(date.today())

    # Get the dominant app per hour using strftime to extract hour from timestamp
    rows = query("""
        SELECT
            CAST(strftime('%H', datetime(timestamp, 'unixepoch', 'localtime')) AS INTEGER) as hour,
            app_name,
            SUM(foreground_seconds) as seconds
        FROM app_usage
        WHERE date = ?
          AND foreground_seconds > 0
          AND app_name NOT LIKE '(%)'
          AND app_name NOT IN (
              'sd-pam','accounts-daemon','agetty','pipewire','wireplumber',
              'pulseaudio','at-spi-bus-laun','at-spi2-registr','xdg-desktop-por',
              'gvfsd','archlinux-keyri','auto-cpufreq','udev-worker',
              'systemd','dbus-daemon','NetworkManager','bluetoothd'
          )
          AND app_name NOT LIKE '%daemon%'
          AND app_name NOT LIKE '%worker%'
          AND app_name NOT LIKE 'sd-%'
        GROUP BY hour, app_name
        ORDER BY hour ASC, seconds DESC
    """, (today,))

    # For each hour, keep only the top app (highest seconds)
    hourly = {}
    for row in rows:
        h = row["hour"]
        if h not in hourly or row["seconds"] > hourly[h]["seconds"]:
            hourly[h] = {"hour": h, "app": row["app_name"], "seconds": row["seconds"]}

    return jsonify(list(hourly.values()))


@app.route("/api/screen-time-week")
def api_screen_time_week():
    """Return last 7 days of screen time as JSON for charts."""
    rows = query("""
        SELECT date(start_time, 'unixepoch') as day,
               SUM(CASE
                   WHEN duration_seconds IS NOT NULL THEN duration_seconds
                   ELSE CAST(strftime('%s','now') AS INTEGER) - start_time
               END) / 3600.0 as hours
        FROM sessions
        WHERE start_time > strftime('%s', 'now', '-7 days')
        GROUP BY day ORDER BY day
    """)
    return jsonify(rows)


@app.route("/api/top-apps-today")
def api_top_apps_today():
    """Top 5 user apps today as JSON."""
    today = str(date.today())
    rows = query("""
        SELECT app_name, SUM(foreground_seconds) as seconds
        FROM app_usage WHERE date = ?
          AND foreground_seconds > 0
          AND app_name NOT LIKE '(%)'
          AND app_name NOT IN ('sd-pam','accounts-daemon','agetty','pipewire',
              'wireplumber','pulseaudio','at-spi-bus-laun','at-spi2-registr',
              'archlinux-keyri','auto-cpufreq','udev-worker','systemd',
              'dbus-daemon','gvfsd','udisksd')
          AND app_name NOT LIKE '%daemon%'
          AND app_name NOT LIKE '%worker%'
          AND app_name NOT LIKE 'sd-%'
        GROUP BY app_name ORDER BY seconds DESC LIMIT 5
    """, (today,))
    return jsonify(rows)


@app.route("/api/live-stats")
def api_live_stats():
    """Live system stats for the dashboard's live section."""
    stats = query("""
        SELECT cpu_percent, ram_percent, cpu_temp, gpu_temp
        FROM system_stats ORDER BY timestamp DESC LIMIT 1
    """, fetch="one")
    return jsonify(stats or {})


@app.route("/api/input-live")
def api_input_live():
    """
    Live input stats for today — polled every 30s by the input-stats page.

    Returns today's running totals directly from the DB.
    The BatchWriter flushes every 30s, so numbers update at most 30s after
    activity happens — much better than waiting for a full page reload.

    The input-stats page JS calls this and updates the stat cards in-place
    without reloading the whole page.
    """
    today = str(date.today())
    row = query("""
        SELECT
            COALESCE(SUM(keystrokes),           0) as total_keys,
            COALESCE(SUM(mouse_clicks),         0) as total_clicks,
            COALESCE(SUM(mouse_scroll_events),  0) as total_scroll,
            COALESCE(SUM(mouse_distance_px),    0) as total_distance_px,
            COALESCE(AVG(NULLIF(wpm_sample,0)), 0) as avg_wpm,
            COUNT(*)                               as flush_count
        FROM input_stats
        WHERE date = ?
    """, (today,), fetch="one")
    return jsonify(row or {})




@app.route("/api/network-week")
def api_network_week():
    """Network usage per day for last 7 days."""
    rows = query("""
        SELECT date,
               SUM(bytes_received) / 1048576.0 as recv_mb,
               SUM(bytes_sent) / 1048576.0 as sent_mb
        FROM network_usage
        WHERE date > date('now', '-7 days')
        GROUP BY date ORDER BY date
    """)
    return jsonify(rows)


import threading
import signal as _signal

# ── Dashboard auto-shutdown state ──
# Tracks last browser activity. If no request comes in for N minutes,
# the web server shuts itself down to free the port.
_last_activity = {"t": time.time()}
_shutdown_timer = None
_IDLE_TIMEOUT = None  # Set at startup from config (None = never auto-shutdown)


def _record_activity():
    """Called on every request to reset the idle timer."""
    _last_activity["t"] = time.time()


@app.before_request
def before_request_hook():
    """Reset activity timer on every incoming request."""
    _record_activity()


@app.route("/shutdown", methods=["POST"])
def shutdown():
    """
    Gracefully kill the dashboard web server.
    Called by the 'Kill Dashboard' button in the UI,
    or by the CLI with  spank --kill-dashboard

    We schedule the shutdown 300ms after responding so the
    browser gets the response before the server dies.
    """
    def _do_shutdown():
        time.sleep(0.3)
        os.kill(os.getpid(), _signal.SIGTERM)

    t = threading.Thread(target=_do_shutdown, daemon=True)
    t.start()
    return jsonify({"status": "shutting down"}), 200


@app.route("/api/timeline")
def api_timeline():
    """
    Returns a full hierarchical timeline for a given date.

    Structure returned:
    [
      {
        "app": "brave",
        "start_time": 1710000000,       ← Unix timestamp first seen
        "end_time":   1710014400,
        "duration_seconds": 14400,
        "is_flatpak": 0,
        "crashed": false,               ← true if session ended abnormally
        "domains": [                    ← browser tab breakdown from browser_history
          {
            "domain": "youtube.com",
            "duration_seconds": 3300,
            "pages": [
              {
                "title": "Sajna Ve (Official Video)",
                "url": "...",
                "timestamp": 1710001000,
                "duration_seconds": 540,
                "is_youtube": 1,
                "youtube_title": "Sajna Ve (Official Video)"
              }
            ]
          }
        ],
        "notifications": [              ← notifications received WHILE this app was active
          {
            "timestamp": 1710005000,
            "app_name": "whatsapp",
            "summary": "New message",
            "body": "Hey! Are you free?",
            "action": "clicked"
          }
        ]
      }
    ]
    """
    target_date = request.args.get("date", str(date.today()))

    # ── Step 1: Get all app usage blocks for the day ──
    # Each row = one app's activity block with start time approximated
    # from the session + app tracking data
    apps_raw = query("""
        SELECT
            app_name,
            SUM(foreground_seconds) as fg_seconds,
            SUM(background_seconds) as bg_seconds,
            MAX(is_flatpak) as is_flatpak,
            MAX(window_title) as last_title,
            MIN(timestamp) as first_seen,
            MAX(timestamp) as last_seen
        FROM app_usage
        WHERE date = ?
          AND foreground_seconds > 0
          AND app_name NOT LIKE '(%)'
          AND app_name NOT IN (
              'sd-pam','accounts-daemon','agetty','rtkit-daemon','polkitd',
              'udisksd','upowerd','colord','avahi-daemon','NetworkManager',
              'wpa_supplicant','bluetoothd','pipewire','wireplumber','pulseaudio',
              'at-spi-bus-laun','at-spi2-registr','xdg-desktop-por',
              'gvfsd','gvfsd-fuse','dconf-service','archlinux-keyri',
              'auto-cpufreq','udev-worker','systemd','dbus-daemon','dbus-broker'
          )
          AND app_name NOT LIKE '%daemon%'
          AND app_name NOT LIKE '%worker%'
          AND app_name NOT LIKE 'kworker%'
          AND app_name NOT LIKE 'sd-%'
        GROUP BY app_name
        ORDER BY first_seen DESC
    """, (target_date,))

    # ── Step 2: Get browser history grouped by app+domain for the day ──
    browser_raw = query("""
        SELECT
            browser,
            domain,
            SUM(MIN(visit_duration_seconds, 300)) as total_duration,
            COUNT(*) as visit_count
        FROM browser_history
        WHERE date = ?
        GROUP BY browser, domain
        ORDER BY total_duration DESC
    """, (target_date,))

    # ── Step 3: Get individual page visits (deduplicated, newest first) ──
    # Problem was: every tab refocus creates a new row for the same URL.
    # A YouTube video opened 22 times appeared as 22 separate entries.
    # Fix: GROUP BY a clean title (strips "(N) " prefix) + domain so each
    # unique video/page appears ONCE with summed duration and latest timestamp.
    # ORDER BY latest timestamp DESC so most recent activity is at top.
    pages_raw = query("""
        SELECT
            browser,
            domain,
            TRIM(
                CASE
                    WHEN COALESCE(youtube_title, title) GLOB '([0-9]*) *'
                    THEN SUBSTR(COALESCE(youtube_title, title),
                                INSTR(COALESCE(youtube_title, title), ' ') + 1)
                    ELSE COALESCE(youtube_title, title)
                END
            )                               AS clean_title,
            MAX(url)                        AS url,
            MAX(timestamp)                  AS timestamp,
            SUM(MIN(visit_duration_seconds, 300))     AS visit_duration_seconds,
            MAX(is_youtube)                 AS is_youtube,
            TRIM(
                CASE
                    WHEN COALESCE(youtube_title, title) GLOB '([0-9]*) *'
                    THEN SUBSTR(COALESCE(youtube_title, title),
                                INSTR(COALESCE(youtube_title, title), ' ') + 1)
                    ELSE COALESCE(youtube_title, '')
                END
            )                               AS youtube_title
        FROM browser_history
        WHERE date = ?
          AND domain NOT LIKE '127.0.0.1%'
          AND domain NOT LIKE 'localhost%'
        GROUP BY browser, domain, TRIM(
            CASE
                WHEN COALESCE(youtube_title, title) GLOB '([0-9]*) *'
                THEN SUBSTR(COALESCE(youtube_title, title),
                            INSTR(COALESCE(youtube_title, title), ' ') + 1)
                ELSE COALESCE(youtube_title, title)
            END
        )
        ORDER BY timestamp DESC
    """, (target_date,))

    # ── Step 4: Get all notifications for the day ──
    notifs_raw = query("""
        SELECT app_name, summary, body, action, urgency, timestamp
        FROM notifications
        WHERE date = ?
        ORDER BY timestamp ASC
    """, (target_date,))

    # ── Step 5: Get sessions to detect crashes ──
    sessions_raw = query("""
        SELECT start_time, end_time, duration_seconds
        FROM sessions
        WHERE date(start_time, 'unixepoch') = ?
        ORDER BY start_time ASC
    """, (target_date,))

    # ── Build domain → pages map ──
    # {browser: {domain: [page, ...]}}
    # Pages are already deduplicated and newest-first from the query above.
    from collections import defaultdict
    domain_pages = defaultdict(lambda: defaultdict(list))
    for p in pages_raw:
        domain_pages[p["browser"]][p["domain"] or "other"].append({
            "title":          p["clean_title"] or p["url"] or "",
            "url":            p["url"] or "",
            "timestamp":      p["timestamp"],
            "duration_seconds": p["visit_duration_seconds"] or 0,
            "is_youtube":     p["is_youtube"],
            "youtube_title":  p["youtube_title"] or "",
        })

    # ── Build browser domain summary ──
    # {browser: [{domain, total_duration, visit_count, pages:[...]}]}
    browser_domains = defaultdict(list)
    for row in browser_raw:
        pages = domain_pages[row["browser"]].get(row["domain"], [])
        browser_domains[row["browser"]].append({
            "domain":           row["domain"],
            "duration_seconds": row["total_duration"] or 0,
            "visit_count":      row["visit_count"],
            # Pages already deduplicated + sorted newest-first by query
            "pages": pages,
        })

    # ── Detect if any session ended abnormally (crash = large gap) ──
    crashed_sessions = set()
    for i, s in enumerate(sessions_raw):
        if i + 1 < len(sessions_raw):
            gap = sessions_raw[i+1]["start_time"] - (s["start_time"] + (s["duration_seconds"] or 0))
            if gap > 600 and s["end_time"] is None:
                crashed_sessions.add(s["start_time"])

    # ── Build timeline entries ──────────────────────────────────────────────
    # For each notification, find which app was the FOREGROUND app at that
    # exact timestamp. We do this by checking app_usage records to find which
    # app was last seen before the notification arrived.
    #
    # WHY NOT USE first_seen/last_seen:
    #   app_usage only has first_seen and last_seen for the WHOLE DAY.
    #   If you used kitty 10:00-12:00, did other things, then 14:00-16:00,
    #   last_seen=16:00 and first_seen=10:00. Any notification at 13:00
    #   (when you weren't using kitty) would wrongly be attributed to it.
    #
    # FIX: We build a timeline of which app was foreground at each minute,
    #   then for each notification we find the closest preceding app record.
    #   Since app_usage stores individual rows per session segment, we can
    #   find the app that was last active just before each notification.

    # Build a sorted list of (timestamp, app_name) from all app_usage records
    # for the day — each row represents one activity segment.
    app_activity_log = query("""
        SELECT app_name, timestamp, foreground_seconds
        FROM app_usage
        WHERE date = ?
          AND foreground_seconds > 0
          AND app_name NOT LIKE '(%)'
          AND app_name NOT LIKE '%daemon%'
        ORDER BY timestamp ASC
    """, (target_date,))

    # For each notification, find which app was active at that moment.
    # Strategy: find the app_usage row with the largest timestamp that is
    # still <= notification timestamp (i.e. the most recent app before the notif).
    def find_app_at_time(notif_ts: int) -> str:
        """Return the app_name that was most recently active before notif_ts."""
        best_app  = None
        best_ts   = -1
        for row in app_activity_log:
            row_ts  = row["timestamp"] or 0
            row_end = row_ts + (row["foreground_seconds"] or 0)
            # App was active from row_ts to row_end.
            # Notification arrived at notif_ts.
            # If notification is within the active window → definite match.
            if row_ts <= notif_ts <= row_end:
                return row["app_name"]
            # Otherwise track the app that ended most recently before the notif.
            if row_end <= notif_ts and row_end > best_ts:
                best_ts  = row_end
                best_app = row["app_name"]
        return best_app or ""

    # Build a map: app_name → [notifications that arrived while it was active]
    notif_by_app = {}
    used_notif_ids = set()
    for idx, n in enumerate(notifs_raw):
        notif_ts  = n["timestamp"] or 0
        owner_app = find_app_at_time(notif_ts)
        if owner_app:
            notif_by_app.setdefault(owner_app, []).append(n)
            used_notif_ids.add(idx)

    timeline = []

    # Build a set of browser app_names that actually had foreground time today.
    # This prevents firefox (installed but never opened) from getting domains
    # from Brave's history just because firefox.db exists on disk.
    active_browser_apps = {
        row["app_name"].lower()
        for row in apps_raw
        if (row["fg_seconds"] or 0) > 0
        and any(b in row["app_name"].lower()
                for b in ("brave", "firefox", "chrome", "chromium"))
    }

    for app_row in apps_raw:
        app_name = app_row["app_name"]

        # Find browser domains for this app (if it's a browser)
        browser_names = {"brave", "firefox", "chrome", "chromium",
                         "brave-browser", "firefox-esr", "google-chrome"}
        domains = []
        app_lower = app_name.lower()
        is_browser = (app_lower.replace("-browser", "") in browser_names or
                      any(b in app_lower for b in browser_names))

        if is_browser and app_lower in active_browser_apps:
            # Match this app to its browser key in browser_domains.
            # browser_domains keys come from browser_history.browser column ("brave","firefox" etc.)
            # We must ONLY assign brave's domains to brave, firefox's to firefox.
            # Never fall back to "only one browser has data" — that causes
            # firefox to show all of Brave's history just because firefox is installed.
            for browser_key in browser_domains:
                bk = browser_key.lower()
                # Direct match: "firefox" in "firefox", "brave" in "com.brave.browser"
                if bk in app_lower or app_lower in bk:
                    domains = browser_domains[browser_key]
                    break
                # Prefix match: "brave-browser" → "brave"
                if bk.split("-")[0] in app_lower or app_lower.split("-")[0] in bk:
                    domains = browser_domains[browser_key]
                    break
            # Flatpak name matching: "com.brave.browser" → look for "brave" key
            if not domains:
                for browser_key in browser_domains:
                    if browser_key.lower() in app_lower:
                        domains = browser_domains[browser_key]
                        break
            # NO further fallback — if we can't positively match, show no domains.
            # Better to show nothing than to show another browser's history.

        # Use the correctly attributed notifications for this app
        app_notifs = notif_by_app.get(app_name, [])

        # Format time strings
        def ts_to_time(ts):
            if not ts:
                return ""
            try:
                return datetime.fromtimestamp(ts).strftime("%I:%M %p").lstrip("0")
            except Exception:
                return ""

        timeline.append({
            "app": app_name,
            "start_time": app_row["first_seen"],
            "end_time": app_row["last_seen"],
            "start_fmt": ts_to_time(app_row["first_seen"]),
            "end_fmt": ts_to_time(app_row["last_seen"]),
            "fg_seconds": app_row["fg_seconds"] or 0,
            "bg_seconds": app_row["bg_seconds"] or 0,
            "is_flatpak": app_row["is_flatpak"],
            "last_title": app_row["last_title"] or "",
            "crashed": app_row["first_seen"] in crashed_sessions,
            "domains": sorted(domains, key=lambda x: x["duration_seconds"], reverse=True),
            "notifications": [
                {**n, "time_fmt": ts_to_time(n["timestamp"])}
                for n in app_notifs
            ],
        })

    return jsonify({
        "date": target_date,
        "timeline": timeline,
        "total_apps": len(timeline),
        "total_notifs": len(notifs_raw),
    })


@app.route("/api/dashboard-status")
def dashboard_status():
    """Returns uptime, idle seconds, and system health diagnostics."""
    import glob, subprocess as sp

    idle = int(time.time() - _last_activity["t"])

    # ── Health checks ──
    health = {}

    # 1. Check if daemon is collecting app data (any fg time in last 10 min)
    recent_apps = query("""
        SELECT COUNT(*) as cnt FROM app_usage
        WHERE timestamp > ? AND foreground_seconds > 0
    """, (int(time.time()) - 600,), fetch="one")
    health["app_tracking"] = (recent_apps["cnt"] > 0) if recent_apps else False

    # 2. Check Hyprland env vars (are they available?)
    health["hyprland_env"] = bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))
    health["wayland_display"] = os.environ.get("WAYLAND_DISPLAY", "")

    # 3. Check input group membership
    try:
        r = sp.run(["groups"], capture_output=True, text=True)
        health["in_input_group"] = "input" in r.stdout
    except Exception:
        health["in_input_group"] = False

    # 4. Check input devices accessible
    input_devices = glob.glob("/dev/input/event*")
    readable_inputs = [d for d in input_devices if os.access(d, os.R_OK)]
    health["input_devices_readable"] = len(readable_inputs)

    # 5. Check if dbus-python is available
    try:
        import dbus
        health["dbus_available"] = True
    except ImportError:
        health["dbus_available"] = False

    # 6. Check hyprland socket
    uid = os.getuid()
    hypr_sockets = glob.glob(f"/run/user/{uid}/hypr/*/.socket.sock")
    health["hyprland_socket"] = hypr_sockets[0] if hypr_sockets else None

    # 7. Check recent session data
    recent_session = query("""
        SELECT id, duration_seconds, start_time FROM sessions
        WHERE end_time IS NULL OR end_time > ?
        ORDER BY start_time DESC LIMIT 1
    """, (int(time.time()) - 3600,), fetch="one")
    health["active_session"] = recent_session["id"] if recent_session else None

    return jsonify({
        "idle_seconds":  idle,
        "idle_timeout":  _IDLE_TIMEOUT,
        "pid":           os.getpid(),
        "health":        health,
    })


def _update_shell_alias(old_command: str, new_command: str):
    """
    Update the shell alias in ~/.bashrc and ~/.zshrc when the user
    renames the app from the dashboard.
    """
    rc_files = [Path.home() / ".bashrc", Path.home() / ".zshrc"]
    for rc_file in rc_files:
        if not rc_file.exists():
            continue
        try:
            content = rc_file.read_text()
            old_alias = f"alias {old_command}="
            new_alias = f"alias {new_command}="
            if old_alias in content:
                content = content.replace(old_alias, new_alias)
                rc_file.write_text(content)
        except Exception:
            pass


def start_server(host="127.0.0.1", port=None, idle_timeout=None):
    """
    Start the Flask dashboard.

    idle_timeout: seconds of no browser activity before auto-shutdown.
                  None = stay alive forever until manually killed.
                  300 = shut down after 5 minutes of no tab activity.
    """
    global _IDLE_TIMEOUT
    config = load_config()
    port = port or config.get("dashboard_port", 6969)
    _IDLE_TIMEOUT = idle_timeout

    if idle_timeout:
        # Background watcher: kills the server after N seconds of idle
        def _idle_watcher():
            while True:
                time.sleep(30)
                idle = time.time() - _last_activity["t"]
                if idle >= idle_timeout:
                    print(f"\n[ByteSlut] Dashboard idle for {idle:.0f}s — auto-shutdown.")
                    os.kill(os.getpid(), _signal.SIGTERM)
                    break
        threading.Thread(target=_idle_watcher, daemon=True).start()

    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    start_server()