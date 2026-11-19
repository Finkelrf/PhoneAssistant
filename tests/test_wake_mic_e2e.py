"""The whole openWakeWord path with Chromium's fake microphone playing "hey jarvis" (tests/fixtures/mic_hey_jarvis.wav,
looped): getUserMedia, the 16 kHz worklet, the WebSocket, the server's detector, and the conversation starting.
Speech recognition and speech output are still fake. Its own file: the browser needs its own launch flags."""
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from test_voice_e2e import FAKES

MIC = Path(__file__).parent / "fixtures" / "mic_hey_jarvis.wav"


@pytest.fixture(scope="module")
def mic_browser():
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                                    f"--use-file-for-fake-audio-capture={MIC}", "--autoplay-policy=no-user-gesture-required"])
        yield b
        b.close()


def test_real_microphone_path(mic_browser, server):
    ctx = mic_browser.new_context(viewport={"width": 640, "height": 360}, permissions=["microphone"])
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.route("**/api/spotify/player", lambda r: r.fulfill(json={"connected": False}))
    page.add_init_script((FAKES % '"what time is it"').replace("  wake: async", "  _wake: async"))
    page.route("**/api/voice/ask", lambda r: r.fulfill(json={"answer": "It's ten past three."}))
    page.goto(server.url + "/")
    page.click("#start")
    expect(page.locator("#wakeDot")).to_be_visible(timeout=15000)
    expect(page.locator("#voiceA")).to_have_text("It's ten past three.", timeout=20000)
    for _ in range(50):
        if page.evaluate("window.__spoken.length"):
            break
        page.wait_for_timeout(100)
    assert page.evaluate("window.__spoken")[0] == "It's ten past three."
    assert errors == []
    ctx.close()
