"""Browser checks at the Moto G5s sizes: landscape 640x360 (main use) and portrait 360x640."""
import json
import threading
import time
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

SHOTS = Path(__file__).parent / "shots"
ART = ("data:image/svg+xml," + "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 10'%3E%3Cdefs%3E%3ClinearGradient id='g'"
       " x2='1' y2='1'%3E%3Cstop stop-color='%23ff6a3d'/%3E%3Cstop offset='1' stop-color='%236a3dff'/%3E%3C/linearGradient%3E%3C/defs%3E"
       "%3Crect width='10' height='10' fill='url(%23g)'/%3E%3C/svg%3E")
PLAYER = {"connected": True, "active": True, "playing": True, "title": "Midnight City", "artist": "M83",
          "album": "Hurry Up", "image": ART, "thumb": ART, "progress": 90000, "duration": 243000, "shuffle": True,
          "repeat": "context", "device": {"id": "d1", "name": "Living room", "type": "Speaker", "volume": 35, "can_volume": True}}

SIZES = {"landscape": (640, 360), "portrait": (360, 640)}


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def seed(server):
    for sid, cwd, prompt in [("a", "/home/r/Documents/claude/daily-hub", "Add the evening wrap-up tab"),
                             ("b", "/home/r/Documents/claude/gym", "Fix the history edit bug"),
                             ("c", "/home/r/Documents/claude/remote", "")]:
        server.run_hook("event", {"session_id": sid, "cwd": cwd, "hook_event_name": "SessionStart"})
        if prompt:
            server.run_hook("event", {"session_id": sid, "cwd": cwd, "hook_event_name": "UserPromptSubmit", "prompt": prompt})
    server.run_hook("event", {"session_id": "a", "hook_event_name": "PreToolUse", "tool_name": "Edit",
                              "tool_input": {"file_path": "/x/apps/wrapup.html", "old_string": "a", "new_string": "b"}})
    server.run_hook("event", {"session_id": "b", "hook_event_name": "Stop"})


def open_page(browser, server, size, scale=1):
    w, h = SIZES[size]
    ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=scale, is_mobile=True, has_touch=True)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.route("**/api/spotify/player", lambda r: r.fulfill(json=PLAYER))
    page.route("**/api/spotify/*", lambda r: r.fulfill(json={"ok": True}) if r.request.method == "POST" else r.fallback())
    page.goto(server.url + "/pair?t=" + server.token)
    page.click("#start")
    return ctx, page, errors


def no_overflow(page):
    return page.evaluate("""() => {
        const W = innerWidth, H = innerHeight, bad = [];
        if (document.documentElement.scrollWidth > W) bad.push('page scrolls sideways');
        for (const el of document.querySelectorAll('.card, .ctl, .mode, #devBtn, .btn')) {
          const r = el.getBoundingClientRect();
          if (r.width && (r.right > W + 0.5 || r.bottom > H + 0.5 || r.left < -0.5)) bad.push((el.id || el.className) + ' ' + JSON.stringify(r));
        }
        return bad;
    }""")


@pytest.mark.parametrize("size", ["landscape", "portrait"])
def test_dashboard(browser, server, size):
    seed(server)
    ctx, page, errors = open_page(browser, server, size, scale=2)
    expect(page.locator(".sess")).to_have_count(3)
    expect(page.locator(".sess.working .act").first).to_have_text("Edit wrapup.html")
    expect(page.locator("#trTitle")).to_have_text("Midnight City")
    expect(page.locator("#devName")).to_have_text("Living room")
    assert no_overflow(page) == []
    SHOTS.mkdir(exist_ok=True)
    page.screenshot(path=str(SHOTS / f"dashboard-{size}.png"))
    assert errors == []
    ctx.close()


