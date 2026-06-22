#!/usr/bin/env bash
# ==============================================================================
# ByteSlut — uninstall.sh
# ==============================================================================
# Removes ByteSlut completely from your system.
# Your collected data is PRESERVED by default (unless you pass --purge).
#
# WHAT GETS REMOVED:
#   - systemd user service (byteslut.service)
#   - Shell aliases in ~/.bashrc and ~/.zshrc
#   - Shell hooks (PROMPT_COMMAND / precmd) in ~/.bashrc and ~/.zshrc
#   - PID files in ~/.local/share/byteslut/*.pid
#   - Daemon log files in ~/.local/share/byteslut/logs/
#   - The code directory (daemon/, web/, cli/, config/)
#     → YOU decide whether to delete the project folder itself
#
# WHAT IS PRESERVED BY DEFAULT:
#   - ~/.local/share/byteslut/byteslut.db   (your entire history)
#   - ~/.local/share/byteslut/cmd_log.txt   (command log)
#
# TO ALSO DELETE YOUR DATA (complete wipe):
#   ./uninstall.sh --purge
#
# USAGE:
#   ./uninstall.sh           Uninstall, keep data
#   ./uninstall.sh --purge   Uninstall, delete ALL data too
#   ./uninstall.sh --dry-run Show what would be removed, don't do anything
# ==============================================================================

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

ok()   { echo -e "${GREEN}  ✓${NC} $1"; }
info() { echo -e "${BLUE}  →${NC} $1"; }
warn() { echo -e "${YELLOW}  ⚠${NC} $1"; }
skip() { echo -e "  ${NC}·${NC} $1 ${YELLOW}(skipped)${NC}"; }

PURGE=false
DRY_RUN=false

for arg in "$@"; do
    case "$arg" in
        --purge)   PURGE=true ;;
        --dry-run) DRY_RUN=true ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$HOME/.local/share/byteslut"
PYTHON=$(which python3 2>/dev/null || which python 2>/dev/null)

# Helper: run a command or just print it in dry-run mode
run() {
    if $DRY_RUN; then
        echo -e "  ${YELLOW}[dry-run]${NC} $*"
    else
        eval "$@"
    fi
}

echo ""
echo -e "${BOLD}${RED}╔══════════════════════════════════════╗${NC}"
echo -e "${BOLD}${RED}║   ByteSlut — Uninstaller             ║${NC}"
if $PURGE; then
echo -e "${BOLD}${RED}║   ⚠  PURGE MODE — deletes all data   ║${NC}"
fi
if $DRY_RUN; then
echo -e "${BOLD}${YELLOW}║   DRY RUN — nothing will be deleted  ║${NC}"
fi
echo -e "${BOLD}${RED}╚══════════════════════════════════════╝${NC}"
echo ""

# ── Confirmation ──
if ! $DRY_RUN; then
    if $PURGE; then
        echo -e "${RED}${BOLD}  WARNING: --purge will delete your database and ALL collected history!${NC}"
        echo -e "${RED}  This cannot be undone.${NC}"
        echo ""
    fi
    read -rp "  Proceed with uninstall? [y/N] " CONFIRM
    [[ "$CONFIRM" =~ ^[Yy]$ ]] || { echo "  Aborted."; exit 0; }
    echo ""
fi

# ══════════════════════════════════════
# STEP 1: Stop and disable the daemon
# ══════════════════════════════════════
echo -e "${BOLD}Step 1: Stopping daemon${NC}"

if systemctl --user is-active --quiet byteslut 2>/dev/null; then
    run "systemctl --user stop byteslut"
    ok "Daemon stopped"
fi

if systemctl --user is-enabled --quiet byteslut 2>/dev/null; then
    run "systemctl --user disable byteslut"
    ok "Daemon disabled (won't auto-start anymore)"
fi

# Kill any lingering web dashboard processes
WEB_PID_FILE="$DATA_DIR/web.pid"
if [ -f "$WEB_PID_FILE" ]; then
    WEB_PID=$(cat "$WEB_PID_FILE" 2>/dev/null || echo "")
    if [ -n "$WEB_PID" ] && kill -0 "$WEB_PID" 2>/dev/null; then
        run "kill -TERM $WEB_PID"
        ok "Dashboard stopped (PID $WEB_PID)"
    fi
fi

# ══════════════════════════════════════
# STEP 2: Remove systemd service file
# ══════════════════════════════════════
echo ""
echo -e "${BOLD}Step 2: Removing systemd service${NC}"

SERVICE_FILE="$HOME/.config/systemd/user/byteslut.service"
if [ -f "$SERVICE_FILE" ]; then
    run "rm -f '$SERVICE_FILE'"
    run "systemctl --user daemon-reload"
    ok "Service file removed: $SERVICE_FILE"
else
    skip "Service file not found (already removed?)"
fi

# ══════════════════════════════════════
# STEP 3: Remove shell aliases and hooks
# ══════════════════════════════════════
echo ""
echo -e "${BOLD}Step 3: Cleaning shell config files${NC}"

