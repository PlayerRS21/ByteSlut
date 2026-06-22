"""
collectors/apps.py — Active Window & App Usage Tracker
========================================================
This is one of the most important collectors.
It tracks which app you're using at every moment, for how long,
and whether it's running in the foreground or background.

HOW IT WORKS:
  Every 2 seconds, we ask the OS "what window is currently focused?"
  We compare this to the previous check. If the same app is still focused,
  we add 2 seconds to its foreground time. If a different app is focused,
  we switch tracking.

  Background time = we watch /proc to see which apps are running even
  when not in focus.

HOW WE GET THE ACTIVE WINDOW:
  - Hyprland:  hyprctl activewindow -j  (native JSON, super accurate)
  - Sway/wlr:  swaymsg -t get_tree (wlroots-based compositors)
  - X11:       xdotool getactivewindow + xdotool getwindowname
  - Generic:   wmctrl -l (works on many WMs)
  - Fallback:  /proc scanning (finds running apps, not necessarily focused)

FLATPAK DETECTION:
  Flatpak apps run in a sandbox. Their process names look like:
    /app/bin/brave  (inside the Flatpak sandbox)
  We detect this and map it to the real app name + Flatpak ID.
  We also check ~/.local/share/flatpak/app/ for installed Flatpaks.
"""

import os
import re
import time
import json
import subprocess
import threading
import logging
from datetime import datetime, date
from pathlib import Path
from collections import defaultdict

logger = logging.getLogger(__name__)

# How often to sample the active window (seconds)
POLL_INTERVAL = 2

# ── SYSTEM PROCESS BLACKLIST ──────────────────────────────────────────────────
# These are kernel threads, system daemons, and background services that should
# NEVER appear in your app usage list. They have no windows and are not "apps"
# in any meaningful sense. This was causing (sd-pam), (udev-worker), agetty,
# accounts-daemon etc. to dominate the app list.
#
# Patterns: if ANY of these strings appear in the process name, it's filtered out.
_SYSTEM_PREFIXES = (
    "sd-", "kworker", "kthread", "migration/", "rcu_", "watchdog/",
    "ksoftirq", "kdevtmpfs", "netns", "khungtask", "oom_reaper",
    "writeback", "kcompact", "crypto", "kblockd", "edac-", "irq/",
    "scsi_", "nvme-", "mpt/", "ata_", "i915/", "cfg80211", "jbd2/",
    "ext4-", "xfs-", "btrfs", "ipv6_",
)

_SYSTEM_EXACT = frozenset({
    # systemd and its helpers
    "systemd", "systemd-journal", "systemd-udevd", "systemd-logind",
    "systemd-network", "systemd-resolve", "systemd-timesyn", "systemd-oomd",
    "sd-pam",
    # udev
    "udevd", "udev-worker",
    # dbus
    "dbus-daemon", "dbus-broker", "dbus-broker-lau",
    # login/auth
    "agetty", "login", "sddm", "gdm", "lightdm",
    "accounts-daemon", "polkitd", "rtkit-daemon",
    # PAM
    "pam", "unix_chkpwd",
    # hardware/power
    "acpid", "tlp", "thermald", "auto-cpufreq", "powertop",
    "cpupower", "irqbalance",
    # audio (background daemons)
    "pipewire", "pipewire-media-", "wireplumber", "pulseaudio",
    "jackd", "a2jmidid",
    # network daemons
    "NetworkManager", "wpa_supplicant", "dhcpcd", "dhclient",
    "iwd", "connmand", "avahi-daemon", "avahi-dnsconfd",
    "bluetoothd", "obexd",
    # display/compositor (they run but aren't "user apps")
    "Xorg", "Xwayland",
    # cron/timers
    "crond", "anacron", "atd",
    # system tools that aren't user-facing
    "at-spi-bus-laun", "at-spi2-registr", "at-spi2-registr",
    "gvfsd", "gvfsd-fuse", "gvfs-udisks2-vo", "gvfs-mtp-volume",
    "gvfs-afc-volume", "gvfs-goa-volume", "gvfs-gphoto2-vo",
    "udisksd", "upowerd", "colord", "geoclue", "iio-sensor-prox",
    "xdg-desktop-por", "xdg-document-po", "xdg-permission-",
    "gnome-keyring-d", "gcr-ssh-agent", "seahorse",
    "dconf-service", "gsettings-data-",
    "evolution-sourc", "evolution-calen", "evolution-addre",
    "goa-daemon", "goa-identity-se",
    # arch/pacman helpers
    "archlinux-keyri",
    # kernel helpers exposed as processes
    "bsdtar", "gpg-agent", "ssh-agent",
    # shells when they show 0 foreground (background scripts only)
    # Note: bash/zsh/fish are excluded only from BACKGROUND, not foreground
})

