"""Training a new wake word on the server (server/trainer.py): the pieces, a tiny real run, and the API."""
import json
import time
import urllib.request

import numpy as np
import pytest

from conftest import Server
from server import trainer
from server.wakeword import Detector, Models, check_model

SR = trainer.SR


def tone_burst(seconds=2.0, start=0.6, length=0.6, level=8000):
    x = np.random.default_rng(0).normal(0, 30, int(seconds * SR))
    t = np.arange(int(length * SR)) / SR
    i = int(start * SR)
    x[i:i + len(t)] += level * np.sin(2 * np.pi * 220 * t)
    return x.astype("<i2")


def test_check_sample():
    assert len(trainer.check_sample(tone_burst().tobytes())) == 2 * SR
    with pytest.raises(ValueError, match="Didn't hear"):
        trainer.check_sample(np.zeros(2 * SR, "<i2").tobytes())
    with pytest.raises(ValueError, match="too short"):
        trainer.check_sample(tone_burst(0.3, 0.05, 0.1).tobytes())
    with pytest.raises(ValueError, match="just the wake word"):
        trainer.check_sample(tone_burst(4, 0.2, 3.2).tobytes())


def test_neighbours_and_text():
    rng = np.random.default_rng(1)
    near = trainer.neighbours("Hey Claude", rng, 60)
    assert "hey claude" not in near and "claude" in near and "hey" in near and len(near) >= 40
    assert "claude" in trainer.positive_text("Hey Claude", rng).lower().replace(",", "")


def test_resample_keeps_length_and_pitch():
    x = np.sin(2 * np.pi * 440 * np.arange(22050) / 22050).astype(np.float32)
    y = trainer.resample(x, 22050, SR)
    assert len(y) == SR
    assert abs(np.argmax(np.abs(np.fft.rfft(y))) - 440) <= 1


def test_tiny_training_run(tmp_path):
    """A real, very small run: Piper voices, augmentation, features, numpy training, ONNX export."""
    (tmp_path / "wake_train").mkdir()
    (tmp_path / "wake_train" / "sample_1.pcm").write_bytes(tone_burst().tobytes())
    result = trainer.Run(tmp_path, "Hey Claude", "0123abcd", scale=0.03, seed=3).run()
    out = tmp_path / "wakewords" / "0123abcd.onnx"
    check_model(out)
    det = Detector(Models(), out)
    assert det.frames == 16 and 0 <= det.feed(np.zeros(1280 * 30, np.int16)) <= 1
    assert set(result) >= {"recall", "per_hour", "hours", "own_samples"} and result["own_samples"] == 1
    st = json.loads((tmp_path / "wake_train" / "status.json").read_text())
    assert st["state"] == "done" and st["result"] == result


@pytest.fixture
def small_server(tmp_path):
    s = Server(tmp_path, env={"REMOTE_TRAIN_SCALE": "0.03"})
    yield s
    s.stop()


def post_pcm(server, raw):
    r = urllib.request.Request(server.url + "/api/wake/train/sample", data=raw, method="POST",
                               headers={"Content-Type": "application/octet-stream", "X-Remote": "1"})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_train_api(small_server):
    s = small_server
    assert s.req("/api/wake/train")[1]["state"] == "idle"
    assert post_pcm(s, np.zeros(2 * SR, "<i2").tobytes())[0] == 400
    assert post_pcm(s, tone_burst().tobytes()) == (200, {"samples": 1})
    assert s.req("/api/wake/train/samples/clear", {}) == (200, {"samples": 0})
    assert post_pcm(s, tone_burst().tobytes())[1] == {"samples": 1}
    for bad in ["", "x", "Hey <Claude>", "one two three four five"]:
        code, body = s.req("/api/wake/train", {"phrase": bad})
        assert code == 400 and "wake word" in body["error"]
    code, st = s.req("/api/wake/train", {"phrase": "Hey  Claude"})
    assert code == 200 and st["state"] == "running" and st["phrase"] == "Hey Claude"
    assert s.req("/api/wake/train", {"phrase": "Hey Claude"})[0] == 400       # one at a time
    assert post_pcm(s, tone_burst().tobytes())[0] == 400                      # no recordings while it runs
    for _ in range(600):
        st = s.req("/api/wake/train")[1]
        if st["state"] != "running":
            break
        time.sleep(0.5)
    assert st["state"] == "done", st
    assert st["samples"] == 0                                                 # recordings are deleted
    cfg = s.req("/api/voice/config")[1]
    trained = [m for m in cfg["oww_models"] if m.get("trained")]
    assert [m["label"] for m in trained] == ["Hey Claude"] and trained[0]["id"] == "t_" + st["id"]
    assert cfg["oww_model"] == "hey_jarvis"                                   # not switched on by itself
    code, cfg = s.req("/api/voice/config", {"oww_model": trained[0]["id"]})
    assert code == 200 and cfg["oww_model"] == trained[0]["id"] and cfg["oww_label"] == "Hey Claude"
    code, cfg = s.req("/api/voice/wakeword/trained/delete", {"id": st["id"]})
    assert code == 200 and cfg["oww_model"] == "hey_jarvis" and not [m for m in cfg["oww_models"] if m.get("trained")]
    assert not (s.data / "wakewords" / f"{st['id']}.onnx").exists()
    assert s.req("/api/voice/wakeword/trained/delete", {"id": "../x"})[0] == 400


def test_cancel(small_server):
    s = small_server
    assert s.req("/api/wake/train", {"phrase": "Hey Claude"})[0] == 200
    code, st = s.req("/api/wake/train/cancel", {})
    assert code == 200 and st["state"] == "cancelled"
    assert not [m for m in s.req("/api/voice/config")[1]["oww_models"] if m.get("trained")]
