import threading
import time

S1 = {"session_id": "s1", "cwd": "/home/me/projects/daily-hub"}


def wait_for(fn, t=5):
    end = time.time() + t
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_access(server):
    # the page is open on the home network: no pairing
    assert server.req("/api/state")[0] == 200
    # hooks still need the key from the token file
    assert server.req("/hook/event", {"x": 1})[0] == 401
    assert server.req("/hook/event", {"x": 1}, headers={"X-Remote-Token": "nope"})[0] == 401
    assert server.req("/hook/event", {"x": 1}, headers={"X-Remote-Token": server.token})[0] == 200
    # POST without the custom header is refused (a cross-site form or fetch can't add it without CORS)
    assert server.req("/api/settings", {"away": False}, headers={"X-Remote": "0"})[0] == 403
    # requests for other host names are refused (DNS rebinding)
    for host in ("evil.example", "evil.example:8796"):
        assert server.req("/api/state", headers={"Host": host})[0] == 421
    assert server.req("/api/state", headers={"Host": "192.168.1.50:8796"})[0] == 200
    assert server.req("/api/state", headers={"Host": "localhost"})[0] == 200


def test_session_lifecycle(server):
    assert server.run_hook("event", {**S1, "hook_event_name": "SessionStart"}) == (0, "")
    server.run_hook("event", {**S1, "hook_event_name": "UserPromptSubmit", "prompt": "fix the tests"})
    st = server.req("/api/state")[1]
    s = st["sessions"][0]
    assert s["name"] == "daily-hub" and s["state"] == "working" and s["prompt"] == "fix the tests"
    server.run_hook("event", {**S1, "hook_event_name": "PreToolUse", "tool_name": "Bash",
                              "tool_input": {"command": "pytest", "description": "Run tests"}})
    assert server.req("/api/state")[1]["sessions"][0]["activity"] == "Run tests"
    server.run_hook("event", {**S1, "hook_event_name": "Stop"})
    st = server.req("/api/state")[1]
    assert st["sessions"][0]["state"] == "idle" and st["alert"]["kind"] == "done"
    server.run_hook("event", {**S1, "hook_event_name": "Notification", "notification_type": "permission_prompt",
                              "message": "Claude needs your permission"})
    st = server.req("/api/state")[1]
    assert st["sessions"][0]["state"] == "waiting" and st["alert"]["kind"] == "attention"
    server.run_hook("event", {**S1, "hook_event_name": "SessionEnd", "reason": "exit"})
    assert server.req("/api/state")[1]["sessions"][0]["state"] == "ended"
    assert server.req("/api/sessions/s1/dismiss", {})[0] == 200
    assert server.req("/api/state")[1]["sessions"] == []


def _permission(server, decision):
    server.req("/api/settings", {"away": True})
    ev = {**S1, "hook_event_name": "PermissionRequest", "tool_name": "Bash",
          "tool_input": {"command": "rm -rf build", "description": "Clean build"}}
    p, data = server.hook("permission", ev)
    out = {}
    t = threading.Thread(target=lambda: out.update(r=p.communicate(data, timeout=20)))
    t.start()
    pend = wait_for(lambda: server.req("/api/state")[1]["pending"])
    assert pend[0]["title"] == "Clean build" and "rm -rf build" in pend[0]["detail"]
    assert server.req("/api/state")[1]["sessions"][0]["state"] == "permission"
    assert server.req(f"/api/requests/{pend[0]['id']}", {"decision": decision}) == (200, {"ok": True})
    t.join(20)
    return out["r"][0]


def test_permission_allow(server):
    import json
    out = json.loads(_permission(server, "allow"))
    assert out == {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
    st = server.req("/api/state")[1]
    assert st["pending"] == [] and st["sessions"][0]["activity"] == "Allowed from phone"


def test_permission_deny(server):
    import json
    d = json.loads(_permission(server, "deny"))["hookSpecificOutput"]["decision"]
    assert d["behavior"] == "deny" and d["message"]


def test_permission_answered_twice(server):
    _permission(server, "allow")
    assert server.req("/api/requests/whatever", {"decision": "allow"})[0] == 404
    assert server.req("/api/requests/whatever", {"decision": "maybe"})[0] == 400


def test_desk_mode_returns_at_once(server):
    assert server.req("/api/state")[1]["settings"]["away"] is False   # the default
    t = time.time()
    code, out = server.run_hook("permission", {**S1, "hook_event_name": "PermissionRequest", "tool_name": "Read",
                                               "tool_input": {"file_path": "/x/y.txt"}})
    assert time.time() - t < 3 and out == ""
    st = server.req("/api/state")[1]
    assert st["pending"] == [] and st["sessions"][0]["state"] == "waiting" and st["alert"]["kind"] == "attention"


def test_switching_to_desk_releases_waiting_hooks(server):
    server.req("/api/settings", {"away": True})
    ev = {**S1, "hook_event_name": "PermissionRequest", "tool_name": "Write", "tool_input": {"file_path": "/a", "content": "b"}}
    p, data = server.hook("permission", ev)
    out = {}
    t = threading.Thread(target=lambda: out.update(r=p.communicate(data, timeout=20)))
    t.start()
    wait_for(lambda: server.req("/api/state")[1]["pending"])
    server.req("/api/settings", {"away": False})
    t.join(10)
    assert out["r"][0] == "" and server.req("/api/state")[1]["pending"] == []


def test_killed_hook_clears_request(server):
    server.req("/api/settings", {"away": True})
    ev = {**S1, "hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "ls"}}
    p, data = server.hook("permission", ev)
    p.stdin.write(data); p.stdin.close()
    wait_for(lambda: server.req("/api/state")[1]["pending"])
    p.kill(); p.wait()          # Claude Code gave up on the hook (answered at the computer)
    wait_for(lambda: server.req("/api/state")[1]["pending"] == [], t=6)


def test_timeout_hands_back(server):
    server.req("/api/settings", {"wait": 15})
    assert server.req("/api/state")[1]["settings"]["wait"] == 15


def test_hook_fails_open_when_server_down(server):
    server.stop()
    t = time.time()
    code, out = server.run_hook("permission", {**S1, "hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {}})
    assert code == 0 and out == "" and time.time() - t < 5


def test_spotify_not_connected(server):
    assert server.req("/api/spotify/player")[1] == {"connected": False}
    code, st = server.req("/api/spotify/status")
    assert st["connected"] is False and st["phone_url"] == "http://192.168.1.50:8796/"
    assert server.req("/api/spotify/config", {"client_id": "bad id!"})[0] == 400
    assert server.req("/api/spotify/config", {"client_id": "abc123def456"})[1]["configured"] is True


def test_qr(server):
    import urllib.request
    assert urllib.request.urlopen(server.url + "/api/phone-qr.svg").read().startswith(b"<?xml")
