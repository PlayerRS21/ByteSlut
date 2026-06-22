"""
daemon/main.py — ByteSlut Main Daemon
=======================================
This is the entry point for the background collector daemon.
It starts ALL collectors as threads and keeps them running.

ARCHITECTURE:
  Each collector runs in its own thread (or process for heavy ones).
  They all share one BatchWriter which flushes to SQLite every 30 seconds.
  
  Thread model:
  ┌─────────────────────────────────────────┐
  │  Main Thread (this file)                │
  │  - Starts all collector threads         │
  │  - Handles SIGTERM/SIGINT gracefully    │
  │  - Monitors collector health            │
  └─────────────────────────────────────────┘
         │ spawns
  ┌──────┴──────────────────────────────────────────────────┐
  │ Threads:                                                │
  │  SessionCollector    → screen time, idle, sessions      │
  │  AppCollector        → active window, foreground time   │
  │  SystemStatsCollector→ CPU, RAM, disk, temps            │
  │  BrowserCollector    → browser history, YouTube         │
  │  NotificationCollector → DBus notifications             │
  │  CommandCollector    → terminal commands, exit codes    │
  │  NetworkCollector    → per-app bandwidth                │
  │  BatteryCollector    → battery health                   │
  │  PackageCollector    → pacman log                       │
  │  InputCollector      → keystrokes, mouse                │
  │  ProductivityCollector→ score, focus streaks            │
  │  BatchWriter         → flushes to SQLite every 30s      │
  └────────────────────────────────────────────────────────-┘

TO RUN:
  python -m daemon.main
  OR via systemd service (recommended — auto-starts on boot)
"""

import os
import sys
import json
import time
import signal
import logging
import threading
from pathlib import Path

# Add parent directory to path so imports work
sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Logging Setup ──
# We log to both a file and the systemd journal (if available)
def setup_logging():
    log_dir = Path.home() / ".local/share/byteslut/logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "daemon.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ]
    )

setup_logging()
logger = logging.getLogger("byteslut.daemon")


def load_config():
    """Load settings from config/settings.json."""
    config_path = Path(__file__).parent.parent / "config" / "settings.json"
    try:
        with open(config_path) as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Could not load config: {e}. Using defaults.")
        return {
            "collection_interval_seconds": 30,
            "idle_threshold_seconds": 300,
            "privacy": {
                "track_keystrokes": True,
                "track_browser_history": True,
                "track_notifications": True,
                "track_commands": True,
            }
        }


