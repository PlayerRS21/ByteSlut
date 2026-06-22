"""
daemon/analyzer.py — Day Role Detection
=========================================
Detects what kind of day the user had based on actual usage data.

ROLE PHILOSOPHY:
  28 roles, each with a precise description.
  Roles are scored by rules — each rule adds points.
  The highest-scoring role wins. Runner-ups are shown too.

  Rules use features computed from:
    1. app_usage.foreground_seconds  ← GROUND TRUTH (Hyprland IPC)
    2. browser_history (domain visits, YouTube)
    3. commands table (terminal command count)
    4. notifications, packages, sessions

  IMPORTANT: we do NOT use the productivity table as primary signal
  because it may have stale/old rows from before bug fixes.
  We read directly from app_usage + browser_history.
"""

import json
import logging
from datetime import date

logger = logging.getLogger(__name__)

# ── App category sets (match against app_name from app_usage) ──────────────
TERMINAL_APPS   = {"kitty","alacritty","wezterm","foot","gnome-terminal","konsole",
                   "xterm","urxvt","terminator","tilix","st","hyper","rio"}
CODING_APPS     = {"nvim","neovim","vim","emacs","vscode","code","code-oss",
                   "codium","vscodium","jetbrains","idea","pycharm","clion",
                   "goland","webstorm","phpstorm","rubymine","datagrip",
                   "sublime_text","sublime","atom","lite-xl","helix","hx",
                   "zed","lapce","cursor"}
BROWSER_APPS    = {"brave","firefox","chromium","chrome","google-chrome","librewolf",
                   "opera","vivaldi","falkon","epiphany","qutebrowser","com.brave.browser"}
COMM_APPS       = {"telegram","telegram-desktop","discord","slack","element","signal",
                   "whatsapp","teams","zoom","skype","viber","weechat","irssi",
                   "mattermost","rocketchat"}
MEDIA_APPS      = {"mpv","vlc","mplayer","celluloid","totem","rhythmbox","clementine",
                   "strawberry","lollypop","audacious","spotify","netflix"}
DESIGN_APPS     = {"figma","gimp","inkscape","krita","blender","kdenlive","davinci",
                   "darktable","rawtherapee","photoshop","illustrator","canva"}
FILE_APPS       = {"nautilus","dolphin","thunar","pcmanfm","nemo","ranger","lf","yazi"}
GAME_APPS       = {"steam","lutris","heroic","minigalaxy","gamemode","wine","proton",
                   "minecraft","retroarch","dosbox","ppsspp","yuzu","ryujinx"}
DOCS_APPS       = {"obsidian","libreoffice","onlyoffice","notion","logseq","zettlr",
                   "typora","marktext","ghostwriter","writer","impress","calc"}
READER_APPS     = {"zathura","evince","okular","foliate","calibre","mupdf"}
OFFICE_APPS     = {"thunderbird","evolution","geary","mutt","aerc"}

# ── Productive/educational browser domains ─────────────────────────────────
PRODUCTIVE_DOMAINS = {
    "github.com","gitlab.com","stackoverflow.com","stackexchange.com",
    "developer.mozilla.org","docs.python.org","docs.rs","pkg.go.dev",
    "learn.microsoft.com","docs.docker.com","kubernetes.io","terraform.io",
    "archlinux.org","wiki.archlinux.org","man7.org",
    "arxiv.org","scholar.google.com","researchgate.net","semanticscholar.org",
    "pypi.org","npmjs.com","crates.io","rubygems.org","pkg.go.dev",
    "coursera.org","udemy.com","edx.org","mit.edu","khanacademy.org",
    "freecodecamp.org","theodinproject.com","roadmap.sh",
    "leetcode.com","hackerrank.com","codeforces.com","codewars.com",
    "replit.com","codesandbox.io","codepen.io",
    "linear.app","jira.atlassian.com","notion.so","confluence",
    "anthropic.com","openai.com","huggingface.co",
}
SOCIAL_DOMAINS = {
    "instagram.com","facebook.com","twitter.com","x.com","tiktok.com",
    "reddit.com","snapchat.com","linkedin.com","pinterest.com",
    "tumblr.com","threads.net","mastodon","lemmy",
}
ENTERTAINMENT_DOMAINS = {
    "youtube.com","netflix.com","twitch.tv","primevideo.com","disneyplus.com",
    "hulu.com","crunchyroll.com","funimation.com","spotify.com",
    "soundcloud.com","bandcamp.com","bilibili.com","niconico",
}


# ── Feature extraction ──────────────────────────────────────────────────────

