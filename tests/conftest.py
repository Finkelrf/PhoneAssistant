import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class Server:
    def __init__(self, tmp):
        self.port = free_port()
        self.data = tmp / "data"
        self.url = f"http://127.0.0.1:{self.port}"
        self.proc = subprocess.Popen([sys.executable, "-m", "server", "--host", "127.0.0.1", "--port", str(self.port),
                                      "--data", str(self.data)], cwd=ROOT,
                                     env={**os.environ, "REMOTE_URL": "http://192.168.1.50:8796"})
        for _ in range(100):
            try:
                urllib.request.urlopen(self.url + "/health", timeout=1); break
            except OSError:
                time.sleep(0.1)
        self.token = (self.data / "token").read_text().strip()

    def hook(self, mode, ev, timeout=30):
        """Run the real hook script as Claude Code would."""
        env = {**os.environ, "REMOTE_HOOK_URL": self.url, "REMOTE_TOKEN_FILE": str(self.data / "token")}
        return subprocess.Popen([sys.executable, str(ROOT / "hooks" / "claude_hook.py"), mode], env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True), json.dumps(ev)

    def run_hook(self, mode, ev, timeout=30):
        p, data = self.hook(mode, ev)
        out, _ = p.communicate(data, timeout=timeout)
        return p.returncode, out

    def req(self, path, body=None, cookie=True, headers=None):
        h = {"Content-Type": "application/json", "X-Remote": "1", **(headers or {})}
        if cookie:
            h["Cookie"] = f"remote_token={self.token}"
        r = urllib.request.Request(self.url + path, data=None if body is None else json.dumps(body).encode(),
                                   method="GET" if body is None else "POST", headers=h)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw.decode()

    def stop(self):
        self.proc.terminate(); self.proc.wait(5)


@pytest.fixture
def server(tmp_path):
    s = Server(tmp_path)
    yield s
    s.stop()
