"""Song lyrics from LRCLIB (https://lrclib.net), a free open lyrics database with no API key.

Spotify's Web API has no lyrics. The server asks LRCLIB with the song's title, artist, album and length only, and
keeps answers in memory. Synced lyrics (LRC) come back as timed lines, so the page can follow the song.
"""
from __future__ import annotations

import re
from collections import OrderedDict

import httpx

BASE = "https://lrclib.net/api"
LRC_LINE = re.compile(r"^((?:\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\])+)(.*)$")
LRC_TIME = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")


def parse_lrc(text: str) -> list[dict]:
    """'[01:02.30] words' -> [{'t': 62300, 'text': 'words'}], sorted; one tag line can hold several times."""
    out = []
    for raw in (text or "").splitlines():
        m = LRC_LINE.match(raw.strip())
        if not m:
            continue
        words = m.group(2).strip()
        for mm, ss, frac in LRC_TIME.findall(m.group(1)):
            ms = int(frac.ljust(3, "0")[:3]) if frac else 0
            out.append({"t": (int(mm) * 60 + int(ss)) * 1000 + ms, "text": words})
    out.sort(key=lambda x: x["t"])
    return out


def shape(rec: dict) -> dict:
    if rec.get("instrumental"):
        return {"found": True, "instrumental": True, "synced": False, "lines": []}
    synced = parse_lrc(rec.get("syncedLyrics") or "")
    if synced:
        return {"found": True, "instrumental": False, "synced": True, "lines": synced}
    plain = [{"t": None, "text": line.strip()} for line in (rec.get("plainLyrics") or "").splitlines()]
    if any(x["text"] for x in plain):
        return {"found": True, "instrumental": False, "synced": False, "lines": plain}
    return {"found": False}


class Lyrics:
    def __init__(self, client: httpx.AsyncClient | None = None, size: int = 200):
        self.client = client or httpx.AsyncClient(timeout=8, headers={"User-Agent": "Remote/1.0 (home lyrics display)"})
        self.cache: OrderedDict[tuple, dict] = OrderedDict()
        self.size = size

    async def find(self, title: str, artist: str, album: str = "", duration_ms: int = 0) -> dict:
        title, artist = title.strip(), artist.strip()
        if not title:
            return {"found": False}
        key = (title.lower(), artist.lower(), album.lower(), round((duration_ms or 0) / 2000))
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        result = await self._lookup(title, artist.split(",")[0].strip(), album, duration_ms)
        if result.get("error"):          # try again next time
            return result
        self.cache[key] = result
        while len(self.cache) > self.size:
            self.cache.popitem(last=False)
        return result

    async def _lookup(self, title, artist, album, duration_ms) -> dict:
        try:
            params = {"track_name": title, "artist_name": artist}
            if album:
                params["album_name"] = album
            if duration_ms:
                params["duration"] = round(duration_ms / 1000)
            r = await self.client.get(f"{BASE}/get", params=params)
            if r.status_code == 200:
                got = shape(r.json())
                if got.get("found"):
                    return got
            # no exact match: search by title and artist, prefer synced lyrics close to the song's length
            r = await self.client.get(f"{BASE}/search", params={"track_name": title, "artist_name": artist})
            if r.status_code != 200:
                return {"found": False}
            hits = [h for h in r.json() or [] if isinstance(h, dict)]
            secs = (duration_ms or 0) / 1000

            def rank(h):
                near = abs((h.get("duration") or 0) - secs) if secs else 0
                return (0 if h.get("syncedLyrics") else 1, near > 4, near)

            for h in sorted(hits, key=rank)[:5]:
                got = shape(h)
                if got.get("found"):
                    return got
            return {"found": False}
        except (httpx.HTTPError, ValueError):
            return {"found": False, "error": "Couldn't reach the lyrics service"}
