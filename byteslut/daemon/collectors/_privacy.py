"""
collectors/_privacy.py — Shared privacy enforcement mixin
==========================================================
Every collector that can be toggled off imports PrivacyMixin.

HOW IT WORKS:
  1. At startup: load_privacy() reads config/settings.json
  2. At runtime: when user saves settings, web/app.py writes
     daemon/.privacy_changed with the new privacy dict as JSON.
     daemon/main.py reads this file every 10s and calls
     reload_privacy() on every collector that has it.
  3. Each collector's data-writing method calls self.is_allowed(key)
     before calling batch_writer.add(). If False, nothing is written
     and the counter/data is discarded immediately.

KEYS:
  track_keystrokes      — InputCollector (keys + clicks + WPM)
  track_typed_words     — InputCollector WPM only (subset of above)
  track_browser_history — BrowserCollector
  track_notifications   — NotificationCollector
  track_commands        — CommandCollector
  (session and system stats are always on — no privacy concern)
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Path to config from any collector (3 levels up: collectors/ → daemon/ → project/)
_CFG_PATH = Path(__file__).parent.parent.parent / "config" / "settings.json"


def _read_privacy() -> dict:
    """Read privacy section from settings.json. Returns safe defaults on error."""
    try:
        with open(_CFG_PATH) as f:
            return json.load(f).get("privacy", {})
    except Exception as e:
        logger.debug(f"Privacy config read failed: {e} — using all-on defaults")
        return {}


class PrivacyMixin:
    """
    Mix into any collector to get real privacy enforcement.

    Usage in a collector:
        class MyCollector(PrivacyMixin):
            PRIVACY_KEY = "track_notifications"   # the setting that gates this collector

            def __init__(self, batch_writer):
                self._init_privacy()
                ...

            def _write_something(self, data):
                if not self.privacy_allowed:
                    return  # user turned this off — discard, don't write
                self.batch_writer.add("table", data)

    For collectors with sub-keys (InputCollector has both track_keystrokes
    AND track_typed_words), call is_allowed(key) directly:
        if not self.is_allowed("track_typed_words"):
            wpm = 0.0
    """

    # Subclasses set this to their primary privacy key
    PRIVACY_KEY: str = ""

    def _init_privacy(self):
        """Call from __init__ to load initial privacy state."""
        self._privacy = _read_privacy()
        logger.debug(
            f"{self.__class__.__name__}: privacy loaded — "
            f"{self.PRIVACY_KEY}={self._privacy.get(self.PRIVACY_KEY, True)}"
        )

    def reload_privacy(self):
        """
        Called by daemon/main.py when .privacy_changed sentinel is detected.
        Re-reads settings.json and updates the in-memory privacy state.
        Takes effect on the NEXT data write attempt.
        """
        old = self._privacy.get(self.PRIVACY_KEY, True)
        self._privacy = _read_privacy()
        new = self._privacy.get(self.PRIVACY_KEY, True)
        if old != new:
            state = "ENABLED" if new else "DISABLED"
            logger.info(
                f"{self.__class__.__name__}: {self.PRIVACY_KEY} → {state}"
                f" (takes effect immediately)"
            )

    @property
    def privacy_allowed(self) -> bool:
        """True if this collector's primary key is on. Use in data-write guards."""
        if not self.PRIVACY_KEY:
            return True  # No key set — always allowed (session, system stats)
        return self._privacy.get(self.PRIVACY_KEY, True)

    def is_allowed(self, key: str) -> bool:
        """Check any specific privacy key. Used for sub-keys like track_typed_words."""
        return self._privacy.get(key, True)
