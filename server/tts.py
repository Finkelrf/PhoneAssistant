"""Speech for Claude's answers, made on this computer with Piper (github.com/rhasspy/piper, MIT; piper-tts on PyPI).

The phone plays the WAV through the page's own audio, which the first tap already unlocked. That's steadier than the
phone's built-in speech, which Android Chrome may refuse to start when no tap came just before (as after a wake word).

Voices (rhasspy/piper-voices v1.0.0, "medium") are downloaded and hash-checked when the Docker image is built, into
REMOTE_PIPER_VOICES (default <repo>/voices). Each is loaded the first time it's needed and then kept.
"""
from __future__ import annotations

import io
import os
import threading
import wave
from pathlib import Path

VOICE_DIR = Path(os.environ.get("REMOTE_PIPER_VOICES") or Path(__file__).resolve().parent.parent / "voices")
VOICES = {
    "en-US": "en_US-hfc_male-medium",
    "en-GB": "en_GB-alan-medium",
    "pt-BR": "pt_BR-faber-medium",
    "nl-NL": "nl_NL-mls-medium",
    "es-ES": "es_ES-davefx-medium",
}
MAX_TEXT = 4000


class TTSError(Exception):
    pass


class Speaker:
    def __init__(self, voice_dir: Path = VOICE_DIR):
        self.dir = Path(voice_dir)
        self._voices: dict[str, object] = {}
        self._lock = threading.Lock()

    def available(self, lang: str = "en-US") -> bool:
        name = VOICES.get(lang)
        return bool(name) and (self.dir / f"{name}.onnx").exists()

    def languages(self) -> list[str]:
        return [lang for lang in VOICES if self.available(lang)]

    def _voice(self, lang: str):
        name = VOICES.get(lang) or VOICES.get(next((k for k in VOICES if k[:2] == lang[:2]), ""), "")
        if not name or not (self.dir / f"{name}.onnx").exists():
            raise TTSError("No computer voice for that language")
        if name not in self._voices:
            from piper import PiperVoice
            self._voices[name] = PiperVoice.load(self.dir / f"{name}.onnx", config_path=self.dir / f"{name}.onnx.json")
        return self._voices[name]

    def wav(self, text: str, lang: str = "en-US") -> bytes:
        """Text to a mono 16-bit WAV. One at a time: the voices aren't meant to be shared between threads."""
        text = " ".join(str(text or "").split())[:MAX_TEXT]
        if not text:
            raise TTSError("Nothing to say")
        with self._lock:
            voice = self._voice(lang)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                voice.synthesize_wav(text, w)
        return buf.getvalue()
