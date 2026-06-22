"""
collectors/packages.py — Pacman Package History Tracker
=========================================================
Reads /var/log/pacman.log to extract install/remove/upgrade history.
This is a log file that pacman (Arch's package manager) writes to automatically.

Log format:
  [2024-01-15T10:30:00+0000] [ALPM] installed neovim (0.9.5-1)
  [2024-01-15T10:30:01+0000] [ALPM] upgraded firefox (121.0-1 -> 122.0-1)
  [2024-01-15T10:30:02+0000] [ALPM] removed old-package (1.0-1)
"""

import re
import time
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

PACMAN_LOG = "/var/log/pacman.log"

# Regex to parse pacman log entries
# Matches: [TIMESTAMP] [ALPM] ACTION package (version info)
PACMAN_RE = re.compile(
    r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+\-]\d{4})\] \[ALPM\] (installed|removed|upgraded) (\S+) \((.+?)\)"
)


class PackageCollector:
    """
    Reads pacman.log and imports new entries.
    Uses file position tracking so we only read new lines.
    """

    def __init__(self, batch_writer):
        self.batch_writer = batch_writer
        self.running = False
        self.last_pos = 0

    def _import_log(self):
        """Read new entries from pacman.log since last check."""
        if not os.path.exists(PACMAN_LOG):
            logger.warning(f"pacman.log not found at {PACMAN_LOG}")
            return

        try:
            with open(PACMAN_LOG, "r", errors="replace") as f:
                f.seek(self.last_pos)
                new_lines = f.readlines()
                self.last_pos = f.tell()

            for line in new_lines:
                match = PACMAN_RE.search(line)
                if not match:
                    continue

                ts_str, action, pkg_name, version_info = match.groups()

                # Parse the timestamp
                try:
                    ts = int(datetime.fromisoformat(ts_str).timestamp())
                except Exception:
                    ts = int(time.time())

                date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

                # Parse version info
                old_ver, new_ver = None, None
                if action == "upgraded":
                    # Format: "old_version -> new_version"
                    parts = version_info.split(" -> ")
                    old_ver = parts[0].strip() if len(parts) > 0 else None
                    new_ver = parts[1].strip() if len(parts) > 1 else None
                elif action in ("installed", "removed"):
                    new_ver = version_info.strip()

                self.batch_writer.add("packages", {
                    "timestamp": ts,
                    "date": date_str,
                    "action": action,
                    "package_name": pkg_name,
                    "old_version": old_ver,
                    "new_version": new_ver,
                    "reason": "explicit",  # Could be improved with pacman -Qi parsing
                })

        except Exception as e:
            logger.error(f"PackageCollector error: {e}")

    def run(self):
        self.running = True
        logger.info("PackageCollector started")
        self._import_log()  # Import all existing entries first

        while self.running:
            time.sleep(300)  # Check every 5 minutes (packages don't install every second)
            try:
                self._import_log()
            except Exception as e:
                logger.error(f"PackageCollector run error: {e}")

    def stop(self):
        self.running = False


