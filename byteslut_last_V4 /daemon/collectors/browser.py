"""
collectors/browser.py — Browser History & YouTube Tracker
===========================================================
Tracks: URLs visited, time on sites, YouTube video titles, domains.

HOW IT WORKS (no extensions needed!):
  Brave, Chrome, Firefox all store their browsing history in SQLite databases
  on your local machine. We read those databases directly.

  We watch the file modification time — when it changes, the browser updated
  its history, and we read the new entries. This is very efficient:
  we only do work when you actually browse something new.

FLATPAK BRAVE SUPPORT:
  Regular Brave:  ~/.config/BraveSoftware/Brave-Browser/Default/History
  Flatpak Brave:  ~/.var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser/Default/History

  We check both paths automatically.

YOUTUBE DETECTION:
  YouTube URLs look like: https://www.youtube.com/watch?v=XXXXXXXXXXX
  The page title (stored in browser history) contains the video name!
  e.g. title = "Rick Astley - Never Gonna Give You Up - YouTube"
  We parse out the video title by removing " - YouTube" from the end.

VISIT DURATION:
  Browsers store when you visited a URL but not how long you stayed.
  We estimate duration by looking at the gap between consecutive visits.
  If the next visit is within 30 minutes, that gap = time on current page.
  Otherwise, we use a default of 60 seconds.

PRIVACY NOTE:
  This data never leaves your machine. It's stored in YOUR local SQLite DB.
  You can disable browser tracking in settings.
"""

import os
import re
import time
import shutil
import sqlite3
import logging
from datetime import datetime, date
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# How often to check if browser history has been updated (seconds)
CHECK_INTERVAL = 30

# If we can't determine visit duration, use this default
DEFAULT_VISIT_DURATION = 60  # seconds

# Maximum gap to consider "same session" on a page
MAX_SESSION_GAP = 1800  # 30 minutes


