from app.core.cors import parse_cors_origins


def test_wildcard_and_junk_dropped():
    got = parse_cors_origins(" * , https://dealshield.com.ng/ ,, https://*.x.com, ftp://a.b, https://a.b/path", "development")
    assert got == ["https://dealshield.com.ng"]


def test_production_keeps_https_only():
    got = parse_cors_origins("http://localhost:8080,https://dealshield.com.ng,https://www.dealshield.com.ng", "production")
    assert got == ["https://dealshield.com.ng", "https://www.dealshield.com.ng"]


def test_dedupe_and_dev_http_allowed():
    assert parse_cors_origins("http://localhost:8080,http://localhost:8080", "development") == ["http://localhost:8080"]


def test_app_cors_never_wildcard_with_credentials():
    from app.main import app
    from fastapi.middleware.cors import CORSMiddleware
    mw = [m for m in app.user_middleware if m.cls is CORSMiddleware]
    assert mw, "CORS middleware missing"
    opts = mw[0].kwargs if hasattr(mw[0], "kwargs") else mw[0].options
    assert opts["allow_credentials"] is True
    assert "*" not in opts["allow_origins"]