def compute_day_features(target_date: str) -> dict:
    """
    Extract all measurable features for a given day.
    Returns a flat dict of scalar values used by role scoring.

    Data sources (in reliability order):
      1. app_usage.foreground_seconds — Hyprland IPC, ground truth
      2. browser_history — actual tab visits
      3. commands — terminal history
      4. sessions — login/lock events
      5. productivity table — secondary, used only if not mostly idle
    """
    from daemon.db import query
    from daemon.collectors.productivity import (
        classify_app, classify_domain, _normalise_app_name, BROWSERS,
        TIER_VERY_PRODUCTIVE, TIER_PRODUCTIVE, TIER_NEUTRAL,
        TIER_DISTRACTING, TIER_VERY_DISTRACTING,
    )

    # ── App usage: foreground seconds per app ──────────────────────────────
    app_rows = query("""
        SELECT app_name, SUM(foreground_seconds) as total_fg
        FROM app_usage
        WHERE date = ? AND foreground_seconds > 0
          AND app_name NOT IN (
              'unknown','plasmashell','gnome-shell','sd-pam',
              'xdg-desktop-portal-gtk','xdg-desktop-portal-hyprland',
              'polkit','dbus','pulseaudio','pipewire','wireplumber',
              'waybar','swaync','dunst','mako','rofi','wofi',
              'hyprland','dialog','sh','bash','zsh','fish')
        GROUP BY app_name ORDER BY total_fg DESC
    """, (target_date,))

    # Classify each app and accumulate seconds per category
    cat_secs   = {}     # category → seconds
    total_fg   = 0
    top_app    = ""
    top_app_s  = 0
    all_apps   = set()

    for row in app_rows:
        app  = row["app_name"] or ""
        secs = row["total_fg"] or 0
        norm = _normalise_app_name(app)
        total_fg += secs
        all_apps.add(norm)
        if secs > top_app_s:
            top_app_s = secs
            top_app   = norm

        is_browser = norm in BROWSERS or any(
            b in norm for b in ("firefox","chrome","chromium","brave"))
        if is_browser:
            cat_secs["browser_fg"] = cat_secs.get("browser_fg", 0) + secs
            continue

        cat, tier, conf = classify_app(app)
        cat_secs[cat] = cat_secs.get(cat, 0) + secs

    # Convenience totals (seconds in each work category)
    coding_secs  = sum(cat_secs.get(c, 0) for c in
                       ("coding_editor","terminal","localhost_dev","dev_tool"))
    design_secs  = cat_secs.get("design_tool", 0)
    learn_secs   = sum(cat_secs.get(c, 0) for c in ("learning_video","productive_site"))
    ai_secs      = cat_secs.get("ai_tool", 0)
    write_secs   = cat_secs.get("writing_docs", 0)
    comm_secs    = cat_secs.get("work_comm", 0)
    entert_secs  = sum(cat_secs.get(c, 0) for c in
                       ("entertainment_fg","entertainment_app","social_media",
                        "user_entertainment"))
    total_s      = max(total_fg, 1)

    def apct(s): return (s / total_s) * 100

    # ── Browser domain breakdown ────────────────────────────────────────────
    domain_rows = query("""
        SELECT domain,
               SUM(CASE WHEN is_youtube=1 THEN 1 ELSE 0 END) as yt_visits,
               COUNT(*) as visits,
               MIN(300, SUM(MIN(visit_duration_seconds,300))) as dur_capped
        FROM browser_history
        WHERE date = ? AND domain != ''
          AND domain NOT LIKE '%127.0.0.1%' AND domain NOT LIKE '%localhost%'
        GROUP BY domain ORDER BY dur_capped DESC
    """, (target_date,))

    top_domain      = ""
    youtube_visits  = 0
    social_secs     = 0
    entert_br_secs  = 0
    prod_br_secs    = 0
    comm_br_secs    = 0
    total_br_dur    = 0

    for row in domain_rows:
        dom  = (row["domain"] or "").lower()
        secs = row["dur_capped"] or 0
        total_br_dur += secs
        youtube_visits += row["yt_visits"] or 0
        if not top_domain:
            top_domain = dom

        if any(d in dom for d in SOCIAL_DOMAINS):
            social_secs += secs
        elif any(d in dom for d in ENTERTAINMENT_DOMAINS):
            entert_br_secs += secs
        elif any(d in dom for d in PRODUCTIVE_DOMAINS):
            prod_br_secs += secs
        elif any(d in dom for d in {"slack.com","discord.com","teams.microsoft",
                                     "telegram.org","mail.google","outlook"}):
            comm_br_secs += secs

    # Cap youtube visits for role scoring (716 visits shouldn't swamp roles)
    youtube_visits_capped = min(youtube_visits, 200)

    # ── Sessions ──────────────────────────────────────────────────────────
    sess = query("""
        SELECT COUNT(*) as cnt,
               SUM(duration_seconds) as total_secs,
               MAX(duration_seconds) as longest_secs,
               AVG(duration_seconds) as avg_secs
        FROM sessions WHERE date(start_time,'unixepoch')=? AND duration_seconds>60
    """, (target_date,), fetch="one")
    screen_h    = ((sess["total_secs"] or 0) / 3600) if sess else 0
    longest_h   = ((sess["longest_secs"] or 0) / 3600) if sess else 0
    avg_sess_m  = ((sess["avg_secs"] or 0) / 60) if sess else 0
    sess_count  = (sess["cnt"] or 0) if sess else 0

    # ── Evening usage ──────────────────────────────────────────────────────
    eve = query("""
        SELECT COUNT(*) as cnt FROM sessions
        WHERE date(start_time,'unixepoch')=?
          AND CAST(strftime('%H',datetime(start_time,'unixepoch','localtime')) AS INT) >= 19
    """, (target_date,), fetch="one")
    evening_heavy = ((eve["cnt"] or 0) >= 2) if eve else False

    # ── Commands ──────────────────────────────────────────────────────────
    cmds = query("SELECT COUNT(*) as cnt FROM commands WHERE date=?",
                 (target_date,), fetch="one")
    commands = (cmds["cnt"] or 0) if cmds else 0

    # Detect git/docker/package manager commands
    git_cmds = query("""
        SELECT COUNT(*) as cnt FROM commands
        WHERE date=? AND (command LIKE 'git %' OR command LIKE '%git %')
    """, (target_date,), fetch="one")
    git_commands = (git_cmds["cnt"] or 0) if git_cmds else 0

    pkg_cmds = query("""
        SELECT COUNT(*) as cnt FROM commands
        WHERE date=? AND (command LIKE 'pacman%' OR command LIKE 'yay%'
              OR command LIKE 'paru%' OR command LIKE 'pip %'
              OR command LIKE 'npm %' OR command LIKE 'cargo %')
    """, (target_date,), fetch="one")
    pkg_commands = (pkg_cmds["cnt"] or 0) if pkg_cmds else 0

    # ── Packages changed ──────────────────────────────────────────────────
    pkgs = query("SELECT COUNT(*) as cnt FROM packages WHERE date=?",
                 (target_date,), fetch="one")
    packages = (pkgs["cnt"] or 0) if pkgs else 0

    # ── Notifications ─────────────────────────────────────────────────────
    notifs = query("SELECT COUNT(*) as cnt FROM notifications WHERE date=?",
                   (target_date,), fetch="one")
    notifications = (notifs["cnt"] or 0) if notifs else 0

    # ── Input stats ────────────────────────────────────────────────────────
    inp = query("""
        SELECT SUM(keystrokes) as ks, AVG(wpm_sample) as wpm
        FROM input_stats WHERE date=?
    """, (target_date,), fetch="one")
    keystrokes = (inp["ks"] or 0) if inp else 0
    avg_wpm    = (inp["wpm"] or 0) if inp else 0

    def any_app(*names):
        return any(n in all_apps or any(n in a for a in all_apps) for n in names)

    # ── Background app usage (music, media playing while doing other things) ─────
    bg_rows = query("""
        SELECT app_name, SUM(background_seconds) as bg_secs
        FROM app_usage
        WHERE date = ? AND background_seconds > 300
        GROUP BY app_name ORDER BY bg_secs DESC
    """, (target_date,))

    music_bg_secs = 0
    video_bg_secs = 0
    MUSIC_APPS = {"spotify","rhythmbox","clementine","strawberry","lollypop",
                  "audacious","cantata","quodlibet","cmus","moc"}
    VIDEO_APPS = {"mpv","vlc","mplayer","celluloid","totem","smplayer"}

    for row in bg_rows:
        norm = _normalise_app_name(row["app_name"] or "")
        secs = row["bg_secs"] or 0
        if norm in MUSIC_APPS or "spotify" in norm or "music" in norm:
            music_bg_secs += secs
        elif norm in VIDEO_APPS or "video" in norm or "player" in norm:
            video_bg_secs += secs

    return {
        # Raw seconds
        "coding_secs":       coding_secs,
        "design_secs":       design_secs,
        "learn_secs":        learn_secs,
        "ai_secs":           ai_secs,
        "write_secs":        write_secs,
        "comm_secs":         comm_secs,
        "entert_secs":       entert_secs + entert_br_secs,
        "social_secs":       social_secs,
        "browser_prod_secs": prod_br_secs,
        "total_fg_secs":     total_fg,
        # Percentages
        "coding_pct":        apct(coding_secs),
        "design_pct":        apct(design_secs),
        "learn_pct":         apct(learn_secs + prod_br_secs),
        "ai_pct":            apct(ai_secs),
        "write_pct":         apct(write_secs),
        "comm_pct":          apct(comm_secs + comm_br_secs),
        "entert_pct":        apct(entert_secs + entert_br_secs + social_secs),
        "social_pct":        apct(social_secs),
        # Session stats
        "screen_h":          screen_h,
        "longest_h":         longest_h,
        "avg_sess_m":        avg_sess_m,
        "sess_count":        sess_count,
        "evening_heavy":     evening_heavy,
        # Activity counts
        "commands":          commands,
        "git_commands":      git_commands,
        "pkg_commands":      pkg_commands,
        "packages":          packages,
        "notifications":     notifications,
        "keystrokes":        keystrokes,
        "avg_wpm":           avg_wpm,
        "youtube_visits":    youtube_visits_capped,
        # App flags
        "top_app":           top_app,
        "top_domain":        top_domain,
        "has_terminal":      any_app(*TERMINAL_APPS) or "terminal" in cat_secs,
        "has_editor":        any_app(*CODING_APPS),
        "has_browser":       cat_secs.get("browser_fg", 0) > 0 or bool(domain_rows),
        "has_comm":          any_app(*COMM_APPS) or comm_secs > 0 or comm_br_secs > 0,
        "has_design":        any_app(*DESIGN_APPS) or design_secs > 0,
        "has_media":         any_app(*MEDIA_APPS),
        "has_game":          any_app(*GAME_APPS),
        "has_docs":          any_app(*DOCS_APPS) or write_secs > 0,
        "has_email":         any_app(*OFFICE_APPS),
        "top_is_terminal":   top_app in TERMINAL_APPS,
        "top_is_browser":    top_app in BROWSER_APPS or "brave" in top_app or "firefox" in top_app,
        # Background media
        "music_bg_secs":     music_bg_secs,
        "video_bg_secs":     video_bg_secs,
        "has_music_bg":      music_bg_secs > 1800,   # 30min+ music in background
        "has_video_bg":      video_bg_secs > 3600,   # 1h+ video in background
    }


