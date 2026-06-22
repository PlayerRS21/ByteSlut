#!/usr/bin/env bash
# ==============================================================================
# ByteSlut — Installer Script for Arch Linux
# ==============================================================================
# Run this once to set everything up:
#   chmod +x install.sh
#   ./install.sh
#
# What this does:
#   1. Checks Python version (need 3.10+)
#   2. Installs Python pip dependencies (psutil, flask, dbus-python, etc.)
#   3. Adds user to 'input' group (for keyboard/mouse tracking)
#   4. Creates the data directory (~/.local/share/byteslut)
#   5. Installs the systemd user service (auto-start on login)
#   6. Adds the 'spank' shell alias to ~/.bashrc and ~/.zshrc
#   7. Initializes the SQLite database
#   8. Starts the daemon
# ==============================================================================

set -e  # Exit on any error

# ── Colors for pretty output ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'  # No Color

ok()   { echo -e "${GREEN}  ✓${NC} $1"; }
info() { echo -e "${BLUE}  →${NC} $1"; }
warn() { echo -e "${YELLOW}  ⚠${NC} $1"; }
err()  { echo -e "${RED}  ✗${NC} $1"; }

# ── Get the directory where this script lives ──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"

echo ""
echo -e "${BOLD}${RED}╔══════════════════════════════════════╗${NC}"
echo -e "${BOLD}${RED}║   ByteSlut — System Monitor          ║${NC}"
echo -e "${BOLD}${RED}║   Arch Linux Installer               ║${NC}"
echo -e "${BOLD}${RED}╚══════════════════════════════════════╝${NC}"
echo ""

# ══════════════════════════════════════
# STEP 1: Python version check
# ══════════════════════════════════════
echo -e "${BOLD}Step 1: Checking Python version${NC}"

PYTHON=$(which python3 2>/dev/null || which python 2>/dev/null)
if [ -z "$PYTHON" ]; then
    err "Python 3 not found. Install it: sudo pacman -S python"
    exit 1
fi

PYVER=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYMAJ=$($PYTHON -c "import sys; print(sys.version_info.major)")
PYMIN=$($PYTHON -c "import sys; print(sys.version_info.minor)")

if [ "$PYMAJ" -lt 3 ] || { [ "$PYMAJ" -eq 3 ] && [ "$PYMIN" -lt 10 ]; }; then
    err "Python 3.10+ required. You have $PYVER"
    info "Install: sudo pacman -S python"
    exit 1
fi

ok "Python $PYVER found at $PYTHON"

# ══════════════════════════════════════
# STEP 2: Install Python dependencies
# ══════════════════════════════════════
echo ""
echo -e "${BOLD}Step 2: Installing Python dependencies${NC}"

# Check if pip is available
if ! $PYTHON -m pip --version &>/dev/null; then
    info "pip not found, installing..."
    sudo pacman -S --noconfirm python-pip
fi

# Core dependencies
# --break-system-packages is needed on Arch because pacman owns the Python installation
# This is safe for user-space tools like ByteSlut
DEPS=(
    "flask>=3.0"         # Web dashboard framework
    "psutil>=5.9"        # CPU, RAM, disk, network stats from /proc
    "dbus-python"        # DBus for notifications and session events
    "PyGObject"          # GLib event loop for DBus
)

info "Installing pip packages (this may take a minute)..."
$PYTHON -m pip install --break-system-packages --quiet "${DEPS[@]}" 2>&1 | grep -E "Successfully|already|error" || true

# Check what actually got installed
for dep in "flask" "psutil"; do
    if $PYTHON -c "import $dep" 2>/dev/null; then
        ok "$dep installed"
    else
        warn "$dep could not be installed — some features may not work"
    fi
done

# dbus-python often needs the pacman version instead of pip
if ! $PYTHON -c "import dbus" 2>/dev/null; then
    info "Trying to install dbus-python via pacman (better for Arch)..."
    sudo pacman -S --noconfirm python-dbus python-gobject 2>/dev/null && ok "dbus-python installed via pacman" || warn "dbus-python not available — notification tracking disabled"
fi

# xprintidle for X11 idle detection (optional)
if ! command -v xprintidle &>/dev/null; then
    warn "xprintidle not found (optional, needed for X11 idle detection)"
    info "Install: sudo pacman -S xprintidle   (skip if you're on Wayland)"
