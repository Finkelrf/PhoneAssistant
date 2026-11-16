"""Remote: a LAN-only page for the phone with a clock, Claude Code sessions (allow / deny) and Spotify controls."""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                                 Response, StreamingResponse)
from starlette.routing import Route

from .spotify import Spotify, SpotifyError
from .state import Hub, describe_tool

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
STATIC = {"/": "index.html", "/app.js": "app.js", "/app.css": "app.css", "/setup": "setup.html",
          "/setup.js": "setup.js", "/icon.svg": "icon.svg"}
CSP = ("default-src 'self'; img-src 'self' data: https:; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def load_token(data_dir: Path) -> str:
    f = data_dir / "token"
    try:
        tok = f.read_text().strip()
        if len(tok) >= 20:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(tok + "\n")
    return tok


def create_app(data_dir: str | Path | None = None, public_url: str | None = None, port: int = 8796) -> Starlette:
    data_dir = Path(data_dir or os.environ.get("REMOTE_DATA", ROOT / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    token = load_token(data_dir)
    public_url = (public_url or os.environ.get("REMOTE_URL") or f"http://127.0.0.1:{port}").rstrip("/")
    hub = Hub(data_dir)
    spotify = Spotify(data_dir, f"http://127.0.0.1:{port}/spotify/callback", os.environ.get("SPOTIFY_CLIENT_ID", ""))

    hosts = {"127.0.0.1", "localhost", urlsplit(public_url).hostname}

    def good(value: str | None) -> bool:
        return bool(value) and hmac.compare_digest(value.encode(), token.encode())

    class Guard:
        """Pure ASGI middleware (BaseHTTPMiddleware would hide client disconnects from the long-poll)."""

        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self.app(scope, receive, send)
            request = Request(scope)
            path = scope["path"]
            deny = None
            if request.url.hostname not in hosts:
                # only our own addresses: a web page can't reach us through DNS rebinding
                deny = PlainTextResponse("Unknown host", 421)
            elif path == "/health":
                pass
            elif path.startswith("/hook/"):
                if not good(request.headers.get("x-remote-token")):
                    deny = JSONResponse({"error": "bad token"}, 401)
            elif request.method == "POST" and request.headers.get("x-remote") != "1":
                deny = JSONResponse({"error": "missing header"}, 403)

            async def send_with_headers(message):
                if message["type"] == "http.response.start":
                    headers = MutableHeaders(scope=message)
                    headers.setdefault("Content-Security-Policy", CSP)
                    headers["X-Content-Type-Options"] = "nosniff"
                    headers["Referrer-Policy"] = "no-referrer"
                    headers.setdefault("Cache-Control", "no-cache")
                await send(message)

            if deny is not None:
                return await deny(scope, receive, send_with_headers)
            await self.app(scope, receive, send_with_headers)

    # ---------- pages ----------
    async def static(request: Request):
        name = STATIC[request.url.path]
        return FileResponse(WEB / name, headers={"Cache-Control": "no-cache"})

    async def phone_qr(_):
        import io
        import segno
        buf = io.BytesIO()
        segno.make(public_url + "/", error="m").save(buf, kind="svg", scale=6, border=2)
        return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})

    async def health(_):
        return JSONResponse({"ok": True})

    # ---------- Claude ----------
    async def state(_):
        return JSONResponse(hub.snapshot())

    async def stream(request: Request):
        q = hub.subscribe()

        async def gen():
            try:
                yield "retry: 2000\n\n"
                yield f"data: {json.dumps(hub.snapshot())}\n\n"
                while True:
                    try:
                        await asyncio.wait_for(q.get(), 15)
                        yield f"data: {json.dumps(hub.snapshot())}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def answer(request: Request):
        body = await request.json()
        decision = body.get("decision")
        if decision not in ("allow", "deny"):
            return JSONResponse({"error": "decision must be allow or deny"}, 400)
        if not hub.resolve(request.path_params["rid"], decision):
            return JSONResponse({"error": "That request is no longer waiting"}, 404)
        return JSONResponse({"ok": True})

    async def settings(request: Request):
        body = await request.json()
        if "away" in body:
            hub.settings["away"] = bool(body["away"])
            if not hub.settings["away"]:
                for rid in list(hub.pending):          # hand open requests back to the computer
                    hub.resolve(rid, None, notify=False)
        if "wait" in body:
            hub.settings["wait"] = max(15, min(3600, int(body["wait"])))
        hub.changed()
        return JSONResponse(hub.settings)

    async def dismiss(request: Request):
        sid = request.path_params["sid"]
        if hub.sessions.pop(sid, None) is not None:
            hub.changed()
        return JSONResponse({"ok": True})

    async def hook_event(request: Request):
        try:
            ev = await request.json()
        except ValueError:
            return JSONResponse({"error": "bad json"}, 400)
        if isinstance(ev, dict):
            hub.event(ev)
        return JSONResponse({"ok": True})

    async def hook_permission(request: Request):
        try:
            ev = await request.json()
        except ValueError:
            return JSONResponse({"decision": None})
        if not isinstance(ev, dict):
            return JSONResponse({"decision": None})
        if not hub.settings.get("away"):
            # At the desk: the computer shows its own prompt; the phone just lets you know.
            s = hub._session(ev)
            title, _ = describe_tool(ev.get("tool_name", ""), ev.get("tool_input", {}))
            s["state"] = "waiting"
            s["activity"] = f"Asks: {title} (answer at the computer)"
            hub.ring("attention", s["id"])
            hub.changed()
            return JSONResponse({"decision": None})
        rid = hub.add_request(ev)
        fut = hub.futures[rid]
        wait = float(hub.settings.get("wait", 600))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        decision = None
        try:
            while True:
                left = deadline - loop.time()
                if left <= 0:
                    break
                try:
                    decision = await asyncio.wait_for(asyncio.shield(fut), min(1.0, left))
                    break
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
        finally:
            if rid in hub.pending:          # timed out, or Claude Code gave up on the hook
                hub.resolve(rid, None, notify=False)
                s = hub.sessions.get(ev.get("session_id", ""))
                if s:
                    s["state"] = "waiting"
                    s["activity"] = "Answer at the computer"
                hub.changed()
        msg = "Denied from the phone" if decision == "deny" else ""
        return JSONResponse({"decision": decision, "message": msg})

    # ---------- Spotify ----------
    def sp_error(e: SpotifyError):
        return JSONResponse({"error": e.message}, e.status if 400 <= e.status < 600 else 502)

    async def sp_status(_):
        return JSONResponse({"configured": spotify.configured, "connected": spotify.connected,
                             "client_id": spotify.cfg.get("client_id", ""), "redirect_uri": spotify.redirect_uri,
                             "phone_url": public_url + "/", "public_url": public_url})

    async def sp_config(request: Request):
        body = await request.json()
        cid = str(body.get("client_id", "")).strip()
        if not cid.isalnum() or len(cid) > 64:
            return JSONResponse({"error": "That doesn't look like a Spotify client ID"}, 400)
        spotify.set_client_id(cid)
        return await sp_status(request)

    async def sp_disconnect(request: Request):
        spotify.disconnect()
        return await sp_status(request)

    async def sp_login(request: Request):
        if not spotify.configured:
            return RedirectResponse("/setup", 303)
        if request.url.hostname != "127.0.0.1":
            return HTMLResponse(
                "<p>Open this from the computer at "
                f"<a href='http://127.0.0.1:{port}/setup'>http://127.0.0.1:{port}/setup</a>: "
                "Spotify only sends the sign-in back to that address.</p>", 400)
        return RedirectResponse(spotify.login_url(), 303)

    async def sp_callback(request: Request):
        if request.query_params.get("error"):
            return RedirectResponse("/setup?spotify=cancelled", 303)
        try:
            await spotify.finish_login(request.query_params.get("code", ""), request.query_params.get("state", ""))
        except SpotifyError as e:
            return HTMLResponse(f"<p>{e.message}</p><p><a href='/setup'>Back</a></p>", 400)
        return RedirectResponse("/setup?spotify=ok", 303)

    async def sp_player(_):
        if not spotify.connected:
            return JSONResponse({"connected": False})
        try:
            return JSONResponse({"connected": True, **(await spotify.player())})
        except SpotifyError as e:
            return sp_error(e)

    async def sp_devices(_):
        try:
            return JSONResponse({"devices": await spotify.devices()})
        except SpotifyError as e:
            return sp_error(e)

    async def sp_playlists(_):
        try:
            return JSONResponse({"playlists": await spotify.playlists()})
        except SpotifyError as e:
            return sp_error(e)

    async def sp_command(request: Request):
        try:
            body = await request.json()
            await spotify.command(request.path_params["action"], body if isinstance(body, dict) else {})
            return JSONResponse({"ok": True})
        except SpotifyError as e:
            return sp_error(e)
        except (KeyError, ValueError, TypeError):
            return JSONResponse({"error": "Bad request"}, 400)

    routes = [Route(p, static) for p in STATIC] + [
        Route("/health", health),
        Route("/api/phone-qr.svg", phone_qr),
        Route("/api/state", state),
        Route("/api/stream", stream),
        Route("/api/requests/{rid}", answer, methods=["POST"]),
        Route("/api/settings", settings, methods=["POST"]),
        Route("/api/sessions/{sid}/dismiss", dismiss, methods=["POST"]),
        Route("/hook/event", hook_event, methods=["POST"]),
        Route("/hook/permission", hook_permission, methods=["POST"]),
        Route("/api/spotify/status", sp_status),
        Route("/api/spotify/config", sp_config, methods=["POST"]),
        Route("/api/spotify/disconnect", sp_disconnect, methods=["POST"]),
        Route("/api/spotify/player", sp_player),
        Route("/api/spotify/devices", sp_devices),
        Route("/api/spotify/playlists", sp_playlists),
        Route("/api/spotify/{action}", sp_command, methods=["POST"]),
        Route("/spotify/login", sp_login),
        Route("/spotify/callback", sp_callback),
    ]
    app = Starlette(routes=routes, middleware=[Middleware(Guard)])
    app.state.hub = hub
    app.state.token = token
    app.state.spotify = spotify
    return app
