#!/usr/bin/env python3
import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
import urllib.request
import json

BASE_DIR = Path(__file__).resolve().parent
PID_FILE = Path("/tmp/agy-api.pid")
LOG_FILE = Path.home() / ".cache" / "agy-api.log"

def is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False

def get_running_pid():
    if PID_FILE.exists():
        try:
            with open(PID_FILE, "r") as f:
                pid = int(f.read().strip())
            if is_running(pid):
                return pid
            else:
                PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    # Fallback: check if server.py is running via systemd or background process
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "python3.*Projects/agy-openclaw-bridge/server.py"]
        ).decode().strip()
        pids = [int(p) for p in out.split() if p.isdigit() and int(p) != os.getpid()]
        if pids:
            return pids[0]
    except Exception:
        pass
    return None

def start_server(foreground: bool = False):
    pid = get_running_pid()
    if pid:
        print(f"⚠️  AGY API Bridge is already running (PID: {pid}).")
        return

    if foreground:
        print("Starting AGY API Bridge in foreground mode...")
        import server
        server.main()
        return

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_fd = open(LOG_FILE, "a")

    print(f"🚀 Starting AGY API Bridge daemon in background...")
    proc = subprocess.Popen(
        [sys.executable, str(BASE_DIR / "server.py")],
        stdout=log_fd,
        stderr=log_fd,
        cwd=str(BASE_DIR),
        start_new_session=True,
    )
    with open(PID_FILE, "w") as f:
        f.write(str(proc.pid))

    # Wait briefly and verify it started
    time.sleep(1.0)
    if is_running(proc.pid):
        print(f"✅ AGY API Bridge running successfully (PID: {proc.pid})")
        print(f"📄 Logs: {LOG_FILE}")
        print(f"🌐 Endpoint: http://127.0.0.1:8000/v1")
    else:
        print("❌ Server failed to start. Check logs:")
        with open(LOG_FILE, "r") as f:
            lines = f.readlines()
            print("".join(lines[-10:]))

def stop_server():
    pid = get_running_pid()
    if not pid:
        print("ℹ️  AGY API Bridge is not running.")
        return

    print(f"🛑 Stopping AGY API Bridge (PID: {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            time.sleep(0.1)
            if not is_running(pid):
                break
        if is_running(pid):
            os.kill(pid, signal.SIGKILL)
        PID_FILE.unlink(missing_ok=True)
        print("✅ Server stopped.")
    except Exception as e:
        print(f"Error stopping process: {e}")

def check_status():
    pid = get_running_pid()
    if pid:
        print(f"🟢 AGY API Bridge is RUNNING (PID: {pid})")
        print(f"🌐 Base URL: http://127.0.0.1:8000/v1")
        # Try pinging /health
        try:
            req = urllib.request.Request("http://127.0.0.1:8000/health")
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read().decode())
                print(f"📊 Health: {data.get('status')}")
                pool = data.get("pool", {})
                workers = pool.get("workers", [])
                if workers:
                    print(f"⚡ Multi-Worker Gateway Pool ({len(workers)} accounts, mode: {pool.get('dispatch_mode', 'least_busy')}):")
                    for w in workers:
                        print(f"   • {w['id']:<5} [{w.get('account', 'unknown')}]: {w.get('total_served', 0)} served, {w.get('active_requests', 0)} active")
        except Exception as e:
            print(f"⚠️  Warning: Service did not respond to /health: {e}")
    else:
        print("🔴 AGY API Bridge is STOPPED.")

def run_tests():
    print("🧪 Running end-to-end tests...")
    subprocess.run([sys.executable, str(BASE_DIR / "test_client.py")])

def print_openclaw_config():
    config_file = BASE_DIR / "openclaw.config.json"
    if config_file.exists():
        print("============================================================")
        print("📋 OpenClaw Configuration Snippet")
        print("Add this to ~/.openclaw/openclaw.json or models.json:")
        print("============================================================")
        with open(config_file, "r") as f:
            print(f.read())
        print("============================================================")

def install_systemd_service():
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    target_service = service_dir / "agy-api.service"
    source_service = BASE_DIR / "agy-api.service"

    if source_service.exists():
        with open(source_service, "r") as f:
            content = f.read()
        with open(target_service, "w") as f:
            f.write(content)
        
        subprocess.run(["systemctl", "--user", "daemon-reload"])
        print(f"✅ Systemd service installed at {target_service}")
        print("You can manage it with:")
        print("  systemctl --user start agy-api")
        print("  systemctl --user enable agy-api")
        print("  systemctl --user status agy-api")

def main():
    parser = argparse.ArgumentParser(description="AGY CLI-to-API Bridge Manager")
    parser.add_argument("command", choices=["start", "stop", "restart", "status", "test", "config", "install-service"], help="Command to run")
    parser.add_argument("-f", "--foreground", action="store_true", help="Run in foreground instead of daemon")

    args = parser.parse_args()

    if args.command == "start":
        start_server(foreground=args.foreground)
    elif args.command == "stop":
        stop_server()
    elif args.command == "restart":
        stop_server()
        time.sleep(1)
        start_server(foreground=args.foreground)
    elif args.command == "status":
        check_status()
    elif args.command == "test":
        run_tests()
    elif args.command == "config":
        print_openclaw_config()
    elif args.command == "install-service":
        install_systemd_service()

if __name__ == "__main__":
    main()
