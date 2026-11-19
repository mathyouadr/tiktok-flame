"""End-to-end runs in a real Chromium against a fake tiktok.com (requests are intercepted by Playwright):
no network access to TikTok, no message is ever sent."""
import io
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path

from tiktok_flame import browser, cli
from tiktok_flame.core import Store

PROFILE = """<html><body><h1 data-e2e="user-title">{u}</h1><h2 data-e2e="user-subtitle">{nick}</h2>
<button>Following</button>{btn}<div>videos…</div></body></html>"""
BTN = """<button onclick="location.href='/messages?u={u}'">Message</button>"""
# Message box inside an iframe, emptied when the message is sent.
CHAT_IFRAME = """<html><body><div data-e2e="chat-uniqueid">{head}</div>
<iframe srcdoc='<div data-e2e="message-input-area"><div contenteditable="true" role="textbox" id="ed"></div></div>
<script>ed.addEventListener("keydown", e => {{ if (e.key === "Enter") {{ e.preventDefault();
  const t = ed.innerText.trim(); if (t) {{ parent.postMessage(t, "*"); ed.innerHTML = ""; }} }} }});</script>'></iframe>
<script>window.sent = []; addEventListener("message", e => window.sent.push(e.data));</script></body></html>"""
# What TikTok really does: a cookie banner (shadow DOM) over the message box, a DraftJS-like box that keeps
# its placeholder and is not emptied after sending, while the new bubble shows up in the conversation.
CHAT_REAL = """<html><body><div data-e2e="chat-uniqueid">@dave</div>
<div id="list"><p data-e2e="dm-new-message-text">🔥</p></div>
<div data-e2e="message-input-area" style="position:fixed;bottom:20px;left:0;right:0">
  <div class="public-DraftEditorPlaceholder-root"><div class="public-DraftEditorPlaceholder-inner">Send a message...</div></div>
  <div contenteditable="true" role="textbox" id="ed" style="min-height:20px"></div></div>
<tiktok-cookie-banner style="position:fixed;left:0;right:0;bottom:0;height:300px;display:block;background:#0aa;z-index:9"></tiktok-cookie-banner>
<script>
const b = document.querySelector("tiktok-cookie-banner"), sr = b.attachShadow({mode: "open"});
sr.innerHTML = '<button>Allow all</button><button id="d">Decline optional cookies</button>';
sr.getElementById("d").onclick = () => b.remove();
window.sent = [];
ed.addEventListener("keydown", e => { if (e.key !== "Enter") return; e.preventDefault();
  const t = ed.innerText.trim(); if (!t) return; window.sent.push(t);
  const p = document.createElement("p"); p.dataset.e2e = "dm-new-message-text"; p.textContent = t; list.appendChild(p); });
</script></body></html>"""
NOT_FOUND = "<html><body><p>Couldn't find this account</p></body></html>"
HTML = "text/html; charset=utf-8"