# ── Role definitions ────────────────────────────────────────────────────────
# 28 roles. Each has:
#   name, emoji, color, description, rules[]
# Each rule: (feature, op, threshold, points)
# op: ">=" | "<=" | "==" | ">" | "<" | "!="
# Role with highest total points wins.

ROLES = [
    # ── Developer roles ──────────────────────────────────────────────
    {
        "name":        "Deep Focus Coder",
        "emoji":       "⚡",
        "color":       "c-blue",
        "description": "Spent the majority of the day writing code. Long uninterrupted sessions.",
        "rules": [
            ("coding_secs",    ">=", 7200, 8),   # 2h+ in editor/terminal
            ("coding_secs",    ">=", 14400, 6),  # 4h+ extra bonus
            ("coding_pct",     ">=", 50,    5),
            ("longest_h",      ">=", 2,     3),
            ("has_editor",     "==", True,  3),
            ("git_commands",   ">=", 10,    2),
        ],
    },
    {
        "name":        "Terminal Warrior",
        "emoji":       "$_",
        "color":       "c-green",
        "description": "Heavy terminal usage — scripting, sysadmin, DevOps, compiling.",
        "rules": [
            ("top_is_terminal","==", True,  7),
            ("commands",       ">=", 100,   5),
            ("commands",       ">=", 300,   4),
            ("coding_secs",    ">=", 1800,  3),
            ("has_terminal",   "==", True,  2),
            ("pkg_commands",   ">=", 5,     2),
        ],
    },
    {
        "name":        "Git Grinder",
        "emoji":       "⎇",
        "color":       "c-blue",
        "description": "Commit-heavy day — lots of version control activity.",
        "rules": [
            ("git_commands",   ">=", 20,    8),
            ("git_commands",   ">=", 50,    5),
            ("has_terminal",   "==", True,  3),
            ("coding_secs",    ">=", 3600,  3),
        ],
    },
    {
        "name":        "Debug Session",
        "emoji":       "🐛",
        "color":       "c-yellow",
        "description": "Mixed editor + terminal + browser — typical debugging flow.",
        "rules": [
            ("has_editor",     "==", True,  4),
            ("has_terminal",   "==", True,  4),
            ("browser_prod_secs",">=",1800, 3),  # stackoverflow etc
            ("coding_secs",    ">=", 3600,  3),
            ("commands",       ">=", 50,    2),
        ],
    },
    {
        "name":        "DevOps Day",
        "emoji":       "🔧",
        "color":       "c-green",
        "description": "Infrastructure, containers, deployment, package management.",
        "rules": [
            ("pkg_commands",   ">=", 10,    6),
            ("packages",       ">=", 5,     5),
            ("commands",       ">=", 200,   4),
            ("has_terminal",   "==", True,  3),
            ("git_commands",   ">=", 5,     2),
        ],
    },
    {
        "name":        "Architecture Day",
        "emoji":       "🏗",
        "color":       "c-purple",
        "description": "Planning and designing — editor + docs + research, less raw coding.",
        "rules": [
            ("has_editor",     "==", True,  4),
            ("has_docs",       "==", True,  4),
            ("browser_prod_secs",">=",3600, 4),
            ("coding_secs",    ">=", 1800,  2),
            ("write_secs",     ">=", 1800,  3),
        ],
    },

    # ── Research / Learning roles ─────────────────────────────────────
    {
        "name":        "Research Mode",
        "emoji":       "🔍",
        "color":       "c-blue",
        "description": "Deep browsing of docs, papers, GitHub — information gathering.",
        "rules": [
            ("browser_prod_secs",">=", 7200, 8),
            ("browser_prod_secs",">=", 3600, 5),
            ("learn_pct",      ">=", 40,    4),
            ("youtube_visits", "<=", 20,    2),  # not just YouTube
            ("top_domain",     "in", "github.com|stackoverflow.com|docs.", 3),
        ],
    },
    {
        "name":        "Study Session",
        "emoji":       "📚",
        "color":       "c-blue",
        "description": "Focused learning — courses, tutorials, educational content.",
        "rules": [
            ("learn_secs",     ">=", 5400,  8),  # 1.5h+ learning apps
            ("learn_pct",      ">=", 40,    5),
            ("browser_prod_secs",">=",1800, 3),
            ("ai_secs",        ">=", 1800,  3),  # AI as learning tool
            ("has_editor",     "==", True,  2),  # following along
        ],
    },
    {
        "name":        "AI-Assisted Work",
        "emoji":       "🤖",
        "color":       "c-purple",
        "description": "Heavy use of AI tools — Claude, ChatGPT, Copilot as main workflow.",
        "rules": [
            ("ai_secs",        ">=", 5400,  8),
            ("ai_pct",         ">=", 30,    6),
            ("ai_secs",        ">=", 1800,  4),
            ("has_editor",     "==", True,  2),
            ("coding_secs",    ">=", 1800,  2),
        ],
    },
    {
        "name":        "Reading Day",
        "emoji":       "📖",
        "color":       "c-muted",
        "description": "Lots of reading — docs, articles, PDFs, ebooks.",
        "rules": [
            ("browser_prod_secs",">=",7200, 6),
            ("learn_secs",     ">=", 3600,  4),
            ("coding_secs",    "<=", 1800,  2),
            ("has_docs",       "==", True,  3),
            ("commands",       "<=", 30,    2),
        ],
    },

    # ── Creative roles ────────────────────────────────────────────────
    {
        "name":        "Creative Sprint",
        "emoji":       "🎨",
        "color":       "c-purple",
        "description": "Design, illustration, video, or other creative work.",
        "rules": [
            ("has_design",     "==", True,  8),
            ("design_secs",    ">=", 7200,  6),
            ("design_pct",     ">=", 40,    5),
            ("design_secs",    ">=", 3600,  3),
        ],
    },
    {
        "name":        "Writing Day",
        "emoji":       "✍",
        "color":       "c-blue",
        "description": "Documentation, articles, notes — words not code.",
        "rules": [
            ("write_secs",     ">=", 7200,  8),
            ("write_pct",      ">=", 40,    6),
            ("has_docs",       "==", True,  4),
            ("write_secs",     ">=", 3600,  3),
            ("coding_secs",    "<=", 1800,  2),
        ],
    },

    # ── Communication roles ───────────────────────────────────────────
    {
        "name":        "Communicator",
        "emoji":       "💬",
        "color":       "c-yellow",
        "description": "Heavy messaging — Telegram, Discord, Slack, email.",
        "rules": [
            ("has_comm",       "==", True,  5),
            ("comm_secs",      ">=", 5400,  6),
            ("comm_pct",       ">=", 30,    5),
            ("notifications",  ">=", 50,    3),
            ("comm_secs",      ">=", 1800,  2),
        ],
    },
    {
        "name":        "Meeting Day",
        "emoji":       "📹",
        "color":       "c-yellow",
        "description": "Lots of video calls and communication tools.",
        "rules": [
            ("has_comm",       "==", True,  5),
            ("comm_pct",       ">=", 40,    6),
            ("notifications",  ">=", 100,   4),
            ("sess_count",     ">=", 4,     3),  # many short sessions = break for calls
        ],
    },

    # ── Mixed/Balanced roles ──────────────────────────────────────────
    {
        "name":        "Full Stack Day",
        "emoji":       "🔀",
        "color":       "c-green",
        "description": "Coding + research + communication — all three in good measure.",
        "rules": [
            ("coding_secs",    ">=", 3600,  4),
            ("browser_prod_secs",">=",1800, 4),
            ("has_comm",       "==", True,  3),
            ("coding_pct",     ">=", 20,    3),
            ("learn_pct",      ">=", 10,    2),
        ],
    },
    {
        "name":        "Grind Day",
        "emoji":       "💪",
        "color":       "c-accent",
        "description": "Long screen time, high keystrokes — a hard working day.",
        "rules": [
            ("screen_h",       ">=", 8,     5),
            ("keystrokes",     ">=", 10000, 5),
            ("coding_secs",    ">=", 3600,  4),
            ("commands",       ">=", 100,   3),
            ("longest_h",      ">=", 3,     3),
        ],
    },
    {
        "name":        "Balanced Day",
        "emoji":       "⚖",
        "color":       "c-green",
        "description": "Healthy mix of work, learning, and breaks.",
        "rules": [
            ("coding_pct",     ">=", 20,    3),
            ("learn_pct",      ">=", 10,    3),
            ("entert_pct",     "<=", 30,    3),
            ("screen_h",       ">=", 4,     2),
            ("screen_h",       "<=", 10,    2),
        ],
    },
    {
        "name":        "Admin Day",
        "emoji":       "📋",
        "color":       "c-muted",
        "description": "Package updates, system maintenance, file management.",
        "rules": [
            ("packages",       ">=", 3,     6),
            ("pkg_commands",   ">=", 5,     5),
            ("has_terminal",   "==", True,  3),
            ("coding_secs",    "<=", 3600,  2),
        ],
    },
    {
        "name":        "Setup Day",
        "emoji":       "⚙",
        "color":       "c-muted",
        "description": "Installing, configuring, dotfiles — system setup work.",
        "rules": [
            ("packages",       ">=", 10,    6),
            ("pkg_commands",   ">=", 10,    5),
            ("commands",       ">=", 150,   4),
            ("has_terminal",   "==", True,  3),
            ("git_commands",   ">=", 5,     2),
        ],
    },

    # ── Content consumption roles ─────────────────────────────────────
    {
        "name":        "YouTube Rabbit Hole",
        "emoji":       "📺",
        "color":       "c-accent",
        "description": "Very high YouTube watch time as main activity.",
        "rules": [
            ("youtube_visits", ">=", 30,    8),
            ("entert_pct",     ">=", 50,    6),
            ("youtube_visits", ">=", 15,    4),
            ("coding_secs",    "<=", 1800,  2),
        ],
    },
    {
        "name":        "Content Consumer",
        "emoji":       "🎬",
        "color":       "c-yellow",
        "description": "Entertainment-heavy day — video, music, streaming.",
        "rules": [
            ("entert_pct",     ">=", 40,    7),
            ("has_media",      "==", True,  4),
            ("entert_secs",    ">=", 7200,  4),
            ("coding_secs",    "<=", 3600,  2),
        ],
    },
    {
        "name":        "Social Media Day",
        "emoji":       "📱",
        "color":       "c-accent",
        "description": "Significant time on social platforms.",
        "rules": [
            ("social_pct",     ">=", 30,    8),
            ("social_secs",    ">=", 5400,  6),
            ("social_pct",     ">=", 15,    4),
        ],
    },
    {
        "name":        "Gaming Session",
        "emoji":       "🎮",
        "color":       "c-purple",
        "description": "Steam, Lutris, or native games — leisure gaming.",
        "rules": [
            ("has_game",       "==", True,  9),
            ("entert_pct",     ">=", 30,    4),
            ("coding_secs",    "<=", 3600,  2),
        ],
    },
    {
        "name":        "Media Creator",
        "emoji":       "🎵",
        "color":       "c-purple",
        "description": "Audio/video editing, music production, content creation.",
        "rules": [
            ("has_media",      "==", True,  4),
            ("has_design",     "==", True,  4),
            ("design_secs",    ">=", 3600,  4),
            ("write_secs",     ">=", 1800,  2),
        ],
    },

    # ── Recovery / light use roles ────────────────────────────────────
    {
        "name":        "Light Use",
        "emoji":       "🌿",
        "color":       "c-muted",
        "description": "Short sessions, minimal activity — easy day.",
        "rules": [
            ("screen_h",       "<=", 3,     5),
            ("keystrokes",     "<=", 2000,  4),
            ("coding_secs",    "<=", 1800,  3),
            ("entert_pct",     "<=", 30,    2),
        ],
    },
    {
        "name":        "Distracted Day",
        "emoji":       "🌀",
        "color":       "c-accent",
        "description": "Lots of app-switching, no deep focus, scattered attention.",
        "rules": [
            ("sess_count",     ">=", 6,     4),
            ("entert_pct",     ">=", 25,    4),
            ("coding_pct",     "<=", 20,    3),
            ("avg_sess_m",     "<=", 30,    3),
            ("social_pct",     ">=", 10,    2),
        ],
    },
    {
        "name":        "Evening Session",
        "emoji":       "🌙",
        "color":       "c-muted",
        "description": "Usage concentrated in the evening — late night work or browsing.",
        "rules": [
            ("evening_heavy",  "==", True,  7),
            ("screen_h",       "<=", 5,     2),
        ],
    },
    {
        "name":        "Rest Day",
        "emoji":       "😴",
        "color":       "c-dim",
        "description": "Minimal computer use today — rest well.",
        "rules": [
            ("screen_h",       "<=", 1,     8),
            ("keystrokes",     "<=", 500,   4),
            ("total_fg_secs",  "<=", 1800,  4),
        ],
    },
    # ── Background-aware combo roles ─────────────────────────────────
    {
        "name":        "Musical Coder",
        "emoji":       "🎵",
        "color":       "c-blue",
        "description": "Coding with music playing in the background. Productive focus with a beat.",
        "rules": [
            ("has_music_bg",   "==", True,  7),
            ("coding_secs",    ">=", 3600,  6),
            ("music_bg_secs",  ">=", 3600,  4),
            ("coding_pct",     ">=", 25,    3),
        ],
    },
    {
        "name":        "Movie Guy",
        "emoji":       "🎬",
        "color":       "c-muted",
        "description": "Whole day with a movie or long video session running — background cinema.",
        "rules": [
            ("has_video_bg",   "==", True,  8),
            ("video_bg_secs",  ">=", 7200,  6),   # 2h+ video in background
            ("entert_secs",    ">=", 5400,  3),
        ],
    },
    {
        "name":        "Soundtrack Warrior",
        "emoji":       "🎧",
        "color":       "c-green",
        "description": "Terminal/sysadmin work with music running all day long.",
        "rules": [
            ("has_music_bg",   "==", True,  6),
            ("has_terminal",   "==", True,  5),
            ("commands",       ">=", 50,    4),
            ("music_bg_secs",  ">=", 5400,  3),
        ],
    },
    {
        "name":        "Background Researcher",
        "emoji":       "📺",
        "color":       "c-blue",
        "description": "Watching tutorials/talks in background while reading docs — passive learning.",
        "rules": [
            ("has_video_bg",     "==", True,  5),
            ("browser_prod_secs",">=", 3600,  5),
            ("video_bg_secs",    ">=", 1800,  4),
            ("learn_pct",        ">=", 15,    3),
        ],
    },
    {
        "name":        "Night Owl Coder",
        "emoji":       "🦉",
        "color":       "c-purple",
        "description": "Coding session that ran deep into the night.",
        "rules": [
            ("evening_heavy",  "==", True,  6),
            ("coding_secs",    ">=", 3600,  6),
            ("has_editor",     "==", True,  3),
            ("commands",       ">=", 30,    2),
        ],
    },
    {
        "name":        "Hyperfocus Day",
        "emoji":       "🔥",
        "color":       "c-accent",
        "description": "One very long uninterrupted session — rare deep work achieved.",
        "rules": [
            ("longest_h",      ">=", 4,     8),
            ("sess_count",     "<=", 2,     5),
            ("coding_secs",    ">=", 5400,  4),
            ("keystrokes",     ">=", 8000,  3),
        ],
    },
    {
        "name":        "Package Manager",
        "emoji":       "📦",
        "color":       "c-green",
        "description": "Installing, upgrading, managing packages all day long.",
        "rules": [
            ("packages",       ">=", 15,    8),
            ("pkg_commands",   ">=", 20,    6),
            ("has_terminal",   "==", True,  3),
        ],
    },
    {
        "name":        "Doom Scroller",
        "emoji":       "📱",
        "color":       "c-accent",
        "description": "Lost in the feed — social media and Reddit all day.",
        "rules": [
            ("social_pct",     ">=", 40,    9),
            ("social_secs",    ">=", 7200,  6),
            ("coding_secs",    "<=", 1800,  3),
        ],
    },
    {
        "name":        "Debug Warrior",
        "emoji":       "🔥",
        "color":       "c-yellow",
        "description": "Deep debugging session — editor, terminal, and browser constantly switching.",
        "rules": [
            ("has_editor",     "==", True,  4),
            ("has_terminal",   "==", True,  4),
            ("sess_count",     ">=", 5,     3),    # many sessions = frustration/restarts
            ("commands",       ">=", 80,    4),
            ("browser_prod_secs",">=",1800, 3),
        ],
    },

    {
        "name":        "Unknown",
        "emoji":       "❓",
        "color":       "c-dim",
        "description": "Not enough data to determine today's role.",
        "rules": [],   # fallback — always gets 0 points, wins only if all others too
    },
]


