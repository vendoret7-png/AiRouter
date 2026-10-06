#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mixed runner: 9Router + Auto Tor IP changer.

Starts 9Router (AI router) in the background, then rotates your Tor IP.

On Render, 9Router listens on 0.0.0.0:$PORT (Render injects PORT env var).
We read PORT from env, pass it to 9Router (which honors $PORT) and also use
it for --router-port wait check.
"""

import argparse
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
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
        # NOTE: do NOT pass --skip-update — we WANT 9Router's built-in
        # auto-update so new versions apply without a manual restart.
        flags = ["--host", "0.0.0.0", "--port", str(port),
                 "--no-browser"]
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
                print(f"[router] exited (code={code}); restarting in 2s "
                      f"(expected after a 9router auto-update).")
                time.sleep(2)
                router_proc = start_router(cmd)
                if not wait_for_port(router_port, timeout=45):
                    print(f"[router] warning: port {router_port} not back yet")
                else:
                    print(f"[router] back up at http://0.0.0.0:{router_port}")
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
        if tor_proc and tor_proc.poll() is None:
            tor_proc.terminate()
        if router_proc and router_proc.poll() is None:
            router_proc.terminate()


if __name__ == "__main__":
    main()
