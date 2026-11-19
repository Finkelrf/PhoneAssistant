"""Settings sheet fits the screen when the browser has no Wake Lock (the Moto G5s over plain http)."""
import pytest
from playwright.sync_api import expect

from test_e2e import SHOTS, browser, open_page  # noqa: F401  (browser is a fixture)

NO_WAKELOCK = "Object.defineProperty(Navigator.prototype, 'wakeLock', {get: () => undefined}); delete Navigator.prototype.wakeLock;"


@pytest.mark.parametrize("size", ["landscape", "portrait"])
def test_settings_fit_without_wakelock(browser, server, size):
    ctx, page, errors = open_page(browser, server, size, scale=2, before=lambda p: p.add_init_script(NO_WAKELOCK))
    page.click("#settingsBtn")
    expect(page.locator(".sheet")).to_contain_text("Not available over plain http")
    page.wait_for_timeout(500)
    out = page.evaluate("""() => {
        const W = innerWidth, bad = [];
        for (const el of document.querySelectorAll('.sheet .row, .sheet .switch, .sheet .seg button, .sheet .chip, .sheet .row svg'))
            { const r = el.getBoundingClientRect(); if (r.width && (r.right > W - 4 || r.left < 0)) bad.push(el.className + ' ' + (el.textContent || '').trim().slice(0, 20) + ' ' + Math.round(r.right)); }
        return bad;
    }""")
    page.screenshot(path=str(SHOTS / f"settings-nowakelock-{size}.png"))
    assert out == [], out
    assert errors == []
    ctx.close()
