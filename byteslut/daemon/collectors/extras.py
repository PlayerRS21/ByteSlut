"""
collectors/notifications.py — Notification Tracker
====================================================
Hooks into the system notification daemon via DBus.
Works with: swaync, dunst, mako, and any freedesktop-compatible notification daemon.

HOW DBUS NOTIFICATIONS WORK:
  All desktop notifications on Linux go through a DBus interface called
  "org.freedesktop.Notifications". When ANY app sends a notification
  (WhatsApp, Discord, system alerts, etc.), it sends a DBus message.
  We subscribe to these messages as a listener — zero overhead approach.

WHAT WE CAPTURE:
  - App name (which app sent it)
  - Summary (notification title)
  - Body (notification text/preview)
  - Exact timestamp
  - Action (clicked / dismissed / expired) — tracked via notification ID
"""

import time
import threading
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class NotificationCollector:
    """
    Listens to DBus for all system notifications.
    Works on Wayland and X11, any notification daemon.
    """

    def __init__(self, batch_writer):
        self.batch_writer = batch_writer
        self.running = False
        # Track pending notifications {notification_id: timestamp}
        # so we can record when/how the user acted on them
        self.pending = {}

    def run(self):
        """Start listening to DBus for notifications."""
        self.running = True
        try:
            import dbus
            from dbus.mainloop.glib import DBusGMainLoop
            from gi.repository import GLib

            DBusGMainLoop(set_as_default=True)
            self.session_bus = dbus.SessionBus()

            # Listen to the Notify method calls — this is fired when any app
            # sends a notification, BEFORE the daemon displays it
            self.session_bus.add_signal_receiver(
                self._on_notification_received,
                dbus_interface="org.freedesktop.Notifications",
                signal_name="Notify",
                path="/org/freedesktop/Notifications"
            )

            # Listen for NotificationClosed — fired when dismissed or clicked
            self.session_bus.add_signal_receiver(
                self._on_notification_closed,
                dbus_interface="org.freedesktop.Notifications",
                signal_name="NotificationClosed",
                path="/org/freedesktop/Notifications"
            )

            # Listen for ActionInvoked — fired when user clicks an action button
            self.session_bus.add_signal_receiver(
                self._on_action_invoked,
                dbus_interface="org.freedesktop.Notifications",
                signal_name="ActionInvoked",
                path="/org/freedesktop/Notifications"
            )

            # Install ourselves as a notification eavesdropper
            # This requires adding a match rule to the DBus daemon
            self.session_bus.add_match_string(
                "interface='org.freedesktop.Notifications',member='Notify'"
            )

            logger.info("NotificationCollector: Listening on DBus")
            loop = GLib.MainLoop()
            loop.run()

        except ImportError:
            logger.warning("dbus-python or pygobject not available. Using fallback swaync log reader.")
            self._fallback_swaync_reader()
        except Exception as e:
            logger.error(f"NotificationCollector DBus error: {e}")
            self._fallback_swaync_reader()

    def _on_notification_received(self, app_name, replaces_id, icon, summary, body,
                                   actions, hints, expire_timeout):
        """Called when a notification is sent."""
        now = int(time.time())
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")

        urgency = "normal"
        if "urgency" in hints:
            urgency_map = {0: "low", 1: "normal", 2: "critical"}
            urgency = urgency_map.get(int(hints["urgency"]), "normal")

        self.batch_writer.add("notifications", {
            "timestamp": now,
            "date": today,
            "app_name": str(app_name),
            "summary": str(summary)[:300],
            "body": str(body)[:500],
            "action": "received",
            "urgency": urgency,
        })

        logger.debug(f"Notification from {app_name}: {summary}")

    def _on_notification_closed(self, notification_id, reason):
        """
        reason: 1=expired, 2=dismissed by user, 3=closed by app, 4=undefined
        """
        action_map = {1: "expired", 2: "dismissed", 3: "closed_by_app"}
        action = action_map.get(int(reason), "unknown")
        logger.debug(f"Notification {notification_id} closed: {action}")

    def _on_action_invoked(self, notification_id, action_key):
        """Called when user clicks an action button on a notification."""
        logger.debug(f"Notification {notification_id} action: {action_key}")

    def _fallback_swaync_reader(self):
        """
        Fallback: read swaync's notification log if DBus isn't available.
        Swaync stores notifications in a JSON file we can read.
        """
        import json
        import os
        from pathlib import Path

        swaync_cache = Path.home() / ".cache/swaync/notifications.json"
        last_mtime = 0

        while self.running:
            try:
                if swaync_cache.exists():
                    mtime = os.path.getmtime(swaync_cache)
                    if mtime > last_mtime:
                        last_mtime = mtime
                        with open(swaync_cache) as f:
                            data = json.load(f)
                        # Parse swaync notification format
                        for notif in data if isinstance(data, list) else []:
                            now = int(time.time())
                            self.batch_writer.add("notifications", {
                                "timestamp": now,
                                "date": datetime.fromtimestamp(now).strftime("%Y-%m-%d"),
                                "app_name": notif.get("appName", "unknown"),
                                "summary": str(notif.get("summary", ""))[:300],
                                "body": str(notif.get("body", ""))[:500],
                                "action": "received",
                                "urgency": notif.get("urgency", "normal"),
                            })
            except Exception as e:
                logger.debug(f"Swaync fallback read error: {e}")
            time.sleep(5)

    def stop(self):
        self.running = False


