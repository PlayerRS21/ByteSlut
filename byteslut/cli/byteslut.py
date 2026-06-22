#!/usr/bin/env python3
"""
cli/byteslut.py — The 'spank' CLI
===================================
MODES:
  spank                  Default: start dashboard, hold terminal (Ctrl+C kills it)
  spank --background     Start in background, return to terminal immediately
  spank --timeout 300    Auto-kill after 5 min of no browser activity
  spank --kill           Kill a running background dashboard, free the port
  spank --daemon         Start background data collector daemon
  spank --stop           Stop the daemon
  spank --status         Show what is running
  spank --install        Re-install systemd service + shell alias
"""

import os, sys, json, time, socket, signal, subprocess, webbrowser, argparse, threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def load_config():
    try:
        with open(PROJECT_ROOT / "config" / "settings.json") as f:
            return json.load(f)
    except Exception:
        return {"app_name": "ByteSlut", "cli_command": "byteslut", "dashboard_port": 6969}


def is_port_open(host, port, timeout=1.0):
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def pid_file(name):
    d = Path.home() / ".local/share/byteslut"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.pid"


def write_pid(name, pid):
    pid_file(name).write_text(str(pid))


def read_pid(name):
    f = pid_file(name)
    if not f.exists():
        return None
    try:
        pid = int(f.read_text().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        f.unlink(missing_ok=True)
        return None


def kill_dashboard(port, config):
    """Kill the dashboard - tries graceful HTTP shutdown first, then SIGTERM by PID."""
    url = f"http://127.0.0.1:{port}"

    # Graceful: POST /shutdown endpoint
    if is_port_open("127.0.0.1", port):
        try:
            import urllib.request
            urllib.request.urlopen(
                urllib.request.Request(f"{url}/shutdown", method="POST", data=b""),
                timeout=3
            )
            time.sleep(0.6)
            if not is_port_open("127.0.0.1", port):
                print(f"✅ Dashboard stopped.")
                pid_file("web").unlink(missing_ok=True)
                return
        except Exception:
            pass

    # Force: SIGTERM via PID file
    pid = read_pid("web")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            pid_file("web").unlink(missing_ok=True)
            print(f"✅ Dashboard stopped (PID {pid}).")
            return
        except ProcessLookupError:
            pid_file("web").unlink(missing_ok=True)

    print(f"ℹ️  Dashboard not running on port {port}.")


def start_daemon():
    p = subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "daemon" / "main.py")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    write_pid("daemon", p.pid)
    print(f"✅ Daemon started (PID: {p.pid})")


def stop_daemon():
    pid = read_pid("daemon")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            pid_file("daemon").unlink(missing_ok=True)
            print(f"✅ Daemon stopped (PID: {pid})")
            return
        except ProcessLookupError:
            pid_file("daemon").unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "stop", "byteslut"],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("✅ Daemon stopped")


def show_status(config):
    port = config.get("dashboard_port", 6969)
    print(f"\n{'═'*42}")
    print(f"  {config['app_name']} Status")
    print(f"{'═'*42}")

    pid = read_pid("daemon")
    if pid:
        print(f"  Daemon:    ● Running (PID {pid})")
    else:
        try:
            r = subprocess.run(["systemctl", "--user", "is-active", "byteslut"],
                               capture_output=True, text=True)
            print(f"  Daemon:    {'● Running (systemd)' if r.stdout.strip()=='active' else '○ Not running'}")
        except Exception:
            print(f"  Daemon:    ○ Not running")

    print(f"  Dashboard: {'● http://127.0.0.1:'+str(port) if is_port_open('127.0.0.1', port) else '○ Not running'}")

    from daemon.db import get_db_path
    db = get_db_path()
    if os.path.exists(db):
        mb = os.path.getsize(db) / 1048576
        print(f"  Database:  ✓ {db} ({mb:.1f} MB)")
    else:
        print(f"  Database:  ✗ Not initialized")

    print(f"  Command:   {config['cli_command']}")
    print(f"{'═'*42}\n")