def find_browser_history_files():
    """
    Find all browser history SQLite files on the system.
    We check all common paths for all major browsers, including Flatpak versions.

    Returns: list of dicts with 'browser', 'path', 'is_flatpak'
    """
    home = Path.home()
    browsers = []

    # ── Brave Browser ──
    brave_paths = [
        # Regular installation
        (home / ".config/BraveSoftware/Brave-Browser/Default/History", False),
        (home / ".config/BraveSoftware/Brave-Browser/Profile 1/History", False),
        # Flatpak installation
        (home / ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser/Default/History", True),
        (home / ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser/Profile 1/History", True),
    ]
    for path, is_flatpak in brave_paths:
        if path.exists():
            browsers.append({"browser": "brave", "path": str(path), "is_flatpak": is_flatpak})

    # ── Firefox ──
    firefox_base = home / ".mozilla/firefox"
    if firefox_base.exists():
        for profile_dir in firefox_base.iterdir():
            if profile_dir.is_dir():
                history_db = profile_dir / "places.sqlite"
                if history_db.exists():
                    browsers.append({"browser": "firefox", "path": str(history_db), "is_flatpak": False})
                    break  # Use first profile found

    # Firefox Flatpak
    ff_flatpak = home / ".var/app/org.mozilla.firefox/.mozilla/firefox"
    if ff_flatpak.exists():
        for profile_dir in ff_flatpak.iterdir():
            if profile_dir.is_dir():
                history_db = profile_dir / "places.sqlite"
                if history_db.exists():
                    browsers.append({"browser": "firefox", "path": str(history_db), "is_flatpak": True})
                    break

    # ── Chrome/Chromium ──
    chrome_paths = [
        (home / ".config/google-chrome/Default/History", "chrome", False),
        (home / ".config/chromium/Default/History", "chromium", False),
        (home / ".var/app/com.google.Chrome/config/google-chrome/Default/History", "chrome", True),
        (home / ".var/app/org.chromium.Chromium/config/chromium/Default/History", "chromium", True),
    ]
    for path, name, is_flatpak in chrome_paths:
        if path.exists():
            browsers.append({"browser": name, "path": str(path), "is_flatpak": is_flatpak})

    logger.info(f"Found {len(browsers)} browser history files: {[b['browser'] for b in browsers]}")
    return browsers


def extract_domain(url: str) -> str:
    """
    Extract just the domain from a full URL.
    'https://www.youtube.com/watch?v=abc123' → 'youtube.com'
    'https://github.com/user/repo' → 'github.com'
    """
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        # Remove 'www.' prefix
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


def parse_youtube_title(title: str, url: str) -> str:
    """
    Extract the video title from a YouTube page title.

    Browser titles for YouTube look like:
    - "Rick Astley - Never Gonna Give You Up (Official Music Video) - YouTube"
    - "How to install Arch Linux - YouTube"

    We strip the " - YouTube" suffix to get the clean video title.
    For YouTube Music: "Song Name - Artist - YouTube Music"
    """
    if not title:
        return ""

    # Remove common suffixes
    for suffix in [" - YouTube", " - YouTube Music", " — YouTube"]:
        if title.endswith(suffix):
            return title[: -len(suffix)].strip()

    return title.strip()


def read_chromium_history(db_path: str, browser: str, is_flatpak: bool, since_timestamp: int = 0):
    """
    Read history from Chrome/Chromium/Brave SQLite database.

    Chromium-based browsers store history in:
      Table: urls
      Columns: id, url, title, visit_count, last_visit_time

    NOTE: Chromium stores timestamps as microseconds since Jan 1, 1601 (Windows FILETIME)
    NOT Unix timestamps! We need to convert.

    Chromium timestamp → Unix: (chromium_ts / 1_000_000) - 11644473600
    """
    # We need to copy the file first because Chrome/Brave locks it while running.
    # Chromium uses WAL (Write-Ahead Log) mode — there are THREE files:
    #   History      ← main database
    #   History-wal  ← pending writes not yet checkpointed
    #   History-shm  ← shared memory file for WAL coordination
    # Copying only the main file without the WAL means SQLite reads an
    # inconsistent snapshot → "database disk image is malformed" error.
    # This is why the browser tab "sometimes works, sometimes doesn't".
    # Fix: copy all three files so the copy is a consistent checkpoint.
    temp_path = f"/tmp/byteslut_history_{browser}_{os.getpid()}.db"
    temp_wal  = temp_path + "-wal"
    temp_shm  = temp_path + "-shm"

    try:
        shutil.copy2(db_path, temp_path)
        # Copy WAL and SHM if they exist (they do when Brave is running)
        if os.path.exists(db_path + "-wal"):
            shutil.copy2(db_path + "-wal", temp_wal)
        if os.path.exists(db_path + "-shm"):
            shutil.copy2(db_path + "-shm", temp_shm)
    except Exception as e:
        logger.warning(f"Could not copy {browser} history (browser might be running): {e}")
        return []

    entries = []
    try:
        conn = sqlite3.connect(temp_path)
        conn.row_factory = sqlite3.Row

        # Chromium epoch offset (seconds between 1601-01-01 and 1970-01-01)
        CHROMIUM_EPOCH_OFFSET = 11644473600

        # Convert our Unix since_timestamp to Chromium timestamp
        since_chromium = (since_timestamp + CHROMIUM_EPOCH_OFFSET) * 1_000_000 if since_timestamp else 0

        rows = conn.execute("""
            SELECT u.url, u.title, v.visit_time
            FROM visits v
            JOIN urls u ON v.url = u.id
            WHERE v.visit_time > ?
            ORDER BY v.visit_time ASC
        """, (since_chromium,)).fetchall()

        for i, row in enumerate(rows):
            url = row["url"]
            title = row["title"] or ""

            # Skip internal browser URLs and localhost (ByteSlut dashboard itself)
            if url.startswith(("chrome://", "brave://", "about:", "chrome-extension://",
                               "http://127.0.0.1", "http://localhost",
                               "https://127.0.0.1", "https://localhost")):
                continue

            # Convert Chromium timestamp to Unix timestamp
            unix_ts = int(row["visit_time"] / 1_000_000) - CHROMIUM_EPOCH_OFFSET

            # Estimate visit duration
            if i + 1 < len(rows):
                next_ts = int(rows[i + 1]["visit_time"] / 1_000_000) - CHROMIUM_EPOCH_OFFSET
                gap = next_ts - unix_ts
                duration = gap if 0 < gap < MAX_SESSION_GAP else DEFAULT_VISIT_DURATION
            else:
                duration = DEFAULT_VISIT_DURATION

            domain = extract_domain(url)
            is_youtube = "youtube.com" in domain
            yt_title = parse_youtube_title(title, url) if is_youtube else ""

            entries.append({
                "timestamp": unix_ts,
                "date": datetime.fromtimestamp(unix_ts).strftime("%Y-%m-%d"),
                "browser": browser,
                "url": url[:2000],     # Limit URL length
                "title": title[:500],
                "domain": domain[:200],
                "is_youtube": 1 if is_youtube else 0,
                "youtube_title": yt_title[:500],
                "visit_duration_seconds": duration,
                "is_flatpak": 1 if is_flatpak else 0,
            })

        conn.close()
    except Exception as e:
        logger.error(f"Error reading {browser} history: {e}")
    finally:
        # Clean up all temp files (main + WAL + SHM)
        for f in (temp_path, temp_wal, temp_shm):
            try:
                os.remove(f)
            except Exception:
                pass

    return entries


def read_firefox_history(db_path: str, is_flatpak: bool, since_timestamp: int = 0):
    """
    Read history from Firefox's places.sqlite database.

    Firefox stores timestamps as MICROSECONDS since Unix epoch (1970-01-01).
    So we just divide by 1_000_000 to get regular Unix seconds.

    Firefox schema:
      Table: moz_places  — stores URLs and titles
      Table: moz_historyvisits  — stores individual visits with timestamps
    """
    temp_path = f"/tmp/byteslut_firefox_{os.getpid()}.db"

    try:
        shutil.copy2(db_path, temp_path)
    except Exception as e:
        logger.warning(f"Could not copy Firefox history: {e}")
        return []

    entries = []
    try:
        conn = sqlite3.connect(temp_path)
        conn.row_factory = sqlite3.Row

        # Firefox uses microseconds since epoch
        since_firefox = since_timestamp * 1_000_000 if since_timestamp else 0

        rows = conn.execute("""
            SELECT p.url, p.title, h.visit_date
            FROM moz_historyvisits h
            JOIN moz_places p ON h.place_id = p.id
            WHERE h.visit_date > ?
            ORDER BY h.visit_date ASC
        """, (since_firefox,)).fetchall()

        for i, row in enumerate(rows):
            url = row["url"]
            title = row["title"] or ""

            if url.startswith(("about:", "moz-extension://",
                               "http://127.0.0.1", "http://localhost",
                               "https://127.0.0.1", "https://localhost")):
                continue

            unix_ts = int(row["visit_date"]) // 1_000_000

            if i + 1 < len(rows):
                next_ts = int(rows[i + 1]["visit_date"]) // 1_000_000
                gap = next_ts - unix_ts
                duration = gap if 0 < gap < MAX_SESSION_GAP else DEFAULT_VISIT_DURATION
            else:
                duration = DEFAULT_VISIT_DURATION

            domain = extract_domain(url)
            is_youtube = "youtube.com" in domain
            yt_title = parse_youtube_title(title, url) if is_youtube else ""

            entries.append({
                "timestamp": unix_ts,
                "date": datetime.fromtimestamp(unix_ts).strftime("%Y-%m-%d"),
                "browser": "firefox",
                "url": url[:2000],
                "title": title[:500],
                "domain": domain[:200],
                "is_youtube": 1 if is_youtube else 0,
                "youtube_title": yt_title[:500],
                "visit_duration_seconds": duration,
                "is_flatpak": 1 if is_flatpak else 0,
            })

        conn.close()
    except Exception as e:
        logger.error(f"Error reading Firefox history: {e}")
    finally:
        try:
            os.remove(temp_path)
        except Exception:
            pass

    return entries


class BrowserCollector:
    """
    Watches all browser history files and imports new entries.

    Strategy: polling with file modification time check.
    - Every CHECK_INTERVAL seconds, check if history file was modified
    - If yes, read only the NEW entries (since last check timestamp)
    - This is very efficient — we skip reading if nothing changed
    """

    def __init__(self, batch_writer):
        self.batch_writer = batch_writer
        self.running = False

        # Track last-seen modification time for each browser file
        # and the last timestamp we've already imported
        self.browser_state = {}  # {path: {"mtime": float, "last_ts": int}}

    def run(self):
        """Main polling loop."""
        self.running = True
        browser_files = find_browser_history_files()

        if not browser_files:
            logger.warning("No browser history files found. Browser tracking disabled.")
            return

        # Initialize state for each browser
        for b in browser_files:
            self.browser_state[b["path"]] = {
                "mtime": 0,
                "last_ts": int(time.time()) - 86400,  # Start from 24 hours ago
                "browser": b["browser"],
                "is_flatpak": b["is_flatpak"],
            }

        logger.info(f"BrowserCollector started, watching: {[b['browser'] for b in browser_files]}")

        while self.running:
            for b in browser_files:
                try:
                    self._check_browser(b)
                except Exception as e:
                    logger.error(f"Browser check error ({b['browser']}): {e}")
            time.sleep(CHECK_INTERVAL)

    def _check_browser(self, browser_info: dict):
        """Check one browser's history file for updates."""
        path = browser_info["path"]
        state = self.browser_state[path]

        # Check if file was modified since last check
        try:
            current_mtime = os.path.getmtime(path)
        except FileNotFoundError:
            return  # Browser history file disappeared (browser reinstalled?)

        if current_mtime <= state["mtime"]:
            return  # File hasn't changed, nothing to do

        # File was modified — read new entries
        state["mtime"] = current_mtime
        browser = browser_info["browser"]
        is_flatpak = browser_info["is_flatpak"]
        since_ts = state["last_ts"]

        if browser == "firefox":
            entries = read_firefox_history(path, is_flatpak, since_ts)
        else:
            # Brave, Chrome, Chromium all use Chromium's format
            entries = read_chromium_history(path, browser, is_flatpak, since_ts)

        if entries:
            for entry in entries:
                self.batch_writer.add("browser_history", entry)

            # Update last timestamp to the most recent entry
            state["last_ts"] = max(e["timestamp"] for e in entries)
            logger.info(f"Imported {len(entries)} new {browser} history entries")

    def stop(self):
        self.running = False
        logger.info("BrowserCollector stopped")
