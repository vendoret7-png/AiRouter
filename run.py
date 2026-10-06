#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mixed runner: 9Router + Auto Tor IP changer.

Starts 9Router (AI router) in the background, then rotates your Tor IP.

On Render, 9Router listens on 0.0.0.0:$PORT (Render injects PORT env var).
We read PORT from env, pass it to 9Router (which honors $PORT) and also use
it for --router-port wait check.
"""

import argparse
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path


def find_9router(mode, port):
    """Return command to launch 9Router on given port."""
    if mode == "docker":
        return ["docker", "run", "--rm", "-e", f"PORT={port}",
                "-p", f"{port}:{port}", "decolua/9router:latest"]
    if mode == "npm":
        npx = shutil.which("npx") or shutil.which("npm")
        router = shutil.which("9router")
        # Docker overrides $HOSTNAME with the container ID, so never rely on
        # env for binding — pass --host/--port explicitly.
        # --skip-update is REQUIRED: without it, in a non-TTY container the
        # launcher menu resolves to "exit" and 9router dies with "Exiting...".
        # run.py owns the update check below instead.
        flags = ["--host", "0.0.0.0", "--port", str(port),
                 "--no-browser", "--skip-update"]
        if router:
            return [router] + flags
        if npx:
            # Use a stable package name (no @latest pin) so the locally
            # installed 9router can self-update in place.
            return [npx, "--yes", "9router"] + flags
        return None
    return None


def start_router(cmd):
    """Start router in background, inheriting stdout so 9Router logs reach Render."""
    print(f"[router] starting: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        stdout=None,
        stderr=subprocess.STDOUT,
        text=True,
        shell=False,
    )
    return proc


def wait_for_port(port, timeout=30):
    """Wait until local port is accepting connections."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except Exception:
            time.sleep(0.5)
    return False


PACKAGE_NAME = "9router"
PACKAGE_MANAGER = "npm"
CURRENT_VERSION = "0.0.0"
UPDATE_CHECK_INTERVAL = int(os.environ.get("ROUTER_UPDATE_INTERVAL", "1800"))


