"""
collectors/session.py — Screen Time & Session Tracker
=======================================================

SCREEN TIME vs ACTIVE TIME:
  Screen Time = total time the computer was ON (lid open, not suspended)
  Active Time  = screen time MINUS idle time (you were actually using it)

  Example:
    Use 10 min → close lid (sleep 3h) → open, use 20 min
    Screen Time  = 30m  ← the 3h sleep is NOT counted
    Active Time  = ~30m (you were active the whole time)

SUSPEND DETECTION — two layers:
  Layer 1: DBus PrepareForSleep fires BEFORE the freeze → perfect timestamp
  Layer 2: Clock-jump detection in heartbeat loop → fallback when DBus missing

  Both run simultaneously. Whichever fires first wins. The other is a no-op.
"""

import os
import time
import subprocess
import threading
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def detect_display_server():
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "tty"


def detect_desktop_env():
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if desktop:
        return desktop
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "hyprland"
    if os.environ.get("SWAYSOCK"):
        return "sway"
    try:
        r = subprocess.run(
            ["pgrep", "-x", "hyprland,sway,gnome-shell,kwin_wayland,openbox,i3,bspwm"],
            capture_output=True, text=True, timeout=2
        )
        if r.stdout.strip():
            return r.stdout.strip().split("\n")[0].split()[-1].lower()
    except Exception:
        pass
    return "unknown"


