"""Security middleware for SafePay backend.
Implements: security headers, HTTPS redirect, rate limiting, input sanitization.
"""
import os
import time
from collections import defaultdict, deque
from fastapi import Request, Response, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware


# ── Security Headers Middleware ──

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds security headers to every response (checklist item #18)."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        # HSTS — only meaningful over HTTPS, but we set it anyway
        # behind a reverse proxy it will be HTTPS (checklist item #19)
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'"
        return response


# ── HTTPS Redirect Middleware ──

class HTTPSRedirectMiddleware(BaseHTTPMiddleware):
    """Force HTTPS — redirect all HTTP requests (checklist item #19).
    Only active when FORCE_HTTPS env var is set (for production behind proxy)."""

    async def dispatch(self, request: Request, call_next):
        force_https = os.getenv("FORCE_HTTPS", "false").lower() == "true"
        if force_https:
            # Check if request is already HTTPS (via proxy header or direct)
            forwarded_proto = request.headers.get("x-forwarded-proto", "")
            if forwarded_proto != "https" and request.url.scheme != "https":
                https_url = request.url.replace(scheme="https")
                return Response(
                    status_code=301,
                    headers={"Location": str(https_url)},
                )
        return await call_next(request)


# ── Rate Limiting Middleware (checklist item #11) ──

class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory rate limiter.
    - Login/register: 5 requests per 60 seconds per IP
    - All other endpoints: 60 requests per 60 seconds per IP
    """

    def __init__(self, app):
        super().__init__(app)
        self.login_attempts: dict[str, deque] = defaultdict(deque)
        self.general_attempts: dict[str, deque] = defaultdict(deque)
        self.login_limit = 5
        self.general_limit = 60
        self.window = 60  # seconds

    async def dispatch(self, request: Request, call_next):
        client_ip = request.client.host if request.client else "unknown"
        path = request.url.path
        now = time.time()

        # Stricter rate limit for auth endpoints
        if path in ("/auth/login", "/auth/register"):
            attempts = self.login_attempts[client_ip]
            while attempts and attempts[0] < now - self.window:
                attempts.popleft()
            if len(attempts) >= self.login_limit:
                return Response(
                    content='{"detail":"Too many login attempts. Please wait 60 seconds."}',
                    status_code=429,
                    media_type="application/json",
                )
            attempts.append(now)
        else:
            attempts = self.general_attempts[client_ip]
            while attempts and attempts[0] < now - self.window:
                attempts.popleft()
            if len(attempts) >= self.general_limit:
                return Response(
                    content='{"detail":"Rate limit exceeded. Please slow down."}',
                    status_code=429,
                    media_type="application/json",
                )
            attempts.append(now)

        # Cleanup old IPs occasionally
        if len(self.general_attempts) > 1000:
            cutoff = now - self.window
            self.general_attempts = {ip: dq for ip, dq in self.general_attempts.items() if dq and dq[-1] >= cutoff}
            self.login_attempts = {ip: dq for ip, dq in self.login_attempts.items() if dq and dq[-1] >= cutoff}

        return await call_next(request)


# ── Input Sanitizer (checklist item #14, #15) ──

def sanitize_text(text: str, max_length: int = 2000) -> str:
    """Sanitize user input to prevent XSS and injection.
    - Strips HTML/script tags
    - Escapes special characters
    - Rejects obvious injection test payloads
    - Truncates to max_length
    """
    if not text:
        return ""
    import html
    import re
    # Remove any HTML tags
    cleaned = re.sub(r'<[^>]*>', '', text)
    # Escape HTML entities
    cleaned = html.escape(cleaned)
    # Truncate
    cleaned = cleaned[:max_length].strip()
    # Reject obvious XSS/injection test patterns
    test_patterns = [
        r'alert\s*\(',
        r'<script',
        r'javascript:',
        r'onerror\s*=',
        r'onload\s*=',
        r'onclick\s*=',
        r'document\.cookie',
        r'eval\s*\(',
    ]
    for pattern in test_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            raise ValueError("Invalid input detected")
    return cleaned


def validate_password_strength(password: str) -> tuple[bool, str]:
    """Validate password meets minimum security requirements.
    Returns (is_valid, error_message).
    """
    if len(password) < 8:
        return False, "Password must be at least 8 characters"
    if len(password) > 128:
        return False, "Password too long (max 128 characters)"
    if not any(c.isalpha() for c in password):
        return False, "Password must contain at least one letter"
    if not any(c.isdigit() for c in password):
        return False, "Password must contain at least one number"
    return True, ""
