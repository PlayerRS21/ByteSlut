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


