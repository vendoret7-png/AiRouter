#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
python3 -m pip install -q -r requirements.txt
exec python3 run.py "$@"
