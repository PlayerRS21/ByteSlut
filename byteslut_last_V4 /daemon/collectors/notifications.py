"""
collectors/notifications.py — Notification Tracker
====================================================
Hooks into the system notification daemon via DBus.
Works with: swaync, dunst, mako, and any freedesktop-compatible notification daemon.

HOW DBUS NOTIFICATIONS WORK:
  All desktop notifications on Linux go through a DBus interface called
  "org.freedesktop.Notifications". When ANY app sends a notification
  (WhatsApp, Discord, system alerts, etc.), it calls the Notify METHOD.

  IMPORTANT — METHOD vs SIGNAL (this was the core bug in the original code):
  ──────────────────────────────────────────────────────────────────────────
  DBus has two types of messages:
    • METHOD CALLS  → one app asks another to DO something  (like a function call)
    • SIGNALS       → one app BROADCASTS that something happened

  "Notify" (sending a notification) is a METHOD CALL — not a signal.
  The original code used add_signal_receiver("Notify") which listens for SIGNALS.
  This SILENTLY DID NOTHING — notifications were NEVER captured.

  The correct approach is to use `dbus-monitor` as a subprocess.
  dbus-monitor is a standard tool that can eavesdrop on all DBus traffic
  including method calls, and outputs them as human-readable text we parse.

  NotificationClosed and ActionInvoked ARE real signals — those worked fine.

WHAT WE CAPTURE:
  - App name (which app sent it)
  - Summary (notification title)
  - Body (notification text/preview)
  - Exact timestamp
  - Action (dismissed / expired / clicked) — via NotificationClosed signal

SETUP (one time):
  Make sure dbus-monitor is installed (it's part of dbus package on Arch):
    which dbus-monitor   # should print a path

  If running under Wayland (sway/hyprland), DBUS_SESSION_BUS_ADDRESS must be set.
  Usually it's set automatically if you launch from your compositor's startup.
"""

