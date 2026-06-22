"""
daemon/analyzer.py — Daily Role Detector & Productivity Analyzer
=================================================================
Runs once a day (called from daily_report.py or on demand).
Does two things:

1. DAILY ROLE DETECTION
   Looks at what you actually did that day and assigns a human-readable
   role like "Deep Focus Coder", "YouTube Binger", "Research Day" etc.
   Saves one row to the daily_roles table per day.

2. PRODUCTIVITY COACHING DATA
   Looks at patterns across your last 30 days and builds structured
   coaching data: what's working, what's hurting your score, and
   specific actionable advice. Saves to productivity_coaching table.
   The web page fetches this + enriches with web search via Claude API.

HOW ROLE IS DETECTED (rule-based, no AI needed):
   We score each day against role templates based on:
   - App usage (kitty/nvim = coding, brave = browsing, spotify = music)
   - Browser domains (github.com, youtube.com, etc.)
   - Time of day patterns (nocturnal vs morning person)
   - Productivity score from ProductivityCollector
   - Session length and count
   The role with the highest score wins.

ROLES AVAILABLE:
   Deep Focus Coder      → high coding_samples, long sessions
   Researcher            → github/arxiv/stackoverflow heavy, learning videos
   Content Consumer      → youtube heavy, entertainment high
   Communicator          → telegram/discord/email heavy
   Creative Session      → design tools, blender, gimp
   Terminal Warrior      → terminal dominant, many commands
   Distracted Day        → many short sessions, high entertainment
   Balanced Day          → mixed productive + entertainment
   Grind Day             → screen time > 10h, high productivity score
   Rest Day              → low screen time, mostly entertainment
   Movie Guy             → vlc/netflix/video heavy evening
   Study Session         → learning videos + docs + low entertainment
   Admin Day             → file manager, settings, many packages
   Unknown               → not enough data
"""

