"""Wake word training: makes a new openWakeWord model for a phrase typed on the phone, on this computer's CPU.

openWakeWord's own recipe (github.com/dscripka/openWakeWord, notebooks/automatic_model_training.ipynb) trains on
thousands of synthetic clips of the phrase and thousands of hours of other audio, on a GPU. This is a small version
of it that runs on the server's CPU in about 10 to 20 minutes:

  positives  the phrase spoken by Piper voices (en_US libritts_r has 904 speakers, plus the other English voices
             installed) at random speeds and styles, and the owner's own recordings from the phone, if any
  negatives  about 10 hours of real speech, music and noise as openWakeWord features (its validation_set_features.npy),
             plus Piper saying words that sound close to the phrase, parts of it and everyday sentences
  each clip  phone-like filtering, room echo, background noise (coloured noise, and the room heard before the owner
             spoke in the recordings), random level
  features   the same melspectrogram and embedding models the listener uses (server/wakeword.py)
  model      16 embeddings (1.28 s) -> 64 -> 64 -> score, trained with numpy, saved as ONNX

At the end it measures how often the model hears held-out examples of the phrase, and how many times an hour it
wakes on held-out real audio. It runs as its own low-priority process (python -m server.trainer), one at a time,
and reports progress in <data>/wake_train/status.json. The owner's recordings are deleted when training ends.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

import numpy as np

from .wakeword import MEL_STEP, MEL_WINDOW, MODEL_DIR, Models

SR = 16000
FRAMES = 16                      # embeddings the model looks at (16 x 80 ms)
POS_LEN = int(2.2 * SR)          # a positive clip: enough audio for 16 embeddings, the phrase ending near its end
NEG_LEN = int(3.5 * SR)
VOICE_DIR = Path(os.environ.get("REMOTE_PIPER_VOICES") or Path(__file__).resolve().parent.parent / "voices")
NEGATIVE_FEATURES = MODEL_DIR / "validation_set_features.npy"
MAX_SAMPLES = 20
MIN_SAMPLE_S, MAX_SAMPLE_S = 0.8, 5.0
PHRASE = re.compile(r"^[A-Za-z][A-Za-z' -]{1,38}[A-Za-z]$")
THRESHOLD = 0.5
COOLDOWN = 25                    # windows (2 s): one wake, however long the score stays high

EVERYDAY = [
    "what time is it", "turn the lights off", "I'll be there in a minute", "can you hear me", "let's go for a walk",
    "did you see the game last night", "where are my keys", "it's raining again", "what's for dinner tonight",
    "I need to call my mother", "the meeting starts at ten", "put it on the table", "thank you so much",
    "how was your day", "I don't think so", "maybe next week", "could you pass the salt", "the cat is asleep",
    "we should leave soon", "that sounds great", "I forgot my phone", "open the window please", "let me check",
    "the coffee is ready", "see you tomorrow", "good morning everyone", "is anybody home", "play some music",
    "the train was late", "I'm working from home today", "can we talk later", "it's on the second shelf",
    "how much does it cost", "the kids are at school", "that was a long day", "the weather looks nice",
    "turn it up a little", "I'll send you the file", "what did you say", "one two three four five",
    "hello how are you", "hi there", "okay sounds good", "no problem at all", "yes please", "not right now",
    "close the door", "where did you put it", "call me back", "set an alarm", "what's the weather like",
    "I love this song", "the dog needs a walk", "we're out of milk", "the printer is broken again",
    "remind me later", "let's watch a movie", "my computer is slow", "a cup of tea", "the bus is coming",
]
FILLERS = ["there", "you", "guys", "man", "everyone", "what", "listen", "look", "wait", "come on", "please"]
VOWELS, CONSONANTS = "aeiou", "bcdfghjklmnprstvwz"


# ---------- audio ----------
def resample(x: np.ndarray, sr_in: float, sr_out: float) -> np.ndarray:
    """FFT resampling (keeps the band below the new Nyquist frequency, no aliasing)."""
    n_out = max(1, int(round(len(x) * sr_out / sr_in)))
    if n_out == len(x):
        return x.astype(np.float32)
    spec = np.fft.rfft(x)
    keep = n_out // 2 + 1
    spec = spec[:keep] if len(spec) >= keep else np.pad(spec, (0, keep - len(spec)))
    return (np.fft.irfft(spec, n=n_out) * (n_out / len(x))).astype(np.float32)


def coloured_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    """White, pink or brown noise, unit RMS."""
    alpha = rng.choice([0.0, 1.0, 2.0])
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.arange(len(spec), dtype=np.float64)
    f[0] = 1
    spec /= f ** (alpha / 2)
    x = np.fft.irfft(spec, n=n)
    return (x / (np.sqrt(np.mean(x ** 2)) + 1e-9)).astype(np.float32)


def speech_bounds(x: np.ndarray) -> tuple[int, int] | None:
    """Where the loud part (the phrase) starts and ends, in samples, by 20 ms frame energy."""
    hop = SR // 50
    n = len(x) // hop
    if n < 5:
        return None
    rms = np.sqrt(np.mean(x[:n * hop].reshape(n, hop).astype(np.float64) ** 2, axis=1))
    floor, peak = np.percentile(rms, 10), rms.max()
    if peak < 300 or peak < floor * 4:          # int16 scale: nothing clearly louder than the room
        return None
    on = np.nonzero(rms > max(floor * 3, peak * 0.12))[0]
    return int(on[0] * hop), int((on[-1] + 1) * hop)


def check_sample(raw: bytes) -> np.ndarray:
    """A recording from the phone (16 kHz 16-bit mono): must hold one clear phrase. Raises ValueError with a reason."""
    if len(raw) % 2 or not MIN_SAMPLE_S * SR * 2 <= len(raw) <= MAX_SAMPLE_S * SR * 2:
        raise ValueError("That recording is too short or too long")
    x = np.frombuffer(raw, dtype="<i2")
    b = speech_bounds(x)
    if not b:
        raise ValueError("Didn't hear anything. Say it a little louder")
    if not 0.2 * SR <= b[1] - b[0] <= 2.5 * SR:
        raise ValueError("Say just the wake word, once")
    return x


class Augmenter:
    def __init__(self, rng: np.random.Generator, rooms: list[np.ndarray]):
        self.rng = rng
        self.rooms = [r for r in rooms if len(r) >= SR // 4]

    def _colour(self, x: np.ndarray) -> np.ndarray:
        """A phone-ish microphone: random high-pass and low-pass, done in the frequency domain."""
        spec = np.fft.rfft(x)
        f = np.fft.rfftfreq(len(x), 1 / SR)
        hp, lp = self.rng.uniform(60, 350), self.rng.uniform(3500, 7800)
        gain = 1 / np.sqrt(1 + (hp / np.maximum(f, 1)) ** 4) / np.sqrt(1 + (f / lp) ** 4)
        return np.fft.irfft(spec * gain, n=len(x)).astype(np.float32)

    def _reverb(self, x: np.ndarray) -> np.ndarray:
        rt60 = self.rng.uniform(0.15, 0.7)
        n = int(rt60 * SR)
        t = np.arange(n) / SR
        rir = self.rng.standard_normal(n) * np.exp(-6.9 * t / rt60) * self.rng.uniform(0.1, 0.5)
        rir[0] = 1.0
        m = len(x) + n - 1
        y = np.fft.irfft(np.fft.rfft(x, m) * np.fft.rfft(rir, m), m)[:len(x)]
        return (y * (np.abs(x).max() / (np.abs(y).max() + 1e-9))).astype(np.float32)

    def _noise(self, n: int) -> np.ndarray:
        if self.rooms and self.rng.random() < 0.5:
            r = self.rooms[self.rng.integers(len(self.rooms))]
            r = np.tile(r, n // len(r) + 1)
            start = self.rng.integers(0, len(r) - n + 1)
            r = r[start:start + n]
            return r / (np.sqrt(np.mean(r ** 2)) + 1e-9)
        return coloured_noise(n, self.rng)

    def clip(self, speech: np.ndarray | None, length: int, end_gap: tuple[float, float] | None) -> np.ndarray:
        """speech (float, any level) placed in a clip of `length` samples, as int16. end_gap: seconds between the end
        of the speech and the end of the clip (positives); None places it anywhere (negatives)."""
        rng = self.rng
        out = np.zeros(length, dtype=np.float32)
        level = 0.0
        if speech is not None and len(speech):
            s = speech
            if rng.random() < 0.7:
                s = resample(s, SR, SR * rng.uniform(0.9, 1.1))        # speed and pitch
            if rng.random() < 0.7:
                s = self._colour(s)
            if rng.random() < 0.5:
                s = self._reverb(s)
            s = s / (np.abs(s).max() + 1e-9) * rng.uniform(0.08, 0.9)
            if end_gap is not None:
                end = length - int(rng.uniform(*end_gap) * SR)
                start = end - len(s)
                if start < 0:
                    s, start = s[-start:], 0
            else:
                if len(s) > length:
                    cut = rng.integers(0, len(s) - length + 1)
                    s = s[cut:cut + length]
                start = rng.integers(0, length - len(s) + 1)
            out[start:start + len(s)] = s
            level = float(np.sqrt(np.mean(s ** 2)))
        if speech is None or rng.random() < 0.85:
            snr = rng.uniform(3, 35)
            noise_rms = level / (10 ** (snr / 20)) if level else rng.uniform(0.002, 0.1)
            out += self._noise(length) * noise_rms
        else:
            out += rng.standard_normal(length).astype(np.float32) * 1e-4
        return (np.clip(out, -1, 1) * 32767).astype(np.int16)


# ---------- texts ----------
def neighbours(phrase: str, rng: np.random.Generator, n: int) -> list[str]:
    """Things that sound a bit like the phrase but aren't it: its parts, and the words with a sound changed."""
    words = phrase.lower().split()
    out = set(words)
    if len(words) > 1:
        out.add(" ".join(words[:-1]))
        out.add(" ".join(words[1:]))
        out.update(f"{words[0]} {f}" for f in FILLERS)
        out.update(f"{f} {words[-1]}" for f in ("the", "a", "my", "oh"))
    for _ in range(n * 20):
        if len(out) >= n:
            break
        ws = list(words)
        i = int(rng.integers(len(ws)))
        w = ws[i]
        op = int(rng.integers(4))
        letters = list(w)
        if op == 0 and any(c in VOWELS for c in w):
            j = rng.choice([k for k, c in enumerate(letters) if c in VOWELS])
            letters[j] = rng.choice([v for v in VOWELS if v != letters[j]])
        elif op == 1 and letters and letters[0] not in VOWELS:
            letters[0] = rng.choice([c for c in CONSONANTS if c != letters[0]])
        elif op == 2 and len(letters) > 3:
            del letters[int(rng.integers(1, len(letters)))]
        else:
            letters.append(rng.choice(list("sdnty")))
        ws[i] = "".join(letters)
        if len(ws) > 1 and rng.random() < 0.3:
            ws[1 - i if len(ws) == 2 else int(rng.integers(len(ws)))] = rng.choice(FILLERS)
        out.add(" ".join(ws))
    out.discard(phrase.lower())
    return sorted(out)