def get_idle_seconds():
    """
    How long has the user been idle (no keyboard/mouse input)?
    Returns 0 as fallback — meaning "assume active".
    """
    # X11: xprintidle is the most accurate method
    if os.environ.get("DISPLAY"):
        try:
            r = subprocess.run(
                ["xprintidle"], capture_output=True, text=True, timeout=2
            )
            if r.returncode == 0:
                return int(r.stdout.strip()) / 1000.0  # ms → seconds
        except (FileNotFoundError, ValueError):
            pass

    # Wayland/any: loginctl idle hint + exact duration
    try:
        r = subprocess.run(
            ["loginctl", "show-session", "self",
             "--property=IdleHint,IdleSinceHintMonotonic"],
            capture_output=True, text=True, timeout=2
        )
        if "IdleHint=yes" in r.stdout:
            for line in r.stdout.splitlines():
                if line.startswith("IdleSinceHintMonotonic="):
                    try:
                        import ctypes
                        since_us = int(line.split("=")[1])
                        if since_us > 0:
                            class _ts(ctypes.Structure):
                                _fields_ = [("tv_sec", ctypes.c_long),
                                            ("tv_nsec", ctypes.c_long)]
                            ts = _ts()
                            ctypes.CDLL("librt.so.1").clock_gettime(
                                1, ctypes.byref(ts)
                            )
                            mono_us = (ts.tv_sec * 1_000_000
                                       + ts.tv_nsec // 1000)
                            return max(0.0, (mono_us - since_us) / 1_000_000.0)
                    except Exception:
                        pass
            return 300.0  # idle but unknown duration — return threshold
    except Exception:
        pass

    return 0.0


class SessionCollector:
    """
    Tracks screen-time sessions.
    One session = one uninterrupted block of use (lid open, not suspended).
    """

    # If the heartbeat loop sees a gap bigger than this, system was suspended
    SUSPEND_GAP_SECONDS = 300  # must be > heartbeat interval (60s)

    def __init__(self, batch_writer, idle_threshold=300):
        self.batch_writer          = batch_writer
        self.idle_threshold        = idle_threshold
        self.current_session_id    = None
        self.session_start_time    = None
        self.is_idle               = False
        self.idle_start_time       = None
        self.total_idle_seconds    = 0.0
        self.running               = False
        self.display_server        = detect_display_server()
        self.desktop_env           = detect_desktop_env()

        # Clock-jump detection state
        self._last_tick            = None
        # Set by DBus handler so clock-jump doesn't double-handle same event
        self._dbus_handled         = False

        logger.info(
            f"SessionCollector: DE={self.desktop_env} "
            f"display={self.display_server}"
        )

    # ─────────────────────────────────────────
    # Session lifecycle
    # ─────────────────────────────────────────

    def start_session(self, reason="startup"):
        """
        Insert a new session row.
        CRITICAL: INSERT and last_insert_rowid() must use the SAME connection.
        Using two separate connections makes rowid return 0 (SQLite per-connection).
        """
        from daemon.db import get_connection

        self.session_start_time  = int(time.time())
        self.total_idle_seconds  = 0.0
        self.is_idle             = False
        self.idle_start_time     = None
        self._last_tick          = time.time()
        self._dbus_handled       = False

        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO sessions "
                "(start_time, display_server, desktop_env, idle_seconds) "
                "VALUES (?, ?, ?, 0)",
                (self.session_start_time, self.display_server, self.desktop_env)
            )
            row = conn.execute(
                "SELECT last_insert_rowid() as id"
            ).fetchone()
            self.current_session_id = row["id"] if row else None
            conn.commit()
        except Exception as e:
            logger.error(f"start_session INSERT failed: {e}")
            self.current_session_id = None
        finally:
            conn.close()

        logger.info(
            f"[Session {self.current_session_id}] started "
            f"reason={reason} "
            f"at {datetime.fromtimestamp(self.session_start_time).strftime('%H:%M:%S')}"
        )

    def end_session(self, reason="normal", forced_end_time=None):
        """
        Write final duration + idle seconds to DB.
        Idempotent — safe to call multiple times (no-op after first call).
        forced_end_time lets us back-date the end (e.g. to pre-suspend time).
        """
        from daemon.db import execute

        if self.current_session_id is None:
            return

        # Count any pending idle time
        if self.is_idle and self.idle_start_time:
            self.total_idle_seconds += time.time() - self.idle_start_time

        end_time = forced_end_time or int(time.time())
        duration = max(0, end_time - (self.session_start_time or end_time))

        execute(
            "UPDATE sessions "
            "SET end_time=?, duration_seconds=?, idle_seconds=? "
            "WHERE id=?",
            (end_time, duration,
             int(self.total_idle_seconds),
             self.current_session_id)
        )

        logger.info(
            f"[Session {self.current_session_id}] ended "
            f"reason={reason} "
            f"duration={duration//3600}h{(duration%3600)//60}m{duration%60}s "
            f"idle={int(self.total_idle_seconds)}s"
        )
        self.current_session_id = None
        self.session_start_time = None

    def heartbeat_update(self):
        """
        Called every 60 seconds.
        1. Writes current elapsed time to DB so dashboard shows live data.
        2. Detects suspend via clock jump (fallback for when DBus misses it).

        CLOCK JUMP DETECTION:
          Heartbeat normally runs every 60s. If time.time() jumped by
          more than SUSPEND_GAP_SECONDS since last call, the system was
          frozen (suspended). We end the old session with pre-sleep timestamp
          and start a fresh one.
        """
        from daemon.db import execute

        now = time.time()

        # ── Suspend detection ─────────────────────────────────────────
        if self._last_tick is not None and not self._dbus_handled:
            gap = now - self._last_tick
            if gap > self.SUSPEND_GAP_SECONDS:
                logger.info(
                    f"Clock jump detected: {gap:.0f}s between heartbeats "
                    f"→ system was suspended. Ending session, starting new."
                )
                # End session at the time BEFORE the sleep (last_tick)
                self.end_session(
                    reason="suspend-clock-jump",
                    forced_end_time=int(self._last_tick)
                )
                self.start_session(reason="resume-clock-jump")
                return  # start_session already reset _last_tick

        self._dbus_handled = False
        self._last_tick    = now

        # ── Normal heartbeat write ────────────────────────────────────
        if self.current_session_id is None or self.session_start_time is None:
            return

        duration = int(now) - self.session_start_time

        execute(
            "UPDATE sessions SET duration_seconds=?, idle_seconds=? "
            "WHERE id=?",
            (duration, int(self.total_idle_seconds), self.current_session_id)
        )
        logger.debug(
            f"[Session {self.current_session_id}] heartbeat "
            f"{duration//60}m elapsed, {int(self.total_idle_seconds)}s idle"
        )

    # ─────────────────────────────────────────
    # Idle tracking
    # ─────────────────────────────────────────

    def check_idle(self):
        """
        Check if user is idle and accumulate idle time.
        Called every 10 seconds from main loop.
        """
        idle = get_idle_seconds()

        if idle >= self.idle_threshold:
            if not self.is_idle:
                # Just became idle
                self.is_idle       = True
                self.idle_start_time = time.time()
                logger.debug(f"Idle started (system idle={idle:.0f}s)")
        else:
            if self.is_idle:
                # Just returned from idle
                elapsed = time.time() - (self.idle_start_time or time.time())
                self.total_idle_seconds += elapsed
                self.is_idle            = False
                self.idle_start_time    = None
                logger.debug(
                    f"Idle ended after {elapsed:.0f}s "
                    f"(total idle this session: {self.total_idle_seconds:.0f}s)"
                )

    # ─────────────────────────────────────────
    # Main loop
    # ─────────────────────────────────────────

    def run(self):
        """
        Main collector loop.
        First injects Hyprland/Wayland env vars if systemd didn't pass them.
        """
        self.running     = True
        self._last_tick  = time.time()

        # Inject environment from running Hyprland process
        # so DBus and display detection work when started by systemd
        try:
            from daemon.collectors.apps import _inject_wayland_environment
            _inject_wayland_environment()
            # Re-detect after injection
            self.display_server = detect_display_server()
            self.desktop_env    = detect_desktop_env()
            logger.info(
                f"Environment after injection: "
                f"DE={self.desktop_env} display={self.display_server} "
                f"WAYLAND={os.environ.get('WAYLAND_DISPLAY','<none>')} "
                f"HYPR={os.environ.get('HYPRLAND_INSTANCE_SIGNATURE','<none>')[:12] if os.environ.get('HYPRLAND_INSTANCE_SIGNATURE') else '<none>'}"
            )
        except Exception as e:
            logger.debug(f"Environment injection skipped: {e}")

        self.start_session(reason="daemon-start")

        # Start DBus watcher in background (instant suspend/lock signals)
        dbus_thread = threading.Thread(
            target=self._watch_logind,
            daemon=True,
            name="byteslut-logind-watcher"
        )
        dbus_thread.start()

        last_heartbeat = time.time()

        while self.running:
            try:
                self.check_idle()

                if time.time() - last_heartbeat >= 60:
                    self.heartbeat_update()
                    last_heartbeat = time.time()

            except Exception as e:
                logger.error(f"Session loop error: {e}")

            time.sleep(10)

    def stop(self):
        self.running = False
        self.end_session(reason="daemon-stop")

    # ─────────────────────────────────────────
    # DBus watcher (instant events)
    # ─────────────────────────────────────────

    def _watch_logind(self):
        """
        Listen on DBus for:
          PrepareForSleep(true/false)  → suspend / resume
          ScreenSaver Locked/Unlocked  → screen lock / unlock
          GNOME ActiveChanged          → GNOME screensaver

        This is the fast path. Clock-jump detection is the fallback.
        Both run simultaneously — whichever fires first handles the event.
        """
        try:
            import dbus
            from dbus.mainloop.glib import DBusGMainLoop
            from gi.repository import GLib

            DBusGMainLoop(set_as_default=True)

            # System bus for suspend/resume
            system_bus = dbus.SystemBus()
            system_bus.add_signal_receiver(
                self._on_prepare_sleep,
                signal_name="PrepareForSleep",
                dbus_interface="org.freedesktop.login1.Manager",
                bus_name="org.freedesktop.login1",
                path="/org/freedesktop/login1",
            )

            # Session bus for screen lock
            try:
                session_bus = dbus.SessionBus()
                session_bus.add_signal_receiver(
                    self._on_locked,
                    signal_name="Locked",
                    dbus_interface="org.freedesktop.ScreenSaver",
                )
                session_bus.add_signal_receiver(
                    self._on_unlocked,
                    signal_name="Unlocked",
                    dbus_interface="org.freedesktop.ScreenSaver",
                )
                # GNOME screensaver
                session_bus.add_signal_receiver(
                    self._on_gnome_screensaver,
                    signal_name="ActiveChanged",
                    dbus_interface="org.gnome.ScreenSaver",
                )
            except Exception as e:
                logger.debug(f"Session bus signals not available: {e}")

            logger.info("DBus logind watcher ready")
            GLib.MainLoop().run()

        except ImportError:
            logger.warning(
                "dbus-python or PyGObject not installed — using clock-jump "
                "detection only. Install: sudo pacman -S python-dbus python-gobject"
            )
        except Exception as e:
            logger.warning(
                f"DBus watcher failed: {e} — "
                "clock-jump detection still active as fallback"
            )

    def _on_prepare_sleep(self, going_to_sleep):
        """
        PrepareForSleep(true)  = about to suspend → end session NOW
        PrepareForSleep(false) = just woke up     → start new session
        """
        if going_to_sleep:
            logger.info("DBus: PrepareForSleep(true) → ending session before suspend")
            self._dbus_handled = True
            self.end_session(reason="suspend-dbus")
        else:
            logger.info("DBus: PrepareForSleep(false) → resumed, starting new session")
            self._dbus_handled = True
            self.start_session(reason="resume-dbus")

    def _on_locked(self, *args):
        logger.info("DBus: Screen locked → ending session")
        self._dbus_handled = True
        self.end_session(reason="screen-locked")

    def _on_unlocked(self, *args):
        logger.info("DBus: Screen unlocked → starting new session")
        self._dbus_handled = True
        self.start_session(reason="screen-unlocked")

    def _on_gnome_screensaver(self, active, *args):
        if active:
            self._on_locked()
        else:
            self._on_unlocked()
