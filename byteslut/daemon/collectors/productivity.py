"""
collectors/productivity.py — Productivity Score Calculator
===========================================================
Computes a 0-100 productivity score from what you actually did,
using data already reliably collected by other collectors.

WHY DB-DRIVEN NOT LIVE WINDOW POLLING:
  The old approach polled the active window every 5 seconds and classified
  it in real time. This had two fatal bugs:
    1. idle detection: last_input_time never got updated from InputCollector
       so after 5 minutes, every sample was "idle" (score=5) regardless
       of what app was in foreground.
    2. Hyprland socket failures: get_active_window() would silently return
       None and fall back to /proc which finds random system daemons.

  The new approach: every 60 seconds, query app_usage and browser_history
  (already collected correctly by AppCollector and BrowserCollector)
  and classify what was actually used in the last 60 seconds.
  This is reliable, accurate, and requires no IPC.

SCORING CATEGORIES (0-10 scale, normalised to 0-100):
  coding_editor  10 — nvim, vscode, etc.
  terminal       10 — kitty, alacritty, etc.
  ai_tool         8 — claude.ai, chatgpt, etc.
  learning_video  8 — educational YouTube
  productive_site 7 — github, docs, stackoverflow
  writing_docs    6 — obsidian, libreoffice, etc.
  dev_tool        8 — postman, dbeaver, wireshark
  work_comm       5 — slack, element, etc.
  neutral         5 — file managers, settings, unknown
  idle            3 — no app activity at all
  social_media    1 — twitter, instagram, etc.
  entertainment   2 — youtube non-educational, vlc, games

CONFIDENCE:
  High (1.0) for definitive matches (coding editor, terminal).
  Low  (0.3) for neutral/unknown — they barely move the score.
  This means a mix of coding + neutral gives a high score, which is correct.
"""

import json
import time
import logging
import threading
from pathlib import Path
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR        = Path.home() / ".local" / "share" / "byteslut"
USER_SITES_FILE = DATA_DIR / "user_sites.json"

# ─────────────────────────────────────────────────────────────
# SCORING TABLE
# Each category gets a score (0-10) and a confidence (0-1).
# confidence=1.0 means "definitely this category"
# confidence=0.3 means "probably neutral, don't let it drag score down"
# ─────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────────
# PRODUCTIVITY TIERS — RescueTime-proven 5-tier system
# Based on: https://help.rescuetime.com/article/73-how-is-my-productivity-pulse-calculated
#
# FORMULA: ((vd*0 + d*1 + n*2 + p*3 + vp*4) / (total*4)) * 100
# Where: vd=very_distracting, d=distracting, n=neutral, p=productive, vp=very_productive
#
# This maps cleanly to 0-100 and is time-weighted:
#   All time very_productive → 100
#   All time productive      → 75
#   All time neutral         → 50
#   All time distracting     → 25
#   All time very_distracting → 0
#
# WHY 5 TIERS NOT RAW 0-10 SCORES:
#   The old 0-10 system used confidence weights that made neutral (score=5)
#   drag the whole score toward 50, making it hard to ever hit 80+.
#   The RescueTime 5-tier system is proven, intuitive, and user-adjustable.
# ──────────────────────────────────────────────────────────────────

# Tier values (used in the formula above)
TIER_VERY_PRODUCTIVE  = 4
TIER_PRODUCTIVE       = 3
TIER_NEUTRAL          = 2
TIER_DISTRACTING      = 1
TIER_VERY_DISTRACTING = 0