def open_dashboard_foreground(config, idle_timeout=None):
    """
    DEFAULT MODE: Start Flask in a background thread but HOLD the terminal.

    The terminal shows a small status line and waits for:
      - Ctrl+C  (SIGINT)  → stops dashboard, frees port
      - typing 'q' + Enter → same

    WHY THIS IS BETTER THAN PURE BACKGROUND:
      Pure background = orphan process. Port stays occupied even after you
      close the terminal. You forget it's running. 'spank' tries to start
      a second instance and fails because port 6969 is taken.

      Foreground-hold = the terminal is your on/off switch. Close it or
      press Ctrl+C = dashboard is gone and port is free. Clean.
    """
    port = config.get("dashboard_port", 6969)
    url  = f"http://127.0.0.1:{port}"
    name = config.get("app_name", "ByteSlut")

    if is_port_open("127.0.0.1", port):
        print(f"📊 {name} already running → {url}")
        webbrowser.open(url)
        _hold_terminal(config, port, idle_timeout, already_running=True)
        return

    print(f"🚀 Starting {name}...")

    # Start Flask server in a daemon thread (dies when main process exits)
    from web.app import start_server
    t = threading.Thread(
        target=start_server,
        kwargs={"host": "127.0.0.1", "port": port, "idle_timeout": idle_timeout},
        daemon=True
    )
    t.start()

    # Wait up to 10s for Flask to be ready
    for i in range(20):
        time.sleep(0.5)
        if is_port_open("127.0.0.1", port):
            break
        sys.stdout.write(f"\r  Waiting... {(i+1)*0.5:.1f}s")
        sys.stdout.flush()
    else:
        print(f"\n⚠️  Timed out waiting for server. Check:")
        print(f"   python {PROJECT_ROOT}/web/app.py")
        return

    print(f"\r✅ {name} ready at {url}             ")
    webbrowser.open(url)
    write_pid("web", os.getpid())  # PID = this process (thread is inside it)
    _hold_terminal(config, port, idle_timeout)


def _hold_terminal(config, port, idle_timeout=None, already_running=False):
    """Print status and wait for Ctrl+C or 'q' to kill the dashboard."""
    url  = f"http://127.0.0.1:{port}"
    name = config.get("app_name", "ByteSlut")
    timeout_note = f" · auto-kills after {idle_timeout}s idle" if idle_timeout else ""

    print(f"\n  ┌─────────────────────────────────────────┐")
    print(f"  │  {name:<39}│")
    print(f"  │  {url:<39}│")
    print(f"  │                                         │")
    print(f"  │  Ctrl+C  or type 'q' + Enter → stop    │")
    if idle_timeout:
        note = f"  Auto-kills after {idle_timeout}s idle"
        print(f"  │  {note:<39}│")
    print(f"  └─────────────────────────────────────────┘\n")

    # Optional: show idle countdown if timeout is set
    if idle_timeout:
        def _countdown():
            try:
                import urllib.request
                while True:
                    time.sleep(10)
                    try:
                        data = json.loads(
                            urllib.request.urlopen(
                                f"http://127.0.0.1:{port}/api/dashboard-status",
                                timeout=2
                            ).read()
                        )
                        idle = data.get("idle_seconds", 0)
                        remaining = max(0, idle_timeout - idle)
                        sys.stdout.write(f"\r  Idle: {idle}s  |  Auto-kill in: {remaining}s    ")
                        sys.stdout.flush()
                    except Exception:
                        break
            except Exception:
                pass
        threading.Thread(target=_countdown, daemon=True).start()

    try:
        while True:
            try:
                line = input()
                if line.strip().lower() in ("q", "quit", "exit", "stop", "kill", "x"):
                    break
            except EOFError:
                # stdin closed (e.g. script piped) - wait until server dies
                while is_port_open("127.0.0.1", port):
                    time.sleep(2)
                return
    except KeyboardInterrupt:
        pass

    # Cleanup
    print(f"\n\n  Stopping {name}...")
    if not already_running:
        # We own this server (started it in a thread) — just exit
        pid_file("web").unlink(missing_ok=True)
        print(f"  Port {port} released. Bye! 👋\n")
    else:
        kill_dashboard(port, config)
    sys.exit(0)


