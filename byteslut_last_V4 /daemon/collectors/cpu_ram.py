"""
collectors/cpu_ram.py — System Performance & Temperature Tracker
=================================================================
Tracks: CPU usage, RAM, swap, disk I/O, CPU/GPU temperatures.

WHY psutil?
  psutil is a Python library that reads /proc/stat, /proc/meminfo,
  /sys/class/thermal, etc. for us. It's much cleaner than parsing
  those files manually and works on all Linux systems.

HOW TEMPERATURES WORK:
  Linux exposes temperatures via /sys/class/thermal/thermal_zone*/temp
  Each zone is a different sensor. We identify which zone is CPU
  and which is GPU by reading the zone type file.

SAMPLING STRATEGY:
  We sample every 30 seconds and store a snapshot.
  For daily min/max/avg, we query the stored snapshots at end of day.
  This gives accurate historical data without writing every second.
"""

import time
import logging
from datetime import datetime, date

logger = logging.getLogger(__name__)

# Try to import psutil — it's our main tool for system stats
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    logger.warning("psutil not installed. Install with: pip install psutil")


def read_thermal_zones():
    """
    Read CPU and GPU temperatures from /sys/class/thermal/.

    Linux stores temperatures in files like:
      /sys/class/thermal/thermal_zone0/temp  → temperature in millidegrees (85000 = 85°C)
      /sys/class/thermal/thermal_zone0/type  → "x86_pkg_temp" or "gpu_core" etc.

    Returns: dict with 'cpu_temp' and 'gpu_temp' in Celsius, or None if unavailable.
    """
    import os
    temps = {"cpu_temp": None, "gpu_temp": None}

    thermal_base = "/sys/class/thermal"
    if not os.path.exists(thermal_base):
        return temps

    try:
        for zone_dir in sorted(os.listdir(thermal_base)):
            if not zone_dir.startswith("thermal_zone"):
                continue

            zone_path = f"{thermal_base}/{zone_dir}"
            type_file = f"{zone_path}/type"
            temp_file = f"{zone_path}/temp"

            if not (os.path.exists(type_file) and os.path.exists(temp_file)):
                continue

            try:
                with open(type_file) as f:
                    zone_type = f.read().strip().lower()
                with open(temp_file) as f:
                    # Temperature is stored in millidegrees Celsius
                    temp_mc = int(f.read().strip())
                    temp_c = temp_mc / 1000.0  # Convert to Celsius
            except (ValueError, IOError):
                continue

            # CPU thermal zones have these type names:
            cpu_types = ["x86_pkg_temp", "cpu_thermal", "acpitz", "coretemp", "cpu"]
            gpu_types = ["gpu_core", "gpu_memory", "amdgpu", "nvidia"]

            if any(t in zone_type for t in cpu_types):
                # Take the maximum if multiple CPU zones
                if temps["cpu_temp"] is None or temp_c > temps["cpu_temp"]:
                    temps["cpu_temp"] = temp_c

            elif any(t in zone_type for t in gpu_types):
                if temps["gpu_temp"] is None or temp_c > temps["gpu_temp"]:
                    temps["gpu_temp"] = temp_c

    except Exception as e:
        logger.debug(f"Thermal zone read error: {e}")

    # Fallback: use psutil's temperature reading if sysfs failed
    if PSUTIL_AVAILABLE and temps["cpu_temp"] is None:
        try:
            sensor_temps = psutil.sensors_temperatures()
            if sensor_temps:
                # Try common CPU sensor names
                for name in ["coretemp", "k10temp", "acpitz", "cpu_thermal"]:
                    if name in sensor_temps:
                        entries = sensor_temps[name]
                        if entries:
                            # Get the package/max temp
                            pkg = [e for e in entries if "package" in e.label.lower()]
                            temps["cpu_temp"] = pkg[0].current if pkg else entries[0].current
                            break

                # Try GPU sensors
                for name in ["amdgpu", "nvidia", "radeon"]:
                    if name in sensor_temps:
                        entries = sensor_temps[name]
                        if entries:
                            temps["gpu_temp"] = entries[0].current
                            break
        except Exception as e:
            logger.debug(f"psutil temperature read failed: {e}")

    return temps