"""
collectors/commands.py — Terminal Command History Tracker
==========================================================
Tracks every command you run in the terminal with:
  - The exact command
  - Exit code (0 = success, non-zero = error — great for debugging!)
  - Which directory you were in
  - How long it took
  - Whether it needed sudo

HOW IT WORKS:
  We tail the shell history files (~/.bash_history, ~/.zsh_history).
  For real-time tracking with exit codes, we inject a hook into
  the shell's pre/post-command hooks.
  
  The hook is a few lines added to ~/.bashrc or ~/.zshrc:
    PROMPT_COMMAND='__byteslut_log_cmd'
  This calls our logging function before each new prompt is shown.
"""

import os
import re
import time
import subprocess
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Path to the byteslut command log (shell hook writes here, we read it)
CMD_LOG_FILE = os.path.expanduser("~/.local/share/byteslut/cmd_log.txt")


class CommandCollector:
    """
    Reads terminal command history and the real-time command log.
    """

    def __init__(self, batch_writer):
        self.batch_writer = batch_writer
        self.running = False
        self.last_cmd_log_pos = 0  # File position in cmd_log.txt
        self.last_history_line = {}  # {shell: last_line_count}

    def install_shell_hooks(self):
        """
        Add tracking hooks to bash and zsh config files.
        These hooks log every command with exit code and timing.

        The hook is minimal — it just appends a line to a log file.
        Format: TIMESTAMP|EXIT_CODE|PWD|DURATION|COMMAND
        """
        hooks = {
            "bash": {
                "rc_file": Path.home() / ".bashrc",
                "hook": '''
# ByteSlut command tracking hook
__byteslut_cmd_start() {
    __BYTESLUT_CMD_START=$SECONDS
    __BYTESLUT_CMD=$BASH_COMMAND
}
__byteslut_cmd_log() {
    local exit_code=$?
    local duration=$(( SECONDS - ${__BYTESLUT_CMD_START:-$SECONDS} ))
    local cmd="${__BYTESLUT_CMD:-}"
    local log_file="$HOME/.local/share/byteslut/cmd_log.txt"
    [ -n "$cmd" ] && echo "$(date +%s)|$exit_code|$PWD|$duration|$cmd" >> "$log_file" 2>/dev/null
}
trap '__byteslut_cmd_start' DEBUG
PROMPT_COMMAND="${PROMPT_COMMAND:+$PROMPT_COMMAND; }__byteslut_cmd_log"
'''
            },
            "zsh": {
                "rc_file": Path.home() / ".zshrc",
                "hook": '''
# ByteSlut command tracking hook
__byteslut_preexec() {
    __BYTESLUT_CMD_START=$SECONDS
    __BYTESLUT_LAST_CMD="$1"
}
__byteslut_precmd() {
    local exit_code=$?
    local duration=$(( SECONDS - ${__BYTESLUT_CMD_START:-$SECONDS} ))
    local log_file="$HOME/.local/share/byteslut/cmd_log.txt"
    [ -n "${__BYTESLUT_LAST_CMD:-}" ] && echo "$(date +%s)|$exit_code|$PWD|$duration|$__BYTESLUT_LAST_CMD" >> "$log_file" 2>/dev/null
    unset __BYTESLUT_LAST_CMD
}
autoload -Uz add-zsh-hook
add-zsh-hook preexec __byteslut_preexec
add-zsh-hook precmd __byteslut_precmd
'''
            }
        }

        for shell, config in hooks.items():
            rc_file = config["rc_file"]
            if rc_file.exists():
                content = rc_file.read_text()
                if "ByteSlut command tracking hook" not in content:
                    with open(rc_file, "a") as f:
                        f.write(config["hook"])
                    logger.info(f"Installed ByteSlut hook into {rc_file}")

        # Ensure log file directory exists
        os.makedirs(os.path.dirname(CMD_LOG_FILE), exist_ok=True)

    def read_cmd_log(self):
        """Read new entries from the real-time command log file."""
        if not os.path.exists(CMD_LOG_FILE):
            return

        try:
            with open(CMD_LOG_FILE, "r") as f:
                f.seek(self.last_cmd_log_pos)
                new_lines = f.readlines()
                self.last_cmd_log_pos = f.tell()

            for line in new_lines:
                line = line.strip()
                if not line:
                    continue
                self._parse_and_store_cmd(line)

        except Exception as e:
            logger.error(f"Error reading cmd log: {e}")

    def _parse_and_store_cmd(self, line: str):
        """
        Parse a log line: TIMESTAMP|EXIT_CODE|PWD|DURATION|COMMAND
        and store it in the database.
        """
        parts = line.split("|", 4)  # Split into max 5 parts (command may contain |)
        if len(parts) < 5:
            return

        try:
            timestamp = int(parts[0])
            exit_code = int(parts[1])
            pwd = parts[2]
            duration = float(parts[3])
            command = parts[4].strip()

            # Detect shell
            shell = "bash"
            if os.environ.get("ZSH_NAME"):
                shell = "zsh"
            elif os.environ.get("FISH_VERSION"):
                shell = "fish"

            is_sudo = 1 if command.startswith("sudo ") else 0

            # Don't log the byteslut command itself (would be recursive)
            if command.startswith("byteslut") or "byteslut" in command:
                return

            self.batch_writer.add("commands", {
                "timestamp": timestamp,
                "date": datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d"),
                "command": command[:1000],
                "exit_code": exit_code,
                "working_directory": pwd[:500],
                "shell": shell,
                "is_sudo": is_sudo,
                "duration_seconds": duration,
            })

        except (ValueError, IndexError) as e:
            logger.debug(f"Could not parse cmd log line: {line} — {e}")

    def run(self):
        self.running = True
        self.install_shell_hooks()
        logger.info("CommandCollector started")

        # Read existing history files once at startup
        self._import_existing_history()

        while self.running:
            try:
                self.read_cmd_log()
            except Exception as e:
                logger.error(f"CommandCollector error: {e}")
            time.sleep(5)  # Check every 5 seconds

    def _import_existing_history(self):
        """Import existing shell history on first run."""
        history_files = [
            (Path.home() / ".bash_history", "bash"),
            (Path.home() / ".zsh_history", "zsh"),
        ]

        from daemon.db import query
        # Check if we've already imported history
        existing = query("SELECT COUNT(*) as cnt FROM commands", fetch="one")
        if existing and existing["cnt"] > 0:
            return  # Already have history, skip re-import

        for hist_file, shell in history_files:
            if not hist_file.exists():
                continue
            try:
                with open(hist_file, "r", errors="replace") as f:
                    lines = f.readlines()

                now = int(time.time())
                for i, line in enumerate(lines[-1000:]):  # Import last 1000 commands
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue

                    # zsh history format: ": TIMESTAMP:DURATION;COMMAND"
                    if shell == "zsh" and line.startswith(":"):
                        match = re.match(r"^: (\d+):(\d+);(.+)$", line)
                        if match:
                            ts = int(match.group(1))
                            duration = float(match.group(2))
                            cmd = match.group(3)
                        else:
                            continue
                    else:
                        ts = now - (len(lines) - i) * 60  # Estimate timestamp
                        cmd = line
                        duration = 0

                    self.batch_writer.add("commands", {
                        "timestamp": ts,
                        "date": datetime.fromtimestamp(ts).strftime("%Y-%m-%d"),
                        "command": cmd[:1000],
                        "exit_code": None,  # Unknown for old history
                        "working_directory": "",
                        "shell": shell,
                        "is_sudo": 1 if cmd.startswith("sudo ") else 0,
                        "duration_seconds": duration,
                    })

                logger.info(f"Imported existing {shell} history ({len(lines)} commands)")
            except Exception as e:
                logger.error(f"History import failed for {hist_file}: {e}")

    def stop(self):
        self.running = False


