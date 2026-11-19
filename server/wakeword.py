"""openWakeWord on the server: hears the wake word in the 16 kHz audio the phone streams over a WebSocket.

A small port of openWakeWord's streaming pipeline (github.com/dscripka/openWakeWord, Apache-2.0, v0.6.0
openwakeword/utils.py AudioFeatures and model.py Model.predict), running its ONNX models with onnxruntime:

  80 ms of audio (1280 samples) ─▶ melspectrogram.onnx ─▶ mel frames (x/10 + 2)
  last 76 mel frames            ─▶ embedding_model.onnx ─▶ one 96-number embedding
  last 16 embeddings            ─▶ <wake word>.onnx     ─▶ score 0..1

The models come from openWakeWord's v0.5.1 release and are downloaded when the Docker image is built (their licence,
CC BY-NC-SA 4.0, allows personal use but they are not kept in this repository). Audio is only held in memory, a
fraction of a second of it, and never written anywhere.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import numpy as np

CHUNK = 1280                 # 80 ms at 16 kHz: what each step works on
CONTEXT = 160 * 3            # extra samples the melspectrogram needs before a chunk
MEL_WINDOW = 76
MEL_STEP = 8
MEL_KEEP = 10 * 97
FEATURES_KEEP = 120
WARMUP_FRAMES = 5            # openWakeWord ignores the first few predictions

MODEL_DIR = Path(os.environ.get("REMOTE_OWW_MODELS") or Path(__file__).resolve().parent.parent / "models")
BUILT_IN = {"hey_jarvis": ("Hey Jarvis", "hey_jarvis_v0.1.onnx"), "alexa": ("Alexa", "alexa_v0.1.onnx"),
            "hey_mycroft": ("Hey Mycroft", "hey_mycroft_v0.1.onnx"), "hey_rhasspy": ("Hey Rhasspy", "hey_rhasspy_v0.1.onnx")}


class WakeWordError(Exception):
    pass


def _session(path: Path, threads: int = 1):
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.inter_op_num_threads = 1
    opts.intra_op_num_threads = threads
    opts.log_severity_level = 3
    return ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])


class Models:
    """The two shared feature models, loaded once, plus wake word models by path (cached)."""

    def __init__(self, model_dir: Path = MODEL_DIR, threads: int = 1):
        self.dir = Path(model_dir)
        self.threads = threads
        self._lock = threading.Lock()
        self._mel = self._emb = None
        self._wake: dict[str, tuple[float, object]] = {}

    def available(self) -> bool:
        return (self.dir / "melspectrogram.onnx").exists() and (self.dir / "embedding_model.onnx").exists()

    def _features(self):
        with self._lock:
            if self._mel is None:
                if not self.available():
                    raise WakeWordError("openWakeWord's models aren't installed in the server")
                self._mel = _session(self.dir / "melspectrogram.onnx", self.threads)
                self._emb = _session(self.dir / "embedding_model.onnx", self.threads)
            return self._mel, self._emb

    def wake(self, path: Path):
        path = Path(path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            raise WakeWordError("That wake word model is missing")
        with self._lock:
            hit = self._wake.get(str(path))
            if hit and hit[0] == mtime:
                return hit[1]
        sess = check_model(path)
        with self._lock:
            self._wake[str(path)] = (mtime, sess)
        return sess

    def melspectrogram(self, audio: np.ndarray) -> np.ndarray:
        mel, _ = self._features()
        out = mel.run(None, {mel.get_inputs()[0].name: audio.astype(np.float32)[None, :]})[0]
        return np.squeeze(out) / 10 + 2

    def embed(self, window: np.ndarray) -> np.ndarray:
        _, emb = self._features()
        x = window.astype(np.float32)
        x = x.reshape((-1, MEL_WINDOW, 32, 1))
        return emb.run(None, {emb.get_inputs()[0].name: x})[0].reshape((x.shape[0], 96))


def check_model(path: Path):
    """Load a wake word model and check it takes [1, frames, 96] embeddings and gives one score."""
    try:
        sess = _session(path)
    except Exception:
        raise WakeWordError("That isn't an openWakeWord model (.onnx)")
    ins, outs = sess.get_inputs(), sess.get_outputs()
    shape = ins[0].shape if ins else []
    if len(ins) != 1 or len(shape) != 3 or shape[-1] != 96 or not isinstance(shape[1], int) or not 1 <= shape[1] <= 64:
        raise WakeWordError("That model doesn't take openWakeWord features")
    if len(outs) != 1:
        raise WakeWordError("That model doesn't give one score")
    return sess


class Detector:
    """Streaming state for one phone. feed() takes int16 samples and returns the best new score."""

    def __init__(self, models: Models, wake_path: Path):
        self.models = models
        self.sess = models.wake(wake_path)
        self.input = self.sess.get_inputs()[0].name
        self.frames = self.sess.get_inputs()[0].shape[1]
        self.reset()

    def reset(self):
        self.raw = np.zeros(0, dtype=np.int16)          # last CONTEXT samples before the pending ones, plus pending
        self.pending = 0
        self.mel = np.ones((MEL_WINDOW, 32), dtype=np.float32)
        noise = np.random.default_rng(0).integers(-1000, 1000, 16000 * 4).astype(np.int16)
        spec = self.models.melspectrogram(noise)
        windows = np.array([spec[i:i + MEL_WINDOW] for i in range(0, spec.shape[0], MEL_STEP)
                            if spec[i:i + MEL_WINDOW].shape[0] == MEL_WINDOW])
        self.features = self.models.embed(windows)
        self.seen = 0

    def feed(self, samples: np.ndarray) -> float:
        best = 0.0
        self.raw = np.concatenate((self.raw, samples.astype(np.int16)))
        self.pending += samples.shape[0]
        while self.pending >= CHUNK:
            end = self.raw.shape[0] - (self.pending - CHUNK)
            chunk = self.raw[max(0, end - CHUNK - CONTEXT):end]
            self.mel = np.vstack((self.mel, self.models.melspectrogram(chunk)))[-MEL_KEEP:]
            self.features = np.vstack((self.features, self.models.embed(self.mel[-MEL_WINDOW:])))[-FEATURES_KEEP:]
            x = self.features[-self.frames:][None, :, :].astype(np.float32)
            score = float(np.asarray(self.sess.run(None, {self.input: x})[0]).reshape(-1)[0])
            self.pending -= CHUNK
            self.seen += 1
            if self.seen > WARMUP_FRAMES:
                best = max(best, score)
        self.raw = self.raw[-(self.pending + CONTEXT):]
        return best