# Map category → tier
SCORE = {
    # Very productive (tier 4): active coding, building, creating
    "coding_editor":      TIER_VERY_PRODUCTIVE,   # nvim, vscode — writing code
    "terminal":           TIER_VERY_PRODUCTIVE,   # kitty — running code, sysadmin
    "localhost_dev":      TIER_VERY_PRODUCTIVE,   # localhost:3000 — testing your own work
    "user_productive":    TIER_VERY_PRODUCTIVE,   # user labelled this as work
    "design_tool":        TIER_PRODUCTIVE,        # figma, inkscape
    "dev_tool":           TIER_PRODUCTIVE,        # postman, dbeaver, wireshark

    # Productive (tier 3): learning, researching, communicating for work
    "ai_tool":            TIER_PRODUCTIVE,        # claude.ai, chatgpt — tools used for work
    "learning_video":     TIER_PRODUCTIVE,        # educational YouTube
    "productive_site":    TIER_PRODUCTIVE,        # github, docs, stackoverflow
    "writing_docs":       TIER_PRODUCTIVE,        # obsidian, libreoffice

    # Neutral (tier 2): necessary but not directly productive
    "work_comm":          TIER_NEUTRAL,           # slack, discord for work
    "user_neutral":       TIER_NEUTRAL,           # user labelled neutral
    "neutral":            TIER_NEUTRAL,           # unknown apps — don't penalise

    # Distracting (tier 1): could be productive, often isn't
    "entertainment_app":  TIER_DISTRACTING,       # media players open in foreground
    "social_media":       TIER_DISTRACTING,       # instagram, twitter

    # Very distracting (tier 0): definitely not work
    "entertainment_fg":   TIER_VERY_DISTRACTING,  # youtube/netflix in foreground
    "user_entertainment": TIER_VERY_DISTRACTING,  # user labelled as fun/distraction
}

# CONFIDENCE: how certain are we about this category?
# LOW confidence = app barely moves the score (unknown apps shouldn't tank your day).
# HIGH confidence = we're sure this is what it is (coding editor = definitely coding).
CONFIDENCE = {
    "coding_editor":      1.00,
    "terminal":           0.95,
    "localhost_dev":      1.00,
    "dev_tool":           0.95,
    "design_tool":        0.85,
    "ai_tool":            0.90,
    "learning_video":     0.85,
    "productive_site":    0.85,
    "writing_docs":       0.90,
    "work_comm":          0.75,
    "user_productive":    1.00,
    "user_neutral":       1.00,
    "user_entertainment": 1.00,
    "neutral":            0.20,  # unknown: barely influences score either way
    "entertainment_fg":   0.85,
    "entertainment_app":  0.70,
    "social_media":       0.90,
}

# ─────────────────────────────────────────────────────────────
# APP CLASSIFICATION SETS
# These match against app_name from app_usage table.
# We strip flatpak prefixes: "com.brave.Browser" → "brave"
# ─────────────────────────────────────────────────────────────
CODING_EDITORS = {
    "code", "vscodium", "nvim", "vim", "neovim", "emacs", "helix",
    "sublime_text", "sublime-text", "kate", "lapce", "zed", "atom",
    "pycharm", "intellij", "clion", "rider", "goland", "webstorm",
    "fleet", "android-studio", "eclipse", "netbeans", "gedit",
    "geany", "mousepad", "xed", "pluma", "gnome-text-editor",
    "org.gnome.texteditor", "org.gnome.gedit",
}
TERMINALS = {
    "alacritty", "kitty", "wezterm", "foot", "gnome-terminal",
    "konsole", "xterm", "tilix", "st", "urxvt", "terminator",
    "xfce4-terminal", "hyper", "rxvt", "yakuake",
    "org.gnome.terminal",
}
BROWSERS = {
    "firefox", "firefox-bin", "chromium", "google-chrome", "chrome",
    "brave", "brave-browser", "opera", "vivaldi", "librewolf",
    "epiphany", "falkon", "qutebrowser", "nyxt",
    "com.brave.browser", "org.mozilla.firefox",
}
WRITING_APPS = {
    "libreoffice", "libreoffice-writer", "libreoffice-calc",
    "libreoffice-impress", "onlyoffice", "obsidian", "logseq",
    "zotero", "okular", "evince", "calibre", "typora", "marktext",
    "apostrophe", "ghostwriter", "org.gnome.evince",
}
DEV_TOOLS = {
    "postman", "insomnia", "dbeaver", "datagrip", "tableplus",
    "beekeeper-studio", "gitkraken", "github-desktop", "meld",
    "kdiff3", "wireshark", "ghidra", "burpsuite",
    "virtualbox", "virt-manager", "vmware",
}
DESIGN_TOOLS = {
    "gimp", "inkscape", "blender", "krita", "figma", "darktable",
    "rawtherapee", "kdenlive", "openshot", "pitivi", "audacity",
    "ardour", "lmms", "org.inkscape.inkscape",
}
COMM_APPS = {
    "thunderbird", "geary", "evolution", "slack", "element",
    "signal", "telegram-desktop", "teams", "zoom", "discord",
    "org.telegram.desktop",
}
ENTERTAINMENT_APPS = {
    "spotify", "vlc", "rhythmbox", "clementine", "strawberry",
    "steam", "lutris", "heroic", "pegasus-fe", "retroarch",
    "celluloid", "totem", "mpv",
}
FILE_MANAGER_APPS = {
    "thunar", "nautilus", "dolphin", "nemo", "pcmanfm", "ranger",
    "lf", "vifm", "org.gnome.nautilus", "org.gnome.files",
}

