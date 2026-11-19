"""Spoken answers made on the server with Piper."""
import io
import json
import urllib.request
import wave

import pytest

from server.tts import Speaker, TTSError


@pytest.fixture(scope="module")
def speaker():
    return Speaker()


def test_all_voices_installed(speaker):
    assert speaker.languages() == ["en-US", "en-GB", "pt-BR", "nl-NL", "es-ES"]


@pytest.mark.parametrize("lang,text", [("en-US", "It's eighteen degrees and sunny."), ("pt-BR", "Está fazendo dezoito graus.")])
def test_wav(speaker, lang, text):
    with wave.open(io.BytesIO(speaker.wav(text, lang))) as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
        assert 1.0 < w.getnframes() / w.getframerate() < 6.0


def test_errors(speaker):
    with pytest.raises(TTSError):
        speaker.wav("   ")
    with pytest.raises(TTSError):
        speaker.wav("hallo", "de-DE")
    assert speaker.wav("hi", "en-AU")[:4] == b"RIFF"           # same language, other country: nearest voice


def test_speak_route(server):
    r = urllib.request.Request(server.url + "/api/voice/speak", data=json.dumps({"text": "Hello.", "lang": "en-GB"}).encode(),
                               method="POST", headers={"Content-Type": "application/json", "X-Remote": "1"})
    with urllib.request.urlopen(r) as resp:
        assert resp.headers["Content-Type"] == "audio/wav" and resp.read(4) == b"RIFF"
    code, c = server.req("/api/voice/config")
    assert c["tts_languages"][0] == "en-US"
    assert server.req("/api/voice/speak", {"text": ""})[0] == 400
