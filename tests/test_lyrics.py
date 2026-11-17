import asyncio

import httpx

from server.lyrics import Lyrics, parse_lrc

LRC = "[ar: Someone]\n[00:01.50] First line\n[00:04.00][00:20.25] Twice\n[00:07] \n[01:02.3] Last"


def test_parse_lrc():
    got = parse_lrc(LRC)
    assert [x["t"] for x in got] == [1500, 4000, 7000, 20250, 62300]
    assert [x["text"] for x in got] == ["First line", "Twice", "", "Twice", "Last"]


def make(handler):
    return Lyrics(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_exact_match_and_cache():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        assert req.url.params["track_name"] == "Song" and req.url.params["artist_name"] == "A"   # first artist only
        assert req.url.params["duration"] == "200"
        return httpx.Response(200, json={"syncedLyrics": LRC, "plainLyrics": "x", "instrumental": False})

    ly = make(handler)
    d = asyncio.run(ly.find("Song", "A, B", "Album", 200400))
    assert d["found"] and d["synced"] and d["lines"][0] == {"t": 1500, "text": "First line"}
    asyncio.run(ly.find("Song", "A, B", "Album", 200400))
    assert calls == ["/api/get"]


def test_search_fallback_prefers_synced_and_close_length():
    def handler(req):
        if req.url.path == "/api/get":
            return httpx.Response(404, json={"code": 404})
        return httpx.Response(200, json=[
            {"duration": 300, "syncedLyrics": "[00:01.00] wrong length"},
            {"duration": 201, "plainLyrics": "plain only"},
            {"duration": 199, "syncedLyrics": "[00:01.00] right one"},
        ])
    d = asyncio.run(make(handler).find("Song", "A", "", 200000))
    assert d["synced"] and d["lines"][0]["text"] == "right one"


def test_plain_instrumental_and_missing():
    d = asyncio.run(make(lambda r: httpx.Response(200, json={"plainLyrics": "one\n\ntwo"})).find("S", "A"))
    assert d["found"] and not d["synced"] and [x["text"] for x in d["lines"]] == ["one", "", "two"]
    d = asyncio.run(make(lambda r: httpx.Response(200, json={"instrumental": True})).find("S", "A"))
    assert d["instrumental"]

    def none(req):
        return httpx.Response(404) if req.url.path == "/api/get" else httpx.Response(200, json=[])
    assert asyncio.run(make(none).find("S", "A")) == {"found": False}


def test_network_error_is_not_cached():
    n = []

    def boom(req):
        n.append(1)
        raise httpx.ConnectError("down")
    ly = make(boom)
    assert asyncio.run(ly.find("S", "A"))["error"]
    asyncio.run(ly.find("S", "A"))
    assert len(n) == 2 and not ly.cache
