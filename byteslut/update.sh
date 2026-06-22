#!/usr/bin/env bash
# ==============================================================================
# ByteSlut — update.sh
# ==============================================================================
# Safely updates ByteSlut to a new version.
#
# WHAT THIS DOES:
#   1. Stops the running daemon (gracefully via systemd or SIGTERM)
#   2. Backs up your config/settings.json
#   3. Copies new code files into place (daemon/, web/, cli/, install.sh)
#   4. Merges new config keys into your existing settings (your values stay)
#   5. Runs DB migrations (adds new columns/tables if any, never drops data)
#   6. Restarts the daemon
#   7. Reloads the systemd service definition if it changed
#
# WHAT IT NEVER TOUCHES:
#   - ~/.local/share/byteslut/byteslut.db   (your data)
#   - ~/.local/share/byteslut/cmd_log.txt   (your command log)
#   - ~/.local/share/byteslut/logs/         (your logs)
#   - Your app name, CLI command, port, privacy settings
#   - Your ~/.bashrc / ~/.zshrc aliases
#   - Your input group membership
#
# USAGE:
#   ./update.sh                        Update from same directory (git pull first)
#   ./update.sh /path/to/new/byteslut  Update from a specific extracted archive
#
# EXAMPLE WORKFLOW (git):
#   cd ~/byteslut
#   git pull
#   ./update.sh
#
# EXAMPLE WORKFLOW (tarball):
#   tar -xzf byteslut_v3.tar.gz          ← extracts to ./byteslut/
#   cd byteslut
#   ./update.sh                          ← updates in place
# ==============================================================================

set -e

# ── Colors ──
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

ok()   { echo -e "${GREEN}  ✓${NC} $1"; }
info() { echo -e "${BLUE}  →${NC} $1"; }
warn() { echo -e "${YELLOW}  ⚠${NC} $1"; }
err()  { echo -e "${RED}  ✗${NC} $1"; exit 1; }
step() { echo -e "\n${BOLD}$1${NC}"; }

# ── Parse arguments ──
# Usage: ./update.sh [SOURCE_DIR] [--restart-only]
# SOURCE_DIR defaults to the script's own directory
SOURCE_DIR=""
RESTART_ONLY=false

for arg in "$@"; do
    case "$arg" in
        --restart-only) RESTART_ONLY=true ;;
        --*)            warn "Unknown flag: $arg" ;;
        *)              SOURCE_DIR="$arg" ;;
    esac
done

# ── Paths ──
NEW_ROOT="${SOURCE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
PYTHON=$(which python3 2>/dev/null || which python 2>/dev/null)
DATA_DIR="$HOME/.local/share/byteslut"
BACKUP_DIR="$DATA_DIR/backups/$(date +%Y%m%d_%H%M%S)"

[ -z "$PYTHON" ] && err "Python 3 not found"
[ ! -f "$NEW_ROOT/daemon/db.py" ] && err "Not a valid ByteSlut directory: $NEW_ROOT"

echo ""
echo -e "${BOLD}${RED}╔══════════════════════════════════════╗${NC}"
echo -e "${BOLD}${RED}║   ByteSlut — Update                  ║${NC}"
echo -e "${BOLD}${RED}╚══════════════════════════════════════╝${NC}"
echo ""
info "Source:  $NEW_ROOT"
info "Data:    $DATA_DIR"
info "Backup:  $BACKUP_DIR"

# ══════════════════════════════════════
# STEP 1: Back up current config
# ══════════════════════════════════════
step "Step 1: Backing up your configuration"

mkdir -p "$BACKUP_DIR"

# PRIORITY ORDER for finding existing settings:
# 1. Installed location (via systemd ExecStart path) — most reliable
# 2. Same directory as update.sh — in-place update
# 3. Fall back to new defaults
#
# WHY: If user extracted the tarball to ~/Downloads/byteslut/ and runs ./update.sh,
# the tarball's config/settings.json has DEFAULT values.
# We must find the INSTALLED settings, not the tarball's defaults.