fi

# ══════════════════════════════════════
# STEP 3: Input group (for keyboard/mouse tracking)
# ══════════════════════════════════════
echo ""
echo -e "${BOLD}Step 3: Setting up input device access${NC}"

if groups "$USER" | grep -q '\binput\b'; then
    ok "User '$USER' is already in the 'input' group"
else
    info "Adding '$USER' to 'input' group for keyboard/mouse tracking..."
    sudo usermod -aG input "$USER"
    ok "Added to input group"
    warn "You need to log out and back in for this to take effect"
    warn "Until then, keystroke tracking will show 0"
fi

# ══════════════════════════════════════
# STEP 4: Create data directory
# ══════════════════════════════════════
echo ""
echo -e "${BOLD}Step 4: Creating data directories${NC}"

DATA_DIR="$HOME/.local/share/byteslut"
LOG_DIR="$DATA_DIR/logs"
mkdir -p "$DATA_DIR" "$LOG_DIR"
ok "Data directory: $DATA_DIR"
ok "Log directory:  $LOG_DIR"

# ══════════════════════════════════════
# STEP 5: Initialize the database
# ══════════════════════════════════════
echo ""
echo -e "${BOLD}Step 5: Initializing SQLite database${NC}"

$PYTHON -c "
import sys
sys.path.insert(0, '$PROJECT_ROOT')
from daemon.db import initialize_database
initialize_database()
print('Database initialized at: ' + __import__('daemon.db', fromlist=['get_db_path']).get_db_path())
" && ok "Database ready" || err "Database initialization failed"

# ══════════════════════════════════════
# STEP 6: Install systemd user service
# ══════════════════════════════════════
echo ""
echo -e "${BOLD}Step 6: Installing systemd user service${NC}"

SERVICE_DIR="$HOME/.config/systemd/user"
mkdir -p "$SERVICE_DIR"

# Detect actual UID for XDG_RUNTIME_DIR
ACTUAL_UID=$(id -u)

cat > "$SERVICE_DIR/byteslut.service" << EOF
[Unit]
Description=ByteSlut System Monitor Daemon
# Use default.target — works on ALL DEs including Hyprland
# (graphical-session.target is GNOME/KDE only and breaks on Hyprland)
After=default.target

[Service]
Type=simple
ExecStart=$PYTHON $PROJECT_ROOT/daemon/main.py

# Restart automatically if it crashes
Restart=on-failure
RestartSec=10

# ── Environment ──
# These are HINTS — the daemon will auto-detect the real values
# from the running Hyprland/Wayland process at startup.
# Setting them here helps when the detection also fails.
Environment=HOME=$HOME
Environment=USER=$USER
Environment=XDG_RUNTIME_DIR=/run/user/${ACTUAL_UID}
Environment=PYTHONPATH=$PROJECT_ROOT
# Wayland display — daemon will find the real one via socket scan
Environment=WAYLAND_DISPLAY=wayland-1

# Pass ALL environment variables from the user session into the service.
# This is the KEY FIX: on Hyprland, the user session has
# HYPRLAND_INSTANCE_SIGNATURE, WAYLAND_DISPLAY, DBUS_SESSION_BUS_ADDRESS
# that the service needs but doesn't inherit by default.
# Run: systemctl --user import-environment  (in your Hyprland config)
# See: hyprland_env_setup note below

StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

# ── HYPRLAND ENV SETUP ──
# Tell the user to add import-environment to Hyprland config
HYPR_CONFIG="$HOME/.config/hypr/hyprland.conf"
HYPR_ENV_LINE="exec-once = systemctl --user import-environment WAYLAND_DISPLAY HYPRLAND_INSTANCE_SIGNATURE XDG_CURRENT_DESKTOP DBUS_SESSION_BUS_ADDRESS"

if [ -f "$HYPR_CONFIG" ]; then
    if ! grep -q "import-environment" "$HYPR_CONFIG"; then
        echo "" >> "$HYPR_CONFIG"
        echo "# ByteSlut: pass Wayland env to systemd user services" >> "$HYPR_CONFIG"
        echo "$HYPR_ENV_LINE" >> "$HYPR_CONFIG"
        ok "Added import-environment to $HYPR_CONFIG"
    else
        ok "import-environment already in hyprland.conf"
    fi
