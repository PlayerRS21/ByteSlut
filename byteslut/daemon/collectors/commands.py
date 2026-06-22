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

from daemon.collectors._privacy import PrivacyMixin

logger = logging.getLogger(__name__)

CMD_LOG_FILE = os.path.expanduser("~/.local/share/byteslut/cmd_log.txt")


class CommandCollector(PrivacyMixin):
    """
    Reads terminal command history and the real-time command log.
    Respects track_commands privacy toggle — when off, reads no data and writes nothing.
    """

    PRIVACY_KEY = "track_commands"

    def __init__(self, batch_writer):
        self._init_privacy()          # ← PrivacyMixin: load privacy state
        self.batch_writer = batch_writer
        self.running = False
        self.last_cmd_log_pos = 0
        self.last_history_line = {}

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

            # PRIVACY GATE: if user turned off track_commands, discard here
            if not self.privacy_allowed:
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
                if self.privacy_allowed:
                    self.read_cmd_log()
                else:
                    logger.debug("CommandCollector: track_commands=off, skipping")
            except Exception as e:
                logger.error(f"CommandCollector error: {e}")
            time.sleep(5)

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


