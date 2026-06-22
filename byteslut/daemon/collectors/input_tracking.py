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

IDLE DETECTION (the fix for Error 1):
  We expose a module-level LAST_INPUT_TIME variable.
  Every keypress or mouse click updates it to time.time().
  session.py's get_idle_seconds() reads this to know how long
  since the user last touched any input device.
  This works perfectly on Wayland/Hyprland where loginctl idle
  hints are never set by the compositor.
"""

import os
import struct
import time
import glob
import logging
import threading
from datetime import date

logger = logging.getLogger(__name__)

# ── Idle detection: shared with session.py ──────────────────────────
# Updated on every keypress, mouse click, or scroll event.
# session.py reads this to compute idle seconds WITHOUT needing loginctl.
LAST_INPUT_TIME: float = time.time()

def get_last_input_time() -> float:
    """Return the timestamp of the last user input event (key/click/scroll)."""
    return LAST_INPUT_TIME


# ── Constants ──────────────────────────────────────────────────────
DEFAULT_FLUSH_INTERVAL = 10   # seconds between DB writes

EV_KEY = 1    # keyboard key / mouse button event type
EV_REL = 2    # relative mouse movement
EV_ABS = 3    # absolute position (touchpad)
EV_SYN = 0    # sync event (marks end of a frame)

REL_X  = 0; REL_Y  = 1       # relative motion axes
ABS_X  = 0; ABS_Y  = 1       # absolute position axes
ABS_MT_POSITION_X = 53        # multitouch X
ABS_MT_POSITION_Y = 54        # multitouch Y
SYN_REPORT = 0                # sub-type of EV_SYN

BTN_LEFT   = 272; BTN_RIGHT = 273; BTN_MIDDLE = 274
BTN_TOUCH  = 330              # touchpad contact — excluded from keystroke count
REL_WHEEL  = 8; REL_HWHEEL = 6

# Bytes in one input_event struct: {unsigned long, unsigned long, unsigned short, unsigned short, int}
INPUT_EVENT_FMT  = "llHHi"
INPUT_EVENT_SIZE = struct.calcsize(INPUT_EVENT_FMT)


class InputCollector:
    """
    Reads raw input events from /dev/input/eventN files.
    Handles both EV_REL (external mice) and EV_ABS (laptop touchpads).
    Updates module-level LAST_INPUT_TIME for idle detection.

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

        # Load privacy settings from config
        self._track_keystrokes  = True
        self._track_typed_words = True
        self._reload_privacy()

        # ── Shared counters — reset after each flush ──────────────────
        self.keystrokes        = 0
        self.mouse_clicks      = 0
        self.scroll_events     = 0
        self.mouse_distance_px = 0

        # Rolling 60-second window for WPM estimation
        self.keystroke_times = []

        # Per-device touchpad state: committed position + pending update
        self._abs_state = {}

    def _reload_privacy(self):
        try:
            from web.utils import load_config
            priv = load_config().get("privacy", {})
            self._track_keystrokes  = priv.get("track_keystrokes",  True)
            self._track_typed_words = priv.get("track_typed_words", True)
        except Exception:
            pass

    def reload_privacy(self):
        self._reload_privacy()

    # ── Input event processing ────────────────────────────────────────

    def _update_last_input(self):
        """
        Update the module-level LAST_INPUT_TIME.
        Called on every real user input event (key, click, scroll, move).
        session.py uses this to measure idle time on Wayland.
        """
        global LAST_INPUT_TIME
        LAST_INPUT_TIME = time.time()

    def _handle_key(self, code: int, value: int):
        """EV_KEY handler — counts keystrokes and mouse buttons."""
        if not self._track_keystrokes:
            return
        if value != 1:   # 1=press, 0=release, 2=repeat — only count press
            return
        if code == BTN_TOUCH:
            return       # touchpad contact, not a real click

        self._update_last_input()

        with self._lock:
            if code in (BTN_LEFT, BTN_RIGHT, BTN_MIDDLE):
                self.mouse_clicks += 1
            else:
                self.keystrokes += 1
                if self._track_typed_words:
                    now = time.time()
                    self.keystroke_times.append(now)
                    # Keep only last 60 seconds
                    cutoff = now - 60
                    self.keystroke_times = [t for t in self.keystroke_times if t > cutoff]

    def _handle_rel(self, code: int, value: int):
        """EV_REL handler — relative mouse movement and scroll."""
        self._update_last_input()
        with self._lock:
            if code == REL_X or code == REL_Y:
                self.mouse_distance_px += abs(value)
            elif code in (REL_WHEEL, REL_HWHEEL):
                self.scroll_events += abs(value)

    def _handle_abs(self, device_path: str, code: int, value: int, syn: bool):
        """
        EV_ABS handler — touchpad absolute positioning.
        Accumulates distance only when SYN_REPORT fires (atomic frame).
        """
        state = self._abs_state.setdefault(device_path, {
            "pending_x": None, "pending_y": None,
            "committed_x": None, "committed_y": None,
        })

        if code == ABS_X or code == ABS_MT_POSITION_X:
            state["pending_x"] = value
        elif code == ABS_Y or code == ABS_MT_POSITION_Y:
            state["pending_y"] = value

        if syn:
            # Commit the pending frame
            px, py = state["pending_x"], state["pending_y"]
            cx, cy = state["committed_x"], state["committed_y"]
            if px is not None and py is not None:
                if cx is not None and cy is not None:
                    dx = abs(px - cx)
                    dy = abs(py - cy)
                    if dx > 0 or dy > 0:
                        self._update_last_input()
                        with self._lock:
                            self.mouse_distance_px += int((dx**2 + dy**2) ** 0.5)
                state["committed_x"] = px
                state["committed_y"] = py
                state["pending_x"] = None
                state["pending_y"] = None

    # ── Device reader thread ──────────────────────────────────────────

    def _read_device(self, device_path: str, stop_event: threading.Event):
        """
        Blocking read loop for one /dev/input/eventN device.
        Runs in its own daemon thread. Exits when stop_event is set.
        """
        try:
            has_abs = self._device_has_abs(device_path)
            with open(device_path, "rb") as f:
                while self.running and not stop_event.is_set():
                    try:
                        raw = f.read(INPUT_EVENT_SIZE)
                    except OSError:
                        break
                    if len(raw) < INPUT_EVENT_SIZE:
                        time.sleep(0.01)
                        continue
                    try:
                        _, _, ev_type, ev_code, ev_value = struct.unpack(
                            INPUT_EVENT_FMT, raw
                        )
                    except struct.error:
                        continue

                    if not self._track_keystrokes:
                        continue

                    if ev_type == EV_KEY:
                        self._handle_key(ev_code, ev_value)
                    elif ev_type == EV_REL:
                        self._handle_rel(ev_code, ev_value)
                    elif ev_type == EV_ABS and has_abs:
                        syn = (ev_type == EV_SYN and ev_code == SYN_REPORT)
                        self._handle_abs(device_path, ev_code, ev_value, syn)
                    elif ev_type == EV_SYN and ev_code == SYN_REPORT and has_abs:
                        self._handle_abs(device_path, -1, 0, True)  # syn-only frame

        except PermissionError:
            logger.warning(
                f"No permission for {device_path}. "
                f"Run: sudo usermod -aG input $USER  then log out and back in."
            )
        except Exception as e:
            logger.debug(f"Device reader {device_path} exited: {e}")

    @staticmethod
    def _device_has_abs(device_path: str) -> bool:
        """Check /proc/bus/input/devices to see if this device reports EV_ABS."""
        try:
            with open("/proc/bus/input/devices") as f:
                content = f.read()
            # Find the block for this device
            for block in content.split("\n\n"):
                if device_path.split("/")[-1] in block or \
                   ("Handlers=" in block and device_path.split("/")[-1] in block):
                    return "EV=abs" in block.lower() or "B: ABS=" in block
        except Exception:
            pass
        return False

    # ── Flush ─────────────────────────────────────────────────────────

    def _flush(self):
        """Write accumulated counts to DB via batch_writer."""
        with self._lock:
            ks    = self.keystrokes
            mc    = self.mouse_clicks
            se    = self.scroll_events
            md    = self.mouse_distance_px
            times = list(self.keystroke_times)
            self.keystrokes = self.mouse_clicks = self.scroll_events = \
                self.mouse_distance_px = 0

        # Skip if nothing happened and no WPM to report
        if ks == 0 and mc == 0 and se == 0 and md == 0:
            return

        # WPM: keystrokes in last 60s / 5 (avg word length) / 1 minute
        wpm = 0.0
        if self._track_typed_words and times:
            recent = [t for t in times if t > time.time() - 60]
            wpm = round((len(recent) / 5.0), 1)

        today = str(date.today())
        self.batch_writer.add("input_stats", {
            "timestamp":          int(time.time()),
            "date":               today,
            "keystrokes":         ks,
            "mouse_clicks":       mc,
            "mouse_scroll_events":se,
            "mouse_distance_px":  md,
            "wpm_sample":         wpm,
        })

    # ── Flush loop ────────────────────────────────────────────────────

    def _flush_loop(self):
        """Periodic flush — runs in calling thread regardless of device count."""
        while self.running:
            time.sleep(self.interval)
            try:
                self._flush()
                # Check for privacy sentinel
                from daemon.collectors._privacy import check_privacy_sentinel
                check_privacy_sentinel(self)
            except Exception as e:
                logger.debug(f"InputCollector flush error: {e}")

    # ── Device management ─────────────────────────────────────────────

    def _find_input_devices(self):
        """Return list of /dev/input/eventN paths the process can open."""
        devices = []
        for path in sorted(glob.glob("/dev/input/event*")):
            if os.access(path, os.R_OK):
                devices.append(path)
        return devices

    def stop(self):
        self.running = False

    def run(self):
        """
        Start reading input events.
        _flush_loop always runs — data is written even if no devices found.
        One thread per device for parallel blocking reads.
        """
        self.running = True
        stop_events  = {}
        devices      = self._find_input_devices()

        if not devices:
            logger.warning(
                "No readable input devices found. "
                "To fix: sudo usermod -aG input $USER  then log out+login. "
                "WPM/keystroke tracking disabled until fixed."
            )
        else:
            logger.info(f"InputCollector monitoring {len(devices)} device(s)")

        for dev in devices:
            stop_ev = threading.Event()
            stop_events[dev] = stop_ev
            t = threading.Thread(
                target=self._read_device,
                args=(dev, stop_ev),
                daemon=True,
                name=f"input-{dev.split('/')[-1]}",
            )
            t.start()

        # flush_loop runs here (blocks until self.running = False)
        self._flush_loop()

        # Signal all device threads to stop
        for stop_ev in stop_events.values():
            stop_ev.set()
