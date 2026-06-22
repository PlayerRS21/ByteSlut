"""
daemon/daily_report.py — Daily Report Scheduler
=================================================
At 6:30 PM every day, opens the dashboard in a terminal window
ONLY when the system is relaxed (not busy with CPU/RAM/temp).

HOW THE "SYSTEM RELAXED" CHECK WORKS:
  Before opening the report, we check:
  1. CPU usage averaged over 2 minutes < threshold (default 30%)
  2. CPU temperature NOT actively fluctuating (stable, not spiking)
  3. RAM usage < threshold (default 85%)

  Why average over 2 minutes?
    A single CPU reading can spike for 1 second then drop.
    Averaging ensures you're actually in a calm period, not
    just lucky during a brief lull.

  Why temperature fluctuation, not just raw temp?
    Your laptop hits 80°C watching YouTube (as you noticed).
    But if temp is STABLE at 80°C (not climbing), the system
    is handling it fine. We only delay if temp is actively rising,
    meaning the system is building up heat from a demanding task.

  If system is busy → wait 2 minutes → check again.
  Max delay: 60 minutes (configurable). After that, opens anyway.

HOW IT OPENS THE DASHBOARD:
  Detects your terminal emulator (kitty, alacritty, foot, wezterm, etc.)
  Opens a new terminal window running the dashboard command.
  Falls back to xdg-open if no terminal is found.
"""

import os
import sys
import time
import json
import logging
import threading
import subprocess
from datetime import datetime, date, timedelta
from pathlib import Path
from collections import deque

logger = logging.getLogger(__name__)

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def load_config():
    """Load settings including daily report config."""
    try:
        with open(PROJECT_ROOT / "config" / "settings.json") as f:
            return json.load(f)
    except Exception:
        return {
            "cli_command": "byteslut",
            "dashboard_port": 6969,
            "daily_report": {
                "enabled": True,
                "time": "18:30",
                "delay_if_cpu_above_percent": 30,
                "delay_if_temp_above_celsius": 75,
                "delay_check_interval_seconds": 120,
                "max_delay_minutes": 60,
            }
        }


def get_cpu_usage_sample():
    """Get current CPU usage percentage (0-100)."""
    try:
        import psutil
        # interval=1 = measure over 1 second for accuracy
        return psutil.cpu_percent(interval=1)
    except ImportError:
        # Fallback: read /proc/stat manually
        try:
            with open("/proc/stat") as f:
                line = f.readline()
            vals = [int(x) for x in line.split()[1:]]
            idle = vals[3]
            total = sum(vals)
            time.sleep(0.5)
            with open("/proc/stat") as f:
                line2 = f.readline()
            vals2 = [int(x) for x in line2.split()[1:]]
            idle2 = vals2[3]
            total2 = sum(vals2)
            delta_idle = idle2 - idle
            delta_total = total2 - total
            if delta_total == 0:
                return 0
            return round((1 - delta_idle / delta_total) * 100, 1)
        except Exception:
            return 0


def get_cpu_temp():
    """Get current CPU temperature in Celsius. Returns None if unavailable."""
    try:
        import psutil
        temps = psutil.sensors_temperatures()
        for name in ["coretemp", "k10temp", "acpitz", "cpu_thermal"]:
            if name in temps and temps[name]:
                entries = temps[name]
                pkg = [e for e in entries if "package" in e.label.lower()]
                return (pkg[0].current if pkg else entries[0].current)
    except Exception:
        pass

    # sysfs fallback
    try:
        for zone in sorted(Path("/sys/class/thermal").iterdir()):
            type_f = zone / "type"
            temp_f = zone / "temp"
            if not (type_f.exists() and temp_f.exists()):
                continue
            zone_type = type_f.read_text().strip().lower()
            if any(t in zone_type for t in ["x86_pkg", "cpu_thermal", "acpitz", "coretemp"]):
                return int(temp_f.read_text().strip()) / 1000.0
    except Exception:
        pass

    return None


def get_ram_usage():
    """Get current RAM usage percentage."""
    try:
        import psutil
        return psutil.virtual_memory().percent
    except Exception:
        try:
            with open("/proc/meminfo") as f:
                lines = f.readlines()
            info = {}
            for line in lines:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])
            total = info.get("MemTotal", 1)
            available = info.get("MemAvailable", total)
            return round((1 - available / total) * 100, 1)
        except Exception:
            return 0