def _installed_version():
    """Return the globally installed 9router version, or None if unknown."""
    npm = shutil.which("npm")
    if not npm:
        return None
    try:
        r = subprocess.run(
            [npm, "list", "-g", PACKAGE_NAME, "--depth=0", "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=60,
        )
        data = json.loads(r.stdout or "{}")
        dep = (data.get("dependencies") or {}).get(PACKAGE_NAME) or {}
        return dep.get("version")
    except Exception:
        return None


def _latest_version(timeout=8):
    """Return latest version published on the npm registry, or None."""
    url = f"https://registry.npmjs.org/{PACKAGE_NAME}/latest"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return data.get("version")
    except Exception as e:
        print(f"[update] registry check failed: {e}")
        return None


def _is_newer(latest, current):
    """True if latest > current using dotted numeric comparison."""
    if not latest or not current:
        return False
    def parts(v):
        out = []
        for chunk in str(v).split("."):
            num = ""
            for ch in chunk:
                if ch.isdigit():
                    num += ch
                else:
                    break
            out.append(int(num) if num else 0)
        return out
    a, b = parts(latest), parts(current)
    size = max(len(a), len(b))
    a += [0] * (size - len(a))
    b += [0] * (size - len(b))
    return a > b


def _npm_install_latest():
    """Install the newest 9router globally. Returns True on success."""
    npm = shutil.which("npm")
    if not npm:
        print("[update] npm not found; skipping install")
        return False
    print(f"[update] installing {PACKAGE_MANAGER} -g {PACKAGE_NAME}@latest ...")
    try:
        r = subprocess.run(
            [PACKAGE_MANAGER, "install", "-g", f"{PACKAGE_NAME}@latest", "--prefer-online"],
            stdout=sys.stdout, stderr=subprocess.STDOUT,
            text=True, timeout=300,
        )
        return r.returncode == 0
    except Exception as e:
        print(f"[update] install failed: {e}")
        return False


def apply_router_update():
    """Install the latest 9router globally (used after the router terminates)."""
    return _npm_install_latest()


def update_watcher(state):
    """Background thread: poll npm for a newer 9router and update in place.

    Runs independently of the router process so a new release can be installed
    without stopping the service. `state` is a shared dict with keys:
      - "action": "" | "update"  (set when a fresh version was installed)
      - "stop":   bool           (set by main to end the thread)
    """
    while not state.get("stop"):
        if state.get("action"):
            # An update is pending a restart; wait for the main loop to handle it.
            time.sleep(5)
            continue
        latest = _latest_version()
        if latest and _is_newer(latest, CURRENT_VERSION):
            print(f"[update] new {PACKAGE_NAME} available: {CURRENT_VERSION} -> {latest}")
            if _npm_install_latest():
                state["action"] = "update"
                state["latest"] = latest
                continue
        time.sleep(UPDATE_CHECK_INTERVAL)


def main():
    # Render injects PORT env; fall back to arg default.
    env_port = os.environ.get("PORT")

    parser = argparse.ArgumentParser(description="AI Router = 9Router + Auto Tor IP changer")
    parser.add_argument("--interval", type=int, default=3, help="Seconds between Tor IP changes")
    if env_port:
        parser.set_defaults(router_port=int(env_port))
    else:
        parser.add_argument("--router-port", type=int, default=20128, help="9Router port")
    parser.add_argument("--router-mode", choices=["npm", "docker", "none"], default="npm",
                        help="How to start 9Router")
    parser.add_argument("--tor-method", choices=["command", "control"], default="control",
                        help="Tor reload method")
    parser.add_argument("--tor-reload", default="service tor reload",
                        help="Command to reload Tor identity (method=command)")
    parser.add_argument("--control-host", default="127.0.0.1")
    parser.add_argument("--control-port", type=int, default=9051)
    parser.add_argument("--control-password", default=None)
    parser.add_argument("--socks-port", type=int, default=9050)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    router_port = getattr(args, "router_port", int(env_port) if env_port else 20128)

    # Export PORT so 9Router subprocess sees it (needed when we launch via npx/9router).
    os.environ["PORT"] = str(router_port)

    router_proc = None
    cmd = None

    # --- auto-update watcher (runs in background, no downtime) -------------
    global CURRENT_VERSION
    if args.router_mode == "npm":
        CURRENT_VERSION = _installed_version() or "0.0.0"
    update_state = {"action": "", "stop": False, "latest": CURRENT_VERSION}
    threading.Thread(
        target=update_watcher, args=(update_state,), daemon=True
    ).start()
    print(f"[update] watcher started (every {UPDATE_CHECK_INTERVAL}s), "
          f"installed {PACKAGE_NAME}@{CURRENT_VERSION}")

    if args.router_mode != "none":
        cmd = find_9router(args.router_mode, router_port)
        if not cmd:
            print(f"[!] Cannot find 9router / npx / npm for router mode {args.router_mode}.")
            sys.exit(1)
        router_proc = start_router(cmd)
        print(f"[router] waiting for port {router_port} ...")
        if wait_for_port(router_port, timeout=45):
            print(f"[router] up at http://0.0.0.0:{router_port}")
        else:
            print(f"[router] warning: port {router_port} not reachable yet; see router.log")

    # Start auto Tor IP changer in background
    tor_cmd = [
        sys.executable,
        str(Path(__file__).parent / "auto_tor.py"),
        "--interval", str(args.interval),
        "--method", args.tor_method,
        "--tor-reload", args.tor_reload,
        "--control-host", args.control_host,
        "--control-port", str(args.control_port),
        "--socks-port", str(args.socks_port),
    ]
    if args.control_password:
        tor_cmd += ["--control-password", args.control_password]
    if args.verbose:
        tor_cmd.append("--verbose")

    print(f"[tor] starting auto IP changer: interval={args.interval}s method={args.tor_method}")
    tor_proc = subprocess.Popen(
        tor_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        while True:
            if router_proc and router_proc.poll() is not None:
                # 9Router exits by itself after applying an auto-update (it prints
                # "Exiting..."). That is NOT a crash -- respawn it so the new
                # version is picked up without restarting the whole service.
                code = router_proc.returncode
                print(f"[router] exited (code={code}); applying update, "
                      f"then restarting in 2s (expected after a 9router auto-update).")
                apply_router_update()
                time.sleep(2)
                router_proc = start_router(cmd)
                if not wait_for_port(router_port, timeout=45):
                    print(f"[router] warning: port {router_port} not back yet")
                else:
                    print(f"[router] back up at http://0.0.0.0:{router_port}")

            if update_state.get("action") == "update":
                # Watcher installed a newer 9router — graceful reload so the
                # service never goes down (wait_for_port gates before signaling
                # the new process; the old one is already gone).
                print(f"[update] restarting router to apply {update_state.get('latest')}")
                old = router_proc
                if old and old.poll() is None:
                    old.terminate()
                    try:
                        old.wait(timeout=10)
                    except Exception:
                        old.kill()
                time.sleep(2)
                router_proc = start_router(cmd)
                if not wait_for_port(router_port, timeout=45):
                    print(f"[update] warning: port {router_port} not back yet")
                else:
                    print(f"[update] {PACKAGE_NAME} updated, back up")
                CURRENT_VERSION = update_state.get("latest") or CURRENT_VERSION
                update_state["action"] = ""

            if tor_proc.poll() is not None:
                print("[tor] auto IP changer exited.")
                break
            line = tor_proc.stdout.readline()
            if line:
                print(line.rstrip())
            else:
                time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n[*] shutting down...")
    finally:
        update_state["stop"] = True
        if tor_proc and tor_proc.poll() is None:
            tor_proc.terminate()
        if router_proc and router_proc.poll() is None:
            router_proc.terminate()


if __name__ == "__main__":
    main()