class ByteSlutDaemon:
    """
    The main daemon class. Manages all collector threads.
    """

    def __init__(self):
        self.config = load_config()
        self.collectors = []   # List of collector instances
        self.threads = []      # List of running threads
        self.running = False

    def setup_database(self):
        """Initialize the database schema."""
        from daemon.db import initialize_database
        logger.info("Initializing database...")
        initialize_database()
        logger.info("Database ready")

    def start_collectors(self):
        """
        Start all collector threads.
        Each collector has a .run() method that loops forever.
        We wrap each in a thread with error recovery.
        """
        from daemon.db import batch_writer

        interval = self.config.get("collection_interval_seconds", 30)
        idle_threshold = self.config.get("idle_threshold_seconds", 300)
        privacy = self.config.get("privacy", {})

        # ── List of (CollectorClass, kwargs) to start ──
        # We build this list based on config privacy settings

        from daemon.collectors.session import SessionCollector
        from daemon.collectors.apps import AppCollector
        from daemon.collectors.cpu_ram import SystemStatsCollector

        # Split extras.py collectors for import
        # (they're in one file but are separate classes)
        import importlib.util, types

        collector_configs = [
            (SessionCollector, {"batch_writer": batch_writer, "idle_threshold": idle_threshold}),
            (AppCollector, {"batch_writer": batch_writer}),
            (SystemStatsCollector, {"batch_writer": batch_writer, "interval": interval}),
        ]

        # ── Load extras module (now split into individual modular files) ──
        # Each collector lives in its own file for easy updating/replacement.
        # To update just notification tracking: edit daemon/collectors/notifications.py
        # To update just battery tracking: edit daemon/collectors/battery.py
        # etc. — no risk of breaking other collectors.
        try:
            from daemon.collectors.notifications import NotificationCollector
            from daemon.collectors.commands     import CommandCollector
            from daemon.collectors.network      import NetworkCollector
            from daemon.collectors.battery      import BatteryCollector
            from daemon.collectors.packages     import PackageCollector
            from daemon.collectors.input_tracking import InputCollector

            if privacy.get("track_notifications", True):
                collector_configs.append((NotificationCollector, {"batch_writer": batch_writer}))

            if privacy.get("track_commands", True):
                collector_configs.append((CommandCollector, {"batch_writer": batch_writer}))

            collector_configs.append((NetworkCollector, {"batch_writer": batch_writer, "interval": 60}))
            collector_configs.append((BatteryCollector, {"batch_writer": batch_writer, "interval": 300}))
            collector_configs.append((PackageCollector, {"batch_writer": batch_writer}))

            if privacy.get("track_keystrokes", True):
                collector_configs.append((InputCollector, {"batch_writer": batch_writer, "interval": 30}))

        except ImportError as e:
            logger.error(f"Could not import modular collectors: {e}")
            # Fallback: try old extras.py if modular files aren't present
            try:
                from daemon.collectors.extras import (
                    NotificationCollector, CommandCollector, NetworkCollector,
                    BatteryCollector, PackageCollector, InputCollector,
                )
                logger.warning("Fell back to extras.py — consider running update.sh")
                if privacy.get("track_notifications", True):
                    collector_configs.append((NotificationCollector, {"batch_writer": batch_writer}))
                if privacy.get("track_commands", True):
                    collector_configs.append((CommandCollector, {"batch_writer": batch_writer}))
                collector_configs.append((NetworkCollector, {"batch_writer": batch_writer, "interval": 60}))
                collector_configs.append((BatteryCollector, {"batch_writer": batch_writer, "interval": 300}))
                collector_configs.append((PackageCollector, {"batch_writer": batch_writer}))
                if privacy.get("track_keystrokes", True):
                    collector_configs.append((InputCollector, {"batch_writer": batch_writer, "interval": 30}))
            except ImportError as e2:
                logger.error(f"Fallback also failed: {e2}")

        # Load browser collector
        if privacy.get("track_browser_history", True):
            try:
                from daemon.collectors.browser import BrowserCollector
                collector_configs.append((BrowserCollector, {"batch_writer": batch_writer}))
            except ImportError as e:
                logger.error(f"Could not import BrowserCollector: {e}")

        # Load productivity collector
        try:
            from daemon.collectors.productivity import ProductivityCollector
            collector_configs.append((ProductivityCollector, {"batch_writer": batch_writer}))
        except ImportError as e:
            logger.warning(f"ProductivityCollector not available: {e}")

        # Load daily report scheduler
        try:
            from daemon.daily_report import DailyReportScheduler
            collector_configs.append((DailyReportScheduler, {}))
        except ImportError as e:
            logger.warning(f"DailyReportScheduler not available: {e}")

        # ── Start each collector in a thread ──
        for CollectorClass, kwargs in collector_configs:
            try:
                collector = CollectorClass(**kwargs)
                self.collectors.append(collector)

                # Wrap run() in error recovery loop
                def make_target(c):
                    def target():
                        while self.running:
                            try:
                                c.run()
                                # run() returned normally (not a crash).
                                # This happens when a collector has no work to do
                                # e.g. InputCollector finds no /dev/input devices,
                                # BrowserCollector finds no browser history files.
                                # Without this sleep, the loop spins at 100% CPU
                                # calling run() thousands of times per second.
                                # Sleep 60s before retrying — conditions may change
                                # (user plugs in a mouse, installs a browser, etc.)
                                if self.running:
                                    logger.debug(
                                        f"{c.__class__.__name__} run() returned "
                                        f"normally. Retrying in 60s..."
                                    )
                                    time.sleep(60)
                            except Exception as e:
                                logger.error(
                                    f"{c.__class__.__name__} crashed: {e}. "
                                    f"Restarting in 10s..."
                                )
                                time.sleep(10)
                    return target

                thread = threading.Thread(
                    target=make_target(collector),
                    name=CollectorClass.__name__,
                    daemon=True  # Dies when main program exits
                )
                thread.start()
                self.threads.append(thread)
                logger.info(f"Started {CollectorClass.__name__}")

            except Exception as e:
                logger.error(f"Failed to start {CollectorClass.__name__}: {e}")

        # Start the batch writer
        batch_writer.start()
        logger.info(f"BatchWriter started (flush every {batch_writer.flush_interval}s)")

    def stop(self):
        """Gracefully stop all collectors."""
        logger.info("Shutting down ByteSlut daemon...")
        self.running = False

        for collector in self.collectors:
            try:
                collector.stop()
            except Exception as e:
                logger.error(f"Error stopping {collector.__class__.__name__}: {e}")

        # Stop batch writer and do final flush
        from daemon.db import batch_writer
        batch_writer.stop()

        logger.info("ByteSlut daemon stopped. Goodbye! 👋")

    def run(self):
        """Main daemon loop."""
        self.running = True

        logger.info("=" * 50)
        logger.info("  ByteSlut Daemon Starting 🔥")
        logger.info("=" * 50)

        # Setup database
        self.setup_database()

        # Start all collectors
        self.start_collectors()

        logger.info(f"All {len(self.collectors)} collectors running. ByteSlut is watching... 👁️")

        # ── Signal handling for graceful shutdown ──
        # SIGTERM is sent by systemd when stopping the service
        # SIGINT is sent by Ctrl+C
        def signal_handler(sig, frame):
            logger.info(f"Received signal {sig}, shutting down...")
            self.stop()
            sys.exit(0)

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        # ── Health monitor loop ──
        # Runs every 10 seconds.
        # 1. Checks if any collector threads have died (restarts via error recovery above)
        # 2. Checks for the .db_wiped sentinel file written by the web server
        #    after the user wipes the database from Settings.
        #
        # WHY THE SENTINEL IS NEEDED:
        #   After a DB wipe all rows are deleted, but the daemon's collectors
        #   still hold stale in-memory state:
        #     - SessionCollector has current_session_id pointing to a deleted row.
        #       Its heartbeat UPDATE runs against a row that doesn't exist → silently
        #       does nothing → no new session ever gets created → recording stops.
        #     - AppCollector has today_accumulator filled with app times from before
        #       the wipe. On next flush it tries to INSERT/UPDATE rows that reference
        #       the deleted session.
        #     - BatchWriter has pending records in its queue that reference deleted rows.
        #   Simply deleting the DB rows is not enough — the in-memory state must be reset.
        #   Restarting the daemon is the user-visible workaround, but we can do it cleanly
        #   by signalling the relevant collectors to reinitialise themselves.

        sentinel = Path(__file__).parent / ".db_wiped"

        while self.running:
            # Check for DB wipe sentinel
            if sentinel.exists():
                try:
                    sentinel.unlink()  # Delete it first so we don't handle it twice
                    logger.warning("DB wipe detected — resetting collector state...")

                    # Reset BatchWriter queue so stale pre-wipe records don't get written
                    from daemon.db import batch_writer as bw
                    with bw.lock:
                        old_count = len(bw.pending)
                        bw.pending.clear()
                    logger.info(f"  Cleared {old_count} stale pending records from BatchWriter")

                    # Reset SessionCollector — end the stale session, start a fresh one
                    for collector in self.collectors:
                        cls = collector.__class__.__name__

                        if cls == "SessionCollector":
                            # Clear stale session ID so heartbeat doesn't UPDATE deleted row
                            collector.current_session_id = None
                            collector.session_start_time = None
                            collector.total_idle_seconds = 0.0
                            collector.is_idle           = False
                            collector.idle_start_time   = None
                            # Start a fresh session in the now-empty DB
                            collector.start_session(reason="post-wipe-reset")
                            logger.info("  SessionCollector reset — new session started")

                        elif cls == "AppCollector":
                            # Clear the per-day accumulator so stale app times don't leak
                            collector.today_accumulator.clear()
                            logger.info("  AppCollector accumulator cleared")

                        elif cls == "ProductivityCollector":
                            # Nothing to reset — it recalculates from DB on next run
                            pass

                    logger.info("Collector reset complete — recording resumes immediately")

                except Exception as e:
                    logger.error(f"Error during post-wipe reset: {e}")

            # Check if any threads died
            dead = [t for t in self.threads if not t.is_alive()]
            if dead:
                for t in dead:
                    logger.warning(f"Thread {t.name} died unexpectedly")
                self.threads = [t for t in self.threads if t.is_alive()]

            time.sleep(10)  # Check every 10s (was 60s — faster sentinel detection)


def main():
    """Entry point for the daemon."""
    daemon = ByteSlutDaemon()
    daemon.run()


if __name__ == "__main__":
    main()