"""
collectors/network.py — Network Usage Tracker
==============================================
Tracks internet usage per app and total up/down per day.

HOW WE TRACK PER-APP BANDWIDTH:
  Every process has a file: /proc/{pid}/net/dev
  But this shows ALL network usage for that process's network namespace.

  Better approach: use /proc/{pid}/fd to see which sockets a process has,
  then correlate with /proc/net/tcp and /proc/net/udp to get bytes transferred.

  For simplicity and reliability, we use nethogs-style reading:
  We sample total interface bytes every 30s, and track per-process
  via /proc/{pid}/net/tcp socket accounting.

TOTAL USAGE: Very accurate via /proc/net/dev
PER-APP:     Approximate (Linux doesn't have simple per-process network counters
             without kernel modules like nethogs or ebpf)
"""

import os
import re
import time
import logging
from datetime import datetime, date
from collections import defaultdict

logger = logging.getLogger(__name__)

# Try to use psutil for network stats
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False


def read_proc_net_dev():
    """
    Read /proc/net/dev which gives cumulative bytes for each network interface.
    Returns: {interface: {bytes_recv, bytes_sent}}
    """
    result = {}
    try:
        with open("/proc/net/dev") as f:
            lines = f.readlines()

        for line in lines[2:]:  # First 2 lines are headers
            parts = line.split()
            if len(parts) < 10:
                continue
            iface = parts[0].rstrip(":")
            # Skip loopback
            if iface == "lo":
                continue
            result[iface] = {
                "bytes_recv": int(parts[1]),
                "bytes_sent": int(parts[9]),
            }
    except Exception as e:
        logger.debug(f"proc/net/dev read error: {e}")
    return result