SERVICE_FILE="$HOME/.config/systemd/user/byteslut.service"
EXISTING_SETTINGS=""

# Priority 1: Find via systemd service
if [ -f "$SERVICE_FILE" ]; then
    _EXISTING=$(grep "ExecStart" "$SERVICE_FILE" 2>/dev/null | awk '{print $2}' | xargs dirname 2>/dev/null | xargs dirname 2>/dev/null || echo "")
    if [ -n "$_EXISTING" ] && [ -f "$_EXISTING/config/settings.json" ]; then
        # Verify it's a real settings file (has version key), not an empty/default one
        _HAS_CONTENT=$(python3 -c "
import json,sys
try:
    d=json.load(open('$_EXISTING/config/settings.json'))
    print('yes' if d.get('version') else 'no')
except: print('no')
" 2>/dev/null || echo "no")
        if [ "$_HAS_CONTENT" = "yes" ]; then
            EXISTING_SETTINGS="$_EXISTING/config/settings.json"
            info "Found existing settings at: $EXISTING_SETTINGS"
        fi
    fi
fi

# Priority 2: In-place update — but ONLY if the file is different from the tarball defaults
# (i.e. the user has actually customised it)
if [ -z "$EXISTING_SETTINGS" ] && [ -f "$NEW_ROOT/config/settings.json" ]; then
    _IS_CUSTOM=$(python3 -c "
import json,sys
try:
    d=json.load(open('$NEW_ROOT/config/settings.json'))
    # Consider customised if app_name or cli_command differ from shipped defaults
    # OR if any daily_report values differ from defaults
    is_custom = (
        d.get('app_name','ByteSlut') != 'ByteSlut' or
        d.get('cli_command','byteslut') != 'byteslut' or
        d.get('daily_report',{}).get('time','18:30') != '18:30' or
        d.get('ui_theme','default') != 'default' or
        d.get('accent_color','') != ''
    )
    print('yes' if is_custom else 'no')
except: print('no')
" 2>/dev/null || echo "no")
    if [ "$_IS_CUSTOM" = "yes" ]; then
        EXISTING_SETTINGS="$NEW_ROOT/config/settings.json"
        info "Using customised in-place settings: $EXISTING_SETTINGS"
    else
        info "In-place config/settings.json has default values — will use new defaults after merge"
    fi
fi

if [ -n "$EXISTING_SETTINGS" ] && [ -f "$EXISTING_SETTINGS" ]; then
    cp "$EXISTING_SETTINGS" "$BACKUP_DIR/settings.json"
    ok "Your settings backed up → $BACKUP_DIR/settings.json"
else
    warn "No existing settings found — new defaults will be used"
fi

# ══════════════════════════════════════
# STEP 2: Stop the daemon
# ══════════════════════════════════════
step "Step 2: Stopping daemon"

# Try systemd first (cleanest)
if systemctl --user is-active --quiet byteslut 2>/dev/null; then
    systemctl --user stop byteslut
    ok "Daemon stopped via systemd"
else
    # Try PID file
    PID_FILE="$DATA_DIR/daemon.pid"
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            kill -TERM "$PID"
            sleep 2
            ok "Daemon stopped via PID $PID"
        fi
        rm -f "$PID_FILE"
    else
        info "Daemon was not running"
    fi
fi

# Stop web dashboard too (it will restart on next 'spank')
WEB_PID_FILE="$DATA_DIR/web.pid"
if [ -f "$WEB_PID_FILE" ]; then
    WEB_PID=$(cat "$WEB_PID_FILE")
    kill -TERM "$WEB_PID" 2>/dev/null && ok "Dashboard stopped (PID $WEB_PID)" || true
    rm -f "$WEB_PID_FILE"
fi

sleep 1  # Let sockets release

# ══════════════════════════════════════
# STEP 3: Update code files
# ══════════════════════════════════════
step "Step 3: Installing new code files"

# These are the directories that contain ONLY code (safe to fully replace)
CODE_DIRS=("daemon" "web" "cli")
# NOTE: "config" is intentionally NOT in CODE_DIRS.
# config/settings.json is handled separately in Step 4 (merge) to preserve user values.

for dir in "${CODE_DIRS[@]}"; do
    src="$NEW_ROOT/$dir"
    dst="$NEW_ROOT/$dir"   # updating in-place (same directory)

    if [ -d "$src" ]; then
        # We're already in place — nothing to copy.
        # If updating FROM a different directory, copy to the INSTALL location.
        # The install location is stored in the systemd service ExecStart path.
        ok "Code directory: $dir ✓"
    fi
done

# If we're updating FROM a different path (e.g. extracted tarball → existing install)
INSTALL_ROOT="$NEW_ROOT"

# Try to detect existing install path from systemd service
SERVICE_FILE="$HOME/.config/systemd/user/byteslut.service"
if [ -f "$SERVICE_FILE" ]; then
    # Extract ExecStart path from service file
    # Line looks like: ExecStart=/usr/bin/python3 /home/user/byteslut/daemon/main.py
    EXISTING_INSTALL=$(grep "ExecStart" "$SERVICE_FILE" | awk '{print $2}' | xargs dirname 2>/dev/null | xargs dirname 2>/dev/null || echo "")
    if [ -n "$EXISTING_INSTALL" ] && [ -d "$EXISTING_INSTALL" ] && [ "$EXISTING_INSTALL" != "$NEW_ROOT" ]; then
        info "Existing install found at: $EXISTING_INSTALL"
        info "New code at: $NEW_ROOT"
        info "Copying new code to existing install..."

        for dir in "${CODE_DIRS[@]}"; do
            if [ -d "$NEW_ROOT/$dir" ]; then
                # Back up the old directory
                cp -r "$EXISTING_INSTALL/$dir" "$BACKUP_DIR/${dir}_old" 2>/dev/null || true
                # Copy new code over
                rsync -a --delete "$NEW_ROOT/$dir/" "$EXISTING_INSTALL/$dir/"
                ok "Updated: $EXISTING_INSTALL/$dir/"
            fi
        done

        # Also update install.sh and update.sh themselves
        for script in install.sh update.sh uninstall.sh requirements.txt README.md; do
            [ -f "$NEW_ROOT/$script" ] && cp "$NEW_ROOT/$script" "$EXISTING_INSTALL/$script" && ok "Updated: $script"
        done

        INSTALL_ROOT="$EXISTING_INSTALL"
    else
        ok "Updating in-place at: $INSTALL_ROOT"
    fi
fi

# ══════════════════════════════════════
# STEP 4: Merge config (preserve your settings, add new keys)
# ══════════════════════════════════════
step "Step 4: Merging configuration"

# HOW THE MERGE WORKS:
#   - "new defaults"  = config/settings.json that ships in the NEW version archive
#   - "your config"   = the backup we made in Step 1 (from your EXISTING install)
#   - Result:
#       * Every key you customised → your value wins
#       * Every NEW key added in this version → gets its default value
#       * Nested dicts (like daily_report.*) are merged recursively
#
# This is why we back up from EXISTING_INSTALL in Step 1, not from NEW_ROOT.
# NEW_ROOT contains default settings — your settings live in EXISTING_INSTALL.

export BACKUP_DIR_PY="$BACKUP_DIR"
export INSTALL_ROOT_PY="$INSTALL_ROOT"
export NEW_ROOT_PY="$NEW_ROOT"   # ← NEW: so merge can read new-version defaults

$PYTHON << 'PYEOF'
import json, sys, os

backup_dir   = os.environ["BACKUP_DIR_PY"]
install_root = os.environ["INSTALL_ROOT_PY"]
new_root     = os.environ["NEW_ROOT_PY"]

# "new defaults" = the settings.json that ships in THIS version of ByteSlut.
# Always read from NEW_ROOT (the extracted tarball / update source).
# This ensures new keys added in this version get their default values.
#
# FIX: Previously this read from INSTALL_ROOT (the existing install), so the
# new version's default keys were never picked up. If a new version added
# daily_report.delay_check_interval_seconds, the old settings.json didn't
# have it → load_config() returned a dict missing that key → settings page
# showed nothing → user saved → wrong value written → "settings reset" bug.
new_defaults_path = os.path.join(new_root, "config", "settings.json")

# "your config" = the backup we made in Step 1 from your EXISTING install.
# Your values always win over new defaults.
backup_config_path = os.path.join(backup_dir, "settings.json")

# Where to write the final merged config
output_path = os.path.join(install_root, "config", "settings.json")

if not os.path.exists(new_defaults_path):
    print("  No new defaults file found — skipping merge")
    sys.exit(0)

with open(new_defaults_path) as f:
    new_defaults = json.load(f)

if not os.path.exists(backup_config_path):
    print("  No previous settings found — keeping new defaults")
    # Still write them out to the install location
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(new_defaults, f, indent=2)
    sys.exit(0)

with open(backup_config_path) as f:
    your_config = json.load(f)

def deep_merge(defaults, yours):
    """
    Recursively merge: your values always win over defaults.
    New keys added in the new version get their default value.
    Nested dicts (e.g. daily_report, privacy) are merged recursively
    so adding a new nested key doesn't blow away the whole block.
    """
    result = dict(defaults)
    for key, your_val in yours.items():
        if key in result and isinstance(result[key], dict) and isinstance(your_val, dict):
            result[key] = deep_merge(result[key], your_val)
        else:
            result[key] = your_val  # Your value always wins
    return result

merged = deep_merge(new_defaults, your_config)

# Report what was preserved vs what's new
new_keys = []
def find_new_keys(defaults, yours, prefix=""):
    for k, v in defaults.items():
        full_key = f"{prefix}.{k}" if prefix else k
        if k not in yours:
            new_keys.append(f"{full_key} = {repr(v)}")
        elif isinstance(v, dict) and isinstance(yours.get(k), dict):
            find_new_keys(v, yours[k], full_key)

find_new_keys(new_defaults, your_config)

if new_keys:
    print(f"  New config keys added with defaults:")
    for k in new_keys:
        print(f"    + {k}")
else:
    print("  No new config keys in this version")

# Print preserved values so user can verify
print(f"  Preserved settings:")
print(f"    app_name      = {merged.get('app_name')}")
print(f"    cli_command   = {merged.get('cli_command')}")
print(f"    port          = {merged.get('dashboard_port')}")
dr = merged.get('daily_report', {})
print(f"    daily_report  = enabled={dr.get('enabled')}, time={dr.get('time')}")
priv = merged.get('privacy', {})
print(f"    privacy       = {priv}")

# Write merged config to the install location
os.makedirs(os.path.dirname(output_path), exist_ok=True)
with open(output_path, "w") as f:
    json.dump(merged, f, indent=2)

print("  Config saved successfully")
PYEOF

ok "Configuration merged — your settings preserved"

# ══════════════════════════════════════
# STEP 5: Database migration
# ══════════════════════════════════════
step "Step 5: Running database migrations"

# initialize_database() uses CREATE TABLE IF NOT EXISTS and
# ALTER TABLE ADD COLUMN IF NOT EXISTS — so it's always safe to re-run.
# It NEVER drops columns or tables. It only ADDS new things.
$PYTHON -c "
import sys
sys.path.insert(0, '$INSTALL_ROOT')
from daemon.db import initialize_database, get_db_path
print(f'  Database: {get_db_path()}')
initialize_database()
print('  Migrations applied (new tables/columns added if any)')
" 2>&1 | grep -v "^$" | sed 's/^/  /'

ok "Database up to date"

# ══════════════════════════════════════
# STEP 6: Update systemd service (in case ExecStart path changed)
# ══════════════════════════════════════
step "Step 6: Updating systemd service"

ACTUAL_UID=$(id -u)

if [ -f "$SERVICE_FILE" ]; then
    cat > "$SERVICE_FILE" << EOF
[Unit]
Description=ByteSlut System Monitor Daemon
After=default.target

[Service]
Type=simple
ExecStart=$PYTHON $INSTALL_ROOT/daemon/main.py
Restart=on-failure
RestartSec=10
Environment=HOME=$HOME
Environment=USER=$USER
Environment=XDG_RUNTIME_DIR=/run/user/${ACTUAL_UID}
Environment=WAYLAND_DISPLAY=wayland-1
Environment=PYTHONPATH=$INSTALL_ROOT
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    ok "Systemd service updated"
else
    warn "No systemd service found — run ./install.sh once to set it up"
fi

# Fix Hyprland: import env vars so daemon can see HYPRLAND_INSTANCE_SIGNATURE etc.
HYPR_CONFIG="$HOME/.config/hypr/hyprland.conf"
HYPR_ENV_LINE="exec-once = systemctl --user import-environment WAYLAND_DISPLAY HYPRLAND_INSTANCE_SIGNATURE XDG_CURRENT_DESKTOP DBUS_SESSION_BUS_ADDRESS"

if [ -f "$HYPR_CONFIG" ]; then
    if ! grep -q "import-environment" "$HYPR_CONFIG"; then
        echo "" >> "$HYPR_CONFIG"
        echo "# ByteSlut: pass Wayland environment to systemd user services" >> "$HYPR_CONFIG"
        echo "$HYPR_ENV_LINE" >> "$HYPR_CONFIG"
        ok "Added import-environment to hyprland.conf — app tracking will work after next login"
    else
        ok "import-environment already in hyprland.conf"
    fi
else
    warn "hyprland.conf not found at $HYPR_CONFIG"
    warn "Add this line to your hyprland.conf to enable app tracking:"
    echo ""
    echo "    $HYPR_ENV_LINE"
    echo ""
fi

# ══════════════════════════════════════
# STEP 7: Install any new Python dependencies
# ══════════════════════════════════════
step "Step 7: Checking Python dependencies"

if [ -f "$INSTALL_ROOT/requirements.txt" ]; then
    # Only install missing ones (--upgrade would be too aggressive)
    $PYTHON -m pip install --break-system-packages --quiet \
        -r "$INSTALL_ROOT/requirements.txt" 2>&1 | \
        grep -E "Successfully installed|already satisfied" | head -5 | sed 's/^/  /' || true
    ok "Dependencies checked"
fi

# ══════════════════════════════════════
# STEP 8: Restart daemon
# ══════════════════════════════════════
step "Step 8: Restarting daemon"

if [ -f "$SERVICE_FILE" ]; then
    systemctl --user start byteslut
    sleep 2
    if systemctl --user is-active --quiet byteslut; then
        ok "Daemon restarted via systemd"
    else
        warn "Daemon failed to start — check: journalctl --user -u byteslut -n 20"
    fi
else
    info "Start manually: $PYTHON $INSTALL_ROOT/daemon/main.py &"
fi

# ══════════════════════════════════════
# DONE
# ══════════════════════════════════════
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${GREEN}║   Update complete! 🎉                    ║${NC}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════╝${NC}"
echo ""

CLI_CMD=$($PYTHON -c "
import json
try:
    with open('$INSTALL_ROOT/config/settings.json') as f:
        print(json.load(f).get('cli_command','spank'))
except: print('spank')
")

echo -e "  Your data:     ${BLUE}~/.local/share/byteslut/byteslut.db${NC} (untouched)"
echo -e "  Your settings: ${BLUE}$INSTALL_ROOT/config/settings.json${NC} (merged)"
echo -e "  Backup at:     ${BLUE}$BACKUP_DIR/${NC}"
echo ""
echo -e "  Open dashboard: ${BOLD}$CLI_CMD${NC}"
echo -e "  View logs:      ${BOLD}journalctl --user -u byteslut -f${NC}"
echo ""