import json
import time
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# ROLE DEFINITIONS
# Each role has:
#   name        → display name
#   description → shown on the Day Roles page
#   emoji       → visual icon
#   color       → CSS color class
#   rules       → list of (field, operator, value, weight) tuples
#
# SCORING: for each rule that matches, add weight to role's total score.
# Role with highest total score wins.
# ─────────────────────────────────────────────────────────────────
ROLES = [
    {
        "name": "Deep Focus Coder",
        "description": "You spent most of the day writing code. Long sessions, high concentration.",
        "emoji": "⚡",
        "color": "c-blue",
        "rules": [
            ("coding_samples_pct", ">=", 40, 5),
            ("coding_samples_pct", ">=", 60, 5),
            ("productivity_score", ">=", 70, 3),
            ("longest_session_h",  ">=", 3,  2),
            ("session_count",      "<=", 4,  1),
        ]
    },
    {
        "name": "Terminal Warrior",
        "description": "Heavy terminal and command usage. DevOps, scripting, or sysadmin work.",
        "emoji": "$_",
        "color": "c-green",
        "rules": [
            ("top_app_is_terminal", "==", True, 6),
            ("commands_count",      ">=", 50,   4),
            ("commands_count",      ">=", 200,  4),
            ("coding_samples_pct",  ">=", 30,   2),
        ]
    },
    {
        "name": "Researcher",
        "description": "Lots of reading, learning, and exploring new topics online.",
        "emoji": "🔍",
        "color": "c-purple",
        "rules": [
            ("learning_samples_pct", ">=", 30, 5),
            ("top_domain_category",  "==", "productive", 4),
            ("productivity_score",   ">=", 60, 2),
            ("youtube_count",        ">=", 5,  1),  # learning videos
        ]
    },
    {
        "name": "Study Session",
        "description": "Focused learning day — tutorials, courses, documentation.",
        "emoji": "📚",
        "color": "c-blue",
        "rules": [
            ("learning_samples_pct",     ">=", 40, 6),
            ("entertainment_samples_pct","<=", 20, 3),
            ("productivity_score",       ">=", 65, 2),
        ]
    },
    {
        "name": "Content Consumer",
        "description": "Mostly watching videos and browsing. High YouTube and entertainment time.",
        "emoji": "📺",
        "color": "c-yellow",
        "rules": [
            ("entertainment_samples_pct", ">=", 40, 5),
            ("youtube_count",             ">=", 10, 4),
            ("youtube_count",             ">=", 30, 4),
            ("top_app_is_browser",        "==", True, 2),
        ]
    },
    {
        "name": "Movie Guy",
        "description": "Long media watching sessions. VLC, streaming, or YouTube movies.",
        "emoji": "🎬",
        "color": "c-yellow",
        "rules": [
            ("media_player_used",         "==", True, 6),
            ("entertainment_samples_pct", ">=", 50,   5),
            ("evening_dominant",          "==", True,  2),
        ]
    },
    {
        "name": "Communicator",
        "description": "Heavy messaging and communication. Telegram, Discord, email.",
        "emoji": "💬",
        "color": "c-blue",
        "rules": [
            ("top_app_is_comm",           "==", True, 6),
            ("notifications_count",       ">=", 50,   3),
            ("notifications_count",       ">=", 100,  3),
        ]
    },
    {
        "name": "Creative Session",
        "description": "Design and creative work. Blender, GIMP, Inkscape, or similar.",
        "emoji": "🎨",
        "color": "c-purple",
        "rules": [
            ("design_tools_used", "==", True, 8),
            ("productivity_score",">=", 55,   2),
        ]
    },
    {
        "name": "Grind Day",
        "description": "Marathon session. Very high screen time and productivity.",
        "emoji": "🔥",
        "color": "c-accent",
        "rules": [
            ("screen_time_h",    ">=", 8,  4),
            ("screen_time_h",    ">=", 10, 4),
            ("productivity_score",">=",70, 3),
            ("session_count",    ">=", 2,  1),
        ]
    },
    {
        "name": "Distracted Day",
        "description": "Lots of short sessions, frequent context switching, low productivity.",
        "emoji": "😵",
        "color": "c-accent",
        "rules": [
            ("session_count",             ">=", 8,  4),
            ("avg_session_m",             "<=", 30, 3),
            ("entertainment_samples_pct", ">=", 35, 3),
            ("productivity_score",        "<=", 40, 2),
        ]
    },
    {
        "name": "Admin Day",
        "description": "System maintenance, package updates, file management.",
        "emoji": "🔧",
        "color": "c-muted",
        "rules": [
            ("packages_changed", ">=", 5,    5),
            ("packages_changed", ">=", 20,   4),
            ("top_app_is_files", "==", True, 3),
            ("commands_count",   ">=", 30,   2),
        ]
    },
    {
        "name": "Balanced Day",
        "description": "Good mix of work and relaxation. Healthy productivity pattern.",
        "emoji": "⚖️",
        "color": "c-green",
        "rules": [
            ("productivity_score",        ">=", 50, 3),
            ("productivity_score",        "<=", 75, 2),
            ("entertainment_samples_pct", ">=", 15, 2),
            ("entertainment_samples_pct", "<=", 40, 2),
        ]
    },
    {
        "name": "Rest Day",
        "description": "Light use day. Low screen time, mostly casual browsing.",
        "emoji": "😌",
        "color": "c-muted",
        "rules": [
            ("screen_time_h", "<=", 3, 5),
            ("screen_time_h", "<=", 1, 4),
        ]
    },
]

COMM_APPS    = {"telegram", "telegram-desktop", "discord", "slack", "element", "signal"}
DESIGN_APPS  = {"gimp", "inkscape", "blender", "krita", "figma", "darktable", "kdenlive"}
MEDIA_APPS   = {"vlc", "mpv", "celluloid", "rhythmbox", "clementine", "totem"}
TERMINAL_APPS= {"kitty", "alacritty", "foot", "wezterm", "gnome-terminal", "konsole", "xterm"}
FILE_APPS    = {"thunar", "nautilus", "dolphin", "nemo", "pcmanfm"}


