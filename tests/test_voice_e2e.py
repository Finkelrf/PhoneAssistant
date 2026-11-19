"""Voice in the browser, with fake speech recognition, speech output and wake word (Chromium has no real ones here)."""
import json

import pytest
from playwright.sync_api import expect

from test_e2e import SHOTS, SIZES, browser, open_page  # noqa: F401  (browser is a fixture)

FAKES = """
try { const p = JSON.parse(localStorage.getItem("remote.prefs") || "{}");
      if (!window.__computerVoice) p.voiceOut = "phone"; localStorage.setItem("remote.prefs", JSON.stringify(p)); } catch (e) {}
window.__spoken = []; window.__heard = [%s]; window.__starts = 0; window.__wake = {listening: false, detect: null};
class FakeRecognition {
  start() {
    const text = window.__heard.length ? window.__heard.shift() : "";
    window.__starts++;
    setTimeout(() => {
      if (text) this.onresult({resultIndex: 0, results: [Object.assign([{transcript: text}], {isFinal: true})]});
      else if (this.onerror) this.onerror({error: "no-speech"});
      this.onend && this.onend();
    }, 250);
  }
  abort() { this.onend && this.onend(); }
}
window.__voiceFakes = {
  Recognition: FakeRecognition,
  Utterance: class { constructor(t) { this.text = t; } },
  speech: { speaking: false, getVoices: () => [], cancel() {},
            speak(u) { window.__spoken.push(u.text); setTimeout(() => u.onend && u.onend(), 400); } },
  wake: async (onDetect) => { window.__wake.detect = onDetect; return {
    listen: async () => { window.__wake.listening = true; },
    pause: async () => { window.__wake.listening = false; },
    stop: async () => { window.__wake.listening = false; } }; },
};
"""

CONFIG = {"engine": "porcupine", "access_key": "", "keyword": "Jarvis", "custom": False, "custom_label": "Hey Claude", "sensitivity": 0.6,
          "built_in": ["Jarvis", "Computer", "Alexa"], "claude_ready": True, "version": 0,
          "oww_model": "hey_jarvis", "oww_label": "Hey Jarvis", "oww_custom": False, "oww_custom_label": "Hey Claude", "oww_ready": True,
          "oww_models": [{"id": "hey_jarvis", "label": "Hey Jarvis"}, {"id": "alexa", "label": "Alexa"}]}


def setup(page, heard, answers, config=None, calls=None, music=None):
    page.add_init_script(FAKES % ",".join(json.dumps(h) for h in heard))
    page.route("**/api/voice/config", lambda r: r.fulfill(json=config or CONFIG))
    page.route("**/api/links", lambda r: r.fulfill(json={"public_url": "http://192.168.1.50:8796", "secure_url": "https://192.168.1.50:8797"}))
    queue = list(answers)

    def ask(route):
        if calls is not None:
            calls.append(json.loads(route.request.post_data))
        status, body = queue.pop(0)
        route.fulfill(status=status, json=body)
    page.route("**/api/voice/ask", ask)
    if music is not None:
        page.route("**/api/spotify/pause", lambda r: (music.append("pause"), r.fulfill(json={"ok": True})))
        page.route("**/api/spotify/play", lambda r: (music.append("play"), r.fulfill(json={"ok": True})))


@pytest.mark.parametrize("size", ["landscape", "portrait"])
def test_tap_mic_ask_and_answer(browser, server, size):
    calls, music = [], []
    answer = "It's 18 degrees and sunny in Amsterdam, with a light breeze from the west this afternoon."
    ctx, page, errors = open_page(browser, server, size, scale=2,
                                  before=lambda p: setup(p, ["what's the weather"], [(200, {"answer": answer})], calls=calls, music=music))
    page.wait_for_timeout(1200)            # Spotify state arrives (it is playing)
    page.click("#voiceBtn")
    expect(page.locator("#voice")).to_be_visible()
    expect(page.locator("#voiceA")).to_have_text(answer, timeout=4000)
    expect(page.locator("#voiceQ")).to_have_text("“what's the weather”")
    page.screenshot(path=str(SHOTS / f"voice-{size}.png"))
    vw, vh = page.viewport_size["width"], page.viewport_size["height"]
    box = page.locator(".voice-card").bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= vw
    assert box["y"] < 20 and box["height"] < vh * 0.35          # a slim bar at the top, not a full-screen box
    expect(page.locator("#voice")).to_be_hidden(timeout=8000)
    assert page.evaluate("window.__spoken") == [answer]
    assert calls == [{"text": "what's the weather", "history": []}]
    assert music == ["pause", "play"]
    assert errors == []
    ctx.close()