# ── Rule evaluation ─────────────────────────────────────────────────────────

def _check_rule(features: dict, field: str, op: str, value) -> bool:
    """Evaluate one rule condition against the feature dict."""
    actual = features.get(field)
    if actual is None:
        return False
    if op == "in":
        # value is a pipe-separated list of substrings
        return any(v in str(actual) for v in str(value).split("|"))
    try:
        if   op == ">=": return actual >= value
        elif op == "<=": return actual <= value
        elif op == ">":  return actual >  value
        elif op == "<":  return actual <  value
        elif op == "==": return actual == value
        elif op == "!=": return actual != value
    except TypeError:
        return False
    return False


def score_roles(features: dict) -> list:
    """
    Score every role against the extracted features.
    Returns list of dicts sorted by score descending.
    """
    results = []
    for role in ROLES:
        points = 0
        matched = []
        for rule in role["rules"]:
            field, op, threshold, pts = rule
            if _check_rule(features, field, op, threshold):
                points += pts
                matched.append(f"{field}{op}{threshold}(+{pts})")
        results.append({
            "role_name":   role["name"],
            "emoji":       role["emoji"],
            "color":       role["color"],
            "description": role["description"],
            "role_score":  points,
            "matched":     matched,
        })
    results.sort(key=lambda x: x["role_score"], reverse=True)
    return results