class SystemReadinessChecker:
    """
    Checks if the system is "relaxed" enough to show the daily report.

    Keeps a rolling 2-minute history of CPU readings to calculate
    a real average (not just the last sample which can spike).

    Also watches temperature trend: if temp increased >5°C in the
    last 2 minutes, the system is under load and we should wait.
    """

    def __init__(self, cpu_threshold=30, temp_threshold=75, ram_threshold=85):
        self.cpu_threshold  = cpu_threshold   # % above = busy
        self.temp_threshold = temp_threshold  # °C above = hot
        self.ram_threshold  = ram_threshold   # % above = memory pressure

        # Rolling 2-minute history (one sample per 10 seconds = 12 samples)
        self._cpu_history  = deque(maxlen=12)
        self._temp_history = deque(maxlen=12)

        # Collect baseline samples
        self._collecting = True
        t = threading.Thread(target=self._collect_loop, daemon=True)
        t.start()

    def _collect_loop(self):
        """Background thread — takes a reading every 10 seconds."""
        while self._collecting:
            cpu = get_cpu_usage_sample()
            temp = get_cpu_temp()
            self._cpu_history.append(cpu)
            if temp is not None:
                self._temp_history.append(temp)
            time.sleep(10)

    def stop(self):
        self._collecting = False

    def is_system_relaxed(self):
        """
        Returns (is_relaxed: bool, reason: str)
        reason explains WHY if not relaxed — used in logs.
        """
        # Need at least 3 samples (30 seconds of data) before we can decide
        if len(self._cpu_history) < 3:
            return False, "collecting baseline (30s)"

        # 2-minute average CPU
        avg_cpu = sum(self._cpu_history) / len(self._cpu_history)
        if avg_cpu > self.cpu_threshold:
            return False, f"CPU busy: {avg_cpu:.1f}% avg (threshold: {self.cpu_threshold}%)"

        # RAM check
        ram = get_ram_usage()
        if ram > self.ram_threshold:
            return False, f"RAM under pressure: {ram:.1f}% (threshold: {self.ram_threshold}%)"

        # Temperature check — only if we have temp data
        if len(self._temp_history) >= 3:
            current_temp = self._temp_history[-1]
            oldest_temp  = self._temp_history[0]
            temp_rise    = current_temp - oldest_temp

            # If current temp is above threshold AND actively rising → busy
            if current_temp > self.temp_threshold and temp_rise > 3:
                return False, (f"Temp rising: {oldest_temp:.0f}→{current_temp:.0f}°C "
                               f"(threshold: {self.temp_threshold}°C)")

            # If temp is very high (>85°C) regardless of trend → wait
            if current_temp > 85:
                return False, f"Temp too high: {current_temp:.0f}°C"

        return True, "system relaxed"


def find_terminal():
    """
    Find an available terminal emulator on the system.
    Returns (binary, args_to_run_command) or (None, None).

    Each tuple is: (terminal_binary, flag_before_command)
    e.g. ("kitty", "--") means: kitty -- byteslut
    """
    candidates = [
        # Wayland-native terminals (preferred on Hyprland)
        ("kitty",    ["-e"]),
        ("foot",     ["-e"]),
        ("alacritty", ["-e"]),
        ("wezterm",  ["start", "--"]),
        # X11 / generic
        ("xterm",    ["-e"]),
        ("urxvt",    ["-e"]),
        ("konsole",  ["-e"]),
        ("gnome-terminal", ["--"]),
        ("xfce4-terminal", ["-e"]),
        ("lxterminal",     ["-e"]),
    ]

    for binary, args in candidates:
        try:
            result = subprocess.run(
                ["which", binary], capture_output=True, text=True
            )
            if result.returncode == 0 and result.stdout.strip():
                return binary, args
        except Exception:
            continue

    return None, None


