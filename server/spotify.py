"""Spotify Web API, called by the server on behalf of the phone (Authorization Code with PKCE, no client secret).

The sign-in runs once from the computer at http://127.0.0.1:<port>/spotify/login, because Spotify only accepts
http redirect URIs on the loopback address. The refresh token is kept in <data>/spotify.json.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

API = "https://api.spotify.com/v1"
ACCOUNTS = "https://accounts.spotify.com"
SCOPES = ("user-read-playback-state user-modify-playback-state user-read-currently-playing "
          "playlist-read-private playlist-read-collaborative")


class SpotifyError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Spotify:
    def __init__(self, data_dir: Path, redirect_uri: str, client_id: str = ""):
        self.file = Path(data_dir) / "spotify.json"
        self.redirect_uri = redirect_uri
        self.cfg = {}
        try:
            self.cfg = json.loads(self.file.read_text())
        except (OSError, ValueError):
            pass
        if client_id and not self.cfg.get("client_id"):
            self.cfg["client_id"] = client_id
        self._pkce: dict[str, str] = {}
        self.client = httpx.AsyncClient(timeout=10)

    def _save(self):
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.cfg, f)
        os.replace(tmp, self.file)

    @property
    def configured(self) -> bool:
        return bool(self.cfg.get("client_id"))

    @property
    def connected(self) -> bool:
        return bool(self.cfg.get("refresh_token"))

    def set_client_id(self, client_id: str):
        client_id = client_id.strip()
        if client_id != self.cfg.get("client_id"):
            self.cfg = {"client_id": client_id}
            self._save()

    def disconnect(self):
        self.cfg = {"client_id": self.cfg.get("client_id", "")}
        self._save()

    # ---------- sign-in ----------
    def login_url(self) -> str:
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(16)
        self._pkce = {state: verifier}
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        return f"{ACCOUNTS}/authorize?" + urlencode({
            "client_id": self.cfg["client_id"], "response_type": "code", "redirect_uri": self.redirect_uri,
            "scope": SCOPES, "state": state, "code_challenge_method": "S256", "code_challenge": challenge,
        })

    async def finish_login(self, code: str, state: str):
        verifier = self._pkce.pop(state, None)
        if not verifier:
            raise SpotifyError(400, "This sign-in link expired. Start again.")
        await self._token({"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
                           "client_id": self.cfg["client_id"], "code_verifier": verifier})

    async def _token(self, form: dict):
        r = await self.client.post(f"{ACCOUNTS}/api/token", data=form)
        if r.status_code != 200:
            if form.get("grant_type") == "refresh_token" and r.status_code == 400:
                self.disconnect()
            raise SpotifyError(502, f"Spotify sign-in failed ({r.status_code})")
        t = r.json()
        self.cfg["access_token"] = t["access_token"]
        self.cfg["expires"] = time.time() + int(t.get("expires_in", 3600)) - 60
        if t.get("refresh_token"):
            self.cfg["refresh_token"] = t["refresh_token"]
        self._save()

    async def _access(self) -> str:
        if not self.connected:
            raise SpotifyError(409, "Spotify is not connected")
        if time.time() >= self.cfg.get("expires", 0):
            await self._token({"grant_type": "refresh_token", "refresh_token": self.cfg["refresh_token"],
                               "client_id": self.cfg["client_id"]})
        return self.cfg["access_token"]

    # ---------- API ----------
    async def call(self, method: str, path: str, params=None, body=None):
        for attempt in (0, 1):
            token = await self._access()
            r = await self.client.request(method, API + path, params=params, json=body,
                                          headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 401 and attempt == 0:
                self.cfg["expires"] = 0
                continue
            break
        if r.status_code == 204 or not r.content:
            if r.status_code >= 400:
                raise SpotifyError(r.status_code, "Spotify refused the request")
            return None
        try:
            data = r.json()
        except ValueError:
            data = None
        if r.status_code >= 400:
            msg = (data or {}).get("error", {}).get("message") if isinstance(data, dict) else None
            if r.status_code == 404:
                msg = "No active device: pick one first"
            elif r.status_code == 403:
                msg = msg or "Spotify Premium is needed for this"
            raise SpotifyError(r.status_code, msg or f"Spotify error {r.status_code}")
        return data

    async def player(self) -> dict:
        data = await self.call("GET", "/me/player", params={"additional_types": "episode"})
        if not data:
            return {"active": False}
        item = data.get("item") or {}
        images = (item.get("album") or {}).get("images") or item.get("images") or (item.get("show") or {}).get("images") or []
        artists = ", ".join(a.get("name", "") for a in item.get("artists") or []) or (item.get("show") or {}).get("name", "")
        dev = data.get("device") or {}
        return {
            "active": True,
            "playing": bool(data.get("is_playing")),
            "title": item.get("name", ""),
            "artist": artists,
            "album": (item.get("album") or {}).get("name", ""),
            "image": images[0]["url"] if images else "",
            "thumb": images[-1]["url"] if images else "",
            "progress": data.get("progress_ms") or 0,
            "duration": item.get("duration_ms") or 0,
            "shuffle": bool(data.get("shuffle_state")),
            "repeat": data.get("repeat_state", "off"),
            "device": {"id": dev.get("id"), "name": dev.get("name", ""), "type": dev.get("type", ""),
                       "volume": dev.get("volume_percent"), "can_volume": dev.get("supports_volume", True)},
        }

    async def devices(self) -> list:
        data = await self.call("GET", "/me/player/devices") or {}
        return [{"id": d.get("id"), "name": d.get("name"), "type": d.get("type"), "active": d.get("is_active"),
                 "volume": d.get("volume_percent")} for d in data.get("devices", []) if d.get("id")]

    async def playlists(self) -> list:
        data = await self.call("GET", "/me/playlists", params={"limit": 50}) or {}
        out = []
        for p in data.get("items", []) or []:
            if not p:
                continue
            imgs = p.get("images") or []
            out.append({"uri": p.get("uri"), "name": p.get("name"), "image": imgs[-1]["url"] if imgs else "",
                        "owner": (p.get("owner") or {}).get("display_name", "")})
        return out

    async def command(self, action: str, body: dict):
        dev = body.get("device_id")
        q = {"device_id": dev} if dev else None
        if action == "play":
            payload = {"context_uri": body["context_uri"]} if body.get("context_uri") else None
            return await self.call("PUT", "/me/player/play", params=q, body=payload)
        if action == "pause":
            return await self.call("PUT", "/me/player/pause", params=q)
        if action == "next":
            return await self.call("POST", "/me/player/next", params=q)
        if action == "previous":
            return await self.call("POST", "/me/player/previous", params=q)
        if action == "volume":
            v = max(0, min(100, int(body.get("volume", 50))))
            return await self.call("PUT", "/me/player/volume", params={"volume_percent": v, **(q or {})})
        if action == "seek":
            return await self.call("PUT", "/me/player/seek", params={"position_ms": max(0, int(body.get("position", 0)))})
        if action == "shuffle":
            return await self.call("PUT", "/me/player/shuffle", params={"state": "true" if body.get("state") else "false"})
        if action == "repeat":
            st = body.get("state") if body.get("state") in ("off", "context", "track") else "off"
            return await self.call("PUT", "/me/player/repeat", params={"state": st})
        if action == "transfer":
            return await self.call("PUT", "/me/player", body={"device_ids": [body["device_id"]], "play": True})
        raise SpotifyError(400, "Unknown action")