else
    warn "hyprland.conf not found at $HYPR_CONFIG"
    warn "Add this line manually to your hyprland.conf:"
    warn "  $HYPR_ENV_LINE"
fi

# Reload systemd and enable the service
systemctl --user daemon-reload
systemctl --user enable byteslut.service
ok "Systemd service installed: $SERVICE_DIR/byteslut.service"
ok "Service enabled (will auto-start on next login)"

# ══════════════════════════════════════
# STEP 7: Add shell alias
# ══════════════════════════════════════
echo ""
echo -e "${BOLD}Step 7: Adding 'spank' command to your shell${NC}"

# Read the current CLI command from config
CLI_CMD=$($PYTHON -c "
import json, sys
try:
    with open('$PROJECT_ROOT/config/settings.json') as f:
        print(json.load(f).get('cli_command', 'spank'))
except:
    print('spank')
")

ALIAS_LINE="alias $CLI_CMD='$PYTHON $PROJECT_ROOT/cli/byteslut.py'"
ALIAS_MARKER="# ByteSlut CLI alias"

# Add to ~/.bashrc
if [ -f "$HOME/.bashrc" ]; then
    if grep -q "ByteSlut CLI alias" "$HOME/.bashrc"; then
        ok "Alias already in ~/.bashrc"
    else
        echo "" >> "$HOME/.bashrc"
        echo "$ALIAS_MARKER" >> "$HOME/.bashrc"
        echo "$ALIAS_LINE" >> "$HOME/.bashrc"
        ok "Alias added to ~/.bashrc"
    fi
fi

# Add to ~/.zshrc
if [ -f "$HOME/.zshrc" ]; then
    if grep -q "ByteSlut CLI alias" "$HOME/.zshrc"; then
        ok "Alias already in ~/.zshrc"
    else
        echo "" >> "$HOME/.zshrc"
        echo "$ALIAS_MARKER" >> "$HOME/.zshrc"
        echo "$ALIAS_LINE" >> "$HOME/.zshrc"
        ok "Alias added to ~/.zshrc"
    fi
fi

# ══════════════════════════════════════
# STEP 8: Start the daemon now
# ══════════════════════════════════════
echo ""
echo -e "${BOLD}Step 8: Starting ByteSlut daemon${NC}"

# Start the systemd service
if systemctl --user start byteslut.service 2>/dev/null; then
    sleep 2
    if systemctl --user is-active --quiet byteslut.service; then
        ok "Daemon is running!"
    else
        warn "Daemon started but may have had issues. Check: journalctl --user -u byteslut -f"
    fi
else
    warn "Could not start via systemd. Starting manually..."
    nohup $PYTHON "$PROJECT_ROOT/daemon/main.py" >> "$LOG_DIR/daemon.log" 2>&1 &
    DAEMON_PID=$!
    echo "$DAEMON_PID" > "$DATA_DIR/daemon.pid"
    ok "Daemon started manually (PID: $DAEMON_PID)"
fi

# ══════════════════════════════════════
# DONE!
# ══════════════════════════════════════
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${GREEN}║   ByteSlut installed successfully! 🔥    ║${NC}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}How to use:${NC}"
echo -e "  ${CYAN}1.${NC} Source your shell config:"
echo -e "     ${BOLD}source ~/.bashrc${NC}   OR restart your terminal"
echo ""
echo -e "  ${CYAN}2.${NC} Open the dashboard:"
echo -e "     ${BOLD}${CLI_CMD}${NC}"
echo ""
echo -e "  ${CYAN}3.${NC} Check daemon status:"
echo -e "     ${BOLD}systemctl --user status byteslut${NC}"
echo ""
echo -e "  ${CYAN}4.${NC} View live logs:"
echo -e "     ${BOLD}journalctl --user -u byteslut -f${NC}"
echo ""
echo -e "  ${CYAN}5.${NC} Rename the app / change CLI command:"
echo -e "     Open dashboard → Settings"
echo ""
echo -e "  ${YELLOW}Note:${NC} Shell command history tracking starts NOW."
echo -e "  Data accumulates — check back tomorrow for meaningful stats!"
echo ""