def get_disk_io():
    """
    Get disk read/write statistics.
    We compare current counters to previous ones to get the CHANGE (delta).

    /proc/diskstats gives cumulative totals since boot.
    By storing the last reading, we calculate bytes transferred since last check.
    """
    if not PSUTIL_AVAILABLE:
        return {"disk_read_mb": 0, "disk_write_mb": 0}

    try:
        io = psutil.disk_io_counters(perdisk=False)
        return {
            "disk_read_mb": io.read_bytes / (1024 * 1024),
            "disk_write_mb": io.write_bytes / (1024 * 1024)
        }
    except Exception:
        return {"disk_read_mb": 0, "disk_write_mb": 0}


class SystemStatsCollector:
    """
    Collects CPU, RAM, swap, disk, and temperature data.

    Collection frequency: every 30 seconds (configurable)
    This is very lightweight — just reading /proc files.

    Daily temperature summary (min/max/avg) is computed and stored
    at the end of each day (or at startup for yesterday if missed).
    """

    def __init__(self, batch_writer, interval=30):
        self.batch_writer = batch_writer
        self.interval = interval
        self.running = False

        today = str(date.today())

        # Track daily temperature extremes in RAM.
        # On startup, reload today's existing readings from temperature_daily
        # so a daemon restart (e.g. after update) doesn't lose the true
        # daily min that was recorded before the restart.
        # Without this: restart at 14:00 after a cold morning → daily_temps
        # starts empty → new min is whatever current temp is (70°C) →
        # the 45°C morning reading is lost → wrong min shown on dashboard.
        self.daily_temps = self._load_or_init_today(today)

        # For delta disk I/O calculation
        self.last_disk_read = 0
        self.last_disk_write = 0
        self._init_disk_baseline()

    def _load_or_init_today(self, today: str) -> dict:
        """
        Try to load today's existing temperature data from the DB.
        If not found, start fresh.

        We store pre-existing min and max as the starting bounds for today's
        in-memory accumulation. New readings still get appended to cpu_temps[]
        for a live avg, but min/max are bounded against the DB values so a
        restart mid-day never shows a wrong (too-high) minimum.
        """
        result = {"date": today, "cpu_temps": [], "gpu_temps": [],
                  "saved_cpu_min": None, "saved_cpu_max": None,
                  "saved_gpu_min": None, "saved_gpu_max": None}
        try:
            from daemon.db import query as db_query
            row = db_query(
                "SELECT cpu_min, cpu_max, cpu_avg, gpu_min, gpu_max "
                "FROM temperature_daily WHERE date = ?",
                (today,), fetch="one"
            )
            if row:
                result["saved_cpu_min"] = row["cpu_min"]
                result["saved_cpu_max"] = row["cpu_max"]
                result["saved_gpu_min"] = row["gpu_min"]
                result["saved_gpu_max"] = row["gpu_max"]
                logger.info(
                    f"SystemStatsCollector: loaded existing today temps "
                    f"CPU min={row['cpu_min']} max={row['cpu_max']}"
                )
        except Exception as e:
            logger.debug(f"Could not load existing daily temps: {e}")
        return result

    def _init_disk_baseline(self):
        """Get initial disk I/O counters to calculate deltas."""
        if PSUTIL_AVAILABLE:
            try:
                io = psutil.disk_io_counters()
                self.last_disk_read = io.read_bytes
                self.last_disk_write = io.write_bytes
            except Exception:
                pass

    def collect(self):
        """
        Collect one snapshot of system stats.
        Called every 'interval' seconds.
        """
        today = str(date.today())

        # ── If day changed, save yesterday's temp summary ──
        if today != self.daily_temps["date"]:
            self._save_daily_temp_summary()
            self.daily_temps = {"date": today, "cpu_temps": [], "gpu_temps": []}

        snapshot = {"date": today}

        # ── CPU Stats ──
        if PSUTIL_AVAILABLE:
            try:
                snapshot["cpu_percent"] = psutil.cpu_percent(interval=1)
                freq = psutil.cpu_freq()
                snapshot["cpu_freq_mhz"] = freq.current if freq else None
            except Exception:
                snapshot["cpu_percent"] = None
                snapshot["cpu_freq_mhz"] = None

            # ── RAM Stats ──
            try:
                mem = psutil.virtual_memory()
                snapshot["ram_percent"] = mem.percent
                snapshot["ram_used_mb"] = mem.used / (1024 * 1024)
                swap = psutil.swap_memory()
                snapshot["swap_percent"] = swap.percent
            except Exception:
                snapshot["ram_percent"] = None
                snapshot["ram_used_mb"] = None
                snapshot["swap_percent"] = None

            # ── Disk I/O Delta ──
            try:
                io = psutil.disk_io_counters()
                read_delta = (io.read_bytes - self.last_disk_read) / (1024 * 1024)
                write_delta = (io.write_bytes - self.last_disk_write) / (1024 * 1024)
                snapshot["disk_read_mb"] = max(0, read_delta)
                snapshot["disk_write_mb"] = max(0, write_delta)
                self.last_disk_read = io.read_bytes
                self.last_disk_write = io.write_bytes
            except Exception:
                snapshot["disk_read_mb"] = 0
                snapshot["disk_write_mb"] = 0

        # ── Temperatures ──
        temps = read_thermal_zones()
        snapshot["cpu_temp"] = temps["cpu_temp"]
        snapshot["gpu_temp"] = temps["gpu_temp"]

        # Accumulate temps for daily summary
        if temps["cpu_temp"]:
            self.daily_temps["cpu_temps"].append(temps["cpu_temp"])
        if temps["gpu_temp"]:
            self.daily_temps["gpu_temps"].append(temps["gpu_temp"])

        # Write to batch
        self.batch_writer.add("system_stats", snapshot)

        # Write temperature_daily immediately (INSERT OR REPLACE) so the
        # dashboard always shows the current running min/max/avg, not just
        # what was saved at the end of the previous day.
        # This fixes the "82°C then scrolls to 83.7°C" inconsistency.
        self._save_daily_temp_summary()

    def _save_daily_temp_summary(self):
        """
        Calculate and store the daily min/max/avg temperatures.
        Respects any saved_cpu_min/max loaded from the DB on startup,
        so a mid-day restart never raises the minimum or lowers the maximum.
        """
        from daemon.db import execute

        d = self.daily_temps
        if not d["cpu_temps"] and not d["gpu_temps"]:
            # No new readings since start — nothing to update
            return

        # Compute from current in-memory readings
        cpu_min_now = min(d["cpu_temps"]) if d["cpu_temps"] else None
        cpu_max_now = max(d["cpu_temps"]) if d["cpu_temps"] else None
        cpu_avg     = (sum(d["cpu_temps"]) / len(d["cpu_temps"])
                       if d["cpu_temps"] else None)
        gpu_min_now = min(d["gpu_temps"]) if d["gpu_temps"] else None
        gpu_max_now = max(d["gpu_temps"]) if d["gpu_temps"] else None
        gpu_avg     = (sum(d["gpu_temps"]) / len(d["gpu_temps"])
                       if d["gpu_temps"] else None)

        # Merge with saved values from before restart.
        # min: take the lower of saved vs current (never raise the true minimum)
        # max: take the higher of saved vs current (never lower the true maximum)
        def _merge_min(saved, current):
            if saved is None: return current
            if current is None: return saved
            return min(saved, current)

        def _merge_max(saved, current):
            if saved is None: return current
            if current is None: return saved
            return max(saved, current)

        cpu_min = _merge_min(d.get("saved_cpu_min"), cpu_min_now)
        cpu_max = _merge_max(d.get("saved_cpu_max"), cpu_max_now)
        gpu_min = _merge_min(d.get("saved_gpu_min"), gpu_min_now)
        gpu_max = _merge_max(d.get("saved_gpu_max"), gpu_max_now)

        execute("""
            INSERT OR REPLACE INTO temperature_daily
                (date, cpu_min, cpu_max, cpu_avg, gpu_min, gpu_max, gpu_avg)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (d["date"], cpu_min, cpu_max, cpu_avg, gpu_min, gpu_max, gpu_avg))

        logger.info(
            f"Saved daily temp summary for {d['date']}: "
            f"CPU {cpu_min:.1f}/{cpu_max:.1f}°C avg={cpu_avg:.1f}°C"
            if cpu_min and cpu_max and cpu_avg else
            f"Saved daily temp summary for {d['date']}"
        )

    def run(self):
        """Main collection loop."""
        self.running = True
        logger.info(f"SystemStatsCollector started (interval: {self.interval}s)")

        while self.running:
            try:
                self.collect()
            except Exception as e:
                logger.error(f"SystemStats collection error: {e}")
            time.sleep(self.interval)

    def stop(self):
        """Stop and save final daily summary."""
        self.running = False
        self._save_daily_temp_summary()
        logger.info("SystemStatsCollector stopped")