_SYSTEM_CONTAINS = (
    "-daemon", "-worker", "-helper", "-agent", "-proxy",
    "-bus", "-registr", "-broker", "kworker", "kthread",
)

# These show in /proc but are kernel threads (wrapped in parens by the kernel)
# e.g. (sd-pam), (udev-worker), (kworker/0:1)
_KERNEL_THREAD_PATTERN = re.compile(r"^\(.*\)$")


def is_system_process(name: str) -> bool:
    """
    Returns True if this process is a system daemon/kernel thread
    that should NOT appear in the user's app usage list.

    This is the fix for (sd-pam), (udev-worker), accounts-daemon,
    agetty, at-spi-bus-laun etc. dominating the app list.
    """
    if not name or len(name) < 2:
        return True

    # Kernel threads are wrapped in parens by the OS: (sd-pam), (kworker/0:1)
    if _KERNEL_THREAD_PATTERN.match(name):
        return True

    name_lower = name.lower().rstrip("0123456789")  # strip trailing numbers

    # Exact match blacklist
    if name in _SYSTEM_EXACT or name_lower in _SYSTEM_EXACT:
        return True

    # Prefix blacklist
    for prefix in _SYSTEM_PREFIXES:
        if name_lower.startswith(prefix):
            return True

    # Contains blacklist
    for fragment in _SYSTEM_CONTAINS:
        if fragment in name_lower:
            return True

    return False


FLATPAK_NAMES = {
    "com.brave.Browser": "brave",
    "org.mozilla.firefox": "firefox",
    "com.google.Chrome": "chrome",
    "org.chromium.Chromium": "chromium",
    "com.spotify.Client": "spotify",
    "org.telegram.desktop": "telegram",
    "com.discordapp.Discord": "discord",
    "com.valvesoftware.Steam": "steam",
    "com.visualstudio.code": "vscode",
    "io.github.celluloid_player.Celluloid": "celluloid",
    "org.videolan.VLC": "vlc",
}


def _find_runtime_dir():
    """
    Find the user's XDG_RUNTIME_DIR even if the env var isn't set.
    Systemd services often don't inherit this. We find it from /run/user/UID/.
    """
    uid = os.getuid()
    candidate = f"/run/user/{uid}"
    if os.path.isdir(candidate):
        return candidate
    return os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}")


def _find_hyprland_signature():
    """
    Find the Hyprland instance signature without needing the env var.
    Hyprland puts its socket at /run/user/UID/hypr/SIGNATURE/.socket.sock
    We scan for it directly.
    """
    # Try env var first (fast path)
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if sig:
        return sig

    # Scan /run/user/UID/hypr/ for any running instance
    runtime_dir = _find_runtime_dir()
    hypr_dir = os.path.join(runtime_dir, "hypr")
    if os.path.isdir(hypr_dir):
        try:
            for entry in os.listdir(hypr_dir):
                socket_path = os.path.join(hypr_dir, entry, ".socket.sock")
                if os.path.exists(socket_path):
                    return entry  # entry IS the signature
        except (PermissionError, OSError):
            pass

    # Also check /tmp/hypr/ (older Hyprland versions used this)
    tmp_hypr = "/tmp/hypr"
    if os.path.isdir(tmp_hypr):
        try:
            for entry in os.listdir(tmp_hypr):
                socket_path = os.path.join(tmp_hypr, entry, ".socket.sock")
                if os.path.exists(socket_path):
                    return entry
        except (PermissionError, OSError):
            pass

    return None