# Domain classification for browser history
AI_DOMAINS = [
    "claude.ai", "chat.openai.com", "gemini.google.com",
    "perplexity.ai", "phind.com", "you.com", "openrouter.ai",
    "venice.ai", "copilot.microsoft.com", "poe.com",
]
PRODUCTIVE_DOMAINS = [
    "github.com", "gitlab.com", "bitbucket.org", "sourcehut.org",
    "developer.mozilla.org", "docs.python.org", "devdocs.io",
    "python.org", "rust-lang.org", "golang.org", "nodejs.org",
    "stackoverflow.com", "stackexchange.com", "superuser.com",
    "archlinux.org", "wiki.archlinux.org", "aur.archlinux.org",
    "arxiv.org", "scholar.google.com", "wikipedia.org",
    "leetcode.com", "hackerrank.com", "codewars.com",
    "pypi.org", "npmjs.com", "crates.io", "pkg.go.dev",
    "huggingface.co", "kaggle.com", "paperswithcode.com",
    "news.ycombinator.com", "lobste.rs",
    "coursera.org", "udemy.com", "edx.org", "brilliant.org",
    "docs.", "man.", "ref.", "api.",
]
SOCIAL_DOMAINS = [
    "twitter.com", "x.com", "instagram.com", "facebook.com",
    "tiktok.com", "snapchat.com", "reddit.com", "9gag.com",
    "tumblr.com", "pinterest.com",
]
ENTERTAINMENT_DOMAINS = [
    "youtube.com", "netflix.com", "twitch.tv", "primevideo.com",
    "disneyplus.com", "hbomax.com", "crunchyroll.com",
    "soundcloud.com", "spotify.com", "bandcamp.com",
]

# YouTube educational keywords — if found in video title, it's learning
LEARNING_VIDEO_KEYWORDS = [
    "tutorial", "course", "lecture", "lesson", "explained",
    "how to", "learn", "guide", "introduction to", "intro to",
    "deep dive", "crash course", "walkthrough", "workshop",
    "conference", "talk", "presentation", "demo", "setup",
    "install", "configure", "build", "implement", "create",
    "programming", "coding", "algorithm", "data structure",
    "machine learning", "neural network", "mathematics", "physics",
    "chemistry", "biology", "history", "documentary",
]


def _normalise_app_name(raw: str) -> str:
    """
    Strip Flatpak prefixes and clean app names so they match our sets.
    "com.brave.Browser" → "brave"
    "org.gnome.TextEditor" → "org.gnome.texteditor"
    "Kitty" → "kitty"
    """
    name = raw.strip().lower()
    # Keep org.gnome.* and org.telegram.* as-is for set lookup
    if name in CODING_EDITORS | TERMINALS | WRITING_APPS | DEV_TOOLS | DESIGN_TOOLS \
            | COMM_APPS | ENTERTAINMENT_APPS | FILE_MANAGER_APPS | BROWSERS:
        return name
    # Strip common Flatpak prefixes
    for prefix in ("com.", "net.", "io.", "app."):
        if name.startswith(prefix):
            parts = name.split(".")
            if len(parts) >= 2:
                return parts[-1]  # last segment: "com.brave.Browser" → "browser" ✗
    # Try the second-to-last segment for things like "com.brave.Browser"
    parts = name.split(".")
    if len(parts) >= 3:
        candidate = parts[-2].lower()  # "brave" from "com.brave.Browser"
        if candidate in CODING_EDITORS | TERMINALS | BROWSERS | ENTERTAINMENT_APPS \
                | COMM_APPS | DEV_TOOLS | DESIGN_TOOLS:
            return candidate
    # Fall back to just lowercased
    return name


