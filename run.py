#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mixed runner: 9Router + Auto Tor IP changer.

Starts 9Router (AI router) in the background, then rotates your Tor IP.
"""

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path


def find_9router(mode):
    """Return command to launch 9Router."""
    if mode == "docker":
        return ["docker", "run", "--rm", "-p", "20128:20128", "decolua/9router:latest"]
    if mode == "npm":
        npx = shutil.which("npx") or shutil.which("npm")
        if npx:
            return [npx, "9router@latest"]
        return None
    return None


def start_router(cmd, log_path):
    """Start router in background, returning Popen."""
    print(f"[router] starting: {' '.join(cmd)}")
    with open(log_path, "w", encoding="utf-8") as f:
        proc = subprocess.Popen(
            cmd,
            stdout=f,
            stderr=subprocess.STDOUT,
            text=True,
            shell=False,
        )
    return proc


def wait_for_port(port, timeout=15):
    """Wait until local port is accepting connections."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except Exception:
            time.sleep(0.5)
    return False


import socket


def main():
    parser = argparse.ArgumentParser(description="AI Router = 9Router + Auto Tor IP changer")
    parser.add_argument("--interval", type=int, default=3, help="Seconds between Tor IP changes")
    parser.add_argument("--router-mode", choices=["npm", "docker", "none"], default="npm",
                        help="How to start 9Router")
    parser.add_argument("--router-port", type=int, default=20128, help="9Router port")
    parser.add_argument("--tor-method", choices=["command", "control"], default="command",
                        help="Tor reload method")
    parser.add_argument("--tor-reload", default="service tor reload",
                        help="Command to reload Tor identity (method=command)")
    parser.add_argument("--control-host", default="127.0.0.1")
    parser.add_argument("--control-port", type=int, default=9051)
    parser.add_argument("--control-password", default=None)
    parser.add_argument("--socks-port", type=int, default=9050)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    log_dir = Path(__file__).parent / ".ai-router-logs"
    log_dir.mkdir(exist_ok=True)

    router_proc = None
    if args.router_mode != "none":
        cmd = find_9router(args.router_mode)
        if not cmd:
            print(f"[!] Cannot find npx/npm or docker for router mode {args.router_mode}.")
            print("    Install Node.js (npm/npx) or Docker, or use --router-mode none.")
            sys.exit(1)
        router_proc = start_router(cmd, log_dir / "router.log")
        print(f"[router] waiting for port {args.router_port} ...")
        if wait_for_port(args.router_port, timeout=20):
            print(f"[router] up at http://localhost:{args.router_port}")
        else:
            print(f"[router] warning: port {args.router_port} not reachable yet; see router.log")

    # Start auto Tor IP changer in background (subprocess)
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

    print(f"[tor] starting auto IP changer: interval={args.interval}s")
    tor_proc = subprocess.Popen(
        tor_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        while True:
            if router_proc and router_proc.poll() is not None:
                print("[router] exited unexpectedly.")
                break
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
