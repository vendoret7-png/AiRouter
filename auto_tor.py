#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Auto Tor IP changer.

Cleaned / adapted version of FDX100/Auto_Tor_IP_changer.
Runs a loop that requests a new Tor identity every N seconds.
"""

import argparse
import os
import shlex
import socket
import subprocess
import sys
import time


def get_public_ip(proxy=None, timeout=5):
    """Return current public IP seen by the internet."""
    import requests
    try:
        if proxy:
            r = requests.get("http://checkip.amazonaws.com", proxies=proxy, timeout=timeout)
            return r.text.strip()
        r = requests.get("http://checkip.amazonaws.com", timeout=timeout)
        return r.text.strip()
    except Exception as e:
        return f"error: {e}"


def reload_tor_command(cmd):
    """Reload tor identity via system command (Linux/macOS style)."""
    try:
        subprocess.run(cmd, shell=True, check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"[tor] reload failed: {e}", file=sys.stderr)
        return False


def newnym_via_control(control_host, control_port, control_password=None):
    """Send NEWNYM to Tor control port to change identity."""
    try:
        s = socket.create_connection((control_host, control_port), timeout=5)
        s.settimeout(5)
        if control_password:
            s.sendall(f'AUTHENTICATE "{control_password}"\r\n'.encode())
            resp = s.recv(1024).decode()
            if "250" not in resp:
                print(f"[tor] auth failed: {resp}", file=sys.stderr)
                return False
        else:
            s.sendall(b'AUTHENTICATE\r\n')
            resp = s.recv(1024).decode()
            if "250" not in resp:
                print(f"[tor] auth required or failed: {resp}", file=sys.stderr)
                return False
        s.sendall(b'SIGNAL NEWNYM\r\n')
        resp = s.recv(1024).decode()
        s.close()
        if "250" in resp:
            return True
        print(f"[tor] NEWNYM response: {resp}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[tor] control connection failed: {e}", file=sys.stderr)
        return False


def check_tor_running():
    """Best-effort check that tor is available."""
    import shutil
    return shutil.which("tor") is not None


def main():
    parser = argparse.ArgumentParser(description="Auto Tor IP changer loop")
    parser.add_argument("--interval", type=int, default=3, help="Seconds between IP changes")
    parser.add_argument("--count", type=int, default=0, help="Number of IP changes (0 = infinite)")
    parser.add_argument("--method", choices=["command", "control"], default="command",
                        help="Reload method: system command or Tor control port")
    parser.add_argument("--tor-reload", default="service tor reload",
                        help="Command to reload Tor identity (method=command)")
    parser.add_argument("--control-host", default="127.0.0.1", help="Tor control host")
    parser.add_argument("--control-port", type=int, default=9051, help="Tor control port")
    parser.add_argument("--control-password", default=None, help="Tor control password")
    parser.add_argument("--socks-port", type=int, default=9050, help="Tor SOCKS port for IP check")
    parser.add_argument("--verbose", action="store_true", help="Print every change")
    args = parser.parse_args()

    if args.method == "command" and not check_tor_running():
        print("[!] Tor binary not found. Install tor first (e.g. sudo apt install tor).")

    proxy = {
        "http": f"socks5h://127.0.0.1:{args.socks_port}",
        "https": f"socks5h://127.0.0.1:{args.socks_port}",
    }

    print(f"[auto_tor] starting: interval={args.interval}s method={args.method}")
    print(f"[auto_tor] initial IP: {get_public_ip(proxy)}")

    n = 0
    try:
        while True:
            time.sleep(args.interval)
            ok = False
            if args.method == "command":
                ok = reload_tor_command(args.tor_reload)
            else:
                ok = newnym_via_control(args.control_host, args.control_port, args.control_password)
            n += 1
            ip = get_public_ip(proxy)
            if args.verbose or ok:
                print(f"[auto_tor] #{n} IP changed: {ip}")
            if args.count and n >= args.count:
                break
    except KeyboardInterrupt:
        print("\n[auto_tor] stopped.")


if __name__ == "__main__":
    main()