class NetworkCollector:
    """
    Tracks network usage totals and attempts per-app tracking.
    """

    def __init__(self, batch_writer, interval=60):
        self.batch_writer = batch_writer
        self.interval = interval
        self.running = False
        self.last_readings = {}  # {iface: {bytes_recv, bytes_sent}}
        self._init_baseline()

    def _init_baseline(self):
        """Get initial readings so we can calculate deltas."""
        self.last_readings = read_proc_net_dev()

    def collect(self):
        """Collect network delta since last check and attribute to apps."""
        today = str(date.today())
        now = int(time.time())
        current = read_proc_net_dev()

        total_sent = 0
        total_recv = 0

        for iface, data in current.items():
            prev = self.last_readings.get(iface, {"bytes_recv": 0, "bytes_sent": 0})
            sent_delta = max(0, data["bytes_sent"] - prev["bytes_sent"])
            recv_delta = max(0, data["bytes_recv"] - prev["bytes_recv"])
            total_sent += sent_delta
            total_recv += recv_delta

        self.last_readings = current

        if total_sent == 0 and total_recv == 0:
            return  # No network activity

        # Try per-app attribution using psutil connections
        app_usage = defaultdict(lambda: {"bytes_sent": 0, "bytes_received": 0})

        if PSUTIL_AVAILABLE:
            try:
                connections = psutil.net_connections(kind="inet")
                active_pids = set()
                for conn in connections:
                    if conn.status == "ESTABLISHED" and conn.pid:
                        active_pids.add(conn.pid)

                if active_pids:
                    # Distribute network usage across active processes
                    per_pid = max(1, len(active_pids))
                    sent_per_pid = total_sent // per_pid
                    recv_per_pid = total_recv // per_pid

                    for pid in active_pids:
                        try:
                            proc = psutil.Process(pid)
                            app_name = proc.name().lower()
                            app_usage[app_name]["bytes_sent"] += sent_per_pid
                            app_usage[app_name]["bytes_received"] += recv_per_pid
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
            except Exception as e:
                logger.debug(f"Per-app network attribution failed: {e}")

        # If we couldn't attribute to apps, log as "system"
        if not app_usage:
            app_usage["system"]["bytes_sent"] = total_sent
            app_usage["system"]["bytes_received"] = total_recv

        for app_name, usage in app_usage.items():
            if usage["bytes_sent"] > 0 or usage["bytes_received"] > 0:
                self.batch_writer.add("network_usage", {
                    "timestamp": now,
                    "date": today,
                    "app_name": app_name,
                    "bytes_sent": usage["bytes_sent"],
                    "bytes_received": usage["bytes_received"],
                    "interface": "all",
                })

    def run(self):
        self.running = True
        logger.info("NetworkCollector started")
        while self.running:
            try:
                self.collect()
            except Exception as e:
                logger.error(f"NetworkCollector error: {e}")
            time.sleep(self.interval)

    def stop(self):
        self.running = False


