"""Everything that drives Chromium (Playwright): login, session check, sending one message."""
import base64
import io
import os
import random
import re
import subprocess
import sys
import time

TT = "https://www.tiktok.com"
LOGIN_QR_URL = TT + "/login/qrcode"
LOGIN_URL = TT + "/login"

# TikTok web selectors. Several candidates each: the web app changes often.
MESSAGE_BUTTONS = ['[data-e2e="message-button"]', '[data-e2e="user-message"]', '[data-e2e="message-icon"]']
MESSAGE_BUTTON_NAME = re.compile(r"^\s*(messages?|send message|envoyer un message|nachricht|mensaje|mensagem|messaggio)\s*$", re.I)
COMPOSERS = ['[data-e2e="message-input-area"] [contenteditable="true"]', '[data-e2e="message-input-area"]',
             '.public-DraftEditor-content', 'div[contenteditable="true"][role="textbox"]', 'div[contenteditable="true"]']
SEND_BUTTONS = ['[data-e2e="message-send"]', '[data-e2e="send-message"]']
CHAT_HEADER = ['[data-e2e="chat-uniqueid"]', '[data-e2e="chat-nickname"]']
BUBBLES = '[data-e2e="dm-new-message-text"]'
COOKIE_DECLINE = re.compile(r"(decline|reject|refuse|refuser|décliner|ablehnen|rechazar|recusar|rifiuta)", re.I)
CAPTCHA = ['#captcha-verify-image', '#captcha_container', '.captcha_verify_container', '.captcha-verify-container',
           'iframe[src*="captcha"]']
NOT_FOUND = re.compile(r"(couldn.t find this account|impossible de trouver ce compte|konto nicht gefunden|"
                       r"no se pudo encontrar esta cuenta)", re.I)


class Fail(Exception):
    """Expected failure with a short, human-readable reason."""


def pause(a=0.8, b=2.2):
    time.sleep(random.uniform(a, b))


# ---------- QR code ----------

def decode_qr(data_url):
    """data:image/png;base64,... -> text of the QR code (a https link), or None."""
    try:
        import zxingcpp
        from PIL import Image
        img = Image.open(io.BytesIO(base64.b64decode(data_url.split(",", 1)[1])))
        for r in zxingcpp.read_barcodes(img):
            if r.text.startswith("https://"):
                return r.text
    except Exception:
        return None
    return None


def qr_matrix(text):
    """QR code of `text` as rows of booleans (True = dark module), quiet zone included."""
    import zxingcpp
    from PIL import Image
    bc = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.QRCode)
    img = Image.fromarray(zxingcpp.write_barcode_to_image(bc, scale=1, add_quiet_zones=True)).convert("L")
    w, h = img.size
    px = img.load()
    return [[px[x, y] < 128 for x in range(w)] for y in range(h)]


def qr_terminal(text, invert=False):
    """Render a QR code with half blocks (2 rows per line). Default suits dark terminals; invert for light ones."""
    m = qr_matrix(text)
    if len(m) % 2:
        m.append([False] * len(m[0]))
    chars = {(True, True): "█", (True, False): "▀", (False, True): "▄", (False, False): " "}
    lines = []
    for y in range(0, len(m), 2):
        # a lit character cell shows the terminal's (light) foreground = a light module
        lines.append("".join(chars[((not a) != invert, (not b) != invert)] for a, b in zip(m[y], m[y + 1])))
    return "\n".join(lines)


def save_qr_png(text, path, scale=8):
    import zxingcpp
    from PIL import Image
    bc = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.QRCode)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        Image.fromarray(zxingcpp.write_barcode_to_image(bc, scale=scale)).save(f, "PNG")


# ---------- browser ----------

def _chrome_major(p):
    try:
        out = subprocess.run([p.chromium.executable_path, "--version"], capture_output=True, text=True, timeout=20).stdout
        return re.search(r"(\d+)\.", out).group(1)
    except Exception:
        pass
    try:
        b = p.chromium.launch()
        try:
            return b.version.split(".")[0]
        finally:
            b.close()
    except Exception:
        return "131"


def user_agent(p):
    """A regular desktop Chrome user agent for this OS (no 'HeadlessChrome')."""
    if sys.platform == "win32":
        osp = "Windows NT 10.0; Win64; x64"
    elif sys.platform == "darwin":
        osp = "Macintosh; Intel Mac OS X 10_15_7"
    else:
        osp = "X11; Linux x86_64"
    return f"Mozilla/5.0 ({osp}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{_chrome_major(p)}.0.0.0 Safari/537.36"


def launch(p, profile, headless=True):
    """Persistent Chromium context: the profile directory keeps the TikTok session between runs."""
    ctx = p.chromium.launch_persistent_context(
        str(profile), channel="chromium", headless=headless,
        args=["--disable-blink-features=AutomationControlled", "--no-first-run", "--disable-dev-shm-usage",
              "--mute-audio"],
        user_agent=user_agent(p), viewport={"width": 1280, "height": 820})
    ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    if headless:   # no videos: less RAM and bandwidth
        ctx.route("**/*", lambda r: r.abort() if r.request.resource_type == "media" else r.continue_())
    ctx.set_default_timeout(20000)
    return ctx


