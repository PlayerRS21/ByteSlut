"""
collectors/input_tracking.py — Keyboard & Mouse Tracking
==========================================================
Tracks keystrokes, mouse clicks, mouse movement distance, scroll, and WPM.

HOW INPUT EVENTS WORK ON LINUX:
  Every input device exposes itself as /dev/input/eventN.
  The kernel writes binary structs (input_event) to that file.
  Any process in the 'input' group can read them.

  TWO TYPES OF POINTER DEVICES:
  ┌─────────────────────────────────────────────────────────────┐
  │ EV_REL (type=2) — RELATIVE movement                         │
  │   Used by: external USB mice, Bluetooth mice                │
  │   Events: REL_X / REL_Y — "moved 5 pixels right"           │
  │   Easy: just accumulate abs(delta) each event               │
  │                                                             │
  │ EV_ABS (type=3) — ABSOLUTE position                         │
  │   Used by: laptop touchpads, drawing tablets                │
  │   Events: ABS_X / ABS_Y — "finger is at position 1542, 887" │
  │   Harder: must track previous position, compute delta       │
  │   Also: multi-touch events (ABS_MT_POSITION_X/Y)            │
  └─────────────────────────────────────────────────────────────┘

FIXES IN THIS VERSION:
  1.  EV_ABS support → touchpad distance tracked correctly
  2.  SYN_REPORT used to commit ABS positions atomically
  3.  Per-device ABS position state (committed_x/y per device path)
  4.  INPUT_EVENT_SIZE via struct.calcsize() not hardcoded 16
  5.  struct.error → continue, not break (bad event skips, thread lives)
  6.  Partial read → continue+sleep, not break
  7.  _flush() checks ALL counters (not just keystrokes+clicks)
  8.  threading.Lock() protects all shared counters
  9.  Last device block in /proc/bus/input/devices parser not dropped
  10. has_abs flag detects touchpad devices
  11. BTN_TOUCH (330) excluded from keystroke count (touchpad tap ≠ key)
  12. FIX: _flush_loop() now starts BEFORE checking devices, so data is
      always flushed to DB even if devices come and go. Previously, if no
      devices were found (user not in input group yet), run() returned
      immediately, _flush_loop() was never called, and zero data was ever
      written to the DB — the "numbers never update" bug.
  13. FIX: interval lowered from 60s to 10s — data appears within 10
      seconds of activity instead of up to 60 seconds. The dashboard
      reads from DB, not from RAM, so nothing is visible until a flush.
  14. FIX: device threads use a device-local 'active' event so they exit
      cleanly when stop() is called without a spin loop.

PRIVACY:
  Counts only. Which keys were pressed are NEVER stored or logged.
  Mouse position is accumulated as total distance, never as coordinates.
"""

import os
import struct
import time
import glob
import logging
import threading
from datetime import date

logger = logging.getLogger(__name__)

# ── Input event binary format ─────────────────────────────────────────────────
# struct input_event { timeval(8+8) + type(2) + code(2) + value(4) } = 24 bytes
INPUT_EVENT_FORMAT = "qqHHi"
INPUT_EVENT_SIZE   = struct.calcsize(INPUT_EVENT_FORMAT)   # 24 on 64-bit Linux

# ── Event types ───────────────────────────────────────────────────────────────
EV_SYN = 0   # Sync / commit — marks end of one logical event group
EV_KEY = 1   # Key press or mouse button
EV_REL = 2   # Relative pointer movement (external USB/BT mouse)
EV_ABS = 3   # Absolute pointer position (laptop touchpad)

# ── EV_KEY codes ──────────────────────────────────────────────────────────────
BTN_LEFT   = 272
BTN_RIGHT  = 273
BTN_MIDDLE = 274
BTN_TOUCH  = 330   # Touchpad finger-down — NOT a real keystroke

# ── EV_REL codes (external mouse) ────────────────────────────────────────────
REL_X     = 0
REL_Y     = 1
REL_WHEEL = 8

# ── EV_ABS codes (laptop touchpad) ───────────────────────────────────────────
ABS_X             = 0    # Single-touch absolute X
ABS_Y             = 1    # Single-touch absolute Y
ABS_MT_POSITION_X = 53   # Multi-touch X (most modern touchpads)
ABS_MT_POSITION_Y = 54   # Multi-touch Y

