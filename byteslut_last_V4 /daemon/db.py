"""
db.py — ByteSlut Database Handler
==================================
This is the heart of ByteSlut's storage system.
We use SQLite with WAL (Write-Ahead Logging) mode which means:
  - Multiple things can READ the database at the same time
  - WRITES don't block reads (no freezing)
  - Data is safe even if the program crashes mid-write

HOW BATCH WRITING WORKS:
  Instead of writing to disk every second (which stresses old hardware),
  we collect data in RAM (a Python list) and flush it to disk every 30 seconds.
  This is called "batching" — think of it like saving a draft every 30s instead
  of every keystroke.

STORAGE ESTIMATE:
  ~5-20MB per year for all your tracking data. SQLite is incredibly efficient.
"""

import sqlite3
import os
import json
import threading
import time
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def get_db_path():
    """
    Figure out where to store the database file.
    We use ~/.local/share/byteslut/byteslut.db
    This is the standard Linux location for app data (XDG Base Directory spec).
    """
    # Load config to get custom data dir if set
    config_path = Path(__file__).parent.parent / "config" / "settings.json"
    try:
        with open(config_path) as f:
            config = json.load(f)
        data_dir = os.path.expanduser(config.get("data_dir", "~/.local/share/byteslut"))
        db_name = config.get("db_filename", "byteslut.db")
    except Exception:
        data_dir = os.path.expanduser("~/.local/share/byteslut")
        db_name = "byteslut.db"

    # Create the directory if it doesn't exist
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, db_name)


def get_connection():
    """
    Create a SQLite connection with optimal settings for our use case.
    WAL mode = Write-Ahead Logging (allows concurrent reads + writes)
    """
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, check_same_thread=False)

    # WAL mode: allows reading while writing simultaneously
    conn.execute("PRAGMA journal_mode=WAL")

    # Synchronous=NORMAL: safe but faster than FULL (default)
    # FULL waits for OS to confirm every write — too slow for us
    conn.execute("PRAGMA synchronous=NORMAL")

    # Cache size: keep 10MB of data in RAM to speed up queries
    conn.execute("PRAGMA cache_size=-10000")

    # Store temp tables in RAM instead of disk
    conn.execute("PRAGMA temp_store=MEMORY")

    conn.row_factory = sqlite3.Row  # Rows behave like dicts: row["column_name"]
    return conn