def positive_text(phrase: str, rng: np.random.Generator) -> str:
    w = phrase.split()
    sep = rng.choice([" ", ", ", " "])
    return sep.join(w) + rng.choice(["", ".", "!", "?", ""])


# ---------- speech ----------
class Voices:
    """The installed English Piper voices, with how many speakers each has."""

    def __init__(self, voice_dir: Path, threads: int):
        from piper import PiperVoice
        self.items = []
        for path in sorted(Path(voice_dir).glob("en_*.onnx")):
            cfg = json.loads(path.with_name(path.name + ".json").read_text())
            voice = PiperVoice.load(path, config_path=path.with_name(path.name + ".json"))
            try:   # fewer threads for Piper's own onnxruntime session, so the server stays responsive
                import onnxruntime as ort
                opts = ort.SessionOptions()
                opts.intra_op_num_threads = threads
                opts.log_severity_level = 3
                voice.session = ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])
            except Exception:
                pass
            speakers = int(cfg.get("num_speakers") or 1)
            self.items.append((voice, speakers, int(cfg["audio"]["sample_rate"])))
        if not self.items:
            raise RuntimeError("No English Piper voices are installed")
        # the many-speaker voice gets most of the clips
        w = np.array([max(1.0, np.sqrt(s) * 3) for _, s, _ in self.items])
        self.weights = w / w.sum()

    def say(self, text: str, rng: np.random.Generator) -> np.ndarray:
        from piper import SynthesisConfig
        voice, speakers, rate = self.items[rng.choice(len(self.items), p=self.weights)]
        cfg = SynthesisConfig(speaker_id=int(rng.integers(speakers)) if speakers > 1 else None,
                              length_scale=float(rng.uniform(0.75, 1.35)), noise_scale=float(rng.uniform(0.4, 0.9)),
                              noise_w_scale=float(rng.uniform(0.5, 1.0)))
        audio = np.concatenate([c.audio_int16_array for c in voice.synthesize(text, syn_config=cfg)] or [np.zeros(1, np.int16)])
        x = resample(audio.astype(np.float32) / 32768, rate, SR)
        b = speech_bounds((x * 32767).astype(np.int16))
        return x[b[0]:b[1]] if b else x


