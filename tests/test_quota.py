import asyncio
import json
import time

import httpx

from server.quota import Quota, normalize

ROWS = {"limits": [
    {"kind": "session", "group": "session", "percent": 42.0, "resets_at": "2026-11-18T14:00:00Z", "severity": "normal", "is_active": True},
    {"kind": "weekly_all", "group": "weekly", "percent": 18.5, "resets_at": "2026-11-21T08:00:00+00:00", "severity": "normal", "is_active": False},
    {"kind": "weekly_scoped", "group": "weekly", "percent": 91, "resets_at": None, "severity": "critical", "is_active": False,
     "scope": {"model": {"display_name": "Opus"}}},
], "extra_usage": {"is_enabled": True, "monthly_limit": 5000, "used_credits": 1234, "utilization": 24.7, "currency": "usd"}}


def test_normalize_rows():
    n = normalize(ROWS)
    a, b, c = n["limits"]
    assert (a["label"], a["short"], a["percent"], a["active"]) == ("Current session", "Session", 42.0, True)
    from datetime import datetime, timezone
    assert a["resets_at"] == datetime(2026, 11, 18, 14, tzinfo=timezone.utc).timestamp()
    assert (b["label"], b["short"]) == ("Current week (all models)", "Week")
    assert (c["label"], c["short"], c["severity"], c["resets_at"]) == ("Current week (Opus)", "Opus", "critical", None)
    assert n["extra"] == {"used": 12.34, "limit": 50.0, "percent": 24.7, "currency": "USD"}


def test_normalize_named_windows():
    n = normalize({"five_hour": {"utilization": 80, "resets_at": "2026-11-18T14:00:00Z"},
                   "seven_day": {"utilization": 10, "resets_at": None}, "seven_day_opus": None,
                   "extra_usage": {"is_enabled": False}})
    assert [(l["short"], l["percent"], l["severity"]) for l in n["limits"]] == [("Session", 80.0, "warning"), ("Week", 10.0, "normal")]
    assert n["extra"] is None


def creds(tmp_path, expires_in_s):
    d = tmp_path / "claude"
    d.mkdir()
    (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "OLD", "refreshToken": "R1", "expiresAt": int((time.time() + expires_in_s) * 1000),
        "subscriptionType": "pro", "scopes": ["user:inference", "user:profile"]}}))
    return d / ".credentials.json"