def detect_role(target_date: str) -> dict:
    """
    Detect the best-matching role for a given date.
    Returns the top role with runner-ups included.
    """
    try:
        features   = compute_day_features(target_date)
        scored     = score_roles(features)
        winner     = scored[0]
        runner_ups = scored[1:4]

        # If winner has 0 points, fall back to Unknown
        if winner["role_score"] == 0:
            winner = next(r for r in ROLES if r["name"] == "Unknown")
            winner = {**winner, "role_score": 0, "matched": [], "alternatives": "[]"}

        winner["alternatives"] = json.dumps([
            {"name": r["role_name"], "score": r["role_score"],
             "emoji": r["emoji"], "color": r["color"]}
            for r in runner_ups
        ])
        winner["features_snapshot"] = json.dumps({
            k: round(v, 1) if isinstance(v, float) else v
            for k, v in features.items()
            if k not in ("top_app", "top_domain")  # exclude PII
        })
        logger.info(
            f"Role for {target_date}: {winner['emoji']} {winner['role_name']} "
            f"(score={winner['role_score']}) | "
            f"runner-ups: {[r['role_name'] for r in runner_ups[:2]]}"
        )
        return winner
    except Exception as e:
        logger.error(f"detect_role error for {target_date}: {e}", exc_info=True)
        fallback = next(r for r in ROLES if r["name"] == "Unknown")
        return {**fallback, "role_score": 0, "matched": [],
                "alternatives": "[]", "features_snapshot": "{}"}


