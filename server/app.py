"""Remote: a LAN-only page for the phone with a clock, Claude Code sessions (allow / deny) and Spotify controls."""
from __future__ import annotations

import asyncio
import time
import hmac
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection, Request
from starlette.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                                 Response, StreamingResponse)
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .lyrics import Lyrics
from .phonekey import COOKIE as PHONE_COOKIE, PhoneKeys
from .quota import Quota, QuotaError
from .spotify import Spotify, SpotifyError
from .state import Hub, describe_tool
from .trainer import MAX_SAMPLE_S, SR as TRAIN_SR, Training
from .tls import ca_der
from .tts import Speaker, TTSError
from .voice import Voice, VoiceError
from .wakeword import Detector, Models as WakeModels, WakeWordError

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
STATIC = {"/": "index.html", "/app.js": "app.js", "/app.css": "app.css", "/setup": "setup.html",
          "/setup.js": "setup.js", "/icon.svg": "icon.svg", "/voice.js": "voice.js", "/wake-worklet.js": "wake-worklet.js"}
VENDOR = {"porcupine-web-4.0.1.min.js", "web-voice-processor-4.0.10.min.js", "porcupine_params-4.0.pv"}
# Porcupine (wake word) runs as WebAssembly in workers made from blob: URLs, and checks its AccessKey with Picovoice.
CSP = ("default-src 'self'; img-src 'self' data: https:; style-src 'self'; "
       "script-src 'self' 'wasm-unsafe-eval' blob:; worker-src 'self' blob:; "
       "connect-src 'self' https://rest.picovoice.ai; media-src 'self' blob:; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
MAX_PPN_BODY = 64 * 1024
MAX_OWW_BODY = 4 * 1024 * 1024
MAX_LISTENERS = 3          # phones streaming to the wake word listener at once
WAKE_COOLDOWN_S = 2.0


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


def create_app(data_dir: str | Path | None = None, public_url: str | None = None, port: int = 8796,
               secure_url: str | None = None) -> Starlette:
    data_dir = Path(data_dir or os.environ.get("REMOTE_DATA", ROOT / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    token = load_token(data_dir)
    public_url = (public_url or os.environ.get("REMOTE_URL") or f"http://127.0.0.1:{port}").rstrip("/")
    hub = Hub(data_dir)
    spotify = Spotify(data_dir, f"http://127.0.0.1:{port}/spotify/callback", os.environ.get("SPOTIFY_CLIENT_ID", ""))
    lyrics = Lyrics()
    quota = Quota(data_dir)
    voice = Voice(data_dir, before_run=quota.ensure_login)
    training = Training(data_dir, on_done=lambda model_id, phrase, result: voice.add_trained(model_id, phrase, result))
    phones = PhoneKeys(data_dir)
    wake_models = WakeModels()
    speaker = Speaker()
    listeners: set = set()
    secure_url = (secure_url or os.environ.get("REMOTE_SECURE_URL") or "").rstrip("/")

    hosts = {"127.0.0.1", "localhost", urlsplit(public_url).hostname}

    def good(value: str | None) -> bool:
        return bool(value) and hmac.compare_digest(value.encode(), token.encode())

    class Guard:
        """Pure ASGI middleware (BaseHTTPMiddleware would hide client disconnects from the long-poll)."""

        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "websocket":
                conn = HTTPConnection(scope)
                origin = conn.headers.get("origin")
                # same host checks as pages, and only our own page may open it (no cross-site WebSockets)
                if conn.url.hostname not in hosts or (origin and urlsplit(origin).hostname not in hosts) or not origin:
                    await receive()
                    return await send({"type": "websocket.close", "code": 1008})
                return await self.app(scope, receive, send)
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

    async def vendor(request: Request):
        name = request.path_params["name"]
        if name not in VENDOR:
            return PlainTextResponse("Not found", 404)
        kind = "application/octet-stream" if name.endswith(".pv") else "text/javascript"
        # versioned names: safe to keep for a year
        return FileResponse(WEB / "vendor" / name, media_type=kind,
                            headers={"Cache-Control": "public, max-age=31536000, immutable"})

    async def ca_cert(_):
        der = ca_der(data_dir)
        if der is None:
            return PlainTextResponse("HTTPS is off on this server", 404)
        return Response(der, media_type="application/x-x509-ca-cert",
                        headers={"Content-Disposition": 'attachment; filename="remote-ca.crt"',
                                 "Cache-Control": "no-store"})

    async def links(_):
        return JSONResponse({"public_url": public_url, "secure_url": secure_url})

    # ---------- voice ----------
    def v_error(e: VoiceError):
        return JSONResponse({"error": e.message}, e.status)

    async def v_config(request: Request):
        return JSONResponse({**voice.public(), "tts_languages": speaker.languages(), "linked": linked(request),
                             "secure": request.url.scheme == "https"})

    async def v_speak(request: Request):
        """{text, lang} -> audio/wav of Claude's answer, spoken by Piper on this computer."""
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError
            lang = str(body.get("lang", "en-US"))[:10]
            print(f"voice: speaking an answer ({lang})", flush=True)
            audio = await asyncio.to_thread(speaker.wav, str(body.get("text", ""))[:4000], lang)
        except TTSError as e:
            return JSONResponse({"error": str(e)}, 400)
        except ValueError:
            return JSONResponse({"error": "Bad request"}, 400)
        return Response(audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    async def v_set(request: Request):
        try:
            body = await request.json()
            return JSONResponse(voice.update(body if isinstance(body, dict) else {}))
        except VoiceError as e:
            return v_error(e)
        except ValueError:
            return JSONResponse({"error": "Bad request"}, 400)

    async def v_keyword_get(_):
        if not voice.ppn.exists():
            return PlainTextResponse("Not found", 404)
        return FileResponse(voice.ppn, media_type="application/octet-stream", headers={"Cache-Control": "no-cache"})

    async def v_keyword_put(request: Request):
        n = request.headers.get("content-length", "")
        if not n.isdigit() or int(n) > MAX_PPN_BODY:
            return JSONResponse({"error": "That file is too large for a wake word"}, 413)
        raw = await request.body()
        try:
            return JSONResponse(voice.save_keyword(raw[:MAX_PPN_BODY + 1], request.query_params.get("label", "")))
        except VoiceError as e:
            return v_error(e)

    async def v_oww_put(request: Request):
        n = request.headers.get("content-length", "")
        if not n.isdigit() or int(n) > MAX_OWW_BODY:
            return JSONResponse({"error": "That file is too large for a wake word model"}, 413)
        raw = await request.body()
        try:
            return JSONResponse(await asyncio.to_thread(voice.save_oww, raw[:MAX_OWW_BODY + 1],
                                                        request.query_params.get("label", "")))
        except VoiceError as e:
            return v_error(e)

    # ---------- training a new wake word (server/trainer.py) ----------
    async def train_status(_):
        return JSONResponse(training.status())

    async def train_start(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Bad request")
            return JSONResponse(await training.start(body.get("phrase", "")))
        except ValueError as e:
            return JSONResponse({"error": str(e) or "Bad request"}, 400)

    async def train_cancel(_):
        return JSONResponse(await training.cancel())

    async def train_sample(request: Request):
        """One recording of the wake word from the phone: 16 kHz 16-bit mono, as the wake word listener gets it."""
        n = request.headers.get("content-length", "")
        if not n.isdigit() or int(n) > MAX_SAMPLE_S * TRAIN_SR * 2:
            return JSONResponse({"error": "That recording is too long"}, 413)
        raw = await request.body()
        try:
            return JSONResponse({"samples": await asyncio.to_thread(training.add_sample, raw)})
        except ValueError as e:
            return JSONResponse({"error": str(e)}, 400)

    async def train_clear(_):
        try:
            return JSONResponse({"samples": training.clear_samples()})
        except ValueError as e:
            return JSONResponse({"error": str(e)}, 409)

    async def v_trained_delete(request: Request):
        try:
            body = await request.json()
            return JSONResponse(voice.remove_trained(body.get("id", "") if isinstance(body, dict) else ""))
        except VoiceError as e:
            return v_error(e)
        except ValueError:
            return JSONResponse({"error": "Bad request"}, 400)

    async def v_oww_delete(_):
        return JSONResponse(voice.remove_oww())

    async def wake_ws(ws: WebSocket):
        """The phone streams 16 kHz 16-bit mono audio here (binary messages); we answer {"wake": label} on a hit.
        Text "reset" clears the listener (sent after the phone has used the mic for a question)."""
        if len(listeners) >= MAX_LISTENERS:
            await ws.close(code=1013)
            return
        await ws.accept()
        listeners.add(ws)
        try:
            try:
                det = await asyncio.to_thread(Detector, wake_models, voice.wake_path())
            except WakeWordError as e:
                await ws.send_json({"error": str(e)})
                await ws.close()
                return
            label, threshold = voice.wake_label(), voice.threshold()
            await ws.send_json({"ready": True, "label": label, "threshold": threshold})
            quiet_until = 0.0
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("text") == "reset":
                    await asyncio.to_thread(det.reset)
                    continue
                data = msg.get("bytes")
                if not data or len(data) > 64_000 or len(data) % 2:
                    continue
                score = await asyncio.to_thread(det.feed, np.frombuffer(data, dtype="<i2"))
                now = time.monotonic()
                if score >= threshold and now >= quiet_until:
                    quiet_until = now + WAKE_COOLDOWN_S
                    await ws.send_json({"wake": label, "score": round(score, 3)})
        except WebSocketDisconnect:
            pass
        finally:
            listeners.discard(ws)

    async def v_keyword_delete(_):
        return JSONResponse(voice.remove_keyword())

    def linked(request: Request) -> bool:
        """A linked phone, on the HTTPS address (the key cookie is Secure, so it never travels over plain http)."""
        return request.url.scheme == "https" and phones.check(request.cookies.get(PHONE_COOKIE))

    async def v_ask(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError
            return JSONResponse(await voice.ask(body.get("text", ""), body.get("history"), mail=linked(request)))
        except VoiceError as e:
            return v_error(e)
        except ValueError:
            return JSONResponse({"error": "Bad request"}, 400)

    # ---------- phone link (email and calendar for voice) ----------
    async def phone_code(_):
        return JSONResponse(phones.new_code())

    async def phone_unlink_all(_):
        return JSONResponse({"removed": phones.revoke_all()})

    async def phone_link(request: Request):
        if request.url.scheme != "https":
            return JSONResponse({"error": "Open the secure (HTTPS) address on the phone first"}, 400)
        try:
            body = await request.json()
            code = str(body.get("code", ""))[:40]
        except (ValueError, AttributeError):
            return JSONResponse({"error": "Bad request"}, 400)
        key = phones.redeem(code, request.headers.get("user-agent", ""))
        if key is None:
            return JSONResponse({"error": "That code is wrong or has expired. Make a new one on the computer."}, 400)
        r = JSONResponse({"linked": True})
        r.set_cookie(PHONE_COOKIE, key, max_age=10 * 365 * 86400, path="/api", secure=True, httponly=True,
                     samesite="strict")
        return r

    async def phone_unlink(request: Request):
        phones.revoke(request.cookies.get(PHONE_COOKIE))
        r = JSONResponse({"linked": False})
        r.delete_cookie(PHONE_COOKIE, path="/api", secure=True, httponly=True, samesite="strict")
        return r

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
        # from the phone too: there the sign-in ends on an address that doesn't load, and /setup takes it (sp_finish)
        return RedirectResponse(spotify.login_url(), 303)

    async def sp_callback(request: Request):
        if request.query_params.get("error"):
            return RedirectResponse("/setup?spotify=cancelled", 303)
        try:
            await spotify.finish_login(request.query_params.get("code", ""), request.query_params.get("state", ""))
        except SpotifyError as e:
            return HTMLResponse(f"<p>{e.message}</p><p><a href='/setup'>Back</a></p>", 400)
        return RedirectResponse("/setup?spotify=ok", 303)

    async def sp_finish(request: Request):
        """{url}: the address the phone's browser landed on after the Spotify sign-in."""
        try:
            body = await request.json()
            await spotify.finish_from_url(str(body.get("url", "")))
        except SpotifyError as e:
            return JSONResponse({"error": e.message}, e.status if 400 <= e.status < 600 else 502)
        except ValueError:
            return JSONResponse({"error": "bad request"}, 400)
        return await sp_status(request)

    async def sp_player(_):
        if not spotify.connected:
            return JSONResponse({"connected": False})
        try:
            return JSONResponse({"connected": True, **(await spotify.player())})
        except SpotifyError as e:
            return sp_error(e)

    async def claude_quota(request: Request):
        return JSONResponse(await quota.get(force=request.query_params.get("refresh") == "1"))

    async def claude_login(_):
        return JSONResponse({"url": quota.login_url()})

    async def claude_finish(request: Request):
        try:
            body = await request.json()
            return JSONResponse(await quota.finish_login(str(body.get("code", ""))[:2000]))
        except QuotaError as e:
            return JSONResponse({"error": str(e)}, 400)
        except (ValueError, AttributeError):
            return JSONResponse({"error": "Bad request"}, 400)

    async def claude_logout(_):
        quota.logout()
        return JSONResponse({"configured": False})

    async def sp_lyrics(request: Request):
        q = request.query_params
        try:
            duration = int(q.get("duration") or 0)
        except ValueError:
            duration = 0
        return JSONResponse(await lyrics.find(q.get("title", "")[:300], q.get("artist", "")[:300],
                                              q.get("album", "")[:300], duration))

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
        Route("/vendor/{name}", vendor),
        Route("/ca.crt", ca_cert),
        Route("/api/links", links),
        Route("/api/voice/config", v_config),
        Route("/api/voice/config", v_set, methods=["POST"]),
        Route("/api/voice/keyword.ppn", v_keyword_get),
        Route("/api/voice/keyword", v_keyword_put, methods=["POST"]),
        Route("/api/voice/keyword/delete", v_keyword_delete, methods=["POST"]),
        Route("/api/voice/wakeword", v_oww_put, methods=["POST"]),
        Route("/api/voice/wakeword/delete", v_oww_delete, methods=["POST"]),
        Route("/api/voice/wakeword/trained/delete", v_trained_delete, methods=["POST"]),
        Route("/api/wake/train", train_status),
        Route("/api/wake/train", train_start, methods=["POST"]),
        Route("/api/wake/train/cancel", train_cancel, methods=["POST"]),
        Route("/api/wake/train/sample", train_sample, methods=["POST"]),
        Route("/api/wake/train/samples/clear", train_clear, methods=["POST"]),
        WebSocketRoute("/api/wake/ws", wake_ws),
        Route("/api/voice/ask", v_ask, methods=["POST"]),
        Route("/api/voice/speak", v_speak, methods=["POST"]),
        Route("/api/state", state),
        Route("/api/stream", stream),
        Route("/api/requests/{rid}", answer, methods=["POST"]),
        Route("/api/settings", settings, methods=["POST"]),
        Route("/api/sessions/{sid}/dismiss", dismiss, methods=["POST"]),
        Route("/hook/event", hook_event, methods=["POST"]),
        Route("/hook/permission", hook_permission, methods=["POST"]),
        Route("/hook/phone-code", phone_code, methods=["POST"]),
        Route("/hook/phone-unlink-all", phone_unlink_all, methods=["POST"]),
        Route("/api/phone/link", phone_link, methods=["POST"]),
        Route("/api/phone/unlink", phone_unlink, methods=["POST"]),
        Route("/api/spotify/status", sp_status),
        Route("/api/spotify/config", sp_config, methods=["POST"]),
        Route("/api/spotify/disconnect", sp_disconnect, methods=["POST"]),
        Route("/api/spotify/finish", sp_finish, methods=["POST"]),
        Route("/api/spotify/player", sp_player),
        Route("/api/spotify/devices", sp_devices),
        Route("/api/lyrics", sp_lyrics),
        Route("/api/claude/quota", claude_quota),
        Route("/api/claude/login", claude_login, methods=["POST"]),
        Route("/api/claude/finish", claude_finish, methods=["POST"]),
        Route("/api/claude/logout", claude_logout, methods=["POST"]),
        Route("/api/spotify/playlists", sp_playlists),
        Route("/api/spotify/{action}", sp_command, methods=["POST"]),
        Route("/spotify/login", sp_login),
        Route("/spotify/callback", sp_callback),
    ]
    app = Starlette(routes=routes, middleware=[Middleware(Guard)])
    app.state.hub = hub
    app.state.token = token
    app.state.spotify = spotify
    app.state.lyrics = lyrics
    app.state.quota = quota
    app.state.voice = voice
    app.state.training = training
    app.state.phones = phones
    return app