def run(tmp_path, handler, **kw):
    q = Quota(tmp_path, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return q, asyncio.run(q.get(**kw))


def test_not_configured(tmp_path):
    _, got = run(tmp_path, lambda r: httpx.Response(500))
    assert got == {"configured": False}


def test_fresh_token_reads_usage_and_hides_tokens(tmp_path):
    creds(tmp_path, 3600)
    seen = []

    def handler(req):
        seen.append((req.method, req.url.host, req.url.path, req.headers.get("authorization"), req.headers.get("anthropic-beta")))
        return httpx.Response(200, json=ROWS)
    _, got = run(tmp_path, handler)
    assert seen == [("GET", "api.anthropic.com", "/api/oauth/usage", "Bearer OLD", "oauth-2025-04-20")]
    assert got["configured"] and got["plan"] == "pro" and len(got["limits"]) == 3
    assert "OLD" not in json.dumps(got) and "R1" not in json.dumps(got)


def test_expired_token_is_renewed_and_saved(tmp_path):
    f = creds(tmp_path, -10)
    seen = []

    def handler(req):
        seen.append(req.url.path)
        if req.url.path == "/v1/oauth/token":
            body = json.loads(req.content)
            assert body == {"grant_type": "refresh_token", "refresh_token": "R1", "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e"}
            return httpx.Response(200, json={"access_token": "NEW", "refresh_token": "R2", "expires_in": 28800})
        assert req.headers["authorization"] == "Bearer NEW"
        return httpx.Response(200, json=ROWS)
    _, got = run(tmp_path, handler)
    assert seen == ["/v1/oauth/token", "/api/oauth/usage"] and got["limits"]
    o = json.loads(f.read_text())["claudeAiOauth"]
    assert (o["accessToken"], o["refreshToken"], o["subscriptionType"]) == ("NEW", "R2", "pro")
    assert o["expiresAt"] > time.time() * 1000 + 28000_000
    assert (f.stat().st_mode & 0o777) == 0o600


def test_refused_renewal_asks_to_sign_in(tmp_path):
    creds(tmp_path, -10)
    _, got = run(tmp_path, lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    assert got["login_needed"] and "sign in again" in got["error"]


def test_401_triggers_one_renewal(tmp_path):
    creds(tmp_path, 3600)
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/v1/oauth/token":
            return httpx.Response(200, json={"access_token": "NEW", "expires_in": 3600})
        return httpx.Response(200, json=ROWS) if req.headers["authorization"] == "Bearer NEW" else httpx.Response(401)
    _, got = run(tmp_path, handler)
    assert calls == ["/api/oauth/usage", "/v1/oauth/token", "/api/oauth/usage"] and got["limits"]


def test_cache(tmp_path):
    creds(tmp_path, 3600)
    n = []

    def handler(req):
        n.append(1)
        return httpx.Response(200, json=ROWS)
    q = Quota(tmp_path, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def go():
        await q.get(); await q.get(); await q.get(force=True)    # force within 20 s still uses the cache
    asyncio.run(go())
    assert len(n) == 1


def test_endpoint_not_configured(server):
    assert server.req("/api/claude/quota") == (200, {"configured": False})


def test_phone_sign_in(tmp_path):
    from urllib.parse import parse_qs, urlsplit
    seen = []

    def handler(req):
        seen.append(req.url.path)
        if req.url.path == "/v1/oauth/token":
            b = json.loads(req.content)
            assert b["grant_type"] == "authorization_code" and b["code"] == "THECODE"
            assert b["redirect_uri"] == "https://platform.claude.com/oauth/code/callback"
            assert b["code_verifier"] == q._pkce["verifier"] and b["state"] == q._pkce["state"]
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600, "scope": "user:profile"})
        assert req.headers["authorization"] == "Bearer AT"
        return httpx.Response(200, json=ROWS)
    q = Quota(tmp_path, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    url = q.login_url()
    u = urlsplit(url); p = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert u.netloc == "claude.com" and u.path == "/cai/oauth/authorize"
    assert p["scope"] == "user:profile" and p["code_challenge_method"] == "S256" and p["code"] == "true"
    assert p["client_id"] == "9d1c250a-e61b-44d9-88ed-5944d1962f5e" and p["state"] == q._pkce["state"]
    got = asyncio.run(q.finish_login(f"THECODE#{p['state']}"))
    assert got["configured"] and len(got["limits"]) == 3 and seen == ["/v1/oauth/token", "/api/oauth/usage"]
    saved = json.loads((tmp_path / "claude" / ".credentials.json").read_text())["claudeAiOauth"]
    assert (saved["accessToken"], saved["refreshToken"], saved["scopes"]) == ("AT", "RT", ["user:profile"])
    assert ((tmp_path / "claude" / ".credentials.json").stat().st_mode & 0o777) == 0o600
    q.logout()
    assert asyncio.run(q.get(force=True)) == {"configured": False}


def test_phone_sign_in_rejects_wrong_state_and_bad_code(tmp_path):
    import pytest
    from server.quota import QuotaError
    q = Quota(tmp_path, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))))
    with pytest.raises(QuotaError, match="expired"):
        asyncio.run(q.finish_login("x#y"))
    q.login_url()
    with pytest.raises(QuotaError, match="doesn't belong"):
        asyncio.run(q.finish_login("CODE#wrongstate"))
    with pytest.raises(QuotaError, match="didn't accept"):
        asyncio.run(q.finish_login("CODE#" + q._pkce["state"]))
    assert not (tmp_path / "claude" / ".credentials.json").exists()


def test_login_endpoints(server):
    code, d = server.req("/api/claude/login", {})
    assert code == 200 and d["url"].startswith("https://claude.com/cai/oauth/authorize?")
    assert server.req("/api/claude/finish", {"code": "nope#bad"})[0] == 400
    assert server.req("/api/claude/logout", {}) == (200, {"configured": False})
