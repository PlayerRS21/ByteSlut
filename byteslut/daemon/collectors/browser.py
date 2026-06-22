"""
collectors/browser.py — Advanced Hybrid Default Browser History Tracker
===========================================================================
Combines explicit configurations, XDG fallbacks, and runtime file descriptor 
interception to automatically resolve history paths for standard or obscure browsers.
"""

import os
import re
import time
import shutil
import sqlite3
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 30
DEFAULT_VISIT_DURATION = 60
MAX_SESSION_GAP = 1800


def get_xdg_default_browser() -> str:
    """Queries system configuration protocols to deduce fallback browser binary name."""
    try:
        res = subprocess.run(
            ["xdg-settings", "get", "default-web-browser"],
            capture_output=True, text=True, check=True
        )
        desktop_file = res.stdout.strip().lower()
        if desktop_file:
            # Parse binary token out of .desktop format (e.g., google-chrome.desktop -> google-chrome)
            return desktop_file.replace(".desktop", "")
    except Exception:
        pass
    return ""


def load_explicit_browser_config() -> str:
    """Reads target profile selection directly from user configurations or falls back to XDG."""
    config_file = Path.home() / ".config/default_browser.txt"
    
    if not config_file.exists():
        try:
            config_file.parent.mkdir(parents=True, exist_ok=True)
            config_file.write_text(
                "# Enter your active default browser name lowercase below.\n"
                "# Valid targets: brave, firefox, chrome, chromium, librewolf, or 'auto'\nauto\n", 
                encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"Failed to generate explicit config template: {e}")

    target = "auto"
    if config_file.exists():
        try:
            for line in config_file.read_text(encoding="utf-8").splitlines():
                line = line.strip().lower()
                if line and not line.startswith("#"):
                    target = line
                    break
        except Exception as e:
            logger.error(f"Error parsing {config_file}: {e}")
            
    if target == "auto":
        fallback = get_xdg_default_browser()
        if fallback:
            logger.info(f"Auto-detected system default browser binary via XDG: '{fallback}'")
            return fallback
        return "brave" # Global fallback hardcoded target
    return target


def detect_db_type(db_path: Path) -> str:
    """Evaluates sqlite tables directly to determine formatting lineage at runtime."""
    if not db_path.exists():
        return "unknown"
    if db_path.name == "places.sqlite":
        return "firefox"
    if db_path.name == "History":
        return "chromium"

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cursor.fetchall()]
        conn.close()
        if "moz_places" in tables:
            return "firefox"
        if "visits" in tables and "urls" in tables:
            return "chromium"
    except Exception:
        pass
    return "unknown"


def discover_active_browser_db(binary_name: str) -> Path or None:
    """Intercepts open kernel descriptors of a running browser process to hunt down history endpoints."""
    try:
        # Find PIDs associated with the browser token
        pid_res = subprocess.run(["pgrep", "-i", binary_name], capture_output=True, text=True)
        pids = [p.strip() for p in pid_res.stdout.splitlines() if p.strip()]
        
        for pid in pids:
            fd_dir = Path(f"/proc/{pid}/fd")
            if not fd_dir.exists():
                continue
                
            for fd in fd_dir.iterdir():
                try:
                    target_path = Path(os.readlink(str(fd)))
                    if target_path.is_file():
                        # Intercept classic naming conventions or run schema inspections on suspicious sqlite configurations
                        if target_path.name in ["History", "places.sqlite"] or target_path.suffix in [".sqlite", ".db"]:
                            engine = detect_db_type(target_path)
                            if engine != "unknown":
                                logger.info(f"Discovered active runtime engine database target: {target_path}")
                                return target_path
                except Exception:
                    continue
    except Exception as e:
        logger.debug(f"Process monitoring scan failed: {e}")
        
    # Static fallback deep scanning inside XDG default parameters
    home = Path.home()
    scan_vectors = [home / ".config", home / ".local/share", home / ".var/app"]
    for vec in scan_vectors:
        if not vec.exists():
            continue
        try:
            # Look for history parameters modified within last 5 days inside likely paths
            for found in vec.rglob("*"):
                if found.is_file() and binary_name in str(found).lower():
                    if found.name in ["History", "places.sqlite"]:
                        return found
        except Exception:
            continue
            
    return None