"""
collectors/battery.py — Battery Health Tracker
===============================================
Reads battery info from /sys/class/power_supply/

Linux stores all battery info in sysfs:
  /sys/class/power_supply/BAT0/capacity     → current % (0-100)
  /sys/class/power_supply/BAT0/status       → "Charging", "Discharging", "Full"
  /sys/class/power_supply/BAT0/voltage_now  → voltage in microvolts
  /sys/class/power_supply/BAT0/energy_full  → current max capacity (microWh)
  /sys/class/power_supply/BAT0/energy_full_design → original design capacity
  
  Health % = (energy_full / energy_full_design) * 100
  This decreases over time as your battery degrades.
"""

import os
import time
import logging
from datetime import datetime, date
from pathlib import Path

logger = logging.getLogger(__name__)

BATTERY_BASE = "/sys/class/power_supply"


def find_battery():
    """Find the first battery in /sys/class/power_supply/"""
    if not os.path.exists(BATTERY_BASE):
        return None
    for entry in os.listdir(BATTERY_BASE):
        type_file = f"{BATTERY_BASE}/{entry}/type"
        try:
            with open(type_file) as f:
                if f.read().strip() == "Battery":
                    return f"{BATTERY_BASE}/{entry}"
        except Exception:
            continue
    return None


def read_sysfs_int(path: str, divisor: float = 1):
    """Safely read an integer from a sysfs file."""
    try:
        with open(path) as f:
            return int(f.read().strip()) / divisor
    except Exception:
        return None


class BatteryCollector:
    """Collects battery stats every 5 minutes."""

    def __init__(self, batch_writer, interval=300):
        self.batch_writer = batch_writer
        self.interval = interval
        self.running = False
        self.battery_path = find_battery()

        if self.battery_path:
            logger.info(f"Battery found at {self.battery_path}")
        else:
            logger.info("No battery found (desktop machine or battery not detected)")

    def collect(self):
        if not self.battery_path:
            return

        today = str(date.today())
        now = int(time.time())
        bat = self.battery_path

        percent = read_sysfs_int(f"{bat}/capacity")
        status_file = f"{bat}/status"
        try:
            with open(status_file) as f:
                status = f.read().strip()
            is_plugged = 1 if status in ("Charging", "Full") else 0
        except Exception:
            is_plugged = None

        voltage_uv = read_sysfs_int(f"{bat}/voltage_now")
        voltage_v = voltage_uv / 1_000_000 if voltage_uv else None

        capacity_design = read_sysfs_int(f"{bat}/energy_full_design", 1000)   # µWh → mWh
        capacity_full = read_sysfs_int(f"{bat}/energy_full", 1000)            # µWh → mWh

        # Charge cycles stored in some batteries
        charge_cycles = read_sysfs_int(f"{bat}/cycle_count")

        self.batch_writer.add("battery_stats", {
            "timestamp": now,
            "date": today,
            "percent": percent,
            "is_plugged": is_plugged,
            "voltage_v": voltage_v,
            "charge_cycles": charge_cycles,
            "capacity_design_mwh": int(capacity_design) if capacity_design else None,
            "capacity_full_mwh": int(capacity_full) if capacity_full else None,
        })

    def run(self):
        self.running = True
        logger.info("BatteryCollector started")
        while self.running:
            try:
                self.collect()
            except Exception as e:
                logger.error(f"BatteryCollector error: {e}")
            time.sleep(self.interval)

    def stop(self):
        self.running = False


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