@pytest.mark.parametrize("size", ["landscape", "portrait"])
def test_allow_from_phone(browser, server, size):
    seed(server)
    server.req("/api/settings", {"away": True})
    ctx, page, errors = open_page(browser, server, size, scale=2)
    ev = {"session_id": "a", "cwd": "/home/r/Documents/claude/daily-hub", "hook_event_name": "PermissionRequest",
          "tool_name": "Bash", "tool_input": {"command": "docker-compose restart daily-hub", "description": "Restart the hub"}}
    p, data = server.hook("permission", ev)
    out = {}
    t = threading.Thread(target=lambda: out.update(r=p.communicate(data, timeout=30)))
    t.start()
    expect(page.locator("#ask")).to_be_visible()
    expect(page.locator("#askTitle")).to_have_text("Restart the hub")
    expect(page.locator("#askDetail")).to_contain_text("docker-compose restart daily-hub")
    expect(page.locator(".sess.permission")).to_have_count(1)
    assert no_overflow(page) == []
    page.screenshot(path=str(SHOTS / f"permission-{size}.png"))
    page.click("#allowBtn")
    t.join(15)
    assert json.loads(out["r"][0])["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    expect(page.locator("#ask")).to_be_hidden()
    expect(page.locator(".sess.permission")).to_have_count(0)
    assert errors == []
    ctx.close()


def test_deny_and_mode_toggle(browser, server):
    seed(server)
    server.req("/api/settings", {"away": True})
    ctx, page, errors = open_page(browser, server, "landscape")
    ev = {"session_id": "c", "hook_event_name": "PermissionRequest", "tool_name": "Write",
          "tool_input": {"file_path": "/home/r/.bashrc", "content": "alias x=y"}}
    p, data = server.hook("permission", ev)
    out = {}
    t = threading.Thread(target=lambda: out.update(r=p.communicate(data, timeout=30)))
    t.start()
    expect(page.locator("#askTitle")).to_have_text("Write .bashrc")
    page.click("#denyBtn")
    t.join(15)
    assert json.loads(out["r"][0])["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    # switch to "At desk": prompts stay on the computer
    page.click("#mode")
    expect(page.locator("#modeLbl")).to_have_text("At desk")
    code, o = server.run_hook("permission", {**ev, "session_id": "a"})
    assert o == ""
    expect(page.locator(".sess.waiting")).to_have_count(1)
    expect(page.locator("#ask")).to_be_hidden()
    assert errors == []
    ctx.close()


@pytest.mark.parametrize("size", ["landscape", "portrait"])
def test_sheets(browser, server, size):
    ctx, page, errors = open_page(browser, server, size, scale=2)
    page.route("**/api/spotify/devices", lambda r: r.fulfill(json={"devices": [
        {"id": "d1", "name": "Living room", "type": "Speaker", "active": True, "volume": 35},
        {"id": "d2", "name": "Rafael's PC", "type": "Computer", "active": False, "volume": 80}]}))
    page.route("**/api/spotify/playlists", lambda r: r.fulfill(json={"playlists": [
        {"uri": "spotify:playlist:1", "name": "Deep Focus", "image": ART, "owner": "Spotify"},
        {"uri": "spotify:playlist:2", "name": "Friday mix", "image": "", "owner": "me"}]}))
    expect(page.locator(".empty")).to_be_visible()
    page.click("#devBtn")
    expect(page.locator("[data-dev]")).to_have_count(2)
    page.wait_for_timeout(500)   # let the sheet finish sliding in
    page.screenshot(path=str(SHOTS / f"devices-{size}.png"))
    with page.expect_request(lambda r: r.url.endswith("/api/spotify/play") and r.method == "POST") as req:
        page.click("[data-pl='spotify:playlist:1']")
    assert json.loads(req.value.post_data)["context_uri"] == "spotify:playlist:1"
    page.click("#settingsBtn")
    expect(page.locator("[data-pref=sound] .switch.on")).to_have_count(1)
    page.click("[data-pref=doneChime]")
    assert page.evaluate("JSON.parse(localStorage.getItem('remote.prefs')).doneChime") is False
    page.click("[data-wait='300']")
    expect(page.locator("[data-wait='300'].on")).to_have_count(1)
    page.wait_for_timeout(500)
    page.screenshot(path=str(SHOTS / f"settings-{size}.png"))
    assert errors == []
    ctx.close()


def test_not_paired(browser, server):
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(server.url + "/")
    expect(page.locator("h1")).to_have_text("Not paired")
    ctx.close()


def test_setup_page(browser, server):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.goto(server.url + "/pair?t=" + server.token)
    page.goto(server.url + "/setup")
    expect(page.locator("#pairUrl")).to_contain_text("http://192.168.1.50:8796/pair?t=")
    expect(page.locator("#redirect")).to_have_text(f"http://127.0.0.1:{server.port}/spotify/callback")
    page.fill("#cid", "abc123def456")
    page.click("#cidForm button")
    expect(page.locator("#connect")).to_be_visible()
    page.screenshot(path=str(SHOTS / "setup.png"))
    ctx.close()
