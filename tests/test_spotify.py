import asyncio
import json

import httpx

from server.spotify import Spotify


def test_player_and_refresh(tmp_path):
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path))
        if req.url.path == "/api/token":
            return httpx.Response(200, json={"access_token": "A2", "expires_in": 3600})
        if req.url.path == "/v1/me/player":
            assert req.headers["authorization"] == "Bearer A2"
            return httpx.Response(200, json={"is_playing": True, "progress_ms": 1000, "shuffle_state": False, "repeat_state": "off",
                                             "device": {"id": "d1", "name": "Kitchen", "type": "Speaker", "volume_percent": 40},
                                             "item": {"name": "Song", "duration_ms": 200000, "artists": [{"name": "A"}, {"name": "B"}],
                                                      "album": {"name": "Al", "images": [{"url": "big"}, {"url": "small"}]}}})
        if req.url.path == "/v1/me/player/pause":
            return httpx.Response(404, json={"error": {"message": "x"}})
        return httpx.Response(204)

    (tmp_path / "spotify.json").write_text(json.dumps({"client_id": "c", "refresh_token": "R", "access_token": "old", "expires": 0}))
    sp = Spotify(tmp_path, "http://127.0.0.1:8796/spotify/callback")
    sp.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def go():
        p = await sp.player()
        assert p["title"] == "Song" and p["artist"] == "A, B" and p["image"] == "big" and p["device"]["name"] == "Kitchen"
        try:
            await sp.command("pause", {})
            raise AssertionError
        except Exception as e:
            assert "No active device" in str(e)
        await sp.command("volume", {"volume": 150})
    asyncio.run(go())
    assert calls[0] == ("POST", "/api/token")
    assert json.loads((tmp_path / "spotify.json").read_text())["access_token"] == "A2"


def test_login_url_pkce(tmp_path):
    sp = Spotify(tmp_path, "http://127.0.0.1:8796/spotify/callback", client_id="abc")
    url = sp.login_url()
    assert "code_challenge_method=S256" in url and "client_id=abc" in url and "redirect_uri=http%3A%2F%2F127.0.0.1" in url
