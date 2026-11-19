"""Voice: settings, wake word file, questions (with a fake Claude Code), HTTPS certificates."""
import json
import ssl
import stat
import time
import urllib.request

import pytest
from cryptography import x509

from conftest import Server
from server import tls
from server.voice import Voice

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
prompt = sys.stdin.read()
args = sys.argv[1:]
log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"args": args, "prompt": prompt, "config": os.environ.get("CLAUDE_CONFIG_DIR"),
                            "cwd": os.getcwd(), "anthropic": [k for k in os.environ if k.startswith("ANTHROPIC_")],
                            "nonblocking": os.environ.get("MCP_CONNECTION_NONBLOCKING")}) + "\n")
if "LOGIN" in prompt:
    print(json.dumps({"is_error": True, "result": "Not logged in · Please run /login"}))
elif "BROKEN" in prompt:
    print("not json")
else:
    print(json.dumps({"is_error": False, "result": "It is sunny and 18 degrees."}))
'''


def make_fake(tmp_path):
    exe = tmp_path / "claude"
    exe.write_text(FAKE_CLAUDE)
    exe.chmod(0o755)
    creds = tmp_path / "data" / "claude"
    creds.mkdir(parents=True)
    (creds / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "a", "refreshToken": "r", "expiresAt": int((time.time() + 86400) * 1000), "scopes": ["user:inference"]}}))
    return exe


@pytest.fixture
def vserver(tmp_path):
    exe = make_fake(tmp_path)
    log = tmp_path / "claude.log"
    s = Server(tmp_path, env={"REMOTE_CLAUDE_BIN": str(exe), "FAKE_CLAUDE_LOG": str(log), "ANTHROPIC_API_KEY": "x"})
    s.log = log
    yield s
    s.stop()


def test_config_defaults_and_update(vserver):
    code, c = vserver.req("/api/voice/config")
    assert code == 200 and c["access_key"] == "" and c["keyword"] == "Jarvis" and c["custom"] is False
    assert "Hey Google" in c["built_in"] and c["claude_ready"] is True
    code, c = vserver.req("/api/voice/config", {"access_key": "  abcDEF123+/==  ", "keyword": "Computer", "sensitivity": 2})
    assert code == 200 and c["access_key"] == "abcDEF123+/==" and c["keyword"] == "Computer" and c["sensitivity"] == 0.95
    assert stat.S_IMODE((vserver.data / "voice.json").stat().st_mode) == 0o600
    assert vserver.req("/api/voice/config", {"access_key": "<script>"})[0] == 400
    assert vserver.req("/api/voice/config", {"keyword": "Hey Claude"})[0] == 400
    assert vserver.req("/api/voice/config", {"keyword": "custom"})[0] == 400       # no file yet
    # POST needs the X-Remote header like every other change
    r = urllib.request.Request(vserver.url + "/api/voice/config", data=b"{}", method="POST",
                               headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(r)
    assert e.value.code == 403


def upload(server, raw, label="Hey Claude"):
    r = urllib.request.Request(server.url + "/api/voice/keyword?label=" + label.replace(" ", "%20"), data=raw, method="POST",
                               headers={"X-Remote": "1", "Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_custom_keyword(vserver):
    assert upload(vserver, b"x")[0] == 400
    assert upload(vserver, b"x" * (65 * 1024))[0] == 413
    code, c = upload(vserver, b"P" * 3000)
    assert code == 200 and c["custom"] and c["keyword"] == "custom" and c["custom_label"] == "Hey Claude" and c["version"] > 0
    with urllib.request.urlopen(vserver.url + "/api/voice/keyword.ppn") as r:
        assert r.read() == b"P" * 3000
    code, c = vserver.req("/api/voice/keyword/delete", {})
    assert c["custom"] is False and c["keyword"] == "Jarvis"


def test_ask(vserver):
    code, r = vserver.req("/api/voice/ask", {"text": "  what's the   weather ", "history": [{"q": "hi", "a": "Hello!"}]})
    assert code == 200 and r["answer"] == "It is sunny and 18 degrees." and r["mail"] is False
    call = json.loads(vserver.log.read_text().splitlines()[-1])
    a = call["args"]
    assert a[a.index("--tools") + 1] == "WebSearch" and "--no-session-persistence" in a and "--strict-mcp-config" in a
    assert "Local time now" in a[a.index("--system-prompt") + 1]
    assert call["prompt"].endswith("The user now says: what's the weather") and "User: hi\nYou: Hello!" in call["prompt"]
    assert call["config"] == str(vserver.data / "claude") and call["anthropic"] == []
    assert list((__import__("pathlib").Path(call["cwd"])).iterdir()) == []      # empty folder


def test_ask_errors(vserver):
    assert vserver.req("/api/voice/ask", {"text": "   "})[0] == 400
    assert vserver.req("/api/voice/ask", {"text": "x" * 700})[0] == 400
    code, r = vserver.req("/api/voice/ask", {"text": "LOGIN"})
    assert code == 503 and "signed in" in r["error"]
    code, r = vserver.req("/api/voice/ask", {"text": "BROKEN"})
    assert code == 502


def test_ask_needs_login(tmp_path):
    exe = make_fake(tmp_path)
    (tmp_path / "data" / "claude" / ".credentials.json").unlink()
    s = Server(tmp_path, env={"REMOTE_CLAUDE_BIN": str(exe)})
    try:
        code, r = s.req("/api/voice/ask", {"text": "hello"})
        assert code == 503 and "signed in" in r["error"]
    finally:
        s.stop()


def test_prompt_history_is_capped():
    hist = [{"q": f"q{i}", "a": f"a{i}"} for i in range(10)] + [{"q": "", "a": "x"}, "junk"]
    p = Voice.prompt("now?", hist)
    assert "q3" not in p and "q4" in p and "q9" in p and p.endswith("The user now says: now?")
    assert Voice.prompt("hi", None) == "hi"


def test_vendor_files_and_csp(vserver):
    with urllib.request.urlopen(vserver.url + "/vendor/porcupine-web-4.0.1.min.js") as r:
        assert "immutable" in r.headers["Cache-Control"] and r.read(20).startswith(b"var PorcupineWeb")
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(vserver.url + "/vendor/../server/app.py")
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(vserver.url + "/vendor/app.py")
    with urllib.request.urlopen(vserver.url + "/") as r:
        csp = r.headers["Content-Security-Policy"]
    assert "'wasm-unsafe-eval'" in csp and "worker-src 'self' blob:" in csp and "https://rest.picovoice.ai" in csp


# ---------- HTTPS ----------
def test_certificates(tmp_path):
    paths = tls.ensure(tmp_path, "192.168.1.50")
    ca = x509.load_pem_x509_certificate(open(paths["ca"], "rb").read())
    leaf = x509.load_pem_x509_certificate(open(paths["cert"], "rb").read())
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    nc = ca.extensions.get_extension_for_class(x509.NameConstraints).value
    assert {str(n.value) for n in nc.permitted_subtrees} == {"192.168.1.50/32", "127.0.0.1/32", "localhost"}
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert {str(i) for i in san.get_values_for_type(x509.IPAddress)} == {"192.168.1.50", "127.0.0.1"}
    assert leaf.issuer == ca.subject
    assert stat.S_IMODE((tmp_path / "tls" / "ca.key").stat().st_mode) == 0o600
    # same address: kept; new address: new authority
    assert tls.ensure(tmp_path, "192.168.1.50") == paths
    assert open(paths["ca"], "rb").read() == ca.public_bytes(__import__("cryptography.hazmat.primitives.serialization", fromlist=["x"]).Encoding.PEM)
    tls.ensure(tmp_path, "192.168.1.51")
    ca2 = x509.load_pem_x509_certificate(open(paths["ca"], "rb").read())
    assert ca2.serial_number != ca.serial_number
    assert x509.load_der_x509_certificate(tls.ca_der(tmp_path)).subject == ca2.subject


def test_https_server(tmp_path):
    s = Server(tmp_path, https=True)
    try:
        ctx = ssl.create_default_context(cafile=str(s.data / "tls" / "ca.pem"))
        base = f"https://127.0.0.1:{s.https_port}"
        for _ in range(50):
            try:
                with urllib.request.urlopen(base + "/health", context=ctx, timeout=2) as r:
                    assert json.loads(r.read()) == {"ok": True}
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("HTTPS didn't answer")
        with urllib.request.urlopen(s.url + "/ca.crt") as r:
            assert r.headers["Content-Type"] == "application/x-x509-ca-cert"
            assert x509.load_der_x509_certificate(r.read()).subject.rfc4514_string().startswith("O=Remote")
        code, links = s.req("/api/links")
        assert links["secure_url"] == f"https://192.168.1.50:{s.https_port}"
        # plain http keeps working (the hooks use it)
        assert s.req("/api/state")[0] == 200
    finally:
        s.stop()


def test_no_https_no_ca(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(server.url + "/ca.crt")
    assert e.value.code == 404
    assert server.req("/api/links")[1]["secure_url"] == ""