def fake_tiktok(route):
    """alice: OK · bob: no Message button · carol: wrong conversation opens · ghost: no account · dave: real-life quirks."""
    url = route.request.url
    m = re.match(r"https://www\.tiktok\.com/@([^/?]+)", url)
    if m:
        u = m.group(1)
        if u == "ghost":
            return route.fulfill(status=200, content_type=HTML, body=NOT_FOUND)
        btn = "" if u == "bob" else BTN.format(u=u)
        return route.fulfill(status=200, content_type=HTML, body=PROFILE.format(u=u, nick=u.title(), btn=btn))
    m = re.match(r"https://www\.tiktok\.com/messages\?u=(\w+)", url)
    if m:
        if m.group(1) == "dave":
            return route.fulfill(status=200, content_type=HTML, body=CHAT_REAL)
        head = "someoneelse" if m.group(1) == "carol" else m.group(1)
        return route.fulfill(status=200, content_type=HTML, body=CHAT_IFRAME.format(head=head))
    if url.startswith("https://www.tiktok.com/"):
        return route.fulfill(status=200, content_type=HTML, body="<html><body>For You</body></html>")
    return route.abort()


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        browser.pause = lambda *a, **k: None
        real = browser.launch

        def fake_launch(p, profile, headless=True):
            ctx = real(p, profile, headless=headless)
            ctx.route("**/*", fake_tiktok)
            ctx.add_cookies([{"name": "sessionid", "value": "test", "domain": ".tiktok.com", "path": "/"}])
            return ctx
        browser.launch = fake_launch

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="tf-test-")
        self.store = Store(self.home)

    def run_cli(self, *args):
        out, errs = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(errs):
            code = cli.main(["--home", self.home, *args])
        return code, out.getvalue(), errs.getvalue()

    def page(self, fn):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = browser.launch(p, Path(self.home) / "profile")
            try:
                return fn(ctx.new_page())
            finally:
                ctx.close()

    def test_send_then_skip_same_day(self):
        self.run_cli("add", "alice")
        code, out, _ = self.run_cli("send")
        self.assertEqual(code, 0, out)
        self.assertIn("🔥 @alice", out)
        self.assertEqual(self.store.state()["sent"], {"alice": date.today().isoformat()})
        code, out, _ = self.run_cli("send")
        self.assertEqual(code, 0)
        self.assertIn("already been sent today", out)
        self.assertEqual(len(self.store.state()["runs"]), 1)

    def test_failures_are_reported(self):
        self.run_cli("add", "bob", "carol", "ghost", "alice", "eve")
        self.run_cli("pause", "eve")
        code, _, errs = self.run_cli("send", "--quiet")
        self.assertEqual(code, 1)
        res = {r["user"]: r for r in self.store.state()["runs"][0]["results"]}
        self.assertEqual(set(res), {"bob", "carol", "ghost", "alice"})      # eve is paused
        self.assertIn("Message", res["bob"]["error"])
        self.assertIn("not the right one", res["carol"]["error"])
        self.assertIn("not found", res["ghost"]["error"])
        self.assertTrue(res["alice"]["ok"])
        self.assertIn("1/4 flame(s) sent", errs)
        self.assertTrue(list((Path(self.home) / "debug").glob("*-bob.json")))

    def test_message_really_typed_and_wrong_chat_untouched(self):
        def go(page):
            browser.send_one(page, "alice", "🔥")
            page.wait_for_function("window.sent.length > 0", timeout=3000)
            self.assertEqual(page.evaluate("window.sent"), ["🔥"])
            with self.assertRaises(browser.Fail):
                browser.send_one(page, "carol", "🔥")
            self.assertEqual(page.evaluate("window.sent || []"), [])
        self.page(go)

    def test_cookie_banner_placeholder_and_box_not_emptied(self):
        def go(page):
            browser.send_one(page, "dave", "🔥")
            self.assertEqual(page.evaluate("window.sent"), ["🔥"])               # sent exactly once
            self.assertEqual(page.locator("tiktok-cookie-banner").count(), 0)     # banner declined
            self.assertEqual(page.evaluate("ed.innerText.trim()"), "")            # leftover erased
        self.page(go)

    def test_dry_run_never_sends(self):
        self.run_cli("add", "dave")
        code, out, _ = self.run_cli("send", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("nothing was sent", out)
        self.assertEqual(self.store.state()["sent"], {})
        self.assertEqual(self.store.state()["runs"], [])

    def test_composer_text_ignores_placeholder(self):
        def go(page):
            page.set_content(CHAT_REAL)
            area = page.locator('[data-e2e="message-input-area"]')
            self.assertEqual(browser.composer_text(area), "")
            page.evaluate("ed.textContent = '🔥'")
            self.assertEqual(browser.composer_text(area), "🔥")
        self.page(go)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="tf-cli-")

    def run_cli(self, *args):
        out, errs = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(errs):
            code = cli.main(["--home", self.home, *args])
        return code, out.getvalue() + errs.getvalue()

    def test_friends_and_messages(self):
        self.assertEqual(self.run_cli("add", "@Alice", "https://www.tiktok.com/@bob", "-m", "yo")[0], 0)
        self.assertEqual(self.run_cli("add", "not valid")[0], 1)
        self.run_cli("message", "🔥🔥")
        self.run_cli("message", "--user", "alice", "--reset")
        self.run_cli("pause", "bob")
        code, out = self.run_cli("status")
        self.assertIn("@alice", out)
        self.assertIn("paused", out)
        self.assertIn("🔥🔥", out)
        cfg = Store(self.home).config()
        self.assertEqual(cfg["friends"], [{"user": "alice", "on": True}, {"user": "bob", "on": False, "message": "yo"}])
        self.assertEqual(self.run_cli("remove", "alice")[0], 0)
        self.assertEqual([f["user"] for f in Store(self.home).config()["friends"]], ["bob"])

    def test_webhook_validation(self):
        self.assertEqual(self.run_cli("webhook", "ftp://x")[0], 1)
        self.assertEqual(self.run_cli("webhook", "https://example.com/hook")[0], 0)
        self.assertEqual(Store(self.home).config()["webhook"], "https://example.com/hook")
        self.run_cli("webhook", "off")
        self.assertIsNone(Store(self.home).config()["webhook"])

    def test_send_without_friends(self):
        self.assertEqual(self.run_cli("send")[0], 1)


if __name__ == "__main__":
    unittest.main()