def test_wake_word_follow_up_and_history(browser, server):
    calls = []
    cfg = {**CONFIG, "access_key": "KEY123"}
    ctx, page, errors = open_page(browser, server, "landscape", before=lambda p: setup(
        p, ["set a reminder", "at five", "and the weather"],
        [(200, {"answer": "Sure, for what time?"}), (200, {"answer": "Done, five o'clock."}), (200, {"answer": "Sunny."})],
        config=cfg, calls=calls))
    expect(page.locator("#wakeDot")).to_be_visible()
    assert page.evaluate("window.__wake.listening") is True
    page.evaluate("window.__wake.detect({label: 'Jarvis'})")
    expect(page.locator("#voiceA")).to_have_text("Done, five o'clock.", timeout=6000)
    assert page.evaluate("window.__starts") == 2          # Claude asked back: listened again without the wake word
    expect(page.locator("#voice")).to_be_hidden(timeout=8000)
    assert page.evaluate("window.__wake.listening") is True
    assert calls[1]["history"] == [{"q": "set a reminder", "a": "Sure, for what time?"}]
    page.evaluate("window.__wake.detect({label: 'Jarvis'})")
    expect(page.locator("#voiceA")).to_have_text("Sunny.", timeout=6000)
    assert len(calls[2]["history"]) == 2
    assert errors == []
    ctx.close()


def test_error_is_shown_and_said(browser, server):
    ctx, page, errors = open_page(browser, server, "landscape", before=lambda p: setup(
        p, ["hello"], [(503, {"error": "The Remote server isn't signed in to Claude. See the README."})]))
    page.click("#voiceBtn")
    expect(page.locator("#voice")).to_have_attribute("data-state", "error", timeout=4000)
    expect(page.locator("#voiceA")).to_contain_text("isn't signed in")
    page.screenshot(path=str(SHOTS / "voice-error-landscape.png"))
    page.wait_for_timeout(600)
    assert "isn't signed in" in page.evaluate("window.__spoken")[0]
    ctx.close()


def test_nothing_heard_closes(browser, server):
    ctx, page, errors = open_page(browser, server, "landscape", before=lambda p: setup(p, [], []))
    page.click("#voiceBtn")
    expect(page.locator("#voice")).to_be_hidden(timeout=3000)
    assert page.evaluate("window.__spoken") == []
    ctx.close()


@pytest.mark.parametrize("size", ["landscape", "portrait"])
def test_voice_settings_sheet(browser, server, size):
    cfg = {**CONFIG, "access_key": "KEY123", "custom": True, "keyword": "custom", "version": 5}
    ctx, page, errors = open_page(browser, server, size, scale=2, before=lambda p: setup(p, [], [], config=cfg))
    page.click("#settingsBtn")
    expect(page.locator("#voiceSub")).to_contain_text("Listening for")
    page.click("#voiceSettingsBtn")
    expect(page.locator("#vKeyword")).to_have_value("custom")
    page.wait_for_timeout(400)
    page.screenshot(path=str(SHOTS / f"voice-settings-{size}.png"))
    bad = page.evaluate("""() => { const W = innerWidth, bad = [];
        for (const el of document.querySelectorAll('.sheet .row, .sheet select, .sheet .chip, .sheet input'))
          { const r = el.getBoundingClientRect(); if (r.width && (r.right > W - 2 || r.left < 0)) bad.push(el.id || el.className); }
        return bad; }""")
    assert bad == []
    page.click("[data-pref=wakeWord]")
    page.wait_for_timeout(200)
    assert page.evaluate("window.__wake.listening") is False
    expect(page.locator("#wakeDot")).to_be_hidden()
    assert errors == []
    ctx.close()