import re
import time
import subprocess
import threading
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class NotificationCollector:
    """
    Listens for all system notifications by parsing dbus-monitor output.

    Why dbus-monitor instead of dbus Python API?
      Because "Notify" is a METHOD CALL, not a SIGNAL.
      Python's dbus.add_signal_receiver() only catches SIGNALS — it will
      never fire for Notify method calls. dbus-monitor catches everything.

    This runs forever: dbus-monitor blocks reading DBus, we parse each line.
    """

    def __init__(self, batch_writer):
        self.batch_writer = batch_writer
        self.running = False

        # Track pending notifications by ID so we can update their action later
        # Format: { notification_id(int): { "timestamp": int, "app": str, ... } }
        self.pending = {}

        # Lock to protect self.pending (written by both the monitor thread
        # and the signal listener thread)
        self._lock = threading.Lock()

    def run(self):
        """
        Start capturing notifications. Blocks forever until stop() is called.

        Two things run simultaneously:
          1. dbus-monitor subprocess → parses Notify method calls
          2. DBus signal listener    → catches NotificationClosed / ActionInvoked
        """
        self.running = True

        # Start the signal listener in a background thread
        # (catches dismissed/clicked events)
        signal_thread = threading.Thread(
            target=self._listen_signals,
            daemon=True,
            name="notif-signal-listener"
        )
        signal_thread.start()

        # Run the dbus-monitor parser in the main collector thread
        # This is the main loop — it blocks here forever
        self._run_dbus_monitor()

    def _run_dbus_monitor(self):
        """
        Spawn dbus-monitor and parse its output line by line.

        dbus-monitor output for a Notify call looks like this:

            method call time=1710000000.123 sender=:1.45 -> destination=:1.12
               interface=org.freedesktop.Notifications; member=Notify
            string "firefox"           ← app_name
            uint32 0                   ← replaces_id
            string ""                  ← icon
            string "New message"       ← summary  (this is the title)
            string "Hello from Alice"  ← body
            array [                    ← actions
            ]
            array [                    ← hints
            ]
            int32 5000                 ← expire_timeout (ms), -1 = forever

        We parse this by collecting lines after we see member=Notify,
        then extract the fields in order.
        """

        # --profile gives us method calls AND signals, not just signals
        # We filter to only org.freedesktop.Notifications to reduce noise
        cmd = [
            "dbus-monitor",
            "--session",  # session bus (where notifications live)
            "interface='org.freedesktop.Notifications'"  # only notification traffic
        ]

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,   # we read its output
                stderr=subprocess.PIPE,
                text=True,                # give us strings not bytes
                bufsize=1                 # line-buffered so we get lines as they arrive
            )
        except FileNotFoundError:
            logger.error(
                "dbus-monitor not found. Install it: sudo pacman -S dbus\n"
                "Falling back to swaync log reader."
            )
            self._fallback_swaync_reader()
            return

        logger.info("NotificationCollector: dbus-monitor started, listening for notifications...")

        # State machine to parse multi-line dbus-monitor output
        # We collect lines belonging to one Notify call, then parse them together
        in_notify_block = False   # True when we're inside a Notify method call block
        collected_lines = []      # Lines collected for current Notify block
        brace_depth = 0           # Track nested array/dict depth to know when block ends

        try:
            for line in proc.stdout:
                if not self.running:
                    break

                line = line.rstrip("\n")

                # Detect start of a Notify method call block
                # Example line: "   interface=org.freedesktop.Notifications; member=Notify"
                if "member=Notify" in line and "method call" in line.lower():
                    in_notify_block = True
                    collected_lines = []
                    brace_depth = 0
                    continue  # skip the header line itself, collect the arguments

                if in_notify_block:
                    collected_lines.append(line)

                    # Track array/dict nesting depth so we know when the block ends
                    brace_depth += line.count("[") + line.count("{")
                    brace_depth -= line.count("]") + line.count("}")

                    # A Notify call has 8 arguments. We detect end of block when:
                    # we've seen the expire_timeout (int32) at depth 0
                    # Simple heuristic: look for int32 line at depth 0 after we have content
                    if (brace_depth <= 0
                            and len(collected_lines) > 5
                            and re.match(r"\s*int32\s+-?\d+", line)):
                        # We have a complete Notify block — parse it
                        self._parse_notify_block(collected_lines)
                        in_notify_block = False
                        collected_lines = []

        except Exception as e:
            logger.error(f"dbus-monitor read error: {e}")
        finally:
            proc.terminate()
            if self.running:
                # If we get here unexpectedly, try the fallback
                logger.warning("dbus-monitor exited unexpectedly, switching to fallback.")
                self._fallback_swaync_reader()

    def _parse_notify_block(self, lines: list):
        """
        Parse the argument lines of a Notify method call.

        The 8 arguments arrive in this fixed order:
          1. string  app_name
          2. uint32  replaces_id
          3. string  app_icon
          4. string  summary        ← notification title
          5. string  body           ← notification text
          6. array   actions        ← may span multiple lines
          7. array   hints          ← may span multiple lines
          8. int32   expire_timeout

        We extract app_name, summary, body from the string lines in order.
        """
        # Extract all string values in order (ignoring arrays/uint32/int32)
        # dbus-monitor formats strings as:  string "value here"
        strings = []
        for line in lines:
            m = re.match(r'\s*string\s+"(.*)"', line)
            if m:
                strings.append(m.group(1))

        # We need at least 3 strings: app_name, icon, summary, body
        # (replaces_id is uint32 so it's skipped by our string regex)
        # strings[0] = app_name
        # strings[1] = app_icon  (we skip this)
        # strings[2] = summary
        # strings[3] = body
        if len(strings) < 3:
            logger.debug(f"Could not parse Notify block, only got strings: {strings}")
            return

        app_name = strings[0] if len(strings) > 0 else "unknown"
        summary  = strings[2] if len(strings) > 2 else ""
        body     = strings[3] if len(strings) > 3 else ""

        # Extract urgency from hints array
        # Hints look like: dict entry( string "urgency" variant byte 1 )
        urgency = "normal"
        for line in lines:
            m = re.search(r'string\s+"urgency"', line)
            if m:
                # Next line or same area has the value
                urgency_idx = lines.index(line)
                for hint_line in lines[urgency_idx:urgency_idx + 3]:
                    bm = re.search(r"byte\s+(\d+)", hint_line)
                    if bm:
                        urgency_map = {"0": "low", "1": "normal", "2": "critical"}
                        urgency = urgency_map.get(bm.group(1), "normal")
                        break

        now = int(time.time())
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")

        self.batch_writer.add("notifications", {
            "timestamp": now,
            "date":      today,
            "app_name":  app_name[:100],
            "summary":   summary[:300],
            "body":      body[:500],
            "action":    "received",
            "urgency":   urgency,
        })

        logger.debug(f"Notification captured from '{app_name}': {summary}")

    def _listen_signals(self):
        """
        Listen for NotificationClosed and ActionInvoked SIGNALS via Python dbus API.
        These ARE real DBus signals (not method calls), so add_signal_receiver works here.

        Runs in a background thread with its own GLib main loop.
        """
        try:
            import dbus
            from dbus.mainloop.glib import DBusGMainLoop
            from gi.repository import GLib

            # Each thread needs its own main loop
            DBusGMainLoop(set_as_default=True)
            bus = dbus.SessionBus()

            # NotificationClosed: fired when notification expires, dismissed, or closed
            bus.add_signal_receiver(
                self._on_notification_closed,
                dbus_interface="org.freedesktop.Notifications",
                signal_name="NotificationClosed",
                path="/org/freedesktop/Notifications"
            )

            # ActionInvoked: fired when user clicks an action button
            bus.add_signal_receiver(
                self._on_action_invoked,
                dbus_interface="org.freedesktop.Notifications",
                signal_name="ActionInvoked",
                path="/org/freedesktop/Notifications"
            )

            logger.info("NotificationCollector: DBus signal listener active.")

            # Run GLib loop — blocks here until quit() is called
            self._glib_loop = GLib.MainLoop()
            self._glib_loop.run()

        except ImportError:
            logger.warning(
                "dbus-python or pygobject not installed. "
                "Dismissed/clicked events won't be tracked.\n"
                "Install: sudo pacman -S python-dbus python-gobject"
            )
        except Exception as e:
            logger.error(f"DBus signal listener error: {e}")

    def _on_notification_closed(self, notification_id, reason):
        """
        Called when a notification disappears.
        reason: 1=expired, 2=dismissed by user, 3=closed by calling app, 4=undefined
        """
        reason_map = {1: "expired", 2: "dismissed", 3: "closed_by_app", 4: "unknown"}
        action = reason_map.get(int(reason), "unknown")

        now = int(time.time())
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")

        # Record the close action
        self.batch_writer.add("notifications", {
            "timestamp": now,
            "date":      today,
            "app_name":  "unknown",   # we don't have app_name at close time
            "summary":   "",
            "body":      "",
            "action":    action,
            "urgency":   "normal",
        })

        logger.debug(f"Notification {notification_id} closed: {action}")

    def _on_action_invoked(self, notification_id, action_key):
        """Called when user clicks an action button (e.g. 'Reply', 'Open')."""
        now = int(time.time())
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")

        self.batch_writer.add("notifications", {
            "timestamp": now,
            "date":      today,
            "app_name":  "unknown",
            "summary":   "",
            "body":      "",
            "action":    f"action_clicked:{action_key}",
            "urgency":   "normal",
        })

        logger.debug(f"Notification {notification_id} action clicked: {action_key}")

    def _fallback_swaync_reader(self):
        """
        Fallback: poll swaync's notification cache file every 5 seconds.
        Used when dbus-monitor is unavailable.
        Only works with swaync — not dunst or mako.
        """
        import json
        import os
        from pathlib import Path

        swaync_cache = Path.home() / ".cache/swaync/notifications.json"
        last_mtime = 0
        seen_ids = set()  # avoid re-logging the same notification on each poll

        logger.info(f"NotificationCollector fallback: polling {swaync_cache} every 5s")

        while self.running:
            try:
                if swaync_cache.exists():
                    mtime = os.path.getmtime(swaync_cache)
                    if mtime > last_mtime:
                        last_mtime = mtime
                        with open(swaync_cache) as f:
                            data = json.load(f)

                        notifications = data if isinstance(data, list) else []
                        for notif in notifications:
                            # Use id to avoid duplicates across polls
                            notif_id = notif.get("id") or notif.get("timestamp") or str(notif)
                            if notif_id in seen_ids:
                                continue
                            seen_ids.add(notif_id)

                            now = int(time.time())
                            self.batch_writer.add("notifications", {
                                "timestamp": now,
                                "date":      datetime.fromtimestamp(now).strftime("%Y-%m-%d"),
                                "app_name":  notif.get("appName", "unknown")[:100],
                                "summary":   str(notif.get("summary", ""))[:300],
                                "body":      str(notif.get("body", ""))[:500],
                                "action":    "received",
                                "urgency":   notif.get("urgency", "normal"),
                            })
            except json.JSONDecodeError:
                logger.debug("swaync cache JSON malformed — skipping this read")
            except Exception as e:
                logger.debug(f"Swaync fallback read error: {e}")

            time.sleep(5)

    def stop(self):
        """Stop all listeners cleanly."""
        self.running = False

        # Stop the GLib main loop if it's running (the signal listener thread)
        if hasattr(self, "_glib_loop") and self._glib_loop.is_running():
            self._glib_loop.quit()

        logger.info("NotificationCollector stopped.")