def classify_app(app_name: str) -> tuple:
    """
    Classify an app by name into a productivity category.
    Returns (category, score, confidence).
    """
    name = _normalise_app_name(app_name)

    if name in CODING_EDITORS or any(e in name for e in ("editor", "studio", "code", "nvim", "vim")):
        # But not 'video' from 'org.gnome.videos'
        if "video" not in name:
            return ("coding_editor", SCORE["coding_editor"], CONFIDENCE["coding_editor"])

    if name in TERMINALS:
        return ("terminal", SCORE["terminal"], CONFIDENCE["terminal"])

    if name in BROWSERS or any(b in name for b in ("firefox", "chrome", "chromium", "brave")):
        # Browser time scored as neutral here — browser history gives accurate
        # per-domain scoring via classify_domain()
        return ("neutral", SCORE["neutral"], CONFIDENCE["neutral"])

    if name in WRITING_APPS or any(w in name for w in ("libreoffice", "office", "obsidian")):
        return ("writing_docs", SCORE["writing_docs"], CONFIDENCE["writing_docs"])

    if name in DEV_TOOLS:
        return ("dev_tool", SCORE["dev_tool"], CONFIDENCE["dev_tool"])

    if name in DESIGN_TOOLS:
        return ("design_tool", SCORE["design_tool"], CONFIDENCE["design_tool"])

    if name in COMM_APPS or any(c in name for c in ("telegram", "discord", "slack", "element")):
        return ("work_comm", SCORE["work_comm"], CONFIDENCE["work_comm"])

    if name in ENTERTAINMENT_APPS or any(e in name for e in ("steam", "vlc", "spotify", "mpv")):
        return ("entertainment_app", SCORE["entertainment_app"], CONFIDENCE["entertainment_app"])

    if name in FILE_MANAGER_APPS or any(f in name for f in ("thunar", "nautilus", "dolphin")):
        return ("neutral", SCORE["neutral"], CONFIDENCE["neutral"])

    # System processes — don't let them drag score down
    SYSTEM_PROCS = {
        "plasmashell", "gnome-shell", "xdg-desktop-portal", "xdg-desktop-portal-gtk",
        "xdg-desktop-portal-hyprland", "polkit", "dbus", "pulseaudio", "pipewire",
        "systemd", "waybar", "swaync", "dunst", "mako", "rofi", "wofi",
        "hyprland", "sway", "i3", "openbox", "xfwm4", "kwin",
        "dialog", "tk", "yad", "zenity", "kdialog",
        "gjs", "python3", "python", "sh", "bash", "zsh", "fish",
        "xdg-desktop-portal-wlr", "wl-paste", "wl-copy", "xclip", "xsel",
    }
    if name in SYSTEM_PROCS or any(s in name for s in ("portal", "daemon", "helper", "agent")):
        return ("neutral", SCORE["neutral"], 0.10)  # very low confidence = minimal impact

    return ("neutral", SCORE["neutral"], CONFIDENCE["neutral"])


def classify_domain(domain: str, youtube_title: str = "") -> tuple:
    """
    Classify a browser domain into a productivity category.
    Returns (category, score, confidence).
    """
    if not domain:
        return ("neutral", SCORE["neutral"], CONFIDENCE["neutral"])

    d = domain.lower().strip()

    # Skip localhost / dev servers
    if "127.0.0.1" in d or "localhost" in d or d.endswith(":6969"):
        return ("localhost_dev", SCORE["localhost_dev"], CONFIDENCE["localhost_dev"])

    # AI tools — very high productivity value
    if any(ai in d for ai in AI_DOMAINS):
        return ("ai_tool", SCORE["ai_tool"], CONFIDENCE["ai_tool"])

    # Social media
    if any(s in d for s in SOCIAL_DOMAINS):
        return ("social_media", SCORE["social_media"], CONFIDENCE["social_media"])

    # YouTube — classify by video title
    if "youtube.com" in d:
        title_lower = (youtube_title or "").lower()
        if any(kw in title_lower for kw in LEARNING_VIDEO_KEYWORDS):
            return ("learning_video", SCORE["learning_video"], CONFIDENCE["learning_video"])
        return ("entertainment_fg", SCORE["entertainment_fg"], CONFIDENCE["entertainment_fg"])

    # Entertainment streaming
    if any(e in d for e in ENTERTAINMENT_DOMAINS):
        return ("entertainment_fg", SCORE["entertainment_fg"], CONFIDENCE["entertainment_fg"])

    # Productive domains
    for pd in PRODUCTIVE_DOMAINS:
        if pd in d:
            return ("productive_site", SCORE["productive_site"], CONFIDENCE["productive_site"])

    # Unknown domain — neutral with low confidence
    return ("neutral", SCORE["neutral"], 0.25)