"""
collectors/input_stats.py — Keyboard & Mouse Tracking
=======================================================
Tracks keystrokes, mouse clicks, mouse movement, and WPM.

HOW IT WORKS:
  We read from /dev/input/event* devices — these are the raw input events
  from your keyboard and mouse before they go to the window system.

  Each event file gives us:
  - EV_KEY events → keyboard keys pressed, mouse buttons clicked
  - EV_REL events → mouse movement (relative X, Y coordinates)

  We need to open these as root, OR be in the 'input' group.
  The installer adds the user to the 'input' group automatically.

  PRIVACY: We only COUNT events, we don't record WHICH keys were pressed.
  (The 'track_typed_words' setting in config controls whether we store
   actual keystrokes — it's OFF by default.)
"""

import os
import struct
import time
import glob
import logging
import threading
from datetime import datetime, date

logger = logging.getLogger(__name__)

# Linux input event structure: time(8), type(2), code(2), value(4) = 16 bytes
INPUT_EVENT_SIZE = 16
INPUT_EVENT_FORMAT = "llHHi"  # long long, long long, unsigned short, unsigned short, int

# Event types we care about
EV_KEY = 1    # Keyboard and mouse button events
EV_REL = 2    # Mouse movement (relative)

# Key/button codes
BTN_LEFT = 272    # Left mouse button
BTN_RIGHT = 273   # Right mouse button
BTN_MIDDLE = 274  # Middle mouse button
REL_X = 0         # Mouse X movement
REL_Y = 1         # Mouse Y movement
REL_WHEEL = 8     # Mouse scroll wheel


