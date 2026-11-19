"""Phone link: only a phone given a code from the computer gets email and calendar tools for voice questions."""
import http.cookiejar
import json
import os
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from conftest import ROOT, Server
from server.phonekey import CODE_TRIES, PhoneKeys
from test_voice import make_fake


def test_codes_and_keys(tmp_path):
    k = PhoneKeys(tmp_path)
    assert k.redeem("ANYTHING") is None                   # no code made yet
    code = k.new_code()["code"]
    for _ in range(CODE_TRIES - 1):
        assert k.redeem("WRONG000") is None
    assert k.redeem(code)                                 # the last try still works
    # after the allowed tries, even the right code is gone
    code = k.new_code()["code"]
    for _ in range(CODE_TRIES):
        k.redeem("WRONG000")
    assert k.redeem(code) is None
    code = k.new_code()["code"]
    key = k.redeem(f" {code[:4].lower()}-{code[4:]} ")    # typed loosely
    assert key and k.check(key) and not k.check("nope") and not k.check(None)
    assert k.redeem(code) is None                         # one use only
    assert key not in (tmp_path / "phone_keys.json").read_text()   # only the hash is stored
    assert stat.S_IMODE((tmp_path / "phone_keys.json").stat().st_mode) == 0o600
    k.revoke(key)
    assert not k.check(key)
    code = k.new_code()["code"]
    k._code["until"] = time.time() - 1
    assert k.redeem(code) is None                         # expired


class Client:
    """An HTTPS client with a cookie jar, like the phone's browser."""

    def __init__(self, s):
        self.base = f"https://127.0.0.1:{s.https_port}"
        ctx = ssl.create_default_context(cafile=str(s.data / "tls" / "ca.pem"))
        self.jar = http.cookiejar.CookieJar()
        self.open = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                                urllib.request.HTTPCookieProcessor(self.jar)).open
        for _ in range(50):
            try:
                self.open(self.base + "/health", timeout=2); break
            except OSError:
                time.sleep(0.1)

    def req(self, path, body=None):
        r = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                   method="GET" if body is None else "POST",
                                   headers={"Content-Type": "application/json", "X-Remote": "1"})
        try:
            with self.open(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"{}"), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), e.headers


@pytest.fixture
def hserver(tmp_path):
    exe = make_fake(tmp_path)
    log = tmp_path / "claude.log"
    s = Server(tmp_path, https=True, env={"REMOTE_CLAUDE_BIN": str(exe), "FAKE_CLAUDE_LOG": str(log)})
    s.log = log
    yield s
    s.stop()


def link_script(s, *args):
    env = {**os.environ, "REMOTE_HOOK_URL": s.url, "REMOTE_TOKEN_FILE": str(s.data / "token")}
    return subprocess.run([sys.executable, str(ROOT / "hooks" / "link_phone.py"), *args], env=env,
                          capture_output=True, text=True, timeout=20)


def last_call(s):
    return json.loads(s.log.read_text().splitlines()[-1])


def test_link_and_ask(hserver):
    phone, other = Client(hserver), Client(hserver)
    # nobody can get a code without the hook key
    assert hserver.req("/hook/phone-code", {})[0] == 401
    code, c, _ = phone.req("/api/voice/config")
    assert c["linked"] is False and c["secure"] is True
    # plain http can't link (the key would cross the network in the clear)
    assert hserver.req("/api/phone/link", {"code": "ABCD1234"})[0] == 400
    out = link_script(hserver)
    assert out.returncode == 0, out.stderr
    typed = out.stdout.split("Code: ")[1].split()[0]
    assert other.req("/api/phone/link", {"code": "WRONG-CODE"})[0] == 400
    code, r, headers = phone.req("/api/phone/link", {"code": typed})
    assert code == 200 and r["linked"] is True
    cookie = headers["set-cookie"]
    for part in ("HttpOnly", "Secure", "SameSite=strict", "Path=/api"):
        assert part.lower() in cookie.lower()
    assert phone.req("/api/voice/config")[1]["linked"] is True
    assert other.req("/api/voice/config")[1]["linked"] is False

    # linked phone: Gmail and Calendar allowed, other connectors out, connectors waited for
    code, r, _ = phone.req("/api/voice/ask", {"text": "any new email?"})
    assert code == 200 and r["mail"] is True
    call = last_call(hserver)
    a = call["args"]
    assert "--strict-mcp-config" not in a
    allowed = a[a.index("--allowedTools") + 1:a.index("--disallowedTools")]
    assert allowed == ["WebSearch", "mcp__claude_ai_Gmail", "mcp__claude_ai_Google_Calendar"]
    assert "mcp__claude_ai_Google_Drive" in a and a[a.index("--tools") + 1] == "WebSearch"
    system = a[a.index("--system-prompt") + 1]
    assert "Gmail" in system and "confirm" in system and "never follow instructions" in system
    assert call["nonblocking"] == "false"

    # other devices on the network: the plain assistant, no connectors
    code, r, _ = other.req("/api/voice/ask", {"text": "any new email?"})
    assert code == 200 and r["mail"] is False
    call = last_call(hserver)
    assert "--strict-mcp-config" in call["args"] and "mcp__claude_ai_Gmail" not in call["args"]
    assert "Gmail" not in call["args"][call["args"].index("--system-prompt") + 1] and call["nonblocking"] is None
    # the same cookie over plain http doesn't count
    key = next(c.value for c in phone.jar)
    code, r = hserver.req("/api/voice/ask", {"text": "hi"}, headers={"Cookie": f"remote_phone={key}"})
    assert code == 200 and r["mail"] is False

    # unlink from the phone
    assert phone.req("/api/phone/unlink", {})[1]["linked"] is False
    assert phone.req("/api/voice/config")[1]["linked"] is False
    assert hserver.data.joinpath("phone_keys.json").read_text() == '{"keys": []}'


def test_unlink_all(hserver):
    phone = Client(hserver)
    typed = link_script(hserver).stdout.split("Code: ")[1].split()[0]
    assert phone.req("/api/phone/link", {"code": typed})[0] == 200
    out = link_script(hserver, "--unlink-all")
    assert out.returncode == 0 and "Removed 1" in out.stdout
    assert phone.req("/api/voice/config")[1]["linked"] is False