def test_plain_http_explains_https(browser, server):
    """On the LAN address over http the page isn't a secure context: the mic explains what to do."""
    ctx = browser.new_context(viewport={"width": 640, "height": 360})
    page = ctx.new_page()
    page.route("**/api/spotify/player", lambda r: r.fulfill(json={"connected": False}))
    page.add_init_script("Object.defineProperty(window, 'isSecureContext', {get: () => false});")
    page.route("**/api/links", lambda r: r.fulfill(json={"public_url": "http://192.168.1.50:8796", "secure_url": "https://192.168.1.50:8797"}))
    page.goto(server.url + "/")
    page.click("#start")
    expect(page.locator("#voiceBtn")).to_have_class("icon-btn mic off")
    page.click("#voiceBtn")
    expect(page.locator(".sheet")).to_contain_text("secure address")
    expect(page.locator(".sheet a[href='https://192.168.1.50:8797/']")).to_be_visible()
    ctx.close()


def test_real_porcupine_loads_under_csp(browser, server):
    """The real Porcupine bundle starts its WebAssembly worker under our Content Security Policy. A made-up AccessKey
    is refused (or Picovoice can't be reached here); what must not happen is a CSP block."""
    ctx = browser.new_context()
    page = ctx.new_page()
    console = []
    page.on("console", lambda m: console.append(m.text))
    page.route("**/api/spotify/player", lambda r: r.fulfill(json={"connected": False}))
    page.goto(server.url + "/")
    msg = page.evaluate("""async () => {
        const load = (src) => new Promise((ok, bad) => { const s = document.createElement('script'); s.src = src; s.onload = ok; s.onerror = bad; document.head.appendChild(s); });
        await load('/vendor/web-voice-processor-4.0.10.min.js');
        await load('/vendor/porcupine-web-4.0.1.min.js');
        try {
          await PorcupineWeb.PorcupineWorker.create('not-a-real-key', [{builtin: 'Jarvis'}], () => {},
            {publicPath: '/vendor/porcupine_params-4.0.pv', customWritePath: 'test_params', forceWrite: true});
          return 'created';
        } catch (e) { return String(e && e.message || e); }
    }""")
    assert typeof_ok(msg), msg
    assert not [c for c in console if "Content Security Policy" in c], console
    ctx.close()


def typeof_ok(msg):
    return isinstance(msg, str) and "Content Security Policy" not in msg and "wasm" not in msg.lower()