class InputCollector:
    """
    Reads raw input events from /dev/input/event* and counts them.
    Lightweight: we only COUNT events, never record which key was pressed.
    """

    def __init__(self, batch_writer, interval=60):
        self.batch_writer = batch_writer
        self.interval = interval  # How often to flush counts to DB
        self.running = False

        # Counters (reset after each flush)
        self.keystrokes = 0
        self.mouse_clicks = 0
        self.scroll_events = 0
        self.mouse_dx = 0  # Total horizontal movement in pixels
        self.mouse_dy = 0  # Total vertical movement in pixels

        # For WPM calculation: track keystrokes with timestamps
        self.keystroke_times = []  # List of timestamps of recent keystrokes
        self.last_flush = time.time()

    def _find_input_devices(self):
        """
        Find keyboard and mouse event files in /dev/input/
        We filter by checking which devices have KEY events (keyboards)
        and REL events (mice).
        """
        keyboards = []
        mice = []

        # /proc/bus/input/devices lists all input devices with their event files
        try:
            with open("/proc/bus/input/devices") as f:
                content = f.read()

            # Parse device blocks
            current_device = {}
            for line in content.splitlines():
                if line.startswith("N: Name="):
                    current_device["name"] = line.split('"')[1] if '"' in line else ""
                elif line.startswith("H: Handlers="):
                    # Find the event file for this device
                    handlers = line.split("=", 1)[1]
                    for handler in handlers.split():
                        if handler.startswith("event"):
                            current_device["event"] = f"/dev/input/{handler}"
                elif line.startswith("B: KEY="):
                    # Has keyboard events
                    current_device["has_key"] = True
                elif line.startswith("B: REL="):
                    # Has relative movement (mouse)
                    current_device["has_rel"] = True
                elif line == "" and current_device:
                    event_file = current_device.get("event")
                    if event_file and os.access(event_file, os.R_OK):
                        if current_device.get("has_key"):
                            keyboards.append(event_file)
                        if current_device.get("has_rel"):
                            mice.append(event_file)
                    current_device = {}

        except Exception as e:
            logger.warning(f"Could not read /proc/bus/input/devices: {e}")
            # Fallback: just grab all event files
            all_events = glob.glob("/dev/input/event*")
            for ev in all_events:
                if os.access(ev, os.R_OK):
                    keyboards.append(ev)
                    mice.append(ev)

        return list(set(keyboards)), list(set(mice))

    def _read_device(self, device_path: str):
        """
        Read events from one input device file in a thread.
        This is a blocking read — it just sits and waits for input events.
        """
        try:
            with open(device_path, "rb") as f:
                while self.running:
                    try:
                        data = f.read(INPUT_EVENT_SIZE)
                        if len(data) < INPUT_EVENT_SIZE:
                            break

                        # Unpack the binary event struct
                        tv_sec, tv_usec, ev_type, ev_code, ev_value = struct.unpack(
                            INPUT_EVENT_FORMAT, data
                        )

                        # KEY event with value=1 means key/button PRESSED (not released)
                        if ev_type == EV_KEY and ev_value == 1:
                            if ev_code in (BTN_LEFT, BTN_RIGHT, BTN_MIDDLE):
                                self.mouse_clicks += 1
                            else:
                                # It's a keyboard key
                                self.keystrokes += 1
                                self.keystroke_times.append(time.time())
                                # Keep only last 60 seconds of keystrokes for WPM calc
                                cutoff = time.time() - 60
                                self.keystroke_times = [t for t in self.keystroke_times if t > cutoff]

                        # Mouse movement
                        elif ev_type == EV_REL:
                            if ev_code == REL_X:
                                self.mouse_dx += abs(ev_value)
                            elif ev_code == REL_Y:
                                self.mouse_dy += abs(ev_value)
                            elif ev_code == REL_WHEEL:
                                self.scroll_events += abs(ev_value)

                    except struct.error:
                        break
        except PermissionError:
            logger.warning(f"Permission denied: {device_path}. Add user to 'input' group: sudo usermod -aG input $USER")
        except Exception as e:
            logger.debug(f"Input device {device_path} read error: {e}")

    def _calculate_wpm(self):
        """
        Calculate approximate typing speed (WPM) based on recent keystroke rate.
        Average word = 5 characters, so WPM ≈ (keystrokes per minute) / 5
        """
        if not self.keystroke_times:
            return 0
        # Count keystrokes in last 60 seconds
        cutoff = time.time() - 60
        recent = sum(1 for t in self.keystroke_times if t > cutoff)
        return recent / 5  # WPM estimate

    def _flush_loop(self):
        """Periodically flush counters to database."""
        while self.running:
            time.sleep(self.interval)
            try:
                self._flush()
            except Exception as e:
                logger.error(f"Input flush error: {e}")

    def _flush(self):
        """Write current counts to batch writer and reset."""
        if self.keystrokes == 0 and self.mouse_clicks == 0:
            return

        wpm = self._calculate_wpm()
        distance_px = int((self.mouse_dx ** 2 + self.mouse_dy ** 2) ** 0.5)  # Euclidean distance

        self.batch_writer.add("input_stats", {
            "date": str(date.today()),
            "keystrokes": self.keystrokes,
            "mouse_clicks": self.mouse_clicks,
            "mouse_scroll_events": self.scroll_events,
            "mouse_distance_px": distance_px,
            "wpm_sample": round(wpm, 1),
        })

        # Reset counters
        self.keystrokes = 0
        self.mouse_clicks = 0
        self.scroll_events = 0
        self.mouse_dx = 0
        self.mouse_dy = 0

    def run(self):
        self.running = True
        keyboards, mice = self._find_input_devices()
        all_devices = list(set(keyboards + mice))

        if not all_devices:
            logger.warning("No accessible input devices found. Add user to 'input' group.")
            return

        logger.info(f"InputCollector: watching {len(all_devices)} devices")

        # Start a thread per device (they block on read)
        threads = []
        for device in all_devices:
            t = threading.Thread(target=self._read_device, args=(device,), daemon=True)
            t.start()
            threads.append(t)

        # Flush loop
        self._flush_loop()

    def stop(self):
        self.running = False
        self._flush()