def open_dashboard_background(config, idle_timeout=None):
    """Start dashboard fully detached. 'spank --kill' to stop it."""
    port = config.get("dashboard_port", 6969)
    if is_port_open("127.0.0.1", port):
        print(f"📊 Already running → http://127.0.0.1:{port}")
        webbrowser.open(f"http://127.0.0.1:{port}")
        return

    launcher_code = (
        f"import sys; sys.path.insert(0,'{PROJECT_ROOT}'); "
        f"from web.app import start_server; "
        f"start_server(idle_timeout={repr(idle_timeout)})"
    )
    p = subprocess.Popen(
        [sys.executable, "-c", launcher_code],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    write_pid("web", p.pid)

    for i in range(20):
        time.sleep(0.5)
        if is_port_open("127.0.0.1", port):
            url = f"http://127.0.0.1:{port}"
            print(f"✅ Dashboard running at {url} (PID: {p.pid})")
            if idle_timeout:
                print(f"   Auto-kills after {idle_timeout}s of no activity")
            print(f"   Stop with: spank --kill")
            webbrowser.open(url)
            return
        sys.stdout.write(f"\r  Starting... {(i+1)*0.5:.1f}s")
        sys.stdout.flush()

    print(f"\n⚠️  Server didn't start. Try manually:")
    print(f"   python {PROJECT_ROOT}/web/app.py")


def install_systemd_service():
    svc = f"""[Unit]
Description=ByteSlut System Monitor Daemon
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart={sys.executable} {PROJECT_ROOT}/daemon/main.py
Restart=on-failure
RestartSec=10
Environment=PYTHONPATH={PROJECT_ROOT}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=graphical-session.target
"""
    d = Path.home() / ".config/systemd/user"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "byteslut.service"
    f.write_text(svc)
    subprocess.run(["systemctl", "--user", "daemon-reload"])
    subprocess.run(["systemctl", "--user", "enable", "byteslut"])
    print(f"✅ Systemd service installed: {f}")


def install_shell_alias(command):
    this = Path(__file__).resolve()
    alias = f"alias {command}='{sys.executable} {this}'"
    marker = "# ByteSlut CLI alias"
    for rc in [Path.home() / ".bashrc", Path.home() / ".zshrc"]:
        if not rc.exists():
            continue
        if marker in rc.read_text():
            print(f"  ℹ️  Alias already in {rc}")
            continue
        with open(rc, "a") as fh:
            fh.write(f"\n{marker}\n{alias}\n")
        print(f"  ✅ Alias '{command}' added to {rc}")


def main():
    config = load_config()
    cmd    = config.get("cli_command", "byteslut")
    port   = config.get("dashboard_port", 6969)

    p = argparse.ArgumentParser(
        prog=cmd,
        description=f"{config['app_name']} — System Monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
USAGE:
  {cmd}                   Open dashboard, hold terminal (Ctrl+C to kill)
  {cmd} --background      Open dashboard, return to terminal immediately
  {cmd} --timeout 300     Auto-kill after 5 min of no browser activity
  {cmd} --kill            Kill running dashboard, free port {port}
  {cmd} --daemon          Start background data collector
  {cmd} --stop            Stop background data collector
  {cmd} --status          Show what is running
  {cmd} --install         Re-install systemd service + shell alias
        """
    )
    p.add_argument("--background", "-b", action="store_true",
                   help="Start dashboard in background, return to terminal")
    p.add_argument("--timeout", "-t", type=int, default=None, metavar="SECONDS",
                   help="Auto-kill after N seconds of no browser activity")
    p.add_argument("--kill", "-k", action="store_true",
                   help="Kill the running dashboard")
    p.add_argument("--daemon", action="store_true",
                   help="Start background data collector daemon")
    p.add_argument("--stop", action="store_true",
                   help="Stop background data collector daemon")
    p.add_argument("--status", action="store_true",
                   help="Show status of all ByteSlut processes")
    p.add_argument("--install", action="store_true",
                   help="Install systemd service and shell alias")
    p.add_argument("--port", type=int, default=None,
                   help=f"Override dashboard port (default: {port})")

    args = p.parse_args()
    if args.port:
        config["dashboard_port"] = args.port

    if   args.kill:       kill_dashboard(config["dashboard_port"], config)
    elif args.daemon:     start_daemon()
    elif args.stop:       stop_daemon()
    elif args.status:     show_status(config)
    elif args.install:
        print(f"\n🔧 Installing {config['app_name']}...\n")
        install_systemd_service()
        install_shell_alias(cmd)
        print(f"\n✅ Done. Run: source ~/.bashrc  then: {cmd}\n")
    elif args.background: open_dashboard_background(config, idle_timeout=args.timeout)
    else:                 open_dashboard_foreground(config,  idle_timeout=args.timeout)


if __name__ == "__main__":
    main()