# ---------- features ----------
def embeddings(models: Models, clip: np.ndarray) -> np.ndarray:
    """All embeddings of a clip, one every 80 ms, as the listener would see them."""
    mel = models.melspectrogram(clip)
    windows = np.array([mel[i:i + MEL_WINDOW] for i in range(0, mel.shape[0] - MEL_WINDOW + 1, MEL_STEP)])
    return models.embed(windows)


def last_window(models: Models, clip: np.ndarray) -> np.ndarray:
    return embeddings(models, clip)[-FRAMES:]


def all_windows(models: Models, clip: np.ndarray) -> np.ndarray:
    e = embeddings(models, clip)
    return np.stack([e[i:i + FRAMES] for i in range(0, e.shape[0] - FRAMES + 1)]) if e.shape[0] >= FRAMES else np.zeros((0, FRAMES, 96), np.float32)


# ---------- the model ----------
class Net:
    """1536 -> 64 -> 64 -> 1, ReLU, trained with Adam on a weighted logistic loss."""

    def __init__(self, rng: np.random.Generator, mean: np.ndarray, std: np.ndarray, hidden: int = 64):
        d = FRAMES * 96
        self.mean, self.std = mean, std
        self.p = {"W0": rng.standard_normal((d, hidden)) * np.sqrt(2 / d), "b0": np.zeros(hidden),
                  "W1": rng.standard_normal((hidden, hidden)) * np.sqrt(2 / hidden), "b1": np.zeros(hidden),
                  "W2": rng.standard_normal((hidden, 1)) * np.sqrt(1 / hidden), "b2": np.zeros(1)}
        self.p = {k: v.astype(np.float32) for k, v in self.p.items()}
        self.m = {k: np.zeros_like(v) for k, v in self.p.items()}
        self.v = {k: np.zeros_like(v) for k, v in self.p.items()}
        self.t = 0

    def _in(self, x: np.ndarray) -> np.ndarray:
        return (x.reshape(len(x), -1) - self.mean) / self.std

    def logits(self, x: np.ndarray) -> np.ndarray:
        p = self.p
        h0 = np.maximum(self._in(x) @ p["W0"] + p["b0"], 0)
        h1 = np.maximum(h0 @ p["W1"] + p["b1"], 0)
        return (h1 @ p["W2"] + p["b2"])[:, 0]

    def score(self, x: np.ndarray, batch: int = 8192) -> np.ndarray:
        out = [1 / (1 + np.exp(-self.logits(x[i:i + batch]))) for i in range(0, len(x), batch)]
        return np.concatenate(out) if out else np.zeros(0)

    def step(self, x: np.ndarray, y: np.ndarray, w: np.ndarray, lr: float, decay: float = 1e-4) -> float:
        p = self.p
        a = self._in(x)
        z0 = a @ p["W0"] + p["b0"]; h0 = np.maximum(z0, 0)
        z1 = h0 @ p["W1"] + p["b1"]; h1 = np.maximum(z1, 0)
        z = (h1 @ p["W2"] + p["b2"])[:, 0]
        s = 1 / (1 + np.exp(-z))
        loss = float(np.sum(w * (np.logaddexp(0, z) - y * z)) / np.sum(w))
        g = (w * (s - y) / np.sum(w)).astype(np.float32)[:, None]
        grads = {"W2": h1.T @ g, "b2": g.sum(0)}
        g1 = (g @ p["W2"].T) * (z1 > 0)
        grads.update({"W1": h0.T @ g1, "b1": g1.sum(0)})
        g0 = (g1 @ p["W1"].T) * (z0 > 0)
        grads.update({"W0": a.T @ g0, "b0": g0.sum(0)})
        self.t += 1
        for k in p:
            gk = grads[k] + (decay * p[k] if k[0] == "W" else 0)
            self.m[k] = 0.9 * self.m[k] + 0.1 * gk
            self.v[k] = 0.999 * self.v[k] + 0.001 * gk * gk
            mh = self.m[k] / (1 - 0.9 ** self.t)
            vh = self.v[k] / (1 - 0.999 ** self.t)
            p[k] -= (lr * mh / (np.sqrt(vh) + 1e-8)).astype(np.float32)
        return loss

    def export(self, path: Path):
        """Save as ONNX: [batch, 16, 96] -> [batch, 1], with the input scaling folded into the first layer."""
        import onnx
        from onnx import TensorProto, helper, numpy_helper
        p = self.p
        W0 = p["W0"] / self.std[:, None]
        b0 = p["b0"] - (self.mean / self.std) @ p["W0"]
        layers = [(W0, b0), (p["W1"], p["b1"]), (p["W2"], p["b2"])]
        nodes = [helper.make_node("Flatten", ["x"], ["f"], axis=1)]
        inits, cur = [], "f"
        for i, (W, b) in enumerate(layers):
            inits += [numpy_helper.from_array(np.ascontiguousarray(W.T, dtype=np.float32), f"W{i}"),
                      numpy_helper.from_array(b.astype(np.float32), f"b{i}")]
            nodes.append(helper.make_node("Gemm", [cur, f"W{i}", f"b{i}"], [f"g{i}"], transB=1))
            cur = f"g{i}"
            if i < len(layers) - 1:
                nodes.append(helper.make_node("Relu", [cur], [f"r{i}"]))
                cur = f"r{i}"
        nodes.append(helper.make_node("Sigmoid", [cur], ["score"]))
        graph = helper.make_graph(nodes, "wakeword", [helper.make_tensor_value_info("x", TensorProto.FLOAT, ["batch", FRAMES, 96])],
                                  [helper.make_tensor_value_info("score", TensorProto.FLOAT, ["batch", 1])], inits)
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)], producer_name="remote-trainer")
        model.ir_version = 8
        onnx.checker.check_model(model)
        tmp = Path(str(path) + ".tmp")
        onnx.save(model, str(tmp))
        os.replace(tmp, path)


