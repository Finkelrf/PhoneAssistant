"""Lyrics view at the Moto G5s sizes (LRCLIB answer faked; the lines are made up)."""
import json

import pytest
from playwright.sync_api import expect

from test_e2e import SHOTS, browser, no_overflow, open_page  # noqa: F401  (browser is a fixture)

SYNCED = {"found": True, "instrumental": False, "synced": True, "lines": [
    {"t": 70000, "text": "Streetlights hum a quiet tune"},
    {"t": 78000, "text": "The city folds into the night"},
    {"t": 86000, "text": "We drive until the morning comes"},
    {"t": 94000, "text": "And chase the last of the neon light"},
    {"t": 102000, "text": ""},
    {"t": 110000, "text": "Hold on, hold on, the road is long"},
    {"t": 118000, "text": "Hold on, we're almost home"},
]}


@pytest.mark.parametrize("size", ["landscape", "portrait"])
def test_synced_lyrics(browser, server, size):
    ctx, page, errors = open_page(browser, server, size, scale=2)
    asked = []
    page.route("**/api/lyrics?*", lambda r: (asked.append(r.request.url), r.fulfill(json=SYNCED)))
    expect(page.locator("#lyrBtn")).to_be_enabled()
    btn, card = page.locator("#lyrBtn").bounding_box(), page.locator("#music").bounding_box()
    assert btn["x"] + btn["width"] <= card["x"] + card["width"] - 4, "Lyrics button sticks out of the player"
    page.click("#lyrBtn")
    expect(page.locator("#lyrics")).to_be_visible()
    expect(page.locator("#lyrTitle")).to_have_text("Midnight City")
    expect(page.locator(".lyr-line")).to_have_count(7)
    # the fake player is ~90 s in: the third line is current
    expect(page.locator(".lyr-line.now")).to_have_text("We drive until the morning comes")
    assert "title=Midnight+City" in asked[0] and "artist=M83" in asked[0] and "duration=243000" in asked[0]
    assert no_overflow(page) == []
    page.wait_for_timeout(600)
    page.screenshot(path=str(SHOTS / f"lyrics-{size}.png"))
    # tapping a line seeks there
    with page.expect_request(lambda r: r.url.endswith("/api/spotify/seek")) as req:
        page.locator(".lyr-line").nth(5).click()
    assert json.loads(req.value.post_data) == {"position": 110000}
    expect(page.locator(".lyr-line.now")).to_have_text("Hold on, hold on, the road is long")
    # back (Android back button) closes it
    page.go_back()
    expect(page.locator("#lyrics")).to_be_hidden()
    assert errors == []
    ctx.close()


def test_open_by_art_close_button_and_no_lyrics(browser, server):
    ctx, page, errors = open_page(browser, server, "landscape")
    page.route("**/api/lyrics?*", lambda r: r.fulfill(json={"found": False}))
    page.click("#art")
    expect(page.locator(".lyr-msg")).to_contain_text("No lyrics for this song")
    page.click("#lyrClose")
    expect(page.locator("#lyrics")).to_be_hidden()
    assert errors == []
    ctx.close()


def test_plain_lyrics(browser, server):
    ctx, page, errors = open_page(browser, server, "portrait")
    page.route("**/api/lyrics?*", lambda r: r.fulfill(json={"found": True, "synced": False, "instrumental": False,
                                                            "lines": [{"t": None, "text": "Line one"}, {"t": None, "text": ""},
                                                                      {"t": None, "text": "Line two"}]}))
    page.click("#lyrBtn")
    expect(page.locator("#lyrics.plain .lyr-line")).to_have_count(3)
    expect(page.locator(".lyr-line.now")).to_have_count(0)
    assert errors == []
    ctx.close()