def first_page(ctx):
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def logged_in(ctx):
    return any(c["name"] == "sessionid" and c["value"] for c in ctx.cookies(TT))


def first_visible(page, selectors, timeout=0.0):
    """First visible element among `selectors`, in the page or its iframes -> (frame, locator) or (None, None)."""
    end = time.time() + timeout
    while True:
        for fr in page.frames:
            for sel in selectors:
                try:
                    loc = fr.locator(sel).filter(visible=True)
                    if loc.count():
                        return fr, loc.first
                except Exception:
                    continue
        if time.time() >= end:
            return None, None
        time.sleep(0.5)


def dismiss_cookies(page):
    """Decline TikTok's cookie banner (a shadow-DOM element that covers the message box). The choice sticks."""
    try:
        banner = page.locator("tiktok-cookie-banner")
        if not banner.count():
            return False
        btn = banner.get_by_role("button", name=COOKIE_DECLINE)
        if btn.count():
            btn.first.click(timeout=5000)
            pause(0.8, 1.5)
            return True
    except Exception:
        pass
    return False


def check_captcha(page):
    if first_visible(page, CAPTCHA)[1] is not None:
        raise Fail("TikTok is showing a captcha: open TikTok in a normal browser or the app, then try again later")


def read_account(page):
    acc = None
    try:
        acc = page.evaluate("""async () => {
          try {
            const r = await fetch('/passport/web/account/info/?aid=1459', {credentials: 'include'});
            const d = (await r.json()).data || {};
            return d.username ? {user: d.username, name: d.screen_name || null} : null;
          } catch (e) { return null; }
        }""")
    except Exception:
        pass
    if not acc:
        try:
            href = page.locator('a[data-e2e="nav-profile"]').first.get_attribute("href", timeout=3000) or ""
            m = re.search(r"/@([^/?#]+)", href)
            acc = {"user": m.group(1), "name": None} if m else None
        except Exception:
            pass
    return acc


def debug_dump(store, page, tag):
    """Screenshot + list of data-e2e attributes and buttons: enough to fix a selector. Keeps the last 10."""
    try:
        store.debug.mkdir(exist_ok=True)
        os.chmod(store.debug, 0o700)
        base = store.debug / f"{time.strftime('%Y%m%d-%H%M%S')}-{re.sub(r'[^a-z0-9._-]', '', tag)}"
        old = os.umask(0o077)
        try:
            page.screenshot(path=str(base) + ".png")
        finally:
            os.umask(old)
        info = page.evaluate("""() => ({
          url: location.href,
          e2e: [...new Set([...document.querySelectorAll('[data-e2e]')].map(e => e.getAttribute('data-e2e')))],
          buttons: [...document.querySelectorAll('button,[role=button]')].map(b => (b.innerText || b.getAttribute('aria-label') || '').trim()).filter(Boolean).slice(0, 60),
          frames: [...document.querySelectorAll('iframe')].map(f => f.src).slice(0, 10)
        })""")
        from .core import save_json
        save_json(str(base) + ".json", info)
        for png in sorted(store.debug.glob("*.png"))[:-10]:
            png.unlink(missing_ok=True)
            png.with_suffix(".json").unlink(missing_ok=True)
        return str(base) + ".png"
    except Exception:
        return None


# ---------- sending ----------

def open_chat(page, user):
    """Open the conversation with @user from their profile (Message button). Returns the names shown there."""
    page.goto(f"{TT}/@{user}", wait_until="domcontentloaded", timeout=45000)
    pause(2, 4)
    check_captcha(page)
    dismiss_cookies(page)
    body = page.locator("body").inner_text(timeout=10000)
    if NOT_FOUND.search(body):
        raise Fail("account not found (typo in the username?)")
    names = set()      # username + display name (their order on the profile varies)
    for sel in ('[data-e2e="user-title"]', '[data-e2e="user-subtitle"]'):
        try:
            t = page.locator(sel).first.inner_text(timeout=4000).strip().lower().lstrip("@")
            if t:
                names.add(t)
        except Exception:
            pass
    _, btn = first_visible(page, MESSAGE_BUTTONS, timeout=6)
    if btn is None:
        cand = page.get_by_role("button", name=MESSAGE_BUTTON_NAME).filter(visible=True)
        if not cand.count():
            cand = page.get_by_role("link", name=MESSAGE_BUTTON_NAME).filter(visible=True)
        btn = cand.first if cand.count() else None
    if btn is None:
        raise Fail("no 'Message' button on their profile (do you follow each other?)")
    btn.click()
    page.wait_for_url(re.compile(r"/messages"), timeout=25000)
    pause(2, 4)
    return names


