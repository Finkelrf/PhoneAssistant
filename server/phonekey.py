"""Phone link: lets one phone (or a few) use the voice assistant's email and calendar access.

The Remote page has no sign-in, so anyone on the home network can open it. Email and calendar must not be reachable
that way. Linking works like this:
  1. On the computer, `python3 hooks/link_phone.py` asks the server (with the hook key, which only the computer has)
     for a one-time code. The code is good for 10 minutes and 5 tries.
  2. On the phone, Settings > Wake word and voice > Email and calendar: type the code.
  3. The server gives the phone a random key in a cookie (HttpOnly, Secure, SameSite=Strict, HTTPS page only).
     Only a hash of each key is kept, in <data>/phone_keys.json (mode 600).
Voice questions that carry a valid key get the email and calendar tools; others get the plain assistant.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

COOKIE = "remote_phone"
CODE_S = 600
CODE_TRIES = 5
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O or 1/I: easy to type from the screen
MAX_KEYS = 5


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class PhoneKeys:
    def __init__(self, data_dir: str | Path):
        self.file = Path(data_dir) / "phone_keys.json"
        self._code: dict | None = None

    def _load(self) -> list[dict]:
        try:
            keys = json.loads(self.file.read_text()).get("keys")
            return [k for k in keys if isinstance(k, dict) and isinstance(k.get("hash"), str)] if isinstance(keys, list) else []
        except (OSError, ValueError, AttributeError):
            return []

    def _save(self, keys: list[dict]):
        tmp = self.file.with_name(self.file.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"keys": keys}, f)
        os.replace(tmp, self.file)

    def new_code(self) -> dict:
        code = "".join(secrets.choice(ALPHABET) for _ in range(8))
        self._code = {"code": code, "until": time.time() + CODE_S, "tries": 0}
        return {"code": code, "expires_in": CODE_S}

    def redeem(self, code: str, label: str = "") -> str | None:
        """A correct, fresh code -> a new phone key (shown once). Wrong codes use up tries."""
        c = self._code
        code = "".join(str(code or "").upper().split()).replace("-", "")
        if not c or time.time() > c["until"] or c["tries"] >= CODE_TRIES:
            self._code = None
            return None
        if not hmac.compare_digest(code.encode(), c["code"].encode()):
            c["tries"] += 1
            if c["tries"] >= CODE_TRIES:
                self._code = None
            return None
        self._code = None
        key = secrets.token_urlsafe(32)
        keys = self._load()[-(MAX_KEYS - 1):]
        keys.append({"hash": _hash(key), "made": int(time.time()), "label": " ".join(str(label).split())[:60]})
        self._save(keys)
        return key

    def check(self, key: str | None) -> bool:
        if not key or len(key) > 200:
            return False
        h = _hash(key)
        return any(hmac.compare_digest(h, k["hash"]) for k in self._load())

    def revoke(self, key: str | None):
        if not key:
            return
        h = _hash(key)
        self._save([k for k in self._load() if not hmac.compare_digest(h, k["hash"])])

    def revoke_all(self) -> int:
        n = len(self._load())
        self._save([])
        self._code = None
        return n
