"""Claude quota strip and sheet at the Moto G5s sizes (usage service faked)."""
import time

import pytest
from playwright.sync_api import expect

from test_e2e import SHOTS, browser, no_overflow, open_page, seed  # noqa: F401  (browser is a fixture)

def make_quota():
    NOW = time.time()
    return {"configured": True, "plan": "pro", "updated": NOW - 30, "extra": None, "limits": [
    {"kind": "session", "label": "Current session", "short": "Session", "percent": 42, "resets_at": NOW + 2 * 3600 + 600, "severity": "normal", "active": True},
    {"kind": "weekly_all", "label": "Current week (all models)", "short": "Week", "percent": 78, "resets_at": NOW + 3 * 86400, "severity": "warning", "active": False},
    {"kind": "weekly_scoped", "label": "Current week (Opus)", "short": "Opus", "percent": 93, "resets_at": NOW + 3 * 86400, "severity": "critical", "active": False},
    ]}


def inside(page, sel, box_sel):
    a, b = page.locator(sel).bounding_box(), page.locator(box_sel).bounding_box()
    return a["x"] >= b["x"] - .5 and a["y"] >= b["y"] - .5 and a["x"] + a["width"] <= b["x"] + b["width"] + .5 and a["y"] + a["height"] <= b["y"] + b["height"] + .5


@pytest.mark.parametrize("size", ["landscape", "portrait"])
def test_quota_strip_and_sheet(browser, server, size):
    seed(server)
    quota = make_quota()
    ctx, page, errors = open_page(browser, server, size, scale=2, before=lambda p: p.route("**/api/claude/quota*", lambda r: r.fulfill(json=quota)))
    expect(page.locator(".quota .q-row")).to_have_count(3)
    expect(page.locator(".quota .q-row").nth(0)).to_contain_text("Session")
    expect(page.locator(".quota .q-row").nth(0)).to_contain_text("42%")
    expect(page.locator(".quota .q-row").nth(0)).to_contain_text("2h 10m")
    expect(page.locator(".quota .q-row.critical")).to_contain_text("Opus")
    assert inside(page, "#quota", "#claude")
    # bar fill = percent used (CSP blocks style="" attributes, so this checks the real rendered width)
    fills = page.evaluate("""() => [...document.querySelectorAll('#quota .q-bar i')].map(
        i => Math.round(100 * i.getBoundingClientRect().width / i.parentElement.getBoundingClientRect().width))""")
    assert fills == [42, 78, 93], fills
    assert no_overflow(page) == []
    page.screenshot(path=str(SHOTS / f"quota-{size}.png"))
    page.click("#quota")
    expect(page.locator(".q-line")).to_have_count(3)
    expect(page.locator(".sheet")).to_contain_text("Claude plan · Pro")
    expect(page.locator(".q-line").nth(0)).to_contain_text("resets in 2h 10m")
    assert page.evaluate("() => Math.round(100 * document.querySelector('.q-line .q-bar i').getBoundingClientRect().width / document.querySelector('.q-line .q-bar').getBoundingClientRect().width)") == 42
    page.wait_for_timeout(500)
    page.screenshot(path=str(SHOTS / f"quota-sheet-{size}.png"))
    with page.expect_request(lambda r: "refresh=1" in r.url):
        page.click("#qRefresh")
    assert errors == []
    ctx.close()


@pytest.mark.parametrize("size", ["landscape", "portrait"])
def test_sign_in_from_phone(browser, server, size):
    state = {"signed": False}
    def quota_route(r):
        r.fulfill(json=make_quota() if state["signed"] else {"configured": False})
    def finish_route(r):
        import json as _j
        assert _j.loads(r.request.post_data) == {"code": "abc123#st4te"}
        state["signed"] = True
        r.fulfill(json=make_quota())
    def before(p):
        p.route("**/api/claude/quota*", quota_route)
        p.route("**/api/claude/login", lambda r: r.fulfill(json={"url": server.url + "/health"}))
        p.route("**/api/claude/finish", finish_route)
    ctx, page, errors = open_page(browser, server, size, scale=2, before=before)
    expect(page.locator("#quota")).to_contain_text("tap to sign in")
    page.click("#quota")
    expect(page.locator("#qSignIn")).to_be_visible()
    page.wait_for_timeout(500)
    page.screenshot(path=str(SHOTS / f"quota-signin-{size}.png"))
    with page.context.expect_page() as tab:
        page.click("#qSignIn")
    tab.value.wait_for_url(server.url + "/health")          # the sign-in opens in a new tab
    tab.value.close()
    page.click("#qCodeForm button")
    expect(page.locator("#qErr")).to_have_text("Paste the code first.")
    page.fill("#qCode", "  abc123#st4te ")
    page.click("#qCodeForm button")
    expect(page.locator(".q-line")).to_have_count(3)          # the sheet turns into the usage view
    expect(page.locator(".quota .q-row")).to_have_count(3)
    assert errors == []
    ctx.close()


def test_sign_in_bad_code_shows_error(browser, server):
    ctx, page, errors = open_page(browser, server, "landscape")
    page.click("#quota")
    page.fill("#qCode", "whatever")
    page.click("#qCodeForm button")          # real server: no sign-in started
    expect(page.locator("#qErr")).to_contain_text("expired")
    assert errors == []
    ctx.close()


def test_quota_sign_in_ended(browser, server):
    ctx, page, errors = open_page(browser, server, "portrait", before=lambda p: p.route("**/api/claude/quota*", lambda r: r.fulfill(json={
        "configured": True, "login_needed": True, "error": "The Remote server's Claude sign-in has ended: sign in again (see the README)"})))
    expect(page.locator("#quota.warn")).to_contain_text("sign in again")
    page.click("#quota")
    expect(page.locator("#qSignIn")).to_be_visible()
    assert errors == []
    ctx.close()
