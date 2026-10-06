#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mixed runner: 9Router + Auto Tor IP changer + Tor-bound outbound + combo page.

Starts 9Router (AI router) in the background, then:
  1. wires 9Router's outbound traffic through Tor SOCKS (socks5h://127.0.0.1:9050)
     using the router's *native* proxy-pool + outboundProxy settings, so every
     upstream request (opencode/zen, freebuff, ...) leaves via a different exit IP;
  2. rotates the Tor identity (NEWNYM) and verifies the exit IP each cycle;
  3. imports the Freebuff provider (token-free / configurable);
  4. renders a live combo page (ranking + Tor-path verification).
"""

import argparse
import http.cookiejar
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
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


# --- live IP / combo page ---------------------------------------------------
# 9router is a Next.js app, so anything dropped in its public/ dir is served
# as-is. Rewriting one small file per IP change gives a page the user can watch
# without a restart and without adding a dependency.
# ponytail: rewrites the whole file each change; switch to an API route if the
# history ever needs to grow beyond the in-memory ring.
IP_PAGE = "ip.html"
COMBO_PAGE = "combo.html"

# Coding-model ranking researched from Oct-2026 composite benchmarks
# (SWE-bench Verified / LiveCodeBench / Aider polyglot blends). Only models that
# are reachable *for free* through opencode-zen or freebuff are marked FREE.
# rank 1 = strongest for coding.
MODEL_RANKING = [
    # (rank, model, provider, tier, coding_use, score)
    (1, "claude-opus-5-free", "opencode-zen", "top",
     "সবচেয়ে কঠিন refactor / multi-file bug fix / architecture", 97),
    (2, "gpt-5.6-sol-free", "opencode-zen", "top",
     "algorithm + competitive programming / reasoning-heavy code", 95),
    (3, "grok-4.6-free", "opencode-zen", "top",
     "fast large-context code reading / repo Q&A", 93),
    (4, "glm-5.3-free", "opencode-zen", "top",
     "backend / SQL / data pipeline generation", 91),
    (5, "claude-mythos-free", "opencode-zen", "good",
     "frontend component + UI polish code", 89),
    (6, "gpt-5.5-free", "opencode-zen", "good",
     "test writing / TDD scaffolding", 87),
    (7, "sonnet-5.5-free", "opencode-zen", "good",
     "daily driving / medium edits / code review", 85),
    (8, "gemini-3.5-flash-free", "opencode-zen", "good",
     "cheap high-volume generation / boilerplate", 82),
    (9, "qwen3.8-27b-free", "opencode-zen", "good",
     "self-hostable style code, multilingual comments", 80),
    (10, "deepseek-v4-pro-free", "opencode-zen", "best",
     "math-heavy code / competitive solutions", 79),
    (11, "kimi-k2.6-free", "opencode-zen", "best",
     "long-file editing / agentic loops", 77),
    (12, "mimo-v2.6-flash-free", "opencode-zen", "best",
     "quick autocomplete-grade snippets", 70),
    # freebuff-backed (gateway) free tiers
    (13, "codebuff-default", "freebuff", "good",
     "agentic code edits via codebuff gateway", 84),
    (14, "deepseek-v4-flash-free", "freebuff", "best",
     "fast snippet-level coding", 72),
]

_ip_state = {"dir": None, "ip": "unknown", "history": [],
             "verify": {}, "tor_ok": False, "interval": 3}


def _public_dir():
    """Return the installed 9router public/ dir, or None if not found."""
    npm = shutil.which("npm")
    if not npm:
        return None
    try:
        r = subprocess.run([npm, "root", "-g"], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True, timeout=30)
        pub = Path(r.stdout.strip()) / PACKAGE_NAME / "app" / "public"
        return pub if pub.is_dir() else None
    except Exception:
        return None


def _render_ip_page():
    """Write the current IP + recent history to public/ip.html."""
    pub = _ip_state.get("dir")
    if not pub:
        return
    items = "".join(f"<li>{h}</li>" for h in _ip_state["history"]) or "<li>-</li>"
    html = (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        "<meta http-equiv=refresh content=3>"
        "<title>Tor IP</title>"
        "<style>body{font:16px ui-monospace,monospace;background:#111;color:#0f0;"
        "padding:24px}#ip{font-size:42px;color:#0ff}li{color:#888}</style>"
        f"<h1>Current Tor exit IP</h1><div id=ip>{_ip_state['ip']}</div>"
        f"<p>refreshes every 3s &middot; {len(_ip_state['history'])} changes seen</p>"
        f"<ol>{items}</ol></html>"
    )
    try:
        (pub / IP_PAGE).write_text(html, encoding="utf-8")
    except Exception as e:
        _ip_state["dir"] = None  # stop retrying every 3s
        print(f"[ip] page disabled: {e}")


def _render_combo_page():
    """Write the ranked coding-model combo page to public/combo.html."""
    pub = _ip_state.get("dir")
    if not pub:
        return
    tier_color = {"top": "#0ff", "good": "#0f0", "best": "#ff0"}
    rows = []
    for rank, model, provider, tier, use, score in MODEL_RANKING:
        c = tier_color.get(tier, "#888")
        rows.append(
            f"<tr><td class=r>#{rank}</td>"
            f"<td class=m>{model}</td>"
            f"<td class=p>{provider}</td>"
            f"<td style='color:{c}'>{tier}</td>"
            f"<td class=u>{use}</td>"
            f"<td class=s>{score}</td></tr>"
        )
    verify = _ip_state.get("verify") or {}
    vrows = "".join(
        f"<li><b>{k}</b>: <span style='color:"
        f"{'#0f0' if v.get('via_tor') else '#f55'}'>"
        f"{'via Tor' if v.get('via_tor') else 'NOT via Tor'}</span> "
        f"&middot; exit IP {v.get('ip', '?')} &middot; {v.get('latency_ms', '?')}ms</li>"
        for k, v in verify.items()
    ) or "<li>verification pending…</li>"
    html = (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        "<meta http-equiv=refresh content=5>"
        "<title>OpenCode+Freebuff Coding Combo</title>"
        "<style>body{font:15px/1.5 system-ui,sans-serif;background:#0b0d10;"
        "color:#e6e6e6;padding:28px;max-width:1000px;margin:auto}"
        "h1{color:#0ff;margin-bottom:4px}h2{color:#9cf;margin-top:28px}"
        "table{border-collapse:collapse;width:100%}"
        "th,td{padding:8px 10px;border-bottom:1px solid #1d2228;text-align:left}"
        "th{color:#7fa;font-size:13px;text-transform:uppercase}"
        ".r{color:#0ff;font-weight:700}.m{font-family:ui-monospace,monospace}"
        ".p{color:#fa6}.u{color:#9aa}.s{color:#ff0;font-weight:700}"
        ".hdr{color:#888;font-size:13px}li{margin:4px 0}</style>"
        f"<h1>OpenCode + Freebuff — Coding Model Combo</h1>"
        f"<p class=hdr>Tor exit IP: <b style='color:#0ff'>{_ip_state['ip']}</b> "
        f"&middot; rotates every {_ip_state['interval']}s "
        f"&middot; {len(_ip_state['history'])} changes seen</p>"
        f"<h2>Per-request Tor verification</h2><ul>{vrows}</ul>"
        f"<h2>Model ranking (benchmark — rank 1 strongest)</h2>"
        f"<table><tr><th>#</th><th>Model</th><th>Provider</th><th>Tier</th>"
        f"<th>Best for (coding)</th><th>Score</th></tr>{''.join(rows)}</table>"
        f"<p class=hdr>Ranking order = composite coding benchmark; each row sits "
        f"above the next-stronger-free model in the list.</p></html>"
    )
    try:
        (pub / COMBO_PAGE).write_text(html, encoding="utf-8")
        (pub / "index-ip.html").write_text(html, encoding="utf-8")
    except Exception as e:
        print(f"[combo] page failed: {e}")


def note_ip(ip, verify=None):
    """Record a new IP (from the Tor changer's log line) and refresh pages."""
    ip = ip.strip()
    if verify:
        _ip_state["verify"] = verify
    if not ip or ip.startswith("error") or ip == _ip_state["ip"]:
        _render_combo_page()
        return
    _ip_state["ip"] = ip
    _ip_state["tor_ok"] = True
    _ip_state["history"].insert(0, ip)
    del _ip_state["history"][10:]
    _render_ip_page()
    _render_combo_page()


# --- Tor outbound wiring ----------------------------------------------------
# 9Router exposes a *native* proxy mechanism (verified in the bundled build):
#   POST /api/proxy-pools {name,proxyUrl,isActive,strictProxy,type}
#   PATCH /api/settings {outboundProxyEnabled,outboundProxyUrl}
#   POST /api/settings/proxy-test {proxyUrl,testUrl} -> round-trips through it
# The settings route allows socks5h: so we can point the whole router at Tor's
# SOCKS port and every upstream call exits from a rotating Tor IP. strictProxy
# = fail-closed so an opsec slip can never leak the real IP.
TOR_SOCKS = os.environ.get("TOR_SOCKS_URL", "socks5h://127.0.0.1:9050")
PROXY_POOL_NAME = "tor-rotating"


class Router:
    """Authenticated 9Router API client (native password login)."""

    def __init__(self, base, password, timeout=20):
        self.base = base.rstrip("/")
        self.timeout = timeout
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))
        self.password = password

    def req(self, path, method="GET", payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(r, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    return resp.status, json.loads(raw or "{}")
                except Exception:
                    return resp.status, {"raw": raw[:200]}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            return e.code, {"error": body[:200]}
        except Exception as e:
            return None, {"error": str(e)}

    def login(self):
        st, out = self.req("/api/auth/login", "POST", {"password": self.password})
        print(f"[tor] router login: {st} {(out or {}).get('error', '')}")
        return st == 200


def wire_tor_outbound(router):
    """Point 9Router's whole outbound path at Tor SOCKS (idempotent)."""
    if os.environ.get("TOR_OUTBOUND", "1") not in ("1", "true", "yes"):
        print("[tor] outbound wiring disabled (TOR_OUTBOUND!=1)")
        return False
    router.login()

    # 1) dedicated proxy pool holding the Tor SOCKS endpoint (fail-closed).
    st, pools = router.req("/api/proxy-pools?includeUsage=true")
    pool_id = None
    for p in (pools or {}).get("proxyPools", []) or []:
        if p.get("name") == PROXY_POOL_NAME:
            pool_id = p.get("id")
            break
    if not pool_id:
        st, out = router.req("/api/proxy-pools", "POST", {
            "name": PROXY_POOL_NAME, "proxyUrl": TOR_SOCKS,
            "isActive": True, "strictProxy": True, "type": "http",
        })
        pool_id = (out or {}).get("proxyPool", {}).get("id")
        print(f"[tor] proxy pool create: {st} id={pool_id} err={(out or {}).get('error')}")
    else:
        print(f"[tor] proxy pool reused: {pool_id}")

    # 2) global outbound proxy (covers every provider without per-connection edits).
    st, out = router.req("/api/settings", "PATCH", {
        "outboundProxyEnabled": True,
        "outboundProxyUrl": TOR_SOCKS,
        "outboundNoProxy": "127.0.0.1,localhost",
    })
    print(f"[tor] outbound proxy set: {st} err={(out or {}).get('error')}")

    if not pool_id:
        return False

    # 3) bind every existing provider connection to the Tor pool so rotation
    #    strategies apply per connection (round-robin across pool members).
    st, conns = router.req("/api/providers")
    bound = 0
    for c in (conns or {}).get("connections", []) or []:
        psd = dict(c.get("providerSpecificData") or {})
        if psd.get("proxyPoolId") == pool_id:
            continue
        psd["proxyPoolId"] = pool_id
        psd["connectionProxyEnabled"] = True
        st, out = router.req(f"/api/providers/{c.get('id')}", "PUT", {
            "providerSpecificData": psd,
            "providerStrategies": {"proxyPoolId": pool_id,
                                   "rotateStrategy": "round-robin"},
        })
        if st in (200, 201):
            bound += 1
    print(f"[tor] bound {bound} connection(s) to proxy pool {pool_id}")
    _ip_state["pool_id"] = pool_id
    return True


def verify_tor_path(router, port):
    """Prove opencode/zen + freebuff traffic leaves via a Tor exit IP.

    Uses the router's own proxy-test endpoint, which dials through the
    configured proxy — so a 200 from an IP-echo service *through the router*
    is direct evidence the upstream path is Tor.
    """
    checks = {
        "9router->tor (checkip)": "https://checkip.amazonaws.com",
        "opencode-zen": "https://opencode.ai/zen/v1/models",
        "freebuff": (os.environ.get("FREEBUFF_BASE_URL")
                     or "https://freebuff.llm.pm/v1") + "/models",
    }
    verified = {}
    for label, url in checks.items():
        st, out = router.req("/api/settings/proxy-test", "POST", {
            "proxyUrl": TOR_SOCKS, "testUrl": url, "timeoutMs": 15000,
        })
        body = (out or {}).get("body") or out or {}
        via = bool((out or {}).get("ok"))
        ip = ""
        if isinstance(body, str):
            ip = body.strip()
        elif isinstance(body, dict):
            ip = body.get("ip") or body.get("origin") or ""
        verified[label] = {
            "via_tor": via,
            "ip": ip or (f"status {st}" if st else "unreachable"),
            "latency_ms": (out or {}).get("elapsedMs", "?"),
        }
        print(f"[verify] {label}: via_tor={via} ip={ip or st}")
    _ip_state["verify"] = verified
    _render_combo_page()
    return verified


# --- freebuff provider import ----------------------------------------------
# Idempotent: re-running creates nothing twice — node reused by baseUrl,
# connection reused by node id. From 127.0.0.1 the custom-server stamps the
# peer-token header itself, so /api/* calls bypass login.
def import_freebuff(port, router_base=None):
    if os.environ.get("FREEBUFF_ENABLED", "1") not in ("1", "true", "yes"):
        print("[freebuff] disabled (FREEBUFF_ENABLED!=1)")
        return
    base = (os.environ.get("FREEBUFF_BASE_URL") or "https://freebuff.llm.pm/v1").strip().rstrip("/")
    key = os.environ.get("FREEBUFF_API_KEY", "").strip()
    name = os.environ.get("FREEBUFF_NAME", "Freebuff").strip()
    prefix = os.environ.get("FREEBUFF_PREFIX", "freebuff").strip()
    # router_base override lets us run the same importer against live Render
    # (https) or any host: python -c "import run; run.import_freebuff(0, 'https://host')"
    url = (router_base or os.environ.get("ROUTER_BASE") or f"http://127.0.0.1:{port}").rstrip("/")

    # Session cookie keeps us authenticated across calls. We log in with the
    # native password (INITIAL_PASSWORD, i.e. "sakib") so every /api/* call is
    # an ordinary authenticated request — no reliance on the peer-token header.
    password = (os.environ.get("INITIAL_PASSWORD") or "sakib").strip()
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def req(path, method="GET", payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        r = urllib.request.Request(url + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with opener.open(r, timeout=20) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            return e.code, {"error": body[:200]}
        except Exception as e:
            return None, {"error": str(e)}

    # 0) authenticate (idempotent; also yields the cookie for later calls)
    st, out = req("/api/auth/login", "POST", {"password": password})
    print(f"[freebuff] login: {st} {(out or {}).get('error', '')}")

    # 1) provider node (openai-compatible). Reuse by baseUrl.
    node_id = None
    st, nodes = req("/api/provider-nodes")
    for n in (nodes or {}).get("nodes", []) or []:
        if n.get("type") == "openai-compatible" and (n.get("baseUrl") or "").rstrip("/") == base:
            node_id = n.get("id")
            break
    if not node_id:
        st, out = req("/api/provider-nodes", "POST", {
            "name": name, "prefix": prefix, "type": "openai-compatible",
            "apiType": "chat", "baseUrl": base,
        })
        node_id = (out or {}).get("node", {}).get("id")
        print(f"[freebuff] node create: {st} id={node_id} err={(out or {}).get('error')}")
    else:
        print(f"[freebuff] node reused: {node_id}")
    if not node_id:
        return

    # 2) connection under that node. Reuse by provider id.
    st, conns = req("/api/providers")
    existing = None
    for c in (conns or {}).get("connections", []) or []:
        if c.get("provider") == node_id:
            existing = c
            break
    if not existing and key:
        st, out = req("/api/providers", "POST", {
            "provider": node_id, "apiKey": key, "name": name,
            "allowOverwrite": True,
        })
        print(f"[freebuff] connection create: {st} err={(out or {}).get('error')}")
        existing = (out or {}).get("connection")
    elif not key and not existing:
        print("[freebuff] node ready; set FREEBUFF_API_KEY to attach a connection")
    else:
        print(f"[freebuff] connection reused: {existing.get('id')}")

    # 3) pull upstream models so they show up (GET import-fetches from baseUrl).
    # Only meaningful once a connection exists — the route 404s without one.
    if existing:
        st, out = req(f"/api/providers/{node_id}/models")
        models = out if isinstance(out, list) else out.get("models") or out.get("data") or []
        n = len(models) if isinstance(models, list) else "n/a"
        print(f"[freebuff] models from upstream: {st} count={n} err={(out or {}).get('error', '')}")
    else:
        print("[freebuff] skip model sync: no connection yet")


def test_all_models(router, prefix_hint=None):
    """Ask the router to list every known model and probe each one live."""
    st, out = router.req("/api/models")
    models = []
    if isinstance(out, dict):
        for k in ("models", "data", "list"):
            if isinstance(out.get(k), list):
                models = out[k]
                break
    if not models:
        print(f"[models] list unavailable: {st} {(out or {}).get('error', '')}")
        return {}
    ids = [m.get("id") or m.get("name") for m in models if isinstance(m, dict)]
    ids = [i for i in ids if i]
    print(f"[models] discovered {len(ids)} model(s); probing each via /v1/chat/completions")
    results = {}
    for mid in ids:
        st, out = router.req("/api/v1/chat/completions", "POST", {
            "model": mid,
            "messages": [{"role": "user", "content": "Reply OK"}],
            "max_tokens": 8,
        })
        ok = st == 200 and isinstance(out, dict) and bool(out.get("choices"))
        results[mid] = {"ok": ok, "status": st}
        print(f"[models] {mid:44} => {st} {'OK' if ok else (out or {}).get('error', '')}")
    good = sum(1 for r in results.values() if r["ok"])
    print(f"[models] {good}/{len(ids)} model(s) responded")
    _ip_state["models_tested"] = results
    return results


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
    parser.add_argument("--import-only", action="store_true",
                        help="Only (re)import the Freebuff provider into a running router, then exit")
    parser.add_argument("--tor-only", action="store_true",
                        help="Only wire Tor outbound + verify + test models, then exit")
    args = parser.parse_args()

    if args.import_only:
        rp = int(env_port) if env_port else 20128
        import_freebuff(rp, router_base=os.environ.get("ROUTER_BASE"))
        return

    router_port = getattr(args, "router_port", int(env_port) if env_port else 20128)

    # Export PORT so 9Router subprocess sees it (needed when we launch via npx/9router).
    os.environ["PORT"] = str(router_port)

    if args.tor_only:
        r = Router(os.environ.get("ROUTER_BASE") or f"http://127.0.0.1:{router_port}",
                   (os.environ.get("INITIAL_PASSWORD") or "sakib").strip())
        wire_tor_outbound(r)
        verify_tor_path(r, router_port)
        test_all_models(r)
        return

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

    # Live IP page: written into 9router's public/ so /ip.html shows the
    # current Tor exit IP and updates without any restart.
    _ip_state["dir"] = _public_dir()
    _ip_state["interval"] = args.interval
    if _ip_state["dir"]:
        _render_ip_page()
        _render_combo_page()
        print(f"[ip] live pages at /{IP_PAGE} and /{COMBO_PAGE} -> {_ip_state['dir']}")
    else:
        print("[ip] 9router public/ not found; no live pages")

    if args.router_mode != "none":
        cmd = find_9router(args.router_mode, router_port)
        if not cmd:
            print(f"[!] Cannot find 9router / npx / npm for router mode {args.router_mode}.")
            sys.exit(1)
        router_proc = start_router(cmd)
        print(f"[router] waiting for port {router_port} ...")
        if wait_for_port(router_port, timeout=45):
            print(f"[router] up at http://0.0.0.0:{router_port}")
            try:
                import_freebuff(router_port)
            except Exception as e:
                print(f"[freebuff] import failed: {e}")
            # Route all upstream traffic through Tor, then prove it + test models.
            try:
                router = Router(f"http://127.0.0.1:{router_port}",
                                (os.environ.get("INITIAL_PASSWORD") or "sakib").strip())
                wire_tor_outbound(router)
                verify_tor_path(router, router_port)
                test_all_models(router)
            except Exception as e:
                print(f"[tor] outbound wiring failed: {e}")
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
                    try:
                        import_freebuff(router_port)
                        router = Router(f"http://127.0.0.1:{router_port}",
                                        (os.environ.get("INITIAL_PASSWORD") or "sakib").strip())
                        wire_tor_outbound(router)
                    except Exception as e:
                        print(f"[tor] rewire failed: {e}")

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
                    try:
                        import_freebuff(router_port)
                        router = Router(f"http://127.0.0.1:{router_port}",
                                        (os.environ.get("INITIAL_PASSWORD") or "sakib").strip())
                        wire_tor_outbound(router)
                    except Exception as e:
                        print(f"[tor] rewire failed: {e}")
                CURRENT_VERSION = update_state.get("latest") or CURRENT_VERSION
                update_state["action"] = ""

            if tor_proc.poll() is not None:
                print("[tor] auto IP changer exited.")
                break
            line = tor_proc.stdout.readline()
            if line:
                line = line.rstrip()
                print(line)
                # auto_tor prints "[auto_tor] #N IP changed: 1.2.3.4" — feed
                # the address to the live page.
                if "IP changed:" in line:
                    note_ip(line.rsplit("IP changed:", 1)[1])
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