def find_browser_history_files():
    """Maps out profile targets matching explicit tag or executes proactive runtime discovery."""
    home = Path.home()
    target_browser = load_explicit_browser_config()
    
    logger.info(f"Targeting explicit default browser profile match: '{target_browser}'")

    matrix = [
        ("brave", home / ".config/BraveSoftware/Brave-Browser", "History", "chromium", False),
        ("brave", home / ".config/BraveSoftware/Brave-Browser-Nightly", "History", "chromium", False),
        ("brave", home / ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser", "History", "chromium", True),
        ("firefox", home / ".mozilla/firefox", "places.sqlite", "firefox", False),
        ("firefox", home / ".var/app/org.mozilla.firefox/.mozilla/firefox", "places.sqlite", "firefox", True),
        ("librewolf", home / ".librewolf", "places.sqlite", "firefox", False),
        ("librewolf", home / ".var/app/io.gitlab.librewolf-community/.librewolf", "places.sqlite", "firefox", True),
        ("chrome", home / ".config/google-chrome", "History", "chromium", False),
        ("chrome", home / ".var/app/com.google.Chrome/config/google-chrome", "History", "chromium", True),
        ("chromium", home / ".config/chromium", "History", "chromium", False),
        ("chromium", home / ".var/app/org.chromium.Chromium/config/chromium", "History", "chromium", True),
    ]

    # Process standard matrix lookups
    for browser_id, base_path, filename, engine, is_flatpak in matrix:
        if browser_id in target_browser and base_path.exists():
            if engine == "chromium":
                for sub in ["Default", "Profile 1", "Profile 2"]:
                    probing_path = base_path / sub / filename
                    if probing_path.is_file():
                        return [{
                            "browser": target_browser, "path": str(probing_path),
                            "is_flatpak": is_flatpak, "engine": "chromium"
                        }]
                if (base_path / filename).is_file():
                    return [{
                        "browser": target_browser, "path": str(base_path / filename),
                        "is_flatpak": is_flatpak, "engine": "chromium"
                    }]

            elif engine == "firefox":
                try:
                    for item in base_path.iterdir():
                        if item.is_dir() and not item.is_symlink():
                            probing_path = item / filename
                            if probing_path.is_file():
                                return [{
                                    "browser": target_browser, "path": str(probing_path),
                                    "is_flatpak": is_flatpak, "engine": "firefox"
                                }]
                except Exception as e:
                    logger.debug(f"Error accessing profile block path: {e}")

    # Unknown or obscure browser path fallback discovery pipeline
    logger.warning(f"Browser profile match '{target_browser}' not resolved in static matrix. Initializing advanced runtime tracking...")
    discovered_path = discover_active_browser_db(target_browser)
    
    if discovered_path and discovered_path.is_file():
        detected_engine = detect_db_type(discovered_path)
        if detected_engine != "unknown":
            is_flatpak = ".var/app" in str(discovered_path)
            logger.info(f"Advanced tracking engine successfully bound to path: {discovered_path} [{detected_engine}]")
            return [{
                "browser": target_browser,
                "path": str(discovered_path),
                "is_flatpak": is_flatpak,
                "engine": detected_engine
            }]

    logger.error(f"Target file path configurations for default browser option '{target_browser}' could not be resolved.")
    return []


def extract_domain(url: str) -> str:
    """Extract just the domain from a full URL."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


def parse_youtube_title(title: str, url: str) -> str:
    """Extract the video title from a YouTube page title."""
    if not title:
        return ""
    for suffix in [" - YouTube", " - YouTube Music", " — YouTube"]:
        if title.endswith(suffix):
            return title[: -len(suffix)].strip()
    return title.strip()


def read_chromium_history(db_path: str, browser: str, is_flatpak: bool, since_timestamp: int = 0):
    """Isolates active tracking points out of any Chromium history storage layout safely."""
    temp_path = f"/tmp/byteslut_hst_exp_{os.getpid()}.db"
    temp_wal  = temp_path + "-wal"
    temp_shm  = temp_path + "-shm"

    try:
        shutil.copy2(db_path, temp_path)
        if os.path.exists(db_path + "-wal"):
            shutil.copy2(db_path + "-wal", temp_wal)
        if os.path.exists(db_path + "-shm"):
            shutil.copy2(db_path + "-shm", temp_shm)
    except Exception:
        return []

    entries = []
    try:
        conn = sqlite3.connect(temp_path)
        conn.row_factory = sqlite3.Row

        CHROMIUM_EPOCH_OFFSET = 11644473600
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

            if url.startswith(("chrome://", "brave://", "about:", "chrome-extension://",
                               "http://127.0.0.1", "http://localhost",
                               "https://127.0.0.1", "https://localhost")):
                continue

            unix_ts = int(row["visit_time"] / 1_000_000) - CHROMIUM_EPOCH_OFFSET

            if i + 1 < len(rows):
                next_ts = int(rows[i + 1]["visit_time"] / 1_000_000) - CHROMIUM_EPOCH_OFFSET
                gap = next_ts - unix_ts
                duration = min(gap, 300) if 0 < gap else DEFAULT_VISIT_DURATION
            else:
                duration = DEFAULT_VISIT_DURATION

            domain = extract_domain(url)
            is_youtube = "youtube.com" in domain
            yt_title = parse_youtube_title(title, url) if is_youtube else ""

            entries.append({
                "timestamp": unix_ts,
                "date": datetime.fromtimestamp(unix_ts).strftime("%Y-%m-%d"),
                "browser": browser,
                "url": url[:2000],
                "title": title[:500],
                "domain": domain[:200],
                "is_youtube": 1 if is_youtube else 0,
                "youtube_title": yt_title[:500],
                "visit_duration_seconds": duration,
                "is_flatpak": 1 if is_flatpak else 0,
            })
        conn.close()
    except Exception:
        pass
    finally:
        for f in (temp_path, temp_wal, temp_shm):
            try:
                os.remove(f)
            except Exception:
                pass
    return entries


def read_firefox_history(db_path: str, browser: str, is_flatpak: bool, since_timestamp: int = 0):
    """Isolates active tracking points out of any Firefox engine layout safely."""
    temp_path = f"/tmp/byteslut_ff_exp_{os.getpid()}.db"

    try:
        shutil.copy2(db_path, temp_path)
    except Exception:
        return []

    entries = []
    try:
        conn = sqlite3.connect(temp_path)
        conn.row_factory = sqlite3.Row

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
                duration = min(gap, 300) if 0 < gap else DEFAULT_VISIT_DURATION
            else:
                duration = DEFAULT_VISIT_DURATION

            domain = extract_domain(url)
            is_youtube = "youtube.com" in domain
            yt_title = parse_youtube_title(title, url) if is_youtube else ""

            entries.append({
                "timestamp": unix_ts,
                "date": datetime.fromtimestamp(unix_ts).strftime("%Y-%m-%d"),
                "browser": browser,
                "url": url[:2000],
                "title": title[:500],
                "domain": domain[:200],
                "is_youtube": 1 if is_youtube else 0,
                "youtube_title": yt_title[:500],
                "visit_duration_seconds": duration,
                "is_flatpak": 1 if is_flatpak else 0,
            })
        conn.close()
    except Exception:
        pass
    finally:
        try:
            os.remove(temp_path)
        except Exception:
            pass
    return entries


class BrowserCollector:
    """Watches the dynamically targeted browser database for modification events."""

    def __init__(self, batch_writer):
        self.batch_writer = batch_writer
        self.running = False
        self.enabled = True
        self.browser_state = {}
        self.browser_files = []

    def run(self):
        """Main loop initialization block."""
        self.running = True
        self.browser_files = find_browser_history_files()

        # Retry logic inside initialization loop if browser is not open during startup
        if not self.browser_files:
            logger.warning("No default browser files located instantly. Entering dynamic waiting check pool...")

        self._rebuild_tracking_state()

        while self.running:
            if self.enabled:
                # If target wasn't found initially, try locating it again at runtime
                if not self.browser_files:
                    self.browser_files = find_browser_history_files()
                    if self.browser_files:
                        self._rebuild_tracking_state()

                for b in self.browser_files:
                    try:
                        self._check_browser(b)
                    except Exception as e:
                        logger.error(f"Error querying active target descriptor: {e}")
            time.sleep(CHECK_INTERVAL)

    def _rebuild_tracking_state(self):
        """Builds state mapping blocks for active database paths."""
        for b in self.browser_files:
            if b["path"] in self.browser_state:
                continue
                
            from daemon.db import query as db_query
            last_in_db = db_query(
                "SELECT MAX(timestamp) as t FROM browser_history WHERE browser = ?",
                (b["browser"],), fetch="one"
            )
            db_ts = (last_in_db["t"] or 0) if last_in_db else 0
            start_ts = db_ts if db_ts > 0 else int(time.time()) - 86400
            
            self.browser_state[b["path"]] = {
                "mtime": 0,
                "last_ts": start_ts,
                "browser": b["browser"],
                "is_flatpak": b["is_flatpak"],
                "engine": b["engine"]
            }
            logger.info(f"Collector Connected: Monitoring {b['path']}")

    def _check_browser(self, browser_info: dict):
        """Analyze file descriptors for modification states."""
        path = browser_info["path"]
        state = self.browser_state.get(path)
        if not state:
            return

        try:
            current_mtime = os.getmtime(path)
        except FileNotFoundError:
            return

        if current_mtime <= state["mtime"]:
            return

        state["mtime"] = current_mtime
        browser = browser_info["browser"]
        is_flatpak = browser_info["is_flatpak"]
        since_ts = state["last_ts"]
        engine = state["engine"]

        if engine == "firefox":
            entries = read_firefox_history(path, browser, is_flatpak, since_ts)
        elif engine == "chromium":
            entries = read_chromium_history(path, browser, is_flatpak, since_ts)
        else:
            return

        if entries:
            for entry in entries:
                self.batch_writer.add("browser_history", entry)
            state["last_ts"] = max(e["timestamp"] for e in entries)
            logger.info(f"Successfully processed {len(entries)} updates via storage layout.")

    def stop(self):
        self.running = False
        logger.info("BrowserCollector stopped")