def _find_wayland_display():
    """
    Find the Wayland display socket without needing WAYLAND_DISPLAY env var.
    Scans /run/user/UID/ for wayland-* sockets.
    """
    wayland = os.environ.get("WAYLAND_DISPLAY")
    if wayland:
        return wayland

    runtime_dir = _find_runtime_dir()
    if os.path.isdir(runtime_dir):
        try:
            for entry in os.listdir(runtime_dir):
                if entry.startswith("wayland-"):
                    return entry
        except (PermissionError, OSError):
            pass
    return None


def _load_process_environment(pid: int) -> dict:
    """
    Read the full environment of a running process from /proc/PID/environ.
    This is how we get HYPRLAND_INSTANCE_SIGNATURE and WAYLAND_DISPLAY
    even when the daemon was started without them.

    We read Hyprland's own environment — it definitely has these vars set.
    """
    env = {}
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            # environ file: null-byte separated KEY=VALUE pairs
            data = f.read()
        for item in data.split(b"\x00"):
            if b"=" in item:
                key, _, val = item.partition(b"=")
                env[key.decode("utf-8", errors="replace")] = val.decode("utf-8", errors="replace")
    except (FileNotFoundError, PermissionError):
        pass
    return env


def _inject_wayland_environment():
    """
    If the daemon is missing Wayland/Hyprland env vars (happens when started
    by systemd), find the running Hyprland process and steal its environment.

    This is called ONCE at startup. After this, env vars are set correctly
    and all the normal detection logic works.

    Why this works:
      Hyprland sets HYPRLAND_INSTANCE_SIGNATURE, WAYLAND_DISPLAY,
      XDG_RUNTIME_DIR, DBUS_SESSION_BUS_ADDRESS on itself.
      We read /proc/$(pgrep hyprland)/environ and inject those vars
      into our own process environment with os.environ[key] = val.
    """
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return  # Already have it, nothing to do

    # Find the Hyprland process
    try:
        result = subprocess.run(
            ["pgrep", "-x", "Hyprland"],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode != 0 or not result.stdout.strip():
            # Try lowercase
            result = subprocess.run(
                ["pgrep", "-x", "hyprland"],
                capture_output=True, text=True, timeout=2
            )

        if result.returncode == 0 and result.stdout.strip():
            pid = int(result.stdout.strip().split("\n")[0])
            env = _load_process_environment(pid)

            # Inject the important vars into our process
            important_vars = [
                "HYPRLAND_INSTANCE_SIGNATURE",
                "WAYLAND_DISPLAY",
                "DISPLAY",
                "XDG_RUNTIME_DIR",
                "XDG_CURRENT_DESKTOP",
                "XDG_SESSION_TYPE",
                "DBUS_SESSION_BUS_ADDRESS",
                "HOME",
                "USER",
            ]
            injected = []
            for var in important_vars:
                if var in env and var not in os.environ:
                    os.environ[var] = env[var]
                    injected.append(var)

            if injected:
                logger.info(f"Injected env vars from Hyprland (pid={pid}): {injected}")
            return

    except Exception as e:
        logger.debug(f"Could not inject Hyprland environment: {e}")

    # Fallback: try to find vars directly via socket scan
    sig = _find_hyprland_signature()
    if sig:
        os.environ["HYPRLAND_INSTANCE_SIGNATURE"] = sig
        logger.info(f"Found Hyprland signature via socket scan: {sig}")

    wayland = _find_wayland_display()
    if wayland:
        os.environ["WAYLAND_DISPLAY"] = wayland
        logger.info(f"Found Wayland display via socket scan: {wayland}")

    runtime_dir = _find_runtime_dir()
    if runtime_dir and "XDG_RUNTIME_DIR" not in os.environ:
        os.environ["XDG_RUNTIME_DIR"] = runtime_dir


def get_active_window_hyprland():
    """
    Get active window info from Hyprland via its IPC socket.
    Works even without HYPRLAND_INSTANCE_SIGNATURE env var — we find the socket ourselves.
    """
    sig = _find_hyprland_signature()
    if not sig:
        return None

    # Try using hyprctl (which finds the socket itself via the env var or directly)
    env = dict(os.environ)
    env["HYPRLAND_INSTANCE_SIGNATURE"] = sig
    runtime_dir = _find_runtime_dir()
    if runtime_dir:
        env["XDG_RUNTIME_DIR"] = runtime_dir

    try:
        result = subprocess.run(
            ["hyprctl", "activewindow", "-j"],
            capture_output=True, text=True, timeout=2,
            env=env
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            app_class = data.get("class", "").lower()
            title = data.get("title", "")
            if not app_class or app_class == "null":
                return None
            app_name = (app_class
                        .replace("-browser", "")
                        .replace("-stable", "")
                        .replace("com.brave.browser", "brave"))
            return {"app_name": app_name, "title": title}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        pass

    # Direct socket fallback if hyprctl not found
    try:
        import socket as sock_mod
        runtime = env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        socket_path = f"{runtime}/hypr/{sig}/.socket.sock"
        if not os.path.exists(socket_path):
            # Try /tmp/hypr
            socket_path = f"/tmp/hypr/{sig}/.socket.sock"

        if os.path.exists(socket_path):
            client = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
            client.settimeout(2)
            client.connect(socket_path)
            client.send(b"j/activewindow")
            response = b""
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response += chunk
            client.close()

            data = json.loads(response.decode())
            app_class = data.get("class", "").lower()
            title = data.get("title", "")
            if app_class and app_class != "null":
                app_name = app_class.replace("-browser", "").replace("-stable", "")
                return {"app_name": app_name, "title": title}
    except Exception:
        pass

    return None


def get_active_window_sway():
    """Get active window from Sway or other wlroots compositors."""
    # Set SWAYSOCK if we can find it
    if not os.environ.get("SWAYSOCK"):
        runtime_dir = _find_runtime_dir()
        if runtime_dir:
            import glob
            socks = glob.glob(f"{runtime_dir}/sway-ipc.*.*.sock")
            if socks:
                os.environ["SWAYSOCK"] = socks[0]

    try:
        result = subprocess.run(
            ["swaymsg", "-t", "get_tree"],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode == 0:
            tree = json.loads(result.stdout)
            focused = _find_focused_node(tree)
            if focused:
                return {
                    "app_name": (focused.get("app_id") or
                                 focused.get("window_properties", {}).get("class", "unknown")).lower(),
                    "title": focused.get("name", "")
                }
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        pass
    return None


def get_active_window_x11():
    """Get active window on X11 using xdotool."""
    try:
        win_id_result = subprocess.run(
            ["xdotool", "getactivewindow"],
            capture_output=True, text=True, timeout=2
        )
        if win_id_result.returncode != 0:
            return None
        win_id = win_id_result.stdout.strip()
        class_result = subprocess.run(
            ["xdotool", "getwindowclassname", win_id],
            capture_output=True, text=True, timeout=2
        )
        app_name = class_result.stdout.strip().lower() if class_result.returncode == 0 else "unknown"
        title_result = subprocess.run(
            ["xdotool", "getwindowname", win_id],
            capture_output=True, text=True, timeout=2
        )
        title = title_result.stdout.strip() if title_result.returncode == 0 else ""
        return {"app_name": app_name, "title": title}
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def get_active_window_wmctrl():
    """Fallback: xprop for X11 WMs."""
    try:
        result = subprocess.run(
            ["xprop", "-root", "_NET_ACTIVE_WINDOW"],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode != 0:
            return None
        match = re.search(r"0x[0-9a-f]+", result.stdout)
        if not match:
            return None
        win_id = match.group(0)
        info_result = subprocess.run(
            ["xprop", "-id", win_id, "WM_CLASS", "WM_NAME"],
            capture_output=True, text=True, timeout=2
        )
        app_name = "unknown"
        title = ""
        for line in info_result.stdout.splitlines():
            if "WM_CLASS" in line:
                parts = line.split('"')
                if len(parts) >= 4:
                    app_name = parts[3].lower()
            elif "WM_NAME" in line:
                parts = line.split('"')
                if len(parts) >= 2:
                    title = parts[1]
        return {"app_name": app_name, "title": title}
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def get_active_window():
    """
    Master function — tries all methods, returns the active window.
    IMPORTANT: Calls _inject_wayland_environment() first so systemd
    services work even without env vars.
    """
    # This is called on every poll but _inject only does work once
    # (it returns immediately if HYPRLAND_INSTANCE_SIGNATURE is already set)
    _inject_wayland_environment()

    # Try Hyprland first (always — we can find the socket ourselves now)
    result = get_active_window_hyprland()
    if result and result["app_name"] not in ("", "unknown", "null"):
        return result

    # Sway
    if os.environ.get("SWAYSOCK") or _find_runtime_dir():
        result = get_active_window_sway()
        if result and result["app_name"] not in ("", "unknown"):
            return result

    # X11
    if os.environ.get("DISPLAY"):
        result = get_active_window_x11()
        if result and result["app_name"] not in ("", "unknown"):
            return result
        result = get_active_window_wmctrl()
        if result and result["app_name"] not in ("", "unknown"):
            return result

    return {"app_name": "unknown", "title": ""}


def _find_focused_node(node):
    """Recursively search Sway's tree for the focused window."""
    if node.get("focused"):
        return node
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        result = _find_focused_node(child)
        if result:
            return result
    return None


def detect_flatpak(app_name: str, pid: int = None):
    """
    Check if an app is a Flatpak and return its Flatpak ID.

    Flatpak apps run inside a sandbox. We can detect them by:
    1. Checking if the process cmdline contains '/app/' (Flatpak sandbox path)
    2. Checking the app name against our known Flatpak IDs
    3. Looking at /proc/{pid}/cmdline

    Returns: (is_flatpak: bool, flatpak_id: str or None)
    """
    # Check against known Flatpak names
    for flatpak_id, name in FLATPAK_NAMES.items():
        if name.lower() == app_name.lower() or flatpak_id.lower().endswith(app_name.lower()):
            return True, flatpak_id

    # Check the process cmdline if we have a PID
    if pid:
        try:
            cmdline_path = f"/proc/{pid}/cmdline"
            with open(cmdline_path, "r") as f:
                cmdline = f.read()
            if "/app/bin/" in cmdline or "flatpak" in cmdline.lower():
                return True, None
        except (FileNotFoundError, PermissionError):
            pass

    # Check if app is installed as Flatpak
    flatpak_apps_dir = Path.home() / ".local/share/flatpak/app"
    if flatpak_apps_dir.exists():
        for app_dir in flatpak_apps_dir.iterdir():
            if app_name.lower() in app_dir.name.lower():
                return True, app_dir.name

    return False, None


def get_running_processes():
    """
    Get all currently running processes with their names.
    We use /proc filesystem directly — no external tools needed.

    Returns a dict: {pid: process_name}
    This lets us track background app time.
    """
    processes = {}
    try:
        for entry in os.scandir("/proc"):
            # /proc entries that are numbers are processes
            if not entry.name.isdigit():
                continue
            try:
                # /proc/{pid}/comm contains just the process name
                comm_path = f"/proc/{entry.name}/comm"
                with open(comm_path) as f:
                    name = f.read().strip().lower()
                processes[int(entry.name)] = name
            except (FileNotFoundError, PermissionError):
                continue
    except Exception as e:
        logger.error(f"Failed to read /proc: {e}")
    return processes


class AppCollector:
    """
    Tracks which app is active (foreground) and which are running in background.

    Every POLL_INTERVAL seconds:
    1. Get the currently focused window (foreground app)
    2. Add POLL_INTERVAL seconds to that app's foreground time
    3. Get all running processes and add to their background time
    4. Every 60 seconds, flush the accumulated times to the batch writer
    """

    def __init__(self, batch_writer):
        self.batch_writer = batch_writer
        self.running = False

        # Accumulator: {app_name: {foreground_seconds, background_seconds, title}}
        # We accumulate in RAM and write every 60 seconds
        self.today_accumulator = defaultdict(lambda: {
            "foreground_seconds": 0,
            "background_seconds": 0,
            "last_title": "",
            "is_flatpak": False,
            "flatpak_id": None
        })

        self.last_flush_time = time.time()
        self.flush_interval = 60  # Write to DB every 60 seconds
        self.current_date = str(date.today())

    def register_app(self, app_name: str, is_flatpak: bool, flatpak_id: str):
        """
        Register an app in the app_registry table so it persists even after deletion.
        Uses INSERT OR IGNORE so existing apps aren't overwritten.
        """
        from daemon.db import execute
        today = str(date.today())
        execute("""
            INSERT OR IGNORE INTO app_registry
                (app_name, first_seen, last_seen, is_flatpak, flatpak_id)
            VALUES (?, ?, ?, ?, ?)
        """, (app_name, today, today, 1 if is_flatpak else 0, flatpak_id))

        # Update last_seen for existing apps
        execute("""
            UPDATE app_registry SET last_seen = ? WHERE app_name = ?
        """, (today, app_name))

    def flush_accumulator(self):
        """
        Write accumulated app times to the database.
        Called every 60 seconds.
        """
        if not self.today_accumulator:
            return

        today = str(date.today())

        # If day changed, reset accumulator
        if today != self.current_date:
            self.current_date = today
            self.today_accumulator.clear()
            return

        for app_name, data in self.today_accumulator.items():
            if data["foreground_seconds"] == 0 and data["background_seconds"] == 0:
                continue

            self.batch_writer.add("app_usage", {
                "timestamp": int(time.time()),  # REQUIRED: productivity query uses timestamp BETWEEN
                "date": today,
                "app_name": app_name,
                "window_title": data["last_title"][:500] if data["last_title"] else "",
                "foreground_seconds": data["foreground_seconds"],
                "background_seconds": data["background_seconds"],
                "is_flatpak": 1 if data["is_flatpak"] else 0,
                "flatpak_id": data["flatpak_id"],
            })

            # Also update the permanent registry totals
            from daemon.db import execute
            execute("""
                UPDATE app_registry
                SET total_foreground_seconds = total_foreground_seconds + ?,
                    total_background_seconds = total_background_seconds + ?,
                    last_seen = ?
                WHERE app_name = ?
            """, (data["foreground_seconds"], data["background_seconds"], today, app_name))

        # Reset counters after flushing (don't reset the app entries themselves)
        for app_name in self.today_accumulator:
            self.today_accumulator[app_name]["foreground_seconds"] = 0
            self.today_accumulator[app_name]["background_seconds"] = 0

        self.last_flush_time = time.time()

    def run(self):
        """Main tracking loop."""
        self.running = True
        prev_foreground = None

        logger.info("AppCollector started")

        while self.running:
            try:
                # ── Step 1: Get the currently focused app ──
                window = get_active_window()
                app_name = window["app_name"]
                title = window["title"]

                if app_name and app_name != "unknown" and not is_system_process(app_name):
                    # Check if it's a Flatpak
                    is_flatpak, flatpak_id = detect_flatpak(app_name)

                    # Register in permanent registry
                    if app_name != prev_foreground:
                        self.register_app(app_name, is_flatpak, flatpak_id)
                        prev_foreground = app_name

                    # Add foreground time
                    self.today_accumulator[app_name]["foreground_seconds"] += POLL_INTERVAL
                    self.today_accumulator[app_name]["last_title"] = title
                    self.today_accumulator[app_name]["is_flatpak"] = is_flatpak
                    self.today_accumulator[app_name]["flatpak_id"] = flatpak_id

                # ── Step 2: Track background processes ──
                # Only every 10 seconds, and ONLY user-facing apps (not system daemons)
                if int(time.time()) % 10 == 0:
                    running_procs = get_running_processes()
                    for pid, proc_name in running_procs.items():
                        # Skip system processes — this is the fix for sd-pam, udev-worker, etc.
                        if is_system_process(proc_name):
                            continue
                        # Skip the foreground app (already counted above)
                        if proc_name == app_name:
                            continue
                        self.today_accumulator[proc_name]["background_seconds"] += 10

                # ── Step 3: Flush to DB every 60 seconds ──
                if time.time() - self.last_flush_time >= self.flush_interval:
                    self.flush_accumulator()

            except Exception as e:
                logger.error(f"AppCollector error: {e}")

            time.sleep(POLL_INTERVAL)

    def stop(self):
        self.running = False
        self.flush_accumulator()  # Final flush
        logger.info("AppCollector stopped")