def _safe_add_column(cursor, table: str, column: str, col_type: str):
    """
    Add a column to an existing table only if it doesn't already exist.
    Used for safe DB migrations when upgrading from old schema versions.
    SQLite doesn't support IF NOT EXISTS on ALTER TABLE ADD COLUMN,
    so we check the existing columns first.
    """
    try:
        existing = [row[1] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in existing:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except Exception:
        pass  # Table might not exist yet — CREATE TABLE IF NOT EXISTS handles it


def initialize_database():
    """
    Create all tables if they don't exist yet.
    This is safe to run every time — IF NOT EXISTS means it won't overwrite data.

    TABLE DESIGN PHILOSOPHY:
    - Every table has a timestamp so we can query by day/month/year
    - We use INTEGER for timestamps (Unix epoch = seconds since Jan 1 1970)
      because it's smaller and faster than storing text dates
    - TEXT for app names (not foreign keys) so deleted apps are preserved forever
    """
    conn = get_connection()
    cursor = conn.cursor()

    # ─────────────────────────────────────────────
    # SESSION TABLE — tracks when you use your computer
    # ─────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_time INTEGER NOT NULL,       -- Unix timestamp when session started
            end_time INTEGER,                  -- NULL if session is still active
            duration_seconds INTEGER,          -- Calculated on session end
            idle_seconds INTEGER DEFAULT 0,    -- How long you were idle during session
            display_server TEXT,               -- 'wayland', 'x11', or 'tty'
            desktop_env TEXT                   -- 'hyprland', 'gnome', 'kde', etc.
        )
    """)

    # ─────────────────────────────────────────────
    # APP USAGE TABLE — time spent in each app
    # ─────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS app_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,        -- When this record was written
            date TEXT NOT NULL,                -- 'YYYY-MM-DD' for easy day queries
            app_name TEXT NOT NULL,            -- e.g. 'brave', 'kitty', 'code'
            window_title TEXT,                 -- The window title (e.g. YouTube tab name)
            foreground_seconds INTEGER DEFAULT 0,  -- Time app was in focus
            background_seconds INTEGER DEFAULT 0,  -- Time app ran in background
            is_flatpak INTEGER DEFAULT 0,      -- 1 if app is a Flatpak
            flatpak_id TEXT                    -- e.g. 'com.brave.Browser'
        )
    """)

    # Index for fast app queries (searching by app_name and date is very common)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_app_date ON app_usage(app_name, date)")

    # ─────────────────────────────────────────────
    # SYSTEM STATS TABLE — CPU, RAM, disk, temps
    # ─────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            date TEXT NOT NULL,
            cpu_percent REAL,                  -- CPU usage 0-100
            cpu_freq_mhz REAL,                 -- CPU frequency
            ram_percent REAL,                  -- RAM usage 0-100
            ram_used_mb REAL,                  -- RAM used in MB
            swap_percent REAL,                 -- Swap usage
            disk_read_mb REAL,                 -- MB read from disk since last sample
            disk_write_mb REAL,                -- MB written to disk since last sample
            cpu_temp REAL,                     -- CPU temperature in Celsius
            gpu_temp REAL                      -- GPU temperature in Celsius (if available)
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_stats_date ON system_stats(date)")

    # ─────────────────────────────────────────────
    # TEMPERATURE TABLE — daily min/max/avg temps
    # ─────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS temperature_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE NOT NULL,         -- One row per day
            cpu_min REAL,
            cpu_max REAL,
            cpu_avg REAL,
            gpu_min REAL,
            gpu_max REAL,
            gpu_avg REAL
        )
    """)

    # ─────────────────────────────────────────────
    # NETWORK TABLE — internet usage per app
    # ─────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS network_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            date TEXT NOT NULL,
            app_name TEXT NOT NULL,            -- Which app used the internet
            bytes_sent INTEGER DEFAULT 0,      -- Upload in bytes
            bytes_received INTEGER DEFAULT 0,  -- Download in bytes
            interface TEXT                     -- 'eth0', 'wlan0', etc.
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_net_date ON network_usage(date)")

    # ─────────────────────────────────────────────
    # BROWSER HISTORY TABLE — URLs, YouTube, etc.
    # ─────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS browser_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            date TEXT NOT NULL,
            browser TEXT NOT NULL,             -- 'brave', 'firefox', 'chrome'
            url TEXT,                          -- Full URL visited
            title TEXT,                        -- Page title
            domain TEXT,                       -- Just 'youtube.com' extracted from URL
            is_youtube INTEGER DEFAULT 0,      -- 1 if it's a YouTube video
            youtube_title TEXT,                -- Video title if YouTube
            visit_duration_seconds INTEGER,    -- How long on this page (estimated)
            is_flatpak INTEGER DEFAULT 0       -- 1 if browser is a Flatpak
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_browser_date ON browser_history(date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_browser_domain ON browser_history(domain)")

    # ─────────────────────────────────────────────
    # NOTIFICATIONS TABLE — every notification received
    # ─────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            date TEXT NOT NULL,
            app_name TEXT NOT NULL,            -- App that sent the notification
            summary TEXT,                      -- Notification title/summary
            body TEXT,                         -- Notification body text
            action TEXT DEFAULT 'unknown',     -- 'clicked', 'dismissed', 'expired', 'ignored'
            urgency TEXT                       -- 'low', 'normal', 'critical'
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notif_date ON notifications(date)")

    # ─────────────────────────────────────────────
    # COMMANDS TABLE — terminal command history
    # ─────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            date TEXT NOT NULL,
            command TEXT NOT NULL,             -- The full command typed
            exit_code INTEGER,                 -- 0 = success, anything else = error
            working_directory TEXT,            -- Where the command was run
            shell TEXT,                        -- 'bash', 'zsh', 'fish'
            is_sudo INTEGER DEFAULT 0,         -- 1 if command started with sudo
            duration_seconds REAL              -- How long the command took
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cmd_date ON commands(date)")

    # ─────────────────────────────────────────────
    # INPUT STATS TABLE — keyboard/mouse activity
    # ─────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS input_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            date TEXT NOT NULL,
            keystrokes INTEGER DEFAULT 0,      -- Number of keys pressed
            mouse_clicks INTEGER DEFAULT 0,    -- Number of mouse clicks
            mouse_scroll_events INTEGER DEFAULT 0,
            mouse_distance_px INTEGER DEFAULT 0,  -- Mouse travel in pixels
            wpm_sample REAL                    -- Words per minute (rolling sample)
        )
    """)

    # ─────────────────────────────────────────────
    # BATTERY TABLE — battery health over time
    # ─────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS battery_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            date TEXT NOT NULL,
            percent REAL,                      -- Battery percentage 0-100
            is_plugged INTEGER DEFAULT 0,      -- 1 if charging
            voltage_v REAL,                    -- Battery voltage
            temperature REAL,                  -- Battery temp if available
            charge_cycles INTEGER,             -- Total charge cycles (from sysfs)
            capacity_design_mwh INTEGER,       -- Original design capacity
            capacity_full_mwh INTEGER          -- Current max capacity (health indicator)
        )
    """)

    # ─────────────────────────────────────────────
    # PACKAGES TABLE — pacman install/remove/update log
    # ─────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS packages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            date TEXT NOT NULL,
            action TEXT NOT NULL,              -- 'installed', 'removed', 'upgraded'
            package_name TEXT NOT NULL,
            old_version TEXT,                  -- Previous version (for upgrades)
            new_version TEXT,                  -- New version
            reason TEXT                        -- 'explicit' (manual) or 'dependency'
        )
    """)

    # ─────────────────────────────────────────────
    # PRODUCTIVITY TABLE — daily scores
    # ─────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS productivity (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp              INTEGER,
            date                   TEXT NOT NULL,
            productivity_score     REAL,           -- 0-100 confidence-weighted score
            raw_score_weighted     REAL,           -- raw weighted average (1-10 scale)
            dominant_category      TEXT,           -- most common category this interval
            detected_role          TEXT,           -- developer/designer/writer/unknown
            sample_count           INTEGER DEFAULT 0,
            total_confidence       REAL DEFAULT 0,
            category_breakdown     TEXT,           -- JSON: {category: count}
            top_domains            TEXT,           -- JSON: [[domain, count], ...]
            coding_samples         INTEGER DEFAULT 0,
            learning_samples       INTEGER DEFAULT 0,
            ai_samples             INTEGER DEFAULT 0,
            neutral_samples        INTEGER DEFAULT 0,
            entertainment_samples  INTEGER DEFAULT 0
        )
    """)
    # Safe migration: add new columns to existing installs that have the old schema
    _safe_add_column(cursor, "productivity", "timestamp",             "INTEGER")
    _safe_add_column(cursor, "productivity", "raw_score_weighted",    "REAL")
    _safe_add_column(cursor, "productivity", "dominant_category",     "TEXT")
    _safe_add_column(cursor, "productivity", "detected_role",         "TEXT")
    _safe_add_column(cursor, "productivity", "sample_count",          "INTEGER DEFAULT 0")
    _safe_add_column(cursor, "productivity", "total_confidence",      "REAL DEFAULT 0")
    _safe_add_column(cursor, "productivity", "category_breakdown",    "TEXT")
    _safe_add_column(cursor, "productivity", "top_domains",           "TEXT")
    _safe_add_column(cursor, "productivity", "coding_samples",        "INTEGER DEFAULT 0")
    _safe_add_column(cursor, "productivity", "learning_samples",      "INTEGER DEFAULT 0")
    _safe_add_column(cursor, "productivity", "ai_samples",            "INTEGER DEFAULT 0")
    _safe_add_column(cursor, "productivity", "neutral_samples",       "INTEGER DEFAULT 0")
    _safe_add_column(cursor, "productivity", "entertainment_samples", "INTEGER DEFAULT 0")

    # ── CRITICAL MIGRATION: remove UNIQUE constraint from productivity.date ──
    #
    # The old schema had:  date TEXT UNIQUE NOT NULL
    # The new collector writes MULTIPLE rows per day (one every 60 seconds).
    # Every second INSERT fires "UNIQUE constraint failed: productivity.date".
    # BatchWriter wraps all pending records in ONE transaction, so when
    # productivity fails, the ROLLBACK discards EVERYTHING in that batch —
    # input_stats, app_usage, browser_history, all of it. Nothing ever writes.
    # This is why "recording stops" even though the daemon is running fine.
    #
    # SQLite cannot ALTER TABLE to remove a constraint. The only way is:
    #   1. Create a new table with the correct schema (no UNIQUE on date)
    #   2. Copy all existing data into it
    #   3. Drop the old table
    #   4. Rename the new table to 'productivity'
    # We detect this by checking if the CREATE TABLE SQL contains 'UNIQUE' on date.
    try:
        row = cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='productivity'"
        ).fetchone()
        if row and row[0] and "date TEXT UNIQUE" in row[0]:
            # Old schema detected — rebuild the table without the UNIQUE constraint
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS productivity_new (
                    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp              INTEGER,
                    date                   TEXT NOT NULL,
                    productivity_score     REAL,
                    raw_score_weighted     REAL,
                    dominant_category      TEXT,
                    detected_role          TEXT,
                    sample_count           INTEGER DEFAULT 0,
                    total_confidence       REAL DEFAULT 0,
                    category_breakdown     TEXT,
                    top_domains            TEXT,
                    coding_samples         INTEGER DEFAULT 0,
                    learning_samples       INTEGER DEFAULT 0,
                    ai_samples             INTEGER DEFAULT 0,
                    neutral_samples        INTEGER DEFAULT 0,
                    entertainment_samples  INTEGER DEFAULT 0
                )
            """)
            # Copy existing rows — keep the old summary data, just lose the constraint
            cursor.execute("""
                INSERT INTO productivity_new
                    (id, timestamp, date, productivity_score, raw_score_weighted,
                     dominant_category, detected_role, sample_count, total_confidence,
                     category_breakdown, top_domains, coding_samples, learning_samples,
                     ai_samples, neutral_samples, entertainment_samples)
                SELECT id, timestamp, date, productivity_score, raw_score_weighted,
                       dominant_category, detected_role, sample_count, total_confidence,
                       category_breakdown, top_domains, coding_samples, learning_samples,
                       ai_samples, neutral_samples, entertainment_samples
                FROM productivity
            """)
            cursor.execute("DROP TABLE productivity")
            cursor.execute("ALTER TABLE productivity_new RENAME TO productivity")
            import logging as _log
            _log.getLogger(__name__).info(
                "Migrated productivity table: removed UNIQUE constraint from 'date'. "
                "Recording will now work correctly."
            )
    except Exception as _e:
        import logging as _log
        _log.getLogger(__name__).warning(f"productivity migration check failed: {_e}")

    # ─────────────────────────────────────────────
    # DAILY ROLES TABLE — one role per day, detected by analyzer.py
    # ─────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_roles (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            date         TEXT UNIQUE NOT NULL,
            role_name    TEXT,
            description  TEXT,
            emoji        TEXT,
            color        TEXT,
            role_score   REAL,
            features     TEXT,
            alternatives TEXT
        )
    """)

    # ─────────────────────────────────────────────
    # BOOT TIMES TABLE — how long each boot takes
    # ─────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS boot_times (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            date TEXT NOT NULL,
            boot_duration_seconds REAL,        -- Total boot time
            kernel_version TEXT,               -- Which kernel booted
            userspace_seconds REAL             -- Time after kernel to desktop
        )
    """)

    # ─────────────────────────────────────────────
    # APP REGISTRY — permanent record of all apps ever seen
    # Even after deletion, the app stays here forever
    # ─────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS app_registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_name TEXT UNIQUE NOT NULL,
            first_seen TEXT NOT NULL,          -- Date first tracked
            last_seen TEXT,                    -- Date last tracked
            is_flatpak INTEGER DEFAULT 0,
            flatpak_id TEXT,
            category TEXT,                     -- 'work', 'entertainment', etc.
            is_deleted INTEGER DEFAULT 0,      -- 1 if app no longer installed
            total_foreground_seconds INTEGER DEFAULT 0,
            total_background_seconds INTEGER DEFAULT 0
        )
    """)

    conn.commit()
    conn.close()
    logger.info("Database initialized successfully")


class BatchWriter:
    """
    BATCH WRITER — The performance secret weapon.

    Instead of hitting the disk every second, we collect records in RAM
    and write them all at once every 30 seconds (configurable).

    Think of it like this:
    - BAD:  Write 1 record → disk → Write 1 record → disk → (1000x per minute)
    - GOOD: Collect 1000 records in RAM → Write ALL at once → disk (2x per minute)

    This is much gentler on old hardware and SSDs/HDDs alike.
    """

    def __init__(self, flush_interval=30):
        self.flush_interval = flush_interval  # Seconds between disk writes
        self.pending = []                      # Buffer: list of (table, data_dict) tuples
        self.lock = threading.Lock()           # Thread safety — multiple collectors write simultaneously
        self.running = False
        self.thread = None

    def add(self, table: str, data: dict):
        """
        Add a record to the pending buffer.
        This is called by all the collectors constantly.
        It's very fast — just appending to a list in RAM.
        """
        # Auto-add timestamp and date if not provided
        now = int(time.time())
        if "timestamp" not in data:
            data["timestamp"] = now
        if "date" not in data:
            data["date"] = datetime.fromtimestamp(now).strftime("%Y-%m-%d")

        with self.lock:
            self.pending.append((table, data))

    def flush(self):
        """
        Write all pending records to SQLite.

        RESILIENT FLUSH — each record is written independently.
        A bad record (wrong columns, UNIQUE violation, etc.) is logged and
        skipped. It does NOT roll back the entire batch and lose everyone
        else's data. This is the critical fix for "recording stops silently":
        previously one bad productivity INSERT would discard the entire
        30-second batch including input_stats, app_usage, and browser_history.
        """
        with self.lock:
            if not self.pending:
                return
            batch = self.pending.copy()
            self.pending.clear()

        if not batch:
            return

        conn = get_connection()
        ok = 0
        skipped = 0
        try:
            conn.execute("BEGIN TRANSACTION")
            for table, data in batch:
                try:
                    columns      = ", ".join(data.keys())
                    placeholders = ", ".join(["?" for _ in data])
                    conn.execute(
                        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                        list(data.values())
                    )
                    ok += 1
                except Exception as e:
                    # Log the first few failures so the user can diagnose them,
                    # but don't abort — keep writing the rest of the batch.
                    skipped += 1
                    if skipped <= 3:
                        logger.warning(
                            f"Skipped bad record for '{table}': {e} "
                            f"(columns: {list(data.keys())[:5]})"
                        )
            conn.execute("COMMIT")
            if skipped > 0:
                logger.warning(f"Batch flush: wrote {ok}, skipped {skipped} bad records")
            else:
                logger.debug(f"Flushed {ok} records to database")
        except Exception as e:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            logger.error(f"Batch flush failed entirely: {e}")
        finally:
            conn.close()

    def start(self):
        """Start the background flush thread."""
        self.running = True
        self.thread = threading.Thread(target=self._flush_loop, daemon=True)
        self.thread.start()
        logger.info(f"BatchWriter started (flush every {self.flush_interval}s)")

    def stop(self):
        """Stop the flush thread and do a final flush."""
        self.running = False
        self.flush()  # Final flush before shutdown
        logger.info("BatchWriter stopped and final flush completed")

    def _flush_loop(self):
        """Background thread that flushes every N seconds."""
        while self.running:
            time.sleep(self.flush_interval)
            try:
                self.flush()
            except Exception as e:
                logger.error(f"Flush loop error: {e}")


def query(sql: str, params: tuple = (), fetch: str = "all"):
    """
    Run a SELECT query and return results.

    Args:
        sql:    The SQL query string
        params: Values to safely insert (prevents SQL injection)
        fetch:  'all' = all rows, 'one' = first row only

    Example:
        rows = query("SELECT * FROM app_usage WHERE date = ?", ("2024-01-15",))
    """
    conn = get_connection()
    try:
        cursor = conn.execute(sql, params)
        if fetch == "one":
            row = cursor.fetchone()
            return dict(row) if row else None
        else:
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Query failed: {e}\nSQL: {sql}")
        return [] if fetch == "all" else None
    finally:
        conn.close()


def execute(sql: str, params: tuple = ()):
    """
    Run an INSERT, UPDATE, or DELETE query directly (not batched).
    Use this for immediate writes like updating app_registry.
    """
    conn = get_connection()
    try:
        conn.execute(sql, params)
        conn.commit()
    except Exception as e:
        logger.error(f"Execute failed: {e}")
    finally:
        conn.close()


# Global batch writer instance — shared across all collectors
# Import this in your collector files: from daemon.db import batch_writer
batch_writer = BatchWriter(flush_interval=30)