@pytest.mark.parametrize("size", ["landscape", "portrait", "computer"])
def test_setup_voice_section(browser, server, size):
    w, h = SIZES.get(size, (1280, 900))
    ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=2)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(server.url + "/setup#voice")
    page.fill("#pvKey", "KEY123")
    page.click("#keyForm button")
    expect(page.locator("#pvState")).to_contain_text("AccessKey saved")
    page.locator("#voice").scroll_into_view_if_needed()
    page.locator("#voice").screenshot(path=str(SHOTS / f"setup-voice-{size}.png"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert errors == []
    ctx.close()


@pytest.mark.parametrize("size", ["landscape", "portrait"])
def test_voice_settings_openwakeword(browser, server, size):
    cfg = {**CONFIG, "engine": "openwakeword"}
    ctx, page, errors = open_page(browser, server, size, scale=2, before=lambda p: setup(p, [], [], config=cfg))
    expect(page.locator("#wakeDot")).to_be_visible()
    page.click("#settingsBtn")
    page.click("#voiceSettingsBtn")
    expect(page.locator("#vKeyword")).to_have_value("hey_jarvis")
    expect(page.locator("[data-engine=openwakeword]")).to_have_class("on")
    page.wait_for_timeout(400)
    page.screenshot(path=str(SHOTS / f"voice-settings-oww-{size}.png"))
    bad = page.evaluate("""() => { const W = innerWidth, bad = [];
        for (const el of document.querySelectorAll('.sheet .row, .sheet select, .sheet .chip, .sheet input, .sheet .seg'))
          { const r = el.getBoundingClientRect(); if (r.width && (r.right > W - 2 || r.left < 0)) bad.push(el.id || el.className); }
        return bad; }""")
    assert bad == []
    assert errors == []
    ctx.close()


def test_switch_engine(browser, server):
    """Switching engine saves it on the server and redraws the sheet."""
    ctx, page, errors = open_page(browser, server, "landscape", before=lambda p: p.add_init_script(FAKES % ""))
    page.click("#settingsBtn")
    page.click("#voiceSettingsBtn")
    expect(page.locator("[data-engine=openwakeword]")).to_have_class("on")
    page.click("[data-engine=porcupine]")
    expect(page.locator("[data-engine=porcupine]")).to_have_class("on")
    assert server.req("/api/voice/config")[1]["engine"] == "porcupine"
    expect(page.locator("#vKeyword")).to_be_disabled()            # no AccessKey yet
    page.click("[data-engine=openwakeword]")
    expect(page.locator("#vKeyword")).to_be_enabled()
    assert server.req("/api/voice/config")[1]["engine"] == "openwakeword"
    assert errors == []
    ctx.close()



def test_computer_voice(browser, server):
    """Default: the answer is spoken by Piper on the server and played through the page's audio."""
    speak = []
    ctx, page, errors = open_page(browser, server, "landscape", before=lambda p: (
        p.add_init_script("window.__computerVoice = true;"),
        setup(p, ["hello"], [(200, {"answer": "Hello there."})]),
        p.on("request", lambda r: speak.append(json.loads(r.post_data)) if r.url.endswith("/api/voice/speak") else None)))
    page.click("#voiceBtn")
    expect(page.locator("#voiceA")).to_have_text("Hello there.", timeout=6000)
    expect(page.locator("#voice")).to_have_attribute("data-state", "done", timeout=15000)
    assert speak == [{"text": "Hello there.", "lang": "en-US"}]
    assert page.evaluate("window.__spoken") == []           # not the phone's voice
    assert errors == []
    ctx.close()


def test_computer_voice_falls_back_to_phone(browser, server):
    ctx, page, errors = open_page(browser, server, "landscape", before=lambda p: (
        p.add_init_script("window.__computerVoice = true;"),
        setup(p, ["hello"], [(200, {"answer": "Hello there."})]),
        p.route("**/api/voice/speak", lambda r: r.fulfill(status=400, json={"error": "No computer voice for that language"}))))
    page.click("#voiceBtn")
    expect(page.locator("#toast")).to_contain_text("Using the phone's voice", timeout=6000)
    page.wait_for_timeout(600)
    assert page.evaluate("window.__spoken") == ["Hello there."]
    ctx.close()


@pytest.mark.parametrize("size", ["landscape", "portrait"])
def test_email_calendar_link(browser, server, size):
    """Voice settings: type the code from the computer, then the phone shows it's linked and can unlink."""
    state = {"linked": False}
    sent = []

    def before(p):
        setup(p, [], [], config={**CONFIG, "secure": True, "linked": False})
        p.unroute("**/api/voice/config")
        p.route("**/api/voice/config", lambda r: r.fulfill(json={**CONFIG, "secure": True, "linked": state["linked"]}))

        def link(r):
            body = json.loads(r.request.post_data)
            sent.append(body)
            if body["code"] != "ABCD-2345":
                return r.fulfill(status=400, json={"error": "That code is wrong or has expired. Make a new one on the computer."})
            state["linked"] = True
            r.fulfill(json={"linked": True})
        p.route("**/api/phone/link", link)
        p.route("**/api/phone/unlink", lambda r: (state.update(linked=False), r.fulfill(json={"linked": False})))

    ctx, page, errors = open_page(browser, server, size, scale=2, before=before)
    page.click("#settingsBtn")
    page.click("#voiceSettingsBtn")
    page.locator("#vCode").scroll_into_view_if_needed()
    expect(page.locator(".sheet")).to_contain_text("link_phone.py")
    page.fill("#vCode", "WRONG")
    page.click("#vLink")
    expect(page.locator("#toast")).to_contain_text("wrong or has expired")
    page.fill("#vCode", "ABCD-2345")
    page.screenshot(path=str(SHOTS / f"voice-link-{size}.png"))
    bad = page.evaluate("""() => { const W = innerWidth, bad = [];
        for (const el of document.querySelectorAll('.sheet .row, .sheet .chip, .sheet input, .sheet .hint'))
          { const r = el.getBoundingClientRect(); if (r.width && (r.right > W - 2 || r.left < 0)) bad.push(el.id || el.className); }
        return bad; }""")
    assert bad == []
    page.click("#vLink")
    expect(page.locator(".sheet")).to_contain_text("Linked on this phone")
    page.locator("#vUnlink").scroll_into_view_if_needed()
    page.screenshot(path=str(SHOTS / f"voice-linked-{size}.png"))
    page.click("#vUnlink")
    expect(page.locator("#vCode")).to_be_visible()
    assert [s["code"] for s in sent] == ["WRONG", "ABCD-2345"]
    assert errors == []
    ctx.close()


@pytest.mark.parametrize("size", ["landscape", "portrait"])
def test_train_wake_word(browser, server, size):
    """Settings > Train a new wake word: record yourself, start training, see progress, use the new word."""
    oww = {**CONFIG, "engine": "openwakeword", "secure": True}
    state = {"st": {"state": "idle", "samples": 0, "max_samples": 20}, "cfg": oww, "polls": 0}
    sent = []

    def status(r):
        if r.request.method == "POST":
            body = json.loads(r.request.post_data)
            sent.append(body)
            state["st"] = {"state": "running", "phrase": body["phrase"], "id": "0123abcd", "started": 0,
                           "step": "Making voice samples", "progress": 0.3, "samples": 2, "max_samples": 20}
            return r.fulfill(json=state["st"])
        state["polls"] += 1
        if state["st"]["state"] == "running" and state["polls"] > 3:
            state["st"] = {**state["st"], "state": "done", "progress": 1, "step": "Done", "samples": 0,
                           "result": {"recall": 0.93, "per_hour": 0.4, "hours": 2.1}}
            state["cfg"] = {**oww, "oww_models": oww["oww_models"] + [{"id": "t_0123abcd", "label": "Hey Claude", "trained": True,
                                                                     "result": state["st"]["result"]}]}
        r.fulfill(json=state["st"])

    def config(r):
        if r.request.method == "POST":
            state["cfg"] = {**state["cfg"], **json.loads(r.request.post_data), "oww_label": "Hey Claude"}
        r.fulfill(json=state["cfg"])

    def before(p):
        setup(p, [], [], config=oww)
        p.add_init_script("window.__voiceFakes.record = async (ms) => new Int16Array(16 * ms);")
        p.unroute("**/api/voice/config")
        p.route("**/api/voice/config", config)
        p.route("**/api/wake/train", status)
        p.route("**/api/wake/train/sample", lambda r: (state["st"].update(samples=state["st"]["samples"] + 1),
                                                       r.fulfill(json={"samples": state["st"]["samples"]})))

    ctx, page, errors = open_page(browser, server, size, scale=2, before=before)
    page.click("#settingsBtn")
    page.click("#voiceSettingsBtn")
    page.locator("#vTrain").scroll_into_view_if_needed()
    page.click("#vTrain")
    expect(page.locator("#tCount")).to_have_text("No recordings yet")
    page.fill("#tPhrase", "Hey Claude")
    page.click("#tRec")
    expect(page.locator("#tCount")).to_have_text("1 recording of you", timeout=5000)
    page.click("#tRec")
    expect(page.locator("#tCount")).to_have_text("2 recordings of you", timeout=5000)
    page.screenshot(path=str(SHOTS / f"voice-train-{size}.png"))
    bad = page.evaluate("""() => { const W = innerWidth, bad = [];
        for (const el of document.querySelectorAll('.sheet .row, .sheet .chip, .sheet input, .sheet .hint'))
          { const r = el.getBoundingClientRect(); if (r.width && (r.right > W - 2 || r.left < 0)) bad.push(el.id || el.className); }
        return bad; }""")
    assert bad == []
    page.click("#tStart")
    expect(page.locator("#tState")).to_contain_text("Making voice samples · 30%")
    assert sent == [{"phrase": "Hey Claude"}]
    page.locator("#tState").scroll_into_view_if_needed()
    page.screenshot(path=str(SHOTS / f"voice-training-{size}.png"))
    expect(page.locator("#tState")).to_contain_text("is ready", timeout=15000)
    expect(page.locator("#tState")).to_contain_text("Heard 93% of test clips, 0.4 false wakes per hour")
    page.locator("#tState [data-use]").click()
    expect(page.locator("#tList")).to_contain_text("Hey Claude · in use")
    page.locator("#tList").scroll_into_view_if_needed()
    page.screenshot(path=str(SHOTS / f"voice-trained-{size}.png"))
    assert state["cfg"]["oww_model"] == "t_0123abcd"
    assert errors == []
    ctx.close()
