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


