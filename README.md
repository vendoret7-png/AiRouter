# AI Router — 9Router + Auto Tor IP Changer

Mixed runner that starts **9Router** (free AI router / token saver) in the background and keeps your outbound IP rotating through Tor at a fixed interval (e.g. every 3 seconds or every minute).

Based on:
- 9Router — https://github.com/decolua/9router (npm: `9router`)
- Auto_Tor_IP_changer — https://github.com/FDX100/Auto_Tor_IP_changer

## Quick start (Linux / macOS)

```bash
# 1. install deps
sudo apt install tor python3-pip   # Tor + Python
pip install -r requirements.txt    # requests[socks]

# 2. run mixed router + auto IP changer
python3 run.py --interval 3 --router-mode npm
```

## Quick start (Windows)

```powershell
# 1. install deps (needs Tor running as a Windows service / local process)
pip install -r requirements.txt

# 2. run
python run.py --interval 3 --router-mode npm --tor-reload "C:\\Program Files\\Tor\\tor.exe"
```

> On Windows the Tor reload mechanism is different from Linux. Provide the path to a running `tor.exe` with control port 9051 open, or use WSL.

## What happens

1. `run.py` launches 9Router (`npm install -g 9router && 9router` or via Docker) in the background.
2. It then starts the auto Tor IP changer loop which sends `NEWNYM` to the Tor control port (or `service tor reload`) every `--interval` seconds.
3. You get a fresh exit IP at that rate while 9Router keeps routing your AI requests.

## Options

```
python run.py -h

--interval SEC      Seconds between IP changes (default: 3)
--router-mode MODE  npm | docker | none (default: npm)
--router-port PORT  9Router dashboard port (default: 20128)
--tor-control PORT  Tor control port (default: 9051)
--tor-reload CMD    Command to reload Tor identity (default: service tor reload)
--verbose           Print every IP change
```

## Files

- `auto_tor.py` — auto Tor IP changer loop (cleaned version of FDX100's script)
- `run.py` — mixed runner (starts 9Router + auto_tor)
- `requirements.txt` — Python deps
- `start.sh` / `start.bat` — one-click helpers

## Disclaimer

Use responsibly. Rapid IP rotation may violate the terms of service of some providers and can look abusive. For legal/ethical use only.