def false_wakes(scores: np.ndarray, threshold: float = THRESHOLD) -> int:
    n, quiet = 0, -1
    for i in np.nonzero(scores >= threshold)[0]:
        if i > quiet:
            n += 1
            quiet = i + COOLDOWN
    return n


# ---------- the whole run ----------
class Run:
    def __init__(self, data_dir: Path, phrase: str, model_id: str, scale: float = 1.0, threads: int = 3, seed: int | None = None):
        self.data = Path(data_dir)
        self.dir = self.data / "wake_train"
        self.out = self.data / "wakewords" / f"{model_id}.onnx"
        self.phrase, self.id, self.scale, self.threads = phrase, model_id, scale, threads
        self.rng = np.random.default_rng(seed)
        self.started = time.time()

    def status(self, **kw):
        d = {"state": "running", "phrase": self.phrase, "id": self.id, "started": int(self.started), **kw}
        tmp = self.dir / "status.json.tmp"
        tmp.write_text(json.dumps(d))
        os.replace(tmp, self.dir / "status.json")

    def n(self, x: int) -> int:
        return max(4, int(x * self.scale))

    def run(self):
        rng = self.rng
        self.status(step="Getting ready", progress=0.0)
        models = Models(MODEL_DIR, threads=self.threads)
        voices = Voices(VOICE_DIR, self.threads)

        # the owner's recordings: the phrase, and the room before it
        mine, rooms = [], []
        for f in sorted(self.dir.glob("sample_*.pcm")):
            x = np.frombuffer(f.read_bytes(), dtype="<i2")
            b = speech_bounds(x)
            if not b:
                continue
            s = x.astype(np.float32) / 32768
            mine.append(s[max(0, b[0] - SR // 20):b[1] + SR // 20])
            if b[0] > SR // 4:
                rooms.append(s[:max(0, b[0] - SR // 10)])
        aug = Augmenter(rng, rooms)

        # speech
        n_pos, n_neg = self.n(1500), self.n(1500)
        near = neighbours(self.phrase, rng, 120)
        pos_speech, neg_speech = [], []
        total = n_pos + n_neg
        for i in range(total):
            if i % 25 == 0:
                self.status(step="Making voice samples", progress=round(0.05 + 0.45 * i / total, 3))
            if i < n_pos:
                pos_speech.append(voices.say(positive_text(self.phrase, rng), rng))
            else:
                r = rng.random()
                text = rng.choice(near) if r < 0.6 else rng.choice(EVERYDAY) if r < 0.85 else f"{rng.choice(EVERYDAY)} {rng.choice(near)}"
                neg_speech.append(voices.say(str(text), rng))

        # features
        self.status(step="Listening to the samples", progress=0.5)
        per_mine = self.n(40)
        pos_clips = [aug.clip(s, POS_LEN, (0.0, 0.2)) for s in pos_speech for _ in range(2)]
        pos_clips += [aug.clip(s, POS_LEN, (0.0, 0.2)) for s in mine for _ in range(per_mine)]
        pos, neg = [], []
        for i, c in enumerate(pos_clips):
            if i % 100 == 0:
                self.status(step="Listening to the samples", progress=round(0.5 + 0.15 * i / len(pos_clips), 3))
            pos.append(last_window(models, c))
        pos = np.stack(pos).astype(np.float32)
        neg_clips = [aug.clip(s, NEG_LEN, None) for s in neg_speech] + [aug.clip(None, NEG_LEN, None) for _ in range(self.n(200))]
        # the phrase cut short (its start only) must not wake it either
        neg_clips += [aug.clip(s[:int(len(s) * rng.uniform(0.3, 0.6))], POS_LEN, (0.0, 0.2)) for s in pos_speech[: self.n(300)]]
        for i, c in enumerate(neg_clips):
            if i % 100 == 0:
                self.status(step="Listening to the samples", progress=round(0.65 + 0.1 * i / len(neg_clips), 3))
            w = all_windows(models, c)
            neg.append(w[-1:] if len(c) == POS_LEN else w)
        neg = np.concatenate(neg).astype(np.float32)

        # real audio (speech, music, noise): train on the first 80%, measure false wakes on the rest
        real = np.load(NEGATIVE_FEATURES, mmap_mode="r") if NEGATIVE_FEATURES.exists() else np.zeros((0, 96), np.float32)
        cut = int(len(real) * 0.8)
        real_train, real_test = real[:cut], np.asarray(real[cut:], dtype=np.float32)
        if self.scale < 1:
            real_test = real_test[: max(FRAMES + 1, int(len(real_test) * self.scale))]

        # held-out sets
        idx = rng.permutation(len(pos))
        k = max(1, len(pos) // 7)
        pos_test, pos_train = pos[idx[:k]], pos[idx[k:]]
        idx = rng.permutation(len(neg))
        k = max(1, len(neg) // 7)
        neg_test, neg_train = neg[idx[:k]], neg[idx[k:]]

        def real_batch(n):
            if len(real_train) <= FRAMES:
                return np.zeros((0, FRAMES, 96), np.float32)
            s = rng.integers(0, len(real_train) - FRAMES, n)
            return np.asarray(real_train[s[:, None] + np.arange(FRAMES)], dtype=np.float32)

        sample = np.concatenate([pos_train.reshape(len(pos_train), -1), neg_train.reshape(len(neg_train), -1),
                                 real_batch(5000).reshape(-1, FRAMES * 96)])
        net = Net(rng, sample.mean(0).astype(np.float32), (sample.std(0) + 1e-3).astype(np.float32))

        # train, then again with the negatives it got wrong
        steps = self.n(5000)
        hard = np.zeros((0, FRAMES, 96), np.float32)
        for t in range(steps):
            if t % 100 == 0:
                self.status(step="Training", progress=round(0.75 + 0.2 * t / steps, 3))
            if t in (steps * 2 // 5, steps * 7 // 10):       # find the negatives it still gets wrong, train on them more
                self.status(step="Training", progress=round(0.75 + 0.2 * t / steps, 3))
                found = [hard, neg_train[net.score(neg_train) > 0.05]]
                if len(real_train) > FRAMES:
                    view = np.lib.stride_tricks.sliding_window_view(real_train, (FRAMES, 96))[:, 0]
                    for i in range(0, len(view), 2 * 8192):
                        chunk = np.asarray(view[i:i + 2 * 8192:2], dtype=np.float32)
                        found.append(chunk[net.score(chunk) > 0.05])
                hard = np.concatenate(found)[-50000:]
            neg_w = 1 + 19 * t / steps
            xp = pos_train[rng.integers(0, len(pos_train), 128)]
            xn = neg_train[rng.integers(0, len(neg_train), 128)]
            xr = real_batch(256)
            parts = [xp, xn, xr]
            if len(hard):
                parts.append(hard[rng.integers(0, len(hard), 128)])
            x = np.concatenate(parts)
            y = np.concatenate([np.ones(len(xp)), np.zeros(len(x) - len(xp))]).astype(np.float32)
            w = np.concatenate([np.ones(len(xp)), np.full(len(x) - len(xp), neg_w)]).astype(np.float32)
            lr = 1e-3 * (0.5 + 0.5 * np.cos(np.pi * t / steps)) + 1e-5
            net.step(x, y, w, lr)

        # measure
        self.status(step="Testing", progress=0.96)
        recall = float(np.mean(net.score(pos_test) >= THRESHOLD))
        near_rate = float(np.mean(net.score(neg_test) >= THRESHOLD))
        wakes, hours = 0, 0.0
        if len(real_test) > FRAMES:
            win = np.lib.stride_tricks.sliding_window_view(real_test, (FRAMES, 96))[:, 0]
            wakes = false_wakes(net.score(win))
            hours = len(real_test) * 0.08 / 3600
        self.out.parent.mkdir(parents=True, exist_ok=True)
        net.export(self.out)
        result = {"recall": round(recall, 3), "near_misses": round(near_rate, 3), "hours": round(hours, 2),
                  "false_wakes": wakes, "per_hour": round(wakes / hours, 2) if hours else None,
                  "own_samples": len(mine), "seconds": int(time.time() - self.started)}
        clear_samples(self.data)
        self.status(state="done", step="Done", progress=1.0, result=result)
        return result


# ---------- the server side ----------
class Training:
    """The owner's recordings, the one training process, and its progress, for the server (server/app.py).
    on_done(model_id, phrase, result) is called when a model is ready."""

    def __init__(self, data_dir: str | Path, on_done):
        self.data = Path(data_dir)
        self.dir = self.data / "wake_train"
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.on_done = on_done
        self.proc = None
        self._task = None
        self._cancelled = False
        st = self._read()
        if st.get("state") == "running":           # the server restarted while it ran
            self._write({**st, "state": "error", "error": "Training stopped when the server restarted"})
            clear_samples(self.data)

    def _read(self) -> dict:
        try:
            d = json.loads((self.dir / "status.json").read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, d: dict):
        tmp = self.dir / "status.json.tmp"
        tmp.write_text(json.dumps(d))
        os.replace(tmp, self.dir / "status.json")

    def samples(self) -> int:
        return len(list(self.dir.glob("sample_*.pcm")))

    def add_sample(self, raw: bytes) -> int:
        if self.running():
            raise ValueError("Wait until the training finishes")
        if self.samples() >= MAX_SAMPLES:
            raise ValueError(f"That's enough recordings ({MAX_SAMPLES})")
        x = check_sample(raw)
        path = self.dir / f"sample_{int(time.time() * 1000)}_{secrets.token_hex(2)}.pcm"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(x.astype("<i2").tobytes())
        return self.samples()

    def clear_samples(self) -> int:
        if self.running():
            raise ValueError("Wait until the training finishes")
        clear_samples(self.data)
        return 0

    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def status(self) -> dict:
        st = self._read()
        saving = self._task is not None and not self._task.done()
        if saving and st.get("state") in ("done", "running"):
            # the process has finished but the server hasn't added the word yet: the phone mustn't see "done" early
            st = {**st, "state": "running", "step": "Saving", "progress": st.get("progress", 1.0)}
        elif st.get("state") == "running" and not self.running():
            st = {**st, "state": "error", "error": "Training stopped"}
        return {**st, "state": st.get("state", "idle"), "samples": self.samples(), "max_samples": MAX_SAMPLES}

    async def start(self, phrase: str) -> dict:
        import asyncio
        phrase = " ".join(str(phrase or "").split())
        if not PHRASE.match(phrase) or len(phrase.split()) > 4:
            raise ValueError("Type a wake word of one to four English words, letters only")
        if self.running():
            raise ValueError("A wake word is already training")
        if not (MODEL_DIR / "melspectrogram.onnx").exists():
            raise ValueError("openWakeWord's models aren't installed in the server")
        model_id = new_id()
        self._cancelled = False
        self._write({"state": "running", "phrase": phrase, "id": model_id, "started": int(time.time()),
                     "step": "Starting", "progress": 0.0})
        log = open(self.dir / "log.txt", "wb")
        try:
            self.proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "server.trainer", "--data", str(self.data), "--phrase", phrase, "--id", model_id,
                cwd=str(Path(__file__).resolve().parent.parent), stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log)
        finally:
            log.close()
        self._task = asyncio.create_task(self._wait(model_id, phrase))
        return self.status()

    async def _wait(self, model_id: str, phrase: str):
        await self.proc.wait()
        st = self._read()
        if self._cancelled:
            self._write({**st, "state": "cancelled", "step": "Cancelled"})
            (self.data / "wakewords" / f"{model_id}.onnx").unlink(missing_ok=True)
            clear_samples(self.data)
        elif st.get("state") == "done" and st.get("id") == model_id:
            try:
                self.on_done(model_id, phrase, st.get("result") or {})
            except Exception as e:
                self._write({**st, "state": "error", "error": f"Couldn't save the wake word: {e}"[:300]})
        elif st.get("state") == "running":
            self._write({**st, "state": "error", "error": "Training stopped unexpectedly"})

    async def cancel(self) -> dict:
        if self.running():
            self._cancelled = True
            self.proc.terminate()
            if self._task:
                await self._task
        return self.status()


def clear_samples(data_dir: Path):
    for f in (Path(data_dir) / "wake_train").glob("sample_*.pcm"):
        f.unlink(missing_ok=True)


def new_id() -> str:
    return secrets.token_hex(4)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Train a wake word (run by the Remote server)")
    ap.add_argument("--data", required=True)
    ap.add_argument("--phrase", required=True)
    ap.add_argument("--id", required=True)
    args = ap.parse_args(argv)
    try:
        os.nice(10)
    except OSError:
        pass
    scale = float(os.environ.get("REMOTE_TRAIN_SCALE", "1") or 1)
    threads = int(os.environ.get("REMOTE_TRAIN_THREADS", "3") or 3)
    run = Run(Path(args.data), args.phrase, args.id, scale=scale, threads=threads)
    try:
        run.run()
        return 0
    except Exception as e:  # report it on the phone
        run.status(state="error", error=f"Training failed: {e}"[:300])
        return 1
    finally:
        clear_samples(args.data)


if __name__ == "__main__":
    sys.exit(main())
