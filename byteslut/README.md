# ByteSlut — Arch Linux System Monitor

A lightweight, comprehensive system monitor for Arch Linux.
Tracks everything about how you use your computer with a beautiful dark web dashboard.

## Quick Install

```bash
cd byteslut/
chmod +x install.sh
./install.sh
source ~/.bashrc
spank
```

## What It Tracks

| Category | Details |
|---|---|
| **Screen Time** | Daily/monthly/yearly usage, sessions, idle time, averages |
| **App Usage** | Per-app foreground + background time, Flatpak detection, persists after deletion |
| **Browser** | History from Brave/Firefox/Chrome (Flatpak too), YouTube video titles, domain time |
| **Terminal** | Commands, exit codes (errors highlighted), duration, PWD, sudo usage |
| **System** | CPU%, RAM%, Swap, Disk I/O, CPU/GPU temperatures (min/max/avg) |
| **Network** | Upload/download per day, per-app attribution |
| **Battery** | Charge level, health (capacity degradation), charge cycles |
| **Notifications** | App, title, body, timestamp, clicked/dismissed |
| **Packages** | pacman install/remove/upgrade history |
| **Input** | Keystrokes count, mouse clicks, WPM estimate |
| **Productivity** | Daily score, focus streaks, work vs entertainment split |

## Architecture

```
byteslut/
├── daemon/               ← Background collector (runs as systemd service)
│   ├── main.py           ← Entry point, starts all collector threads
│   ├── db.py             ← SQLite + BatchWriter (flushes every 30s)
│   └── collectors/
│       ├── session.py    ← Screen time, idle detection, session events
│       ├── apps.py       ← Active window tracking (Hyprland/Sway/X11)
│       ├── cpu_ram.py    ← CPU, RAM, temperatures
│       ├── browser.py    ← Browser history reader (no extensions needed)
│       ├── extras.py     ← Notifications, commands, network, battery, packages, input
│       └── productivity.py ← Score calculation, focus streaks
├── web/
│   ├── app.py            ← Flask web server (all dashboard routes)
│   └── templates/        ← HTML pages (dark monospace theme)
├── cli/
│   └── byteslut.py       ← The 'spank' command
├── config/
│   └── settings.json     ← All settings (editable from dashboard)
└── install.sh            ← One-shot installer
```

## Display Server Support

Works on ALL Linux desktop environments:

| DE/WM | Window Tracking | Idle Detection |
|---|---|---|
| Hyprland | ✅ Native (hyprctl) | ✅ loginctl |
| Sway | ✅ swaymsg | ✅ loginctl |
| GNOME (Wayland) | ⚡ Best effort | ✅ loginctl |
| KDE (Wayland) | ⚡ Best effort | ✅ loginctl |
| i3/bspwm (X11) | ✅ xdotool | ✅ xprintidle |
| XFCE (X11) | ✅ xprop | ✅ xprintidle |
| Any X11 | ✅ xprop fallback | ✅ xprintidle |

## Browser Support

| Browser | Path | Flatpak |
|---|---|---|
| Brave | `~/.config/BraveSoftware/...` | ✅ `~/.var/app/com.brave.Browser/...` |
| Firefox | `~/.mozilla/firefox/...` | ✅ `~/.var/app/org.mozilla.firefox/...` |
| Chrome | `~/.config/google-chrome/...` | ✅ |
| Chromium | `~/.config/chromium/...` | ✅ |

## CLI Commands

```bash
spank                   # Open dashboard in browser
spank --daemon          # Start background collector
spank --stop            # Stop everything
spank --status          # Show running status
spank --install         # Re-run installation
```

## Systemd Service

```bash
systemctl --user start byteslut      # Start
systemctl --user stop byteslut       # Stop
systemctl --user restart byteslut    # Restart
systemctl --user status byteslut     # Status
journalctl --user -u byteslut -f     # Live logs
```

## Data Storage

- Database: `~/.local/share/byteslut/byteslut.db`
- Logs: `~/.local/share/byteslut/logs/daemon.log`
- Size: ~5–20 MB per year (SQLite with WAL mode)
- **Your data never leaves your machine**

## Rename the App

Open dashboard → Settings → change "App Name" and "CLI Command"
The shell alias in ~/.bashrc / ~/.zshrc updates automatically.

## Privacy Controls

In Settings, you can disable:
- Keystroke counting
- Browser history
- Notification logging  
- Terminal command tracking

## Requirements

- Arch Linux (or any systemd-based distro)
- Python 3.10+
- pip packages: `flask psutil dbus-python PyGObject`
- Optional: `xprintidle` (for X11 idle detection)
- Optional: be in `input` group (for keyboard/mouse counting)
