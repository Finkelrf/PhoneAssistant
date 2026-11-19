"""Claude plan quotas (what /usage shows), read with the Remote server's OWN Claude login.

The login lives in <data>/claude/.credentials.json. It is made once from the phone (quota sheet -> Sign in to Claude,
then paste the code Claude shows), and asks only for the user:profile scope, so it can read usage but not use Claude.
(`CLAUDE_CONFIG_DIR=<data>/claude claude auth login` on the computer works too, with Claude Code's full scopes.)
It is a separate sign-in that only this server uses, so the server renews it itself. That never touches the desktop
app's or the command line's login. The tokens never leave the server: the page only gets percentages and reset times.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx


class QuotaError(Exception):
    pass

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"     # Claude Code's public OAuth client
AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
MANUAL_REDIRECT_URL = "https://platform.claude.com/oauth/code/callback"  # Claude's page that shows the code to paste
SCOPES = "user:profile"                                # read profile and usage only: this login can't use Claude
CACHE_S = 180                                         # ask the usage service at most every 3 minutes
ERROR_CACHE_S = 60

NAMED = {  # older response shape: named windows
    "five_hour": ("session", "Current session"),
    "seven_day": ("weekly_all", "Current week (all models)"),
    "seven_day_sonnet": ("weekly_scoped", "Current week (Sonnet)"),
    "seven_day_opus": ("weekly_scoped", "Current week (Opus)"),
}


def _epoch(value):
    """ISO 8601 or epoch seconds/ms -> epoch seconds (or None)."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def _severity(pct):
    return "critical" if pct >= 90 else "warning" if pct >= 75 else "normal"


def normalize(body: dict) -> dict:
    """The usage service's reply -> {'limits': [...], 'extra': {...} | None} for the page."""
    limits = []
    rows = body.get("limits")
    if isinstance(rows, list):
        for r in rows:
            if not isinstance(r, dict) or r.get("percent") is None:
                continue
            kind = str(r.get("kind") or "")
            scope = r.get("scope") or {}
            scoped = ((scope.get("model") or {}).get("display_name")
                      or (scope.get("surface") or {}).get("display_name") or "")
            if kind == "session":
                label = "Current session"
            elif kind == "weekly_all":
                label = "Current week (all models)"
            elif scoped:
                label = f"Current week ({scoped})" if r.get("group") == "weekly" or kind.startswith("weekly") else scoped
            else:
                label = (r.get("label") or kind.replace("_", " ")).capitalize()
            pct = float(r["percent"])
            limits.append({"kind": kind, "label": label, "short": "Session" if kind == "session" else
                           ("Week" if kind == "weekly_all" else scoped or label),
                           "percent": round(pct, 1), "resets_at": _epoch(r.get("resets_at")),
                           "severity": r.get("severity") or _severity(pct), "active": bool(r.get("is_active"))})
    else:
        for key, (kind, label) in NAMED.items():
            w = body.get(key)
            if not isinstance(w, dict) or w.get("utilization") is None:
                continue
            pct = float(w["utilization"])          # already 0-100 in the usage service's reply
            short = {"five_hour": "Session", "seven_day": "Week", "seven_day_sonnet": "Sonnet", "seven_day_opus": "Opus"}[key]
            limits.append({"kind": kind, "label": label, "short": short, "percent": round(pct, 1),
                           "resets_at": _epoch(w.get("resets_at")), "severity": _severity(pct), "active": False})
    extra = None
    eu = body.get("extra_usage")
    if isinstance(eu, dict) and eu.get("is_enabled"):
        cur = (eu.get("currency") or "USD").upper()
        div = 100.0  # minor units
        extra = {"used": (eu.get("used_credits") or 0) / div, "limit": (eu.get("monthly_limit") or 0) / div or None,
                 "percent": eu.get("utilization"), "currency": cur}
    return {"limits": limits, "extra": extra}