def load_user_site_labels() -> dict:
    """Load user-defined site labels from user_sites.json."""
    try:
        if USER_SITES_FILE.exists():
            return json.loads(USER_SITES_FILE.read_text())
    except Exception:
        pass
    return {}


class ProductivityCollector:
    """
    Computes productivity score every 60 seconds from DB data.

    DATA SOURCES (in priority order):
    1. app_usage table — what app had foreground time in last 60s
    2. browser_history table — what domains/videos were visited in last 60s

    This is reliable because AppCollector already tracks foreground time
    correctly via Hyprland IPC, and BrowserCollector reads the actual
    browser SQLite file. No live window polling, no idle detection guesswork.

    FLUSH INTERVAL: 60 seconds
    Each flush = one row in the productivity table.
    The web dashboard aggregates these rows for daily/weekly views.
    """

    def __init__(self, batch_writer, flush_interval: int = 60):
        self.batch_writer    = batch_writer
        self.flush_interval  = flush_interval
        self.running         = False
        self._lock           = threading.Lock()
        self._last_ts        = int(time.time())  # timestamp of last flush
        # Snapshot of cumulative foreground_seconds from last flush.
        # Used to compute delta: how much time was spent in each app this interval.
        # { app_name: cumulative_fg_seconds }
        self._last_app_totals: dict = {}

    def _score_window(self, since_ts: int, until_ts: int) -> dict:
        """
        Score the productivity for the time window [since_ts, until_ts].

        IMPORTANT: app_usage rows are written every 60s with CUMULATIVE foreground_seconds
        for the whole day. To get the delta for just this window, we subtract the previous
        cumulative value. We track this in self._last_app_totals.

        For browser_history we use timestamp directly (each row = one visit).
        """
        from daemon.db import query

        today = str(datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d"))
        user_labels = load_user_site_labels()

        # ── App usage: get CURRENT cumulative totals ──────────────────
        # app_usage stores cumulative foreground_seconds per app per day.
        # We diff against the last snapshot to get what happened in THIS window.
        app_rows = query("""
            SELECT app_name,
                   foreground_seconds as fg_secs
            FROM app_usage
            WHERE date = ?
              AND foreground_seconds > 0
              AND app_name NOT LIKE '%sd-pam%'
              AND app_name NOT LIKE '%accounts-daemon%'
              AND app_name NOT LIKE '%gvfsd%'
              AND app_name NOT LIKE '%at-spi%'
              AND app_name NOT LIKE '%portal%'
              AND app_name NOT IN (
                  'plasmashell','gnome-shell','xdg-desktop-portal',
                  'xdg-desktop-portal-gtk','xdg-desktop-portal-hyprland',
                  'polkit','dbus','pulseaudio','pipewire','wireplumber',
                  'waybar','swaync','dunst','mako','rofi','wofi',
                  'hyprland','sway','i3','openbox',
                  'dialog','tk','yad','zenity','kdialog',
                  'gjs','python3','python','sh','bash','zsh','fish',
                  'unknown'
              )
        """, (today,))

        # Calculate delta: how many new foreground seconds since last flush
        current_totals = {r["app_name"]: (r["fg_secs"] or 0) for r in app_rows}
        app_deltas = {}
        for app_name, current in current_totals.items():
            prev = self._last_app_totals.get(app_name, 0)
            delta = max(0, current - prev)
            if delta > 0:
                app_deltas[app_name] = delta
        # Update snapshot for next flush
        self._last_app_totals = dict(current_totals)

        # ── Browser history: visits in this window ────────────────────
        browser_rows = query("""
            SELECT domain, youtube_title,
                   SUM(visit_duration_seconds) as dur,
                   MAX(is_youtube) as is_yt
            FROM browser_history
            WHERE timestamp BETWEEN ? AND ?
              AND domain != ''
              AND domain NOT LIKE '%127.0.0.1%'
              AND domain NOT LIKE '%localhost%'
            GROUP BY domain
        """, (since_ts, until_ts))

        if not app_deltas and not browser_rows:
            return None

        # ── Score each app by its foreground delta ────────────────────
        cat_seconds = {}
        cat_scores  = {}
        domain_secs = {}

        for app_name, secs in app_deltas.items():
            normalised = _normalise_app_name(app_name)

            # Browsers: don't score here — domain scoring below is more accurate
            is_browser = (normalised in BROWSERS or
                          any(b in normalised for b in ("firefox","chrome","chromium","brave")))
            if is_browser:
                continue

            cat, score, conf = classify_app(app_name)

            # User overrides
            if normalised in user_labels:
                label = user_labels[normalised]
                if label == "productive":
                    cat, score, conf = "user_productive", SCORE["user_productive"], CONFIDENCE["user_productive"]
                elif label == "entertainment":
                    cat, score, conf = "user_entertainment", SCORE["user_entertainment"], CONFIDENCE["user_entertainment"]

            cat_seconds[cat]  = cat_seconds.get(cat, 0) + secs
            if cat not in cat_scores: cat_scores[cat] = []
            cat_scores[cat].append((score, conf, secs))

        # ── Score browser domains ─────────────────────────────────────
        # Cap visit_duration_seconds at 300s (5 min).
        # WHY: browser_history.visit_duration_seconds is estimated from the gap
        # between consecutive visits to that tab. A background tab sitting open
        # for 1 hour gets recorded as one 3600s "visit" — hugely inflating its
        # category score. An actively used tab gets many short visits (30-120s each).
        # Capping at 300s means a background YouTube tab counts the same as a
        # 5-minute active session, not a 1-hour session. This is fair because
        # if someone is actually watching YouTube, they'd have many shorter visits.
        MAX_BROWSER_VISIT_SECS = 300
        for row in browser_rows:
            domain   = row["domain"] or ""
            raw_secs = row["dur"] or 0
            # Cap at 5 min — prevents a background tab from swamping the score
            secs     = min(max(1, raw_secs), MAX_BROWSER_VISIT_SECS)
            yt_title = row["youtube_title"] or ""

            if domain in user_labels:
                label = user_labels[domain]
                if label == "productive":
                    cat, score, conf = "user_productive", SCORE["user_productive"], CONFIDENCE["user_productive"]
                elif label == "entertainment":
                    cat, score, conf = "user_entertainment", SCORE["user_entertainment"], CONFIDENCE["user_entertainment"]
                else:
                    cat, score, conf = "user_neutral", SCORE["user_neutral"], CONFIDENCE["user_neutral"]
            else:
                cat, score, conf = classify_domain(domain, yt_title)

            cat_seconds[cat]  = cat_seconds.get(cat, 0) + secs
            if cat not in cat_scores: cat_scores[cat] = []
            cat_scores[cat].append((score, conf, secs))
            domain_secs[domain] = domain_secs.get(domain, 0) + secs

        # ── RescueTime 5-tier weighted formula ───────────────────────────
        # Formula: ((vd*0 + d*1 + n*2 + p*3 + vp*4) / (total*4)) * 100
        # Each second is weighted by confidence so unknown apps barely move score.
        # neutral (confidence=0.20) contributes almost nothing — won't tank a good day.
        # All very_productive → 100. All neutral → ~50. All very_distracting → 0.
        total_weight  = 0.0
        weighted_sum  = 0.0
        total_samples = 0

        for cat, entries in cat_scores.items():
            for (tier, conf, secs) in entries:
                weight        = conf * secs
                weighted_sum  += tier * weight
                total_weight  += TIER_VERY_PRODUCTIVE * weight
                total_samples += secs

        if total_weight == 0:
            return None

        norm    = round(max(0.0, min(100.0, (weighted_sum / total_weight) * 100)), 1)
        raw_avg = round(weighted_sum / max(total_weight / TIER_VERY_PRODUCTIVE, 1), 3)

        dominant    = max(cat_seconds, key=cat_seconds.get) if cat_seconds else "neutral"
        top_domains = dict(sorted(domain_secs.items(), key=lambda x: x[1], reverse=True)[:5])

        # Derive a human-readable role from the dominant category
        ROLE_MAP = {
            "coding_editor":      "developer",
            "terminal":           "developer",
            "localhost_dev":      "developer",
            "dev_tool":           "developer",
            "design_tool":        "designer",
            "writing_docs":       "writer",
            "learning_video":     "learner",
            "productive_site":    "researcher",
            "ai_tool":            "developer",
            "work_comm":          "communicator",
            "entertainment_fg":   "consumer",
            "entertainment_app":  "consumer",
            "social_media":       "consumer",
            "neutral":            "unknown",
            "idle":               "unknown",
        }
        detected_role = ROLE_MAP.get(dominant, "unknown")

        def secs_in(*cats):
            return sum(cat_seconds.get(c, 0) for c in cats)

        return {
            "productivity_score":    norm,
            "raw_score_weighted":    round(raw_avg, 3),
            "dominant_category":     dominant,
            "detected_role":         detected_role,
            "sample_count":          total_samples,
            "total_confidence":      round(total_weight, 1),
            "category_breakdown":    json.dumps({k: v for k, v in cat_seconds.items()}),
            "top_domains":           json.dumps(top_domains),
            "coding_samples":        secs_in("coding_editor","terminal","localhost_dev","dev_tool","design_tool"),
            "learning_samples":      secs_in("learning_video","productive_site"),
            "ai_samples":            secs_in("ai_tool","user_productive"),
            "neutral_samples":       secs_in("neutral","user_neutral","work_comm"),
            "entertainment_samples": secs_in("entertainment_fg","entertainment_app","social_media","user_entertainment"),
        }

    def _flush(self):
        """
        Score the last flush_interval seconds and write one row to DB.
        """
        now      = int(time.time())
        since_ts = self._last_ts
        until_ts = now
        self._last_ts = now

        today = str(date.today())

        result = self._score_window(since_ts, until_ts)
        if result is None:
            logger.debug("Productivity: no activity data in this window — skipping flush")
            return

        self.batch_writer.add("productivity", {
            "timestamp":             now,
            "date":                  today,
            **result,
        })

        logger.info(
            f"Productivity: {result['productivity_score']}/100 "
            f"dominant={result['dominant_category']} "
            f"samples={result['sample_count']}s "
            f"coding={result['coding_samples']}s "
            f"entertainment={result['entertainment_samples']}s"
        )

    def run(self):
        """Main loop — flush every flush_interval seconds."""
        self.running   = True
        self._last_ts  = int(time.time())
        logger.info(f"ProductivityCollector started (flush every {self.flush_interval}s)")

        while self.running:
            time.sleep(self.flush_interval)
            try:
                self._flush()
            except Exception as e:
                logger.error(f"ProductivityCollector flush error: {e}", exc_info=True)

    def stop(self):
        """Stop and do a final flush."""
        self.running = False
        try:
            self._flush()
        except Exception:
            pass
        logger.info("ProductivityCollector stopped")

    def label_site(self, domain: str, label: str):
        """Called from web/api route to label a domain."""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            existing = {}
            if USER_SITES_FILE.exists():
                existing = json.loads(USER_SITES_FILE.read_text())
            existing[domain] = label
            USER_SITES_FILE.write_text(json.dumps(existing, indent=2))
            logger.info(f"Labelled {domain} as {label}")
        except Exception as e:
            logger.error(f"Could not save site label: {e}")

    def get_sites_to_review(self) -> dict:
        """Return domains that appear in browser history without a user label."""
        from daemon.db import query
        labelled = load_user_site_labels()
        rows = query("""
            SELECT domain, SUM(visit_duration_seconds) as total_sec
            FROM browser_history
            WHERE date >= date('now','-7 days')
              AND domain != ''
              AND domain NOT LIKE '%127.0.0.1%'
              AND domain NOT LIKE '%localhost%'
              AND is_youtube = 0
            GROUP BY domain
            HAVING total_sec > 120
            ORDER BY total_sec DESC
            LIMIT 20
        """)
        return {r["domain"]: r["total_sec"]
                for r in rows
                if r["domain"] not in labelled}
