#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate
# N02: proxy headers. Safe default = trust only the local machine (127.0.0.1).
# Behind nginx on the same host this is correct. Set FORWARDED_ALLOW_IPS to the
# proxy address(es) if nginx runs elsewhere. Never use "*" unless port 8000 is
# firewalled so only the proxy can reach it.
# The app-level helper (app/core/client_ip.py) additionally reads
# CF-Connecting-IP / X-Forwarded-For only when TRUSTED_PROXY=1.
FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-127.0.0.1}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
python -m uvicorn app.main:app --host "$HOST" --port "$PORT" \
  --proxy-headers --forwarded-allow-ips "$FORWARDED_ALLOW_IPS"