def compute_day_features(target_date: str) -> dict:
    """
    Extract all the features needed to score a day against the role templates.
    Queries the database and returns a flat dict of scalar features.
    """
    from daemon.db import query

    # ── Productivity row (aggregated for day) ──
    prod = query("""
        SELECT AVG(productivity_score) as prod_score,
               SUM(coding_samples)      as coding,
               SUM(learning_samples)    as learning,
               SUM(ai_samples)          as ai,
               SUM(entertainment_samples) as entertainment,
               SUM(neutral_samples)     as neutral,
               SUM(sample_count)        as total_samples
        FROM productivity WHERE date = ?
    """, (target_date,), fetch="one")

    total_s    = (prod["total_samples"] or 1) if prod else 1
    prod_score = prod["prod_score"] if prod else 0

    # ── Sessions ──
    sessions = query("""
        SELECT COUNT(*) as cnt,
               SUM(duration_seconds) as total_secs,
               MAX(duration_seconds) as longest_secs,
               AVG(duration_seconds) as avg_secs
        FROM sessions
        WHERE date(start_time,'unixepoch') = ?
          AND duration_seconds > 60
    """, (target_date,), fetch="one")

    screen_time_h   = ((sessions["total_secs"] or 0) / 3600) if sessions else 0
    longest_sess_h  = ((sessions["longest_secs"] or 0) / 3600) if sessions else 0
    avg_session_m   = ((sessions["avg_secs"] or 0) / 60) if sessions else 0
    session_count   = (sessions["cnt"] or 0) if sessions else 0

    # ── Top app ──
    top_app = query("""
        SELECT app_name, SUM(foreground_seconds) as total
        FROM app_usage
        WHERE date = ? AND foreground_seconds > 0
          AND app_name NOT LIKE '(%)'
          AND app_name NOT LIKE '%daemon%'
        GROUP BY app_name ORDER BY total DESC LIMIT 1
    """, (target_date,), fetch="one")
    top_app_name = (top_app["app_name"] or "").lower() if top_app else ""

    # ── Commands ──
    cmds = query("SELECT COUNT(*) as cnt FROM commands WHERE date = ?",
                 (target_date,), fetch="one")
    commands_count = (cmds["cnt"] or 0) if cmds else 0

    # ── Browser / YouTube ──
    yt = query("""
        SELECT COUNT(*) as cnt FROM browser_history
        WHERE date = ? AND is_youtube = 1
    """, (target_date,), fetch="one")
    youtube_count = (yt["cnt"] or 0) if yt else 0

    top_domain = query("""
        SELECT domain, SUM(visit_duration_seconds) as total
        FROM browser_history WHERE date = ?
        GROUP BY domain ORDER BY total DESC LIMIT 1
    """, (target_date,), fetch="one")
    top_domain_name = (top_domain["domain"] or "").lower() if top_domain else ""

    # ── Packages ──
    pkgs = query("""
        SELECT COUNT(*) as cnt FROM packages WHERE date = ?
    """, (target_date,), fetch="one")
    packages_changed = (pkgs["cnt"] or 0) if pkgs else 0

    # ── Notifications ──
    notifs = query("""
        SELECT COUNT(*) as cnt FROM notifications WHERE date = ?
    """, (target_date,), fetch="one")
    notifications_count = (notifs["cnt"] or 0) if notifs else 0

    # ── Sessions by hour (to detect evening dominance) ──
    evening_rows = query("""
        SELECT COUNT(*) as cnt FROM sessions
        WHERE date(start_time,'unixepoch') = ?
          AND CAST(strftime('%H', datetime(start_time,'unixepoch','localtime')) AS INT) >= 18
    """, (target_date,), fetch="one")
    evening_dominant = ((evening_rows["cnt"] or 0) >= 2) if evening_rows else False

    # ── App-type flags ──
    app_names = query("""
        SELECT DISTINCT app_name FROM app_usage WHERE date = ? AND foreground_seconds > 30
    """, (target_date,))
    all_apps = {r["app_name"].lower() for r in app_names}

    def any_match(names_set):
        return any(a in all_apps or any(a in app for app in all_apps) for a in names_set)

    # Sample percentages
    def pct(val):
        return ((val or 0) / total_s) * 100

    PRODUCTIVE_DOMAINS = ["github.com","gitlab.com","stackoverflow.com","developer.mozilla.org",
                          "docs.","arxiv.org","pypi.org","npmjs.com","coursera.org","udemy.com"]
    top_domain_is_productive = any(d in top_domain_name for d in PRODUCTIVE_DOMAINS)

    return {
        "productivity_score":         prod_score or 0,
        "coding_samples_pct":         pct(prod["coding"] if prod else 0),
        "learning_samples_pct":       pct(prod["learning"] if prod else 0),
        "entertainment_samples_pct":  pct(prod["entertainment"] if prod else 0),
        "neutral_samples_pct":        pct(prod["neutral"] if prod else 0),
        "screen_time_h":              screen_time_h,
        "longest_session_h":          longest_sess_h,
        "avg_session_m":              avg_session_m,
        "session_count":              session_count,
        "commands_count":             commands_count,
        "youtube_count":              youtube_count,
        "packages_changed":           packages_changed,
        "notifications_count":        notifications_count,
        "evening_dominant":           evening_dominant,
        "top_app_is_terminal":        any(a in top_app_name for a in TERMINAL_APPS),
        "top_app_is_browser":         any(b in top_app_name for b in ("brave","firefox","chrome","chromium")),
        "top_app_is_comm":            any_match(COMM_APPS),
        "top_app_is_files":           any_match(FILE_APPS),
        "design_tools_used":          any_match(DESIGN_APPS),
        "media_player_used":          any_match(MEDIA_APPS),
        "top_domain_category":        "productive" if top_domain_is_productive else "neutral",
        "top_app_name":               top_app_name,
        "top_domain_name":            top_domain_name,
    }