# Read current CLI command name to know which alias to remove
CLI_CMD="byteslut"
if [ -f "$SCRIPT_DIR/config/settings.json" ] && [ -n "$PYTHON" ]; then
    CLI_CMD=$($PYTHON -c "
import json
try:
    with open('$SCRIPT_DIR/config/settings.json') as f:
        print(json.load(f).get('cli_command', 'spank'))
except: print('spank')
" 2>/dev/null || echo "byteslut")
fi

info "Removing alias '$CLI_CMD' and ByteSlut hooks from shell configs..."

# Function to clean a shell rc file
clean_rc_file() {
    local rc_file="$1"
    if [ ! -f "$rc_file" ]; then return; fi

    # We use Python for clean multi-line block removal (bash is terrible at this)
    if $DRY_RUN; then
        echo -e "  ${YELLOW}[dry-run]${NC} Would clean $rc_file"
        return
    fi

    $PYTHON << PYEOF
import re, shutil, os

rc_path = "$rc_file"
cli_cmd = "$CLI_CMD"

with open(rc_path, "r") as f:
    content = f.read()

original = content

# Patterns to remove:
# 1. ByteSlut CLI alias block
# 2. ByteSlut command tracking hook (bash version)
# 3. ByteSlut command tracking hook (zsh version)
# 4. Any alias line containing our CLI command

patterns = [
    # Alias block with marker comment
    r'\n# ByteSlut CLI alias\nalias ' + re.escape(cli_cmd) + r"='[^']+'\n?",
    # Fallback: just the alias line (no comment)
    r'\nalias ' + re.escape(cli_cmd) + r"='[^']+'\n?",
    # Bash hook block
    r'\n# ByteSlut command tracking hook.*?(?=\n#|\nPROMPT_COMMAND|\Z)',
    # Bash hook functions
    r'\n__byteslut_cmd_start\(\).*?trap.*?PROMPT_COMMAND.*?\n',
    # Zsh hook block
    r'\n# ByteSlut command tracking hook.*?(?=\nautoload|\Z)',
    # Zsh hook functions
    r'\n__byteslut_preexec\(\).*?add-zsh-hook precmd __byteslut_precmd\n?',
]

for pat in patterns:
    content = re.sub(pat, '\n', content, flags=re.DOTALL)

# Clean up multiple blank lines
content = re.sub(r'\n{3,}', '\n\n', content)

if content != original:
    # Backup the rc file before modifying
    shutil.copy2(rc_path, rc_path + ".byteslut_backup")
    with open(rc_path, "w") as f:
        f.write(content)
    print(f"  Cleaned: {rc_path}  (backup: {rc_path}.byteslut_backup)")
else:
    print(f"  Nothing to remove in: {rc_path}")
PYEOF
}

clean_rc_file "$HOME/.bashrc"
clean_rc_file "$HOME/.zshrc"
ok "Shell configs cleaned"

# ══════════════════════════════════════
# STEP 4: Remove runtime files (PID, logs)
# ══════════════════════════════════════
echo ""
echo -e "${BOLD}Step 4: Removing runtime files${NC}"

# PID files
for pidfile in "$DATA_DIR"/*.pid; do
    [ -f "$pidfile" ] || continue
    run "rm -f '$pidfile'"
    ok "Removed: $pidfile"
done

# Log directory (logs are not your data, just daemon output)
LOG_DIR="$DATA_DIR/logs"
if [ -d "$LOG_DIR" ]; then
    run "rm -rf '$LOG_DIR'"
    ok "Removed logs: $LOG_DIR"
fi

# ══════════════════════════════════════
# STEP 5: Handle user data
# ══════════════════════════════════════
echo ""
echo -e "${BOLD}Step 5: User data${NC}"

DB_FILE="$DATA_DIR/byteslut.db"
CMD_LOG="$DATA_DIR/cmd_log.txt"
BACKUPS_DIR="$DATA_DIR/backups"

if $PURGE; then
    # --purge flag passed: still require interactive confirmation with a random token
    echo ""
    echo -e "${RED}${BOLD}  ╔══════════════════════════════════════════════════════╗${NC}"
    echo -e "${RED}${BOLD}  ║  DATABASE DELETE — THIS CANNOT BE UNDONE             ║${NC}"
    echo -e "${RED}${BOLD}  ╚══════════════════════════════════════════════════════╝${NC}"
    echo ""
    if [ -f "$DB_FILE" ]; then
        DB_SIZE=$(du -sh "$DB_FILE" 2>/dev/null | cut -f1 || echo "?")
        echo -e "  Database: ${RED}$DB_FILE${NC} (${RED}${DB_SIZE}${NC})"
    fi
    echo ""
    echo -e "  This will permanently delete:"
    echo -e "    • Your entire activity history (app usage, browser, commands)"
    echo -e "    • All productivity scores and day roles"
    echo -e "    • All notification and input records"
    echo -e "    • All backups in $BACKUPS_DIR"
    echo ""

    # Generate a random 6-character confirmation token
    CONFIRM_TOKEN=$(tr -dc 'A-Z0-9' < /dev/urandom | head -c 6 2>/dev/null || echo "DELETE")
    echo -e "  ${BOLD}To confirm deletion, type exactly:  ${RED}${CONFIRM_TOKEN}${NC}"
    echo ""
    printf "  Type the code: "
    read -r USER_INPUT

    if [ "$USER_INPUT" = "$CONFIRM_TOKEN" ]; then
        echo ""
        ok "Confirmation accepted — deleting all data..."
        if [ -f "$DB_FILE" ]; then
            rm -f "$DB_FILE" "$DB_FILE-wal" "$DB_FILE-shm" 2>/dev/null || true
            ok "Database deleted"
        fi
        if [ -f "$CMD_LOG" ]; then
            rm -f "$CMD_LOG"
            ok "Command log deleted"
        fi
        if [ -d "$BACKUPS_DIR" ]; then
            rm -rf "$BACKUPS_DIR"
            ok "Backups deleted"
        fi
        # Clean up user-generated JSON files
        for jsonfile in "$DATA_DIR"/*.json; do
            [ -f "$jsonfile" ] && rm -f "$jsonfile"
        done
        if [ -d "$DATA_DIR" ]; then
            rmdir "$DATA_DIR" 2>/dev/null || true
            ok "Data directory removed"
        fi
    else
        echo ""
        warn "Confirmation code did not match — database NOT deleted."
        warn "Your data is safe at: $DATA_DIR"
        echo ""
        info "If you still want to delete your data, run:"
        info "  ./uninstall.sh --purge"
        info "  (and type the confirmation code correctly)"
    fi
else
    # No --purge: offer interactive choice about the database
    echo ""
    if [ -f "$DB_FILE" ]; then
        DB_SIZE=$(du -sh "$DB_FILE" 2>/dev/null | cut -f1 || echo "?")
        echo -e "  Your database is at: ${BOLD}$DB_FILE${NC} (${BOLD}$DB_SIZE${NC})"
        echo ""
        printf "  Do you want to delete your database and all collected history? [y/N] "
        read -r DELETE_CHOICE
        if [[ "$DELETE_CHOICE" =~ ^[Yy]$ ]]; then
            echo ""
            # Require confirmation token even here
            CONFIRM_TOKEN=$(tr -dc 'A-Z0-9' < /dev/urandom | head -c 6 2>/dev/null || echo "WIPE")
            echo -e "  ${BOLD}Last chance — type exactly:  ${RED}${CONFIRM_TOKEN}${NC}"
            printf "  Confirmation code: "
            read -r USER_INPUT
            if [ "$USER_INPUT" = "$CONFIRM_TOKEN" ]; then
                rm -f "$DB_FILE" "$DB_FILE-wal" "$DB_FILE-shm" 2>/dev/null || true
                rm -f "$CMD_LOG" 2>/dev/null || true
                rm -rf "$BACKUPS_DIR" 2>/dev/null || true
                for jsonfile in "$DATA_DIR"/*.json; do
                    [ -f "$jsonfile" ] && rm -f "$jsonfile"
                done
                rmdir "$DATA_DIR" 2>/dev/null || true
                ok "All data deleted."
            else
                warn "Confirmation did not match — database preserved."
            fi
        else
            ok "Database preserved at: $DB_FILE"
            info "To delete later: rm -rf $DATA_DIR"
        fi
    else
        info "No database found at $DATA_DIR"
    fi
fi

# ══════════════════════════════════════
# STEP 6: Offer to remove code directory
# ══════════════════════════════════════
echo ""
echo -e "${BOLD}Step 6: Code directory${NC}"

if ! $DRY_RUN; then
    echo ""
    warn "The code directory is: $SCRIPT_DIR"
    read -rp "  Delete the code directory too? [y/N] " DEL_CODE
    if [[ "$DEL_CODE" =~ ^[Yy]$ ]]; then
        cd "$HOME"  # Get out of the directory before deleting it
        rm -rf "$SCRIPT_DIR"
        ok "Code directory deleted"
    else
        skip "Code directory kept at: $SCRIPT_DIR"
    fi
else
    info "[dry-run] Would ask whether to delete: $SCRIPT_DIR"
fi

# ══════════════════════════════════════
# DONE
# ══════════════════════════════════════
echo ""
if $PURGE; then
echo -e "${BOLD}${RED}╔══════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${RED}║   ByteSlut fully removed (purge) 🗑️      ║${NC}"
echo -e "${BOLD}${RED}╚══════════════════════════════════════════╝${NC}"
else
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${GREEN}║   ByteSlut uninstalled ✓                 ║${NC}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════╝${NC}"
fi
echo ""
if ! $PURGE && ! $DRY_RUN; then
echo -e "  Your history is safe at:  ${BLUE}$DATA_DIR/${NC}"
echo ""
echo -e "  To reinstall later and reconnect to your data:"
echo -e "    ${BOLD}cd /path/to/new/byteslut && ./install.sh${NC}"
echo -e "  Your data will be picked up automatically."
fi
echo ""
