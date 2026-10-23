"""N02: one place to decide the real client IP.

Proxy headers are attacker-controlled unless a trusted proxy set them. They are
only honoured when the operator sets TRUSTED_PROXY=1, which is correct ONLY if
the app is reachable exclusively through Cloudflare/nginx (port 8000 firewalled
from the internet).

Order when TRUSTED_PROXY=1:
  1. CF-Connecting-IP  (Cloudflare overwrites any client-sent value)
  2. X-Forwarded-For   entry number TRUSTED_PROXY_HOPS counted from the RIGHT
                       (default 1 = the address our nearest proxy saw; the
                       left-most entries can be forged by the client)
  3. request.client.host
"""
import ipaddress
import os

from fastapi import Request


def _valid_ip(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def trusted_proxy() -> bool:
    return os.getenv("TRUSTED_PROXY", "0").strip().lower() in ("1", "true", "yes")


def get_client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if not trusted_proxy():
        return peer
    cf = _valid_ip(request.headers.get("cf-connecting-ip", ""))
    if cf:
        return cf
    xff = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if xff:
        try:
            hops = max(1, int(os.getenv("TRUSTED_PROXY_HOPS", "1")))
        except ValueError:
            hops = 1
        candidate = _valid_ip(xff[-hops] if len(xff) >= hops else xff[0])
        if candidate:
            return candidate
    return peer