class Quota:
    def __init__(self, data_dir: Path, client: httpx.AsyncClient | None = None):
        self.file = Path(os.environ.get("REMOTE_CLAUDE_CREDS") or Path(data_dir) / "claude" / ".credentials.json")
        self.client = client or httpx.AsyncClient(timeout=15, headers={"User-Agent": "remote-quota/1.0"})
        self._cached: dict | None = None
        self._at = 0.0
        self._lock = asyncio.Lock()

    # ---------- login file ----------
    def _read(self) -> dict | None:
        try:
            raw = json.loads(self.file.read_text())
        except (OSError, ValueError):
            return None
        o = raw.get("claudeAiOauth")
        return raw if isinstance(o, dict) and o.get("accessToken") else None

    def _write(self, raw: dict):
        tmp = self.file.with_name(self.file.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(raw, f)
        os.replace(tmp, self.file)

    async def _refresh(self, raw: dict) -> bool:
        o = raw["claudeAiOauth"]
        if not o.get("refreshToken"):
            return False
        r = await self.client.post(TOKEN_URL, json={"grant_type": "refresh_token", "refresh_token": o["refreshToken"],
                                                    "client_id": CLIENT_ID})
        if r.status_code != 200:
            return False
        t = r.json()
        o["accessToken"] = t["access_token"]
        if t.get("refresh_token"):
            o["refreshToken"] = t["refresh_token"]
        o["expiresAt"] = int((time.time() + int(t.get("expires_in", 3600))) * 1000)
        if t.get("scope"):
            o["scopes"] = t["scope"].split()
        self._write(raw)
        return True

    # ---------- sign-in from the phone (OAuth with PKCE, the "paste the code" flow) ----------
    def login_url(self) -> str:
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(24)
        self._pkce = {"state": state, "verifier": verifier, "at": time.time()}
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        return AUTHORIZE_URL + "?" + urlencode({
            "code": "true", "client_id": CLIENT_ID, "response_type": "code", "redirect_uri": MANUAL_REDIRECT_URL,
            "scope": SCOPES, "code_challenge": challenge, "code_challenge_method": "S256", "state": state})

    async def finish_login(self, pasted: str) -> dict:
        """pasted is what Claude's page shows after signing in: 'code#state' (or just the code)."""
        p = getattr(self, "_pkce", None)
        if not p or time.time() - p["at"] > 900:
            raise QuotaError("This sign-in expired: tap Sign in to Claude again.")
        pasted = (pasted or "").strip()
        code, _, state = pasted.partition("#")
        if not code or (state and not secrets.compare_digest(state, p["state"])):
            raise QuotaError("That code doesn't belong to this sign-in: tap Sign in to Claude again.")
        r = await self.client.post(TOKEN_URL, json={
            "grant_type": "authorization_code", "code": code, "redirect_uri": MANUAL_REDIRECT_URL,
            "client_id": CLIENT_ID, "code_verifier": p["verifier"], "state": p["state"]})
        if r.status_code != 200:
            raise QuotaError("Claude didn't accept that code. Copy the whole code and try again."
                             if r.status_code in (400, 401) else f"Claude's sign-in answered {r.status_code}")
        t = r.json()
        self._pkce = None
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._write({"claudeAiOauth": {
            "accessToken": t["access_token"], "refreshToken": t.get("refresh_token", ""),
            "expiresAt": int((time.time() + int(t.get("expires_in", 3600))) * 1000),
            "scopes": (t.get("scope") or SCOPES).split(), "subscriptionType": "",
        }})
        self._cached, self._at = None, 0
        return await self.get()

    def logout(self):
        try:
            self.file.unlink()
        except FileNotFoundError:
            pass
        self._cached, self._at = None, 0

    # ---------- usage ----------
    async def get(self, force: bool = False) -> dict:
        async with self._lock:
            age = time.time() - self._at
            if self._cached is not None and not force and age < (ERROR_CACHE_S if self._cached.get("error") else CACHE_S):
                return self._cached
            if force and self._cached is not None and age < 20:     # tapping refresh can't hammer the service
                return self._cached
            self._cached = await self._fetch()
            self._at = time.time()
            return self._cached

    async def ensure_login(self) -> bool:
        """Renew the login here if it's close to expiring, before Claude Code (voice answers) uses the same file.

        Only one renewer at a time: the refresh token changes on each renewal, so Claude Code renewing it at the same
        moment as this server would sign one of them out. With a fresh token, Claude Code has no reason to renew.
        """
        async with self._lock:
            raw = self._read()
            if raw is None:
                return False
            o = raw["claudeAiOauth"]
            if time.time() * 1000 < (o.get("expiresAt") or 0) - 600_000:
                return True
            try:
                return await self._refresh(raw)
            except httpx.HTTPError:
                return False

    async def _fetch(self) -> dict:
        raw = self._read()
        if raw is None:
            return {"configured": False}
        o = raw["claudeAiOauth"]
        plan = o.get("subscriptionType") or ""
        try:
            if time.time() * 1000 >= (o.get("expiresAt") or 0) - 60_000 and not await self._refresh(raw):
                return {"configured": True, "plan": plan, "login_needed": True,
                        "error": "The Remote server's Claude sign-in has ended: sign in again (see the README)"}
            for attempt in (0, 1):
                r = await self.client.get(USAGE_URL, headers={"Authorization": f"Bearer {o['accessToken']}",
                                                              "anthropic-beta": "oauth-2025-04-20"})
                if r.status_code == 401 and attempt == 0 and await self._refresh(raw):
                    continue
                break
        except httpx.HTTPError:
            return {"configured": True, "plan": plan, "error": "Couldn't reach Claude"}
        if r.status_code == 401:
            return {"configured": True, "plan": plan, "login_needed": True,
                    "error": "The Remote server's Claude sign-in was refused: sign in again (see the README)"}
        if r.status_code != 200:
            return {"configured": True, "plan": plan, "error": f"Claude's usage service answered {r.status_code}"}
        try:
            body = r.json()
        except ValueError:
            return {"configured": True, "plan": plan, "error": "Unexpected answer from Claude"}
        return {"configured": True, "plan": plan, "updated": time.time(), **normalize(body if isinstance(body, dict) else {})}