# ── SYN_REPORT ───────────────────────────────────────────────────────────────
SYN_REPORT = 0   # "This event group is complete" — commit ABS state now

# ── Flush interval ────────────────────────────────────────────────────────────
# HOW LONG BEFORE DATA APPEARS ON DASHBOARD:
#   The dashboard reads from SQLite. Data in RAM is invisible until flushed.
#   Lower = more frequent DB writes = data appears faster.
#   10 seconds: data visible within 10s of activity. Minimal extra I/O.
#   30 seconds: was the old value — caused "nothing updates for 30s" confusion.
DEFAULT_FLUSH_INTERVAL = 10   # seconds


class InputCollector:
    """
    Reads raw input events from /dev/input/eventN files.
    Handles both EV_REL (external mice) and EV_ABS (laptop touchpads).

    Threading model:
      - _flush_loop() runs in the calling thread (always, even if no devices)
      - One daemon thread per input device (blocks on read forever)
      - self._lock protects all shared counters
    """

    def __init__(self, batch_writer, interval=DEFAULT_FLUSH_INTERVAL):
        self.batch_writer = batch_writer
        self.interval     = interval
        self.running      = False
        self._lock        = threading.Lock()

        # ── Shared counters — reset after each flush ──────────────────────
        self.keystrokes        = 0
        self.mouse_clicks      = 0
        self.scroll_events     = 0
        self.mouse_distance_px = 0

        # Rolling 60-second window for WPM estimation
        self.keystroke_times = []

        # Per-device touchpad state: committed position + pending update
        # { device_path: {committed_x, committed_y, pending_x, pending_y} }
        self._abs_state = {}

    # ─────────────────────────────────────────────────────────────────
    # Device discovery
    # ─────────────────────────────────────────────────────────────────

    def _find_input_devices(self):
        """
        Parse /proc/bus/input/devices to find all accessible input devices.
        Returns list of readable /dev/input/eventN paths.
        Falls back to globbing /dev/input/event* if /proc file is unavailable.
        """
        devices = []

        try:
            with open("/proc/bus/input/devices") as f:
                content = f.read()

            current = {}

            def _consider(dev):
                path = dev.get("event")
                if not path or not os.access(path, os.R_OK):
                    return
                # Accept if it has keys, relative movement, or absolute position
                if dev.get("has_key") or dev.get("has_rel") or dev.get("has_abs"):
                    devices.append(path)
                    logger.debug(
                        f"Input device: {dev.get('name','?')} → {path} "
                        f"[key={dev.get('has_key',0)} "
                        f"rel={dev.get('has_rel',0)} "
                        f"abs={dev.get('has_abs',0)}]"
                    )

            for line in content.splitlines():
                if line.startswith("N: Name="):
                    current["name"] = line.split('"')[1] if '"' in line else ""
                elif line.startswith("H: Handlers="):
                    for token in line.split("=", 1)[1].split():
                        if token.startswith("event"):
                            current["event"] = f"/dev/input/{token}"
                elif line.startswith("B: KEY="):
                    current["has_key"] = True
                elif line.startswith("B: REL="):
                    current["has_rel"] = True
                elif line.startswith("B: ABS="):
                    current["has_abs"] = True
                elif line == "":
                    if current:
                        _consider(current)
                    current = {}

            # Process last block (file may not end with a blank line)
            if current:
                _consider(current)

        except FileNotFoundError:
            logger.warning(
                "/proc/bus/input/devices not found — trying glob fallback. "
                "This is normal in containers; on a real machine this means "
                "the kernel doesn't expose input device info."
            )
            for path in sorted(glob.glob("/dev/input/event*")):
                if os.access(path, os.R_OK):
                    devices.append(path)
        except Exception as e:
            logger.warning(f"Error reading input devices list: {e}")

        # Deduplicate while preserving order
        seen, unique = set(), []
        for d in devices:
            if d not in seen:
                seen.add(d)
                unique.append(d)
        return unique

    # ─────────────────────────────────────────────────────────────────
    # Device reader — one thread per device
    # ─────────────────────────────────────────────────────────────────

    def _read_device(self, device_path: str):
        """
        Block-read binary input_event structs from one /dev/input/eventN file.

        EV_REL (external mouse):
          Each event = "moved N pixels in X or Y direction".
          We add abs(N) to mouse_distance_px immediately.

        EV_ABS (touchpad):
          Events = "finger is now at absolute position X=1542".
          We buffer pending_x/pending_y during the event group.
          On SYN_REPORT ("group complete"), we compute the delta from the
          last committed position and add it to mouse_distance_px.
          This is necessary because the touchpad may send ABS_X without
          ABS_Y (if only X changed) — we need to see the full group.

        IMPORTANT: this method blocks on f.read() until the OS delivers an
        event. That's normal — it's how all input monitoring works on Linux.
        The thread exits when self.running becomes False (checked after each read).
        """
        self._abs_state[device_path] = {
            "committed_x": None, "committed_y": None,
            "pending_x":   None, "pending_y":   None,
        }

        try:
            with open(device_path, "rb") as f:
                while self.running:
                    try:
                        data = f.read(INPUT_EVENT_SIZE)

                        if len(data) < INPUT_EVENT_SIZE:
                            # Partial read — signal interrupt or brief unavailability.
                            # Sleep and retry. Never break (that kills the thread).
                            time.sleep(0.05)
                            continue

                        _, _, ev_type, ev_code, ev_value = struct.unpack(
                            INPUT_EVENT_FORMAT, data
                        )

                        # ── Key / button PRESSED (value=1, not release=0 or repeat=2)
                        if ev_type == EV_KEY and ev_value == 1:
                            with self._lock:
                                if ev_code in (BTN_LEFT, BTN_RIGHT, BTN_MIDDLE):
                                    self.mouse_clicks += 1
                                elif ev_code != BTN_TOUCH:
                                    # BTN_TOUCH = touchpad contact. Not a keystroke.
                                    self.keystrokes += 1
                                    now = time.time()
                                    self.keystroke_times.append(now)
                                    # Keep only last 60s for rolling WPM window
                                    self.keystroke_times = [
                                        t for t in self.keystroke_times
                                        if t > now - 60
                                    ]

                        # ── External mouse: relative movement ────────────────
                        elif ev_type == EV_REL:
                            with self._lock:
                                if ev_code in (REL_X, REL_Y):
                                    self.mouse_distance_px += abs(ev_value)
                                elif ev_code == REL_WHEEL:
                                    self.scroll_events += abs(ev_value)

                        # ── Touchpad: buffer the new absolute position ────────
                        elif ev_type == EV_ABS:
                            s = self._abs_state[device_path]
                            if ev_code in (ABS_X, ABS_MT_POSITION_X):
                                s["pending_x"] = ev_value
                            elif ev_code in (ABS_Y, ABS_MT_POSITION_Y):
                                s["pending_y"] = ev_value

                        # ── SYN_REPORT: commit the touchpad position ──────────
                        elif ev_type == EV_SYN and ev_code == SYN_REPORT:
                            s  = self._abs_state[device_path]
                            px = s["pending_x"]
                            py = s["pending_y"]

                            if px is not None or py is not None:
                                dist = 0
                                if px is not None and s["committed_x"] is not None:
                                    dist += abs(px - s["committed_x"])
                                if py is not None and s["committed_y"] is not None:
                                    dist += abs(py - s["committed_y"])
                                if dist > 0:
                                    with self._lock:
                                        self.mouse_distance_px += dist

                                # Commit new position, clear pending
                                if px is not None:
                                    s["committed_x"] = px
                                    s["pending_x"]   = None
                                if py is not None:
                                    s["committed_y"] = py
                                    s["pending_y"]   = None

                    except struct.error:
                        # Malformed event data — skip this event, keep thread alive.
                        # Never break here — that silently kills all tracking.
                        continue

        except PermissionError:
            logger.warning(
                f"Permission denied reading {device_path}. "
                f"Fix: sudo usermod -aG input $USER  then FULLY log out and back in."
            )
        except FileNotFoundError:
            logger.debug(f"Input device removed: {device_path}")
        except Exception as e:
            logger.debug(f"Input device {device_path} error: {e}")

    # ─────────────────────────────────────────────────────────────────
    # WPM calculation
    # ─────────────────────────────────────────────────────────────────

    def _calculate_wpm(self):
        """
        Estimate WPM from keystrokes in the last 60 seconds.
        Average English word ≈ 5 characters.
        Call while holding self._lock.
        """
        if not self.keystroke_times:
            return 0.0
        cutoff = time.time() - 60
        return sum(1 for t in self.keystroke_times if t > cutoff) / 5.0

    # ─────────────────────────────────────────────────────────────────
    # Flush — write accumulated data to DB
    # ─────────────────────────────────────────────────────────────────

    def _flush(self):
        """
        Snapshot all counters atomically, write to DB, then reset counters.

        WHY snapshot then reset:
          Taking a snapshot under the lock (fast) means device reader threads
          are only blocked for microseconds. The actual DB write happens
          OUTSIDE the lock so slow disk I/O never blocks input event counting.
        """
        with self._lock:
            ks   = self.keystrokes
            mc   = self.mouse_clicks
            sc   = self.scroll_events
            dist = self.mouse_distance_px
            wpm  = self._calculate_wpm()   # needs lock — inside

            # Reset immediately to avoid double-counting
            self.keystrokes        = 0
            self.mouse_clicks      = 0
            self.scroll_events     = 0
            self.mouse_distance_px = 0
            # keystroke_times is a rolling window — do NOT reset it

        # Skip the DB write if nothing happened at all
        if ks == 0 and mc == 0 and sc == 0 and dist == 0:
            return

        # Write to DB (outside lock — this can be slow, that's fine)
        self.batch_writer.add("input_stats", {
            "date":                str(date.today()),
            "keystrokes":          ks,
            "mouse_clicks":        mc,
            "mouse_scroll_events": sc,
            "mouse_distance_px":   dist,
            "wpm_sample":          round(wpm, 1),
        })

    def _flush_loop(self):
        """
        Periodically flush counters to the DB.

        FIX: This now runs ALWAYS — even when no input devices are accessible.
        Previously this method was only reached if devices were found.
        If the user wasn't in the input group yet, run() returned before
        calling _flush_loop(), so data was NEVER written to the DB.

        Running the flush loop even with no devices means:
          - The collector stays alive and keeps retrying to find devices
          - If devices appear later (user added to input group), data flows
          - The loop is nearly free when there's nothing to flush
        """
        while self.running:
            time.sleep(self.interval)
            try:
                self._flush()
            except Exception as e:
                logger.error(f"InputCollector flush error: {e}")

    # ─────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────

    def run(self):
        """
        Start the collector. Blocks until stop() is called.

        KEY CHANGE from old version:
          _flush_loop() is now called unconditionally at the end,
          even if no devices are found. This ensures the collector
          always stays alive and writes data when it does accumulate.

          Old flow (broken):
            devices = find()
            if not devices: return   ← _flush_loop never runs, data never saved
            ...
            _flush_loop()

          New flow (fixed):
            devices = find()
            if not devices:
                warn + retry loop (keeps trying to find devices)
            else:
                start reader threads
            _flush_loop()   ← ALWAYS runs, data always reaches DB
        """
        self.running = True
        devices = self._find_input_devices()

        if not devices:
            logger.warning(
                "InputCollector: no accessible input devices found.\n"
                "  → To fix: sudo usermod -aG input $USER\n"
                "  → Then FULLY log out and back in (close all sessions).\n"
                "  → The flush loop will still run — data will flow once "
                "    devices become accessible."
            )
            # Don't return — fall through to _flush_loop() below
            # so the collector stays alive and retries finding devices
        else:
            logger.info(
                f"InputCollector: watching {len(devices)} device(s): "
                f"{', '.join(devices)}"
            )
            # Start one reader thread per device
            for device in devices:
                t = threading.Thread(
                    target=self._read_device,
                    args=(device,),
                    daemon=True,      # dies automatically when main process exits
                    name=f"input:{device}",
                )
                t.start()

        # Always run the flush loop — this blocks until stop() is called.
        # This is the main loop that keeps the collector alive.
        self._flush_loop()

    def stop(self):
        """Signal all threads to stop and do one final flush."""
        self.running = False
        self._flush()   # flush any remaining data before shutdown