def open_dashboard_in_terminal(cli_command: str, port: int):
    """
    Open the dashboard in a new terminal window.
    This is what fires when the daily report triggers.
    """
    terminal, args = find_terminal()

    if terminal:
        cmd = [terminal] + args + [sys.executable,
               str(PROJECT_ROOT / "cli" / "byteslut.py")]
        try:
            subprocess.Popen(
                cmd,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(f"Daily report: opened dashboard in {terminal}")
            return True
        except Exception as e:
            logger.error(f"Failed to open terminal {terminal}: {e}")

    # Fallback: open in browser directly (no terminal)
    try:
        import webbrowser
        url = f"http://127.0.0.1:{port}"
        # Start web server first if not running
        subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "web" / "app.py")],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(2)
        webbrowser.open(url)
        logger.info("Daily report: opened dashboard in browser (no terminal found)")
        return True
    except Exception as e:
        logger.error(f"Daily report fallback also failed: {e}")
        return False


class DailyReportScheduler:
    """
    Fires at a configured time each day (default 18:30).
    Waits until the system is relaxed, then opens the dashboard.

    The scheduler runs as part of the daemon — same process, own thread.
    """

    def __init__(self):
        self.running = False
        self._last_report_date = None   # Don't fire twice on the same day
        self._checker = None

    def run(self):
        """Main scheduler loop. Checks time every 30 seconds."""
        self.running = True
        config = load_config()
        report_cfg = config.get("daily_report", {})

        if not report_cfg.get("enabled", True):
            logger.info("Daily report scheduler disabled in config")
            return

        cpu_threshold  = report_cfg.get("delay_if_cpu_above_percent", 30)
        temp_threshold = report_cfg.get("delay_if_temp_above_celsius", 75)
        ram_threshold  = 85
        check_interval = report_cfg.get("delay_check_interval_seconds", 120)
        max_delay_min  = report_cfg.get("max_delay_minutes", 60)
        report_time_str = report_cfg.get("time", "18:30")
        cli_command    = config.get("cli_command", "byteslut")
        port           = config.get("dashboard_port", 6969)

        # Parse the report time (HH:MM)
        try:
            report_hour, report_minute = map(int, report_time_str.split(":"))
        except (ValueError, AttributeError):
            report_hour, report_minute = 18, 30
            logger.warning(f"Invalid daily_report.time '{report_time_str}', using 18:30")

        # Start the background CPU/temp sampler
        self._checker = SystemReadinessChecker(
            cpu_threshold=cpu_threshold,
            temp_threshold=temp_threshold,
            ram_threshold=ram_threshold,
        )

        logger.info(
            f"Daily report scheduled at {report_hour:02d}:{report_minute:02d} "
            f"(CPU<{cpu_threshold}%, temp<{temp_threshold}°C, "
            f"max delay {max_delay_min}min)"
        )

        while self.running:
            now = datetime.now()
            today = str(date.today())

            # Check if it's report time (within a 2-minute window to catch restarts)
            is_report_time = (
                now.hour == report_hour
                and report_minute <= now.minute < report_minute + 2
                and today != self._last_report_date
            )

            if is_report_time:
                logger.info(f"Daily report time reached ({report_time_str}) — checking system load")
                self._fire_report_when_ready(
                    cli_command, port, check_interval, max_delay_min
                )
                self._last_report_date = today

            time.sleep(30)  # Check every 30 seconds

    def _fire_report_when_ready(self, cli_command, port, check_interval, max_delay_min):
        """
        Wait until the system is relaxed, then open the dashboard.
        Gives up and opens anyway after max_delay_min minutes.
        """
        deadline = time.time() + (max_delay_min * 60)
        attempt = 0

        while time.time() < deadline:
            attempt += 1
            is_relaxed, reason = self._checker.is_system_relaxed()

            if is_relaxed:
                if attempt > 1:
                    logger.info(f"System now relaxed after {attempt} checks — opening report")
                open_dashboard_in_terminal(cli_command, port)
                return
            else:
                logger.info(
                    f"Daily report delayed (attempt {attempt}): {reason}. "
                    f"Retrying in {check_interval}s "
                    f"(deadline: {int((deadline - time.time()) / 60)}min)"
                )
                time.sleep(check_interval)

        # Deadline reached — open anyway
        logger.info(f"Daily report max delay reached ({max_delay_min}min) — opening anyway")
        open_dashboard_in_terminal(cli_command, port)

    def stop(self):
        self.running = False
        if self._checker:
            self._checker.stop()
