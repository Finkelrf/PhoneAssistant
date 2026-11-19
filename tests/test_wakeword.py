"""openWakeWord on the server: the detector, the settings, the upload and the WebSocket the phone streams to.

tests/fixtures/*.wav are 16 kHz speech made with Piper TTS (en_US lessac): "hey jarvis" and "what time is it in new york".
"""
import json
import wave
from pathlib import Path

import numpy as np
import pytest
from websockets.sync.client import connect

from server.wakeword import BUILT_IN, MODEL_DIR, Detector, Models, WakeWordError, check_model

FIX = Path(__file__).parent / "fixtures"


def load(name, pad=1.0):
    with wave.open(str(FIX / name)) as w:
        assert w.getframerate() == 16000
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    z = np.zeros(int(16000 * pad), np.int16)
    return np.concatenate([z, a, z])


@pytest.fixture(scope="module")
def models():
    return Models()


def best(det, audio, step=1280):
    return max(det.feed(audio[i:i + step]) for i in range(0, len(audio), step))


def test_hears_hey_jarvis(models):
    d = Detector(models, MODEL_DIR / BUILT_IN["hey_jarvis"][1])
    assert best(d, load("hey_jarvis.wav")) > 0.8
    d.reset()
    assert best(d, load("not_a_wake_word.wav")) < 0.2


def test_other_words_and_odd_chunks(models):
    d = Detector(models, MODEL_DIR / BUILT_IN["alexa"][1])
    assert best(d, load("hey_jarvis.wav")) < 0.2
    d = Detector(models, MODEL_DIR / BUILT_IN["hey_jarvis"][1])
    a = load("hey_jarvis.wav")
    scores, i = [], 0
    for step in [700, 2000, 333, 4096] * 60:
        if i >= len(a):
            break
        scores.append(d.feed(a[i:i + step])); i += step
    assert max(scores) > 0.8


def test_check_model(tmp_path):
    bad = tmp_path / "x.onnx"
    bad.write_bytes(b"not a model" * 200)
    with pytest.raises(WakeWordError):
        check_model(bad)
    with pytest.raises(WakeWordError):          # a model, but not a wake word model
        check_model(MODEL_DIR / "melspectrogram.onnx")
    check_model(MODEL_DIR / BUILT_IN["hey_rhasspy"][1])


def test_engine_settings_and_upload(server):
    code, c = server.req("/api/voice/config")
    assert c["engine"] == "openwakeword" and c["oww_model"] == "hey_jarvis" and c["oww_label"] == "Hey Jarvis" and c["oww_ready"]
    assert [m["id"] for m in c["oww_models"]] == ["hey_jarvis", "alexa", "hey_mycroft", "hey_rhasspy"]
    assert server.req("/api/voice/config", {"engine": "siri"})[0] == 400
    assert server.req("/api/voice/config", {"oww_model": "custom"})[0] == 400
    code, c = server.req("/api/voice/config", {"engine": "porcupine", "oww_model": "alexa"})
    assert c["engine"] == "porcupine" and c["oww_label"] == "Alexa"

    def up(raw):
        import urllib.request
        r = urllib.request.Request(server.url + "/api/voice/wakeword?label=Hey%20Claude", data=raw, method="POST",
                                   headers={"X-Remote": "1", "Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    assert up(b"x" * 5000)[0] == 400
    assert not (server.data / "voice_wakeword.onnx").exists()
    code, c = up((MODEL_DIR / BUILT_IN["hey_mycroft"][1]).read_bytes())
    assert code == 200 and c["oww_custom"] and c["oww_model"] == "custom" and c["oww_label"] == "Hey Claude"
    code, c = server.req("/api/voice/wakeword/delete", {})
    assert not c["oww_custom"] and c["oww_model"] == "hey_jarvis"


def ws_url(server):
    return server.url.replace("http://", "ws://") + "/api/wake/ws"


def test_websocket_hears_the_word(server):
    server.req("/api/voice/config", {"sensitivity": 0.5})
    with connect(ws_url(server), origin=server.url, open_timeout=10) as ws:
        hello = json.loads(ws.recv(timeout=15))
        assert hello == {"ready": True, "label": "Hey Jarvis", "threshold": 0.5}
        for name, expect in [("not_a_wake_word.wav", False), ("hey_jarvis.wav", True)]:
            a = load(name)
            for i in range(0, len(a), 1280):
                ws.send(a[i:i + 1280].astype("<i2").tobytes())
            try:
                msg = json.loads(ws.recv(timeout=5))
            except TimeoutError:
                msg = None
            if expect:
                assert msg and msg["wake"] == "Hey Jarvis" and msg["score"] > 0.5
            else:
                assert msg is None
        ws.send("reset")
        ws.send(b"\x00" * 3)               # odd length: ignored


def test_websocket_checks_origin(server):
    for origin in ["https://evil.example", None]:
        with pytest.raises(Exception):
            with connect(ws_url(server), origin=origin, open_timeout=5) as ws:
                ws.recv(timeout=5)