def save_daily_role(target_date: str) -> dict:
    """Detect role for target_date and save/update it in daily_roles table."""
    from daemon.db import execute, query as dbq
    role = detect_role(target_date)
    # Get productivity score for the day
    prod = dbq("""
        SELECT AVG(productivity_score) as ps
        FROM productivity WHERE date=?
    """, (target_date,), fetch="one")
    prod_score = round(prod["ps"] or 0, 1) if prod else 0.0

    # Get screen time
    sess = dbq("""
        SELECT SUM(CASE WHEN duration_seconds IS NOT NULL THEN duration_seconds
                        ELSE CAST(strftime('%s','now') AS INTEGER) - start_time END) as total
        FROM sessions WHERE date(start_time,'unixepoch')=?
    """, (target_date,), fetch="one")
    screen_secs = (sess["total"] or 0) if sess else 0

    # Top app
    top = dbq("""
        SELECT app_name FROM app_usage WHERE date=? AND foreground_seconds>0
        AND app_name NOT IN ('unknown','plasmashell','gnome-shell','waybar')
        GROUP BY app_name ORDER BY SUM(foreground_seconds) DESC LIMIT 1
    """, (target_date,), fetch="one")
    top_app = (top["app_name"] or "") if top else ""

    execute("""
        INSERT INTO daily_roles
          (date, role_name, emoji, color, description, productivity_score,
           screen_time_seconds, top_app, alternatives)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(date) DO UPDATE SET
          role_name=excluded.role_name, emoji=excluded.emoji,
          color=excluded.color, description=excluded.description,
          productivity_score=excluded.productivity_score,
          screen_time_seconds=excluded.screen_time_seconds,
          top_app=excluded.top_app, alternatives=excluded.alternatives
    """, (
        target_date, role["role_name"], role["emoji"], role["color"],
        role["description"], prod_score, screen_secs, top_app,
        role.get("alternatives", "[]"),
    ))
    return role