def _check_rule(features: dict, field: str, op: str, value) -> bool:
    """Evaluate one rule condition against the feature dict."""
    actual = features.get(field)
    if actual is None:
        return False
    if op == ">=": return actual >= value
    if op == "<=": return actual <= value
    if op == "==": return actual == value
    if op == ">":  return actual > value
    if op == "<":  return actual < value
    return False


def detect_role(target_date: str) -> dict:
    """
    Detect the role for a given day.
    Returns a dict with name, description, emoji, color, score, features.
    """
    features = compute_day_features(target_date)

    # Score every role
    scored = []
    for role in ROLES:
        total_weight = 0
        for (field, op, value, weight) in role["rules"]:
            if _check_rule(features, field, op, value):
                total_weight += weight
        scored.append((total_weight, role))

    # Pick the highest-scoring role (stable sort keeps order for ties)
    scored.sort(key=lambda x: x[0], reverse=True)
    best_weight, best_role = scored[0]

    # If nothing scored above 3, call it Unknown
    if best_weight < 3 or features["screen_time_h"] < 0.1:
        result_role = {
            "name": "Unknown",
            "description": "Not enough data to determine the day's role.",
            "emoji": "?",
            "color": "c-muted",
        }
    else:
        result_role = best_role

    return {
        "date":        target_date,
        "role_name":   result_role["name"],
        "description": result_role["description"],
        "emoji":       result_role["emoji"],
        "color":       result_role["color"],
        "role_score":  best_weight,
        "features":    json.dumps(features),
        # Top 3 competing roles for transparency
        "alternatives": json.dumps([
            {"name": r["name"], "score": w}
            for w, r in scored[1:4] if w > 0
        ]),
    }