def verify_header(page, user, names):
    """If the chat header is readable it must name the right person; otherwise nothing is sent."""
    _, h = first_visible(page, CHAT_HEADER, timeout=3)
    if h is None:
        return
    txt = (h.inner_text() or "").strip().lower()
    if txt and not any(n and n in txt for n in {user, *names}):
        raise Fail(f"the open conversation is not the right one ({txt[:40]}): nothing sent")


def composer_text(loc):
    """Text typed in the message box, without the placeholder ('Send a message...') TikTok puts in the container."""
    try:
        txt = loc.evaluate("""e => {
          const ed = e.matches('[contenteditable="true"]') ? e : (e.querySelector('[contenteditable="true"]') || e);
          const c = ed.cloneNode(true);
          c.querySelectorAll('[class*="Placeholder"], [class*="placeholder"]').forEach(x => x.remove());
          return c.textContent || "";
        }""", timeout=3000)
        return txt.replace("\u200b", "").strip()
    except Exception:
        return ""


def count_bubbles(fr, message):
    """How many messages in the conversation are exactly `message` (-1 if unreadable)."""
    try:
        return fr.evaluate("([sel, m]) => [...document.querySelectorAll(sel)].filter(e => e.innerText.trim() === m).length",
                           [BUBBLES, message.strip()])
    except Exception:
        return -1


def clear_box(page, box):
    box.click(timeout=5000)
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.press("Backspace")


def type_and_send(page, message, dry_run=False):
    fr, box = first_visible(page, COMPOSERS, timeout=25)
    if box is None:
        check_captcha(page)
        raise Fail("message box not found (TikTok may have changed its web app)")
    dismiss_cookies(page)              # the banner can show up after the page has loaded
    try:
        box.click(timeout=8000)
    except Exception:
        if not dismiss_cookies(page):
            raise Fail("the message box is covered by a TikTok popup: nothing sent")
        box.click(timeout=8000)
    before = count_bubbles(fr, message)
    pause(0.5, 1.2)
    page.keyboard.insert_text(message)
    pause(0.6, 1.4)
    if message.strip() not in composer_text(box):
        box.fill(message)
        pause(0.4, 0.9)
        if message.strip() not in composer_text(box):
            raise Fail("could not type in the message box")
    if dry_run:                        # everything but Enter: erase and leave
        clear_box(page, box)
        if composer_text(box):
            raise Fail("dry run: could not erase the test text, please check the conversation")
        return True
    page.keyboard.press("Enter")
    # Success = a new bubble with the message shows up (TikTok does not always empty the box), or the box empties.
    end = time.time() + 15
    clicked = False
    while time.time() < end:
        if before >= 0 and count_bubbles(fr, message) > before:
            if composer_text(box):     # leftover text: erase it without sending
                clear_box(page, box)
            return True
        if not composer_text(box):
            return True
        if not clicked and time.time() > end - 9:
            _, send = first_visible(page, SEND_BUTTONS)
            if send is not None:
                send.click()
            clicked = True
        time.sleep(0.5)
    raise Fail("the message stayed in the box (not sent)")


def send_one(page, user, message, dry_run=False):
    names = open_chat(page, user)
    check_captcha(page)
    verify_header(page, user, names)
    type_and_send(page, message, dry_run=dry_run)


def login_qr(ctx, on_qr, timeout=300, cancelled=lambda: False):
    """Open TikTok's QR login and call on_qr(link) each time a new QR shows up. True once logged in."""
    page = first_page(ctx)
    page.goto(LOGIN_QR_URL, wait_until="domcontentloaded", timeout=45000)
    last, end = None, time.time() + timeout
    while time.time() < end and not cancelled():
        if logged_in(ctx) or "/login" not in page.url:
            break
        try:
            data = page.evaluate("() => { const c = document.querySelector('canvas'); try { return c ? c.toDataURL('image/png') : null } catch (e) { return null } }")
        except Exception:
            data = None
        if data and data != last:
            link = decode_qr(data)
            if link:
                last = data
                on_qr(link)
        try:
            if page.get_by_text(re.compile(r"expired|expiré|abgelaufen|caducado", re.I)).filter(visible=True).count():
                page.reload(wait_until="domcontentloaded")
                last = None
        except Exception:
            pass
        time.sleep(1.5)
    return wait_session(ctx)


def login_window(ctx, timeout=600):
    """Visible window: log in any way you like (password, Google, QR...). True once logged in."""
    page = first_page(ctx)
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
    end = time.time() + timeout
    while time.time() < end:
        if logged_in(ctx):
            break
        if not ctx.pages:              # window closed by the user
            return False
        time.sleep(1.5)
    return wait_session(ctx)


def wait_session(ctx, seconds=20):
    for _ in range(seconds):          # the cookie lands right after the redirect
        if logged_in(ctx):
            return True
        time.sleep(1)
    return False


def describe(exc):
    if isinstance(exc, Fail):
        return str(exc)
    name = type(exc).__name__
    if name == "TimeoutError":
        return "TikTok took too long to respond (timeout)"
    return f"unexpected error ({name})"