def analyze_productivity_patterns(days: int = 30) -> dict:
    """
    Analyze productivity patterns over the last N days.
    Used by the Coach page for trend analysis and AI advice.
    Returns a structured summary of work patterns.
    """
    from daemon.db import query
    import datetime

    end_date   = str(datetime.date.today())
    start_date = str(datetime.date.today() - datetime.timedelta(days=days))

    daily = query("""
        SELECT date,
               AVG(productivity_score) as score,
               SUM(sample_count)       as tracked_secs,
               SUM(coding_samples + learning_samples + ai_samples) as work_secs,
               SUM(entertainment_samples) as entert_secs,
               MAX(dominant_category)  as dominant
        FROM productivity
        WHERE date BETWEEN ? AND ?
        GROUP BY date ORDER BY date DESC
    """, (start_date, end_date))

    if not daily:
        return {"days_tracked": 0, "avg_score": 0, "trend": "no_data",
                "best_day": None, "worst_day": None, "scores": []}

    scores    = [round(r["score"] or 0, 1) for r in daily]
    avg_score = round(sum(scores) / len(scores), 1) if scores else 0
    best      = max(daily, key=lambda r: r["score"] or 0)
    worst     = min(daily, key=lambda r: r["score"] or 0)

    # Trend: compare last 7 days vs previous 7
    recent = scores[:7]
    prev   = scores[7:14]
    trend  = "stable"
    if recent and prev:
        if sum(recent)/len(recent) > sum(prev)/len(prev) + 5:
            trend = "improving"
        elif sum(recent)/len(recent) < sum(prev)/len(prev) - 5:
            trend = "declining"

    # Role distribution
    roles = query("""
        SELECT role_name, COUNT(*) as cnt, AVG(productivity_score) as avg_score
        FROM daily_roles WHERE date BETWEEN ? AND ?
        GROUP BY role_name ORDER BY cnt DESC
    """, (start_date, end_date))

    # Time split
    total_tracked = sum(r["tracked_secs"] or 0 for r in daily)
    total_work    = sum(r["work_secs"]    or 0 for r in daily)
    total_entert  = sum(r["entert_secs"]  or 0 for r in daily)

    return {
        "days_tracked":    len(daily),
        "avg_score":       avg_score,
        "trend":           trend,
        "best_day":        {"date": best["date"], "score": round(best["score"] or 0, 1)},
        "worst_day":       {"date": worst["date"], "score": round(worst["score"] or 0, 1)},
        "scores":          [{"date": r["date"], "score": round(r["score"] or 0, 1)} for r in reversed(daily)],
        "roles":           [{"name": r["role_name"], "count": r["cnt"],
                             "avg_score": round(r["avg_score"] or 0, 1)} for r in (roles or [])],
        "total_tracked_h": round(total_tracked / 3600, 1),
        "work_pct":        round((total_work / max(total_tracked, 1)) * 100, 1),
        "entert_pct":      round((total_entert / max(total_tracked, 1)) * 100, 1),
        "start_date":      start_date,
        "end_date":        end_date,
    }