def save_daily_role(target_date: str) -> dict:
    """
    Detect and save the daily role to the database.
    Returns the role dict. Safe to call multiple times (upserts).
    """
    from daemon.db import execute

    role = detect_role(target_date)

    execute("""
        INSERT OR REPLACE INTO daily_roles
            (date, role_name, description, emoji, color, role_score, features, alternatives)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        role["date"], role["role_name"], role["description"],
        role["emoji"], role["color"], role["role_score"],
        role["features"], role["alternatives"],
    ))

    logger.info(
        f"Daily role for {target_date}: {role['emoji']} {role['role_name']} "
        f"(score={role['role_score']})"
    )
    return role


def analyze_productivity_patterns(days: int = 30) -> dict:
    """
    Analyze the last N days of productivity data and return structured
    coaching data. This is what the Productivity Coach page uses.

    Returns a dict with:
      - strengths: what's working well
      - weaknesses: what's hurting your score
      - patterns: observations about your routine
      - best_day_of_week: which day you're most productive
      - worst_day_of_week: which day needs work
      - avg_score: overall average
      - trend: improving / declining / stable
      - top_distractions: sites/apps pulling score down
      - peak_hours: when you're most focused
    """
    from daemon.db import query

    end_date   = str(date.today())
    start_date = str(date.today() - timedelta(days=days))

    # Aggregate daily productivity
    rows = query("""
        SELECT date,
               AVG(productivity_score)      as score,
               SUM(coding_samples)          as coding,
               SUM(learning_samples)        as learning,
               SUM(entertainment_samples)   as entertainment,
               SUM(neutral_samples)         as neutral,
               SUM(sample_count)            as total_samples,
               (SELECT dominant_category FROM productivity p2
                WHERE p2.date = p.date ORDER BY sample_count DESC LIMIT 1
               ) as dominant
        FROM productivity p
        WHERE date BETWEEN ? AND ?
        GROUP BY date ORDER BY date ASC
    """, (start_date, end_date))

    if not rows or len(rows) < 3:
        return {"error": "not_enough_data", "days_needed": 3 - len(rows)}

    scores = [r["score"] or 0 for r in rows]
    avg_score = sum(scores) / len(scores)

    # Trend: compare first half vs second half
    mid = len(scores) // 2
    first_half_avg  = sum(scores[:mid]) / mid if mid else avg_score
    second_half_avg = sum(scores[mid:]) / max(1, len(scores)-mid)
    diff = second_half_avg - first_half_avg
    trend = "improving" if diff > 5 else ("declining" if diff < -5 else "stable")

    # Day-of-week analysis
    dow_scores = {0:[], 1:[], 2:[], 3:[], 4:[], 5:[], 6:[]}
    dow_names  = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    for r in rows:
        try:
            dow = datetime.strptime(r["date"], "%Y-%m-%d").weekday()
            dow_scores[dow].append(r["score"] or 0)
        except Exception:
            pass

    dow_avgs = {dow: (sum(v)/len(v) if v else 0) for dow, v in dow_scores.items()}
    best_dow  = max(dow_avgs, key=dow_avgs.get)
    worst_dow = min((dow for dow, v in dow_avgs.items() if v), key=dow_avgs.get, default=0)

    # Top distracting domains
    distract = query("""
        SELECT domain, COUNT(*) as visits, SUM(visit_duration_seconds) as total_time
        FROM browser_history
        WHERE date BETWEEN ? AND ?
          AND domain IN (
              'youtube.com','instagram.com','twitter.com','x.com',
              'tiktok.com','reddit.com','9gag.com','facebook.com',
              'twitch.tv','netflix.com'
          )
        GROUP BY domain ORDER BY total_time DESC LIMIT 5
    """, (start_date, end_date))

    # What category dominates
    total_coding  = sum(r["coding"] or 0 for r in rows)
    total_entert  = sum(r["entertainment"] or 0 for r in rows)
    total_learn   = sum(r["learning"] or 0 for r in rows)
    total_samples = sum(r["total_samples"] or 1 for r in rows)

    def pct(v): return round((v / max(total_samples, 1)) * 100, 1)

    strengths = []
    weaknesses = []
    patterns = []

    if pct(total_coding) >= 25:
        strengths.append(f"Strong coding focus — {pct(total_coding):.0f}% of time in editors/terminals")
    if pct(total_learn) >= 15:
        strengths.append(f"Good learning habit — {pct(total_learn):.0f}% of time on learning content")
    if avg_score >= 70:
        strengths.append(f"Consistently high productivity score ({avg_score:.0f}/100)")
    if trend == "improving":
        strengths.append("Your score is trending upward — you're getting better")

    if pct(total_entert) >= 35:
        weaknesses.append(f"High entertainment usage — {pct(total_entert):.0f}% of tracked time on entertainment")
    if avg_score < 50:
        weaknesses.append(f"Low average score ({avg_score:.0f}/100) — room for significant improvement")
    if trend == "declining":
        weaknesses.append("Score is trending downward over the last period")
    if distract:
        top_d = distract[0]
        weaknesses.append(
            f"Frequent visits to {top_d['domain']} "
            f"({top_d['visits']} visits, {round((top_d['total_time'] or 0)/3600, 1)}h total)"
        )

    if dow_avgs[best_dow] > 0:
        patterns.append(f"Most productive day: {dow_names[best_dow]} (avg {dow_avgs[best_dow]:.0f}/100)")
    if dow_avgs[worst_dow] > 0:
        patterns.append(f"Least productive day: {dow_names[worst_dow]} (avg {dow_avgs[worst_dow]:.0f}/100)")
    if len(rows) >= 7 and scores[-3:]:
        recent_avg = sum(scores[-3:]) / 3
        patterns.append(f"Last 3 days average: {recent_avg:.0f}/100")

    return {
        "avg_score":       round(avg_score, 1),
        "trend":           trend,
        "trend_delta":     round(diff, 1),
        "days_analyzed":   len(rows),
        "best_day":        dow_names[best_dow],
        "best_day_score":  round(dow_avgs[best_dow], 1),
        "worst_day":       dow_names[worst_dow],
        "worst_day_score": round(dow_avgs[worst_dow], 1),
        "coding_pct":      pct(total_coding),
        "learning_pct":    pct(total_learn),
        "entertainment_pct": pct(total_entert),
        "top_distractions": [
            {"domain": d["domain"], "hours": round((d["total_time"] or 0)/3600, 1),
             "visits": d["visits"]}
            for d in distract
        ],
        "strengths":  strengths,
        "weaknesses": weaknesses,
        "patterns":   patterns,
        "daily_scores": [{"date": r["date"], "score": round(r["score"] or 0, 1)} for r in rows],
    }
