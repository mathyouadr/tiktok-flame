"""Command line interface: `tiktok-flame <command>`."""
import argparse
import json
import random
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime

from . import __version__
from .core import MAX_FRIENDS, MAX_RUNS, Store, clean_message, clean_user

EXIT_OK, EXIT_FAILED, EXIT_BUSY, EXIT_LOGGED_OUT = 0, 1, 2, 3


def out(msg="", quiet=False):
    if not quiet:
        print(msg, flush=True)


def err(msg):
    print(msg, file=sys.stderr, flush=True)


def notify(cfg, text):
    """POST the text to a webhook (Discord, Slack or anything that accepts JSON)."""
    if not cfg.get("webhook"):
        return
    body = json.dumps({"content": text, "text": text}).encode()
    req = urllib.request.Request(cfg["webhook"], data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "tiktok-flame"})
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as e:
        err(f"webhook failed: {type(e).__name__}")


def playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        err("Playwright is missing: pip install playwright")
        sys.exit(EXIT_FAILED)
    return sync_playwright()


def when(ts):
    d = datetime.fromtimestamp(ts)
    return d.strftime("today %H:%M") if d.date() == date.today() else d.strftime("%Y-%m-%d %H:%M")


# ---------- commands ----------

def cmd_setup(a, store):
    out("Downloading Chromium for Playwright (about 150 MB)...")
    r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])
    if r.returncode:
        err("Install failed. On Linux you may also need system libraries: "
            f"{sys.executable} -m playwright install-deps chromium (as root)")
        return EXIT_FAILED
    store.ensure()
    out(f"Done. Data folder: {store.root}\nNext: tiktok-flame login")
    return EXIT_OK


def cmd_login(a, store):
    from . import browser as B
    with store.lock("login") as ok:
        if not ok:
            err("Another tiktok-flame command is running.")
            return EXIT_BUSY
        with playwright() as p:
            ctx = B.launch(p, store.profile, headless=not a.window)
            try:
                if a.window:
                    out("A browser window is open: log in to TikTok there (any method). Waiting...")
                    okay = B.login_window(ctx, timeout=a.timeout)
                else:
                    def show(link):
                        B.save_qr_png(link, store.qr_path)
                        out("\nScan this QR code with the TikTok app (Profile > ☰ > scan icon, or your camera):\n")
                        out(B.qr_terminal(link, invert=a.invert))
                        out(f"\nOn the phone that has TikTok, you can also just open this link: {link}")
                        wait = f"{a.timeout // 60} min" if a.timeout >= 60 else f"{a.timeout} s"
                        out(f"QR image: {store.qr_path}   (it refreshes by itself, waiting up to {wait})")
                    out("Opening TikTok's QR login...")
                    okay = B.login_qr(ctx, show, timeout=a.timeout)
                if not okay:
                    err("Not logged in (time is up or the window was closed). Run `tiktok-flame login` again.")
                    return EXIT_LOGGED_OUT
                page = B.first_page(ctx)
                page.goto(B.TT + "/", wait_until="domcontentloaded", timeout=45000)
                B.pause(2, 3)
                acc = B.read_account(page)
            finally:
                ctx.close()
                store.qr_path.unlink(missing_ok=True)
        store.update_state(lambda s: s.update(logged_in=True, account=acc))
        out(f"✅ Logged in{' as @' + acc['user'] if acc else ''}. Next: tiktok-flame add <username>")
        return EXIT_OK


def cmd_logout(a, store):
    with store.lock("logout", wait=30) as ok:
        if not ok:
            err("Another tiktok-flame command is running.")
            return EXIT_BUSY
        shutil.rmtree(store.profile, ignore_errors=True)
        store.update_state(lambda s: s.update(logged_in=False, account=None, sent={}))
    out("Logged out: the browser profile (your TikTok session) was deleted.")
    return EXIT_OK


def cmd_check(a, store):
    from . import browser as B
    with store.lock("check", wait=60) as ok:
        if not ok:
            err("Another tiktok-flame command is running.")
            return EXIT_BUSY
        with playwright() as p:
            ctx = B.launch(p, store.profile)
            try:
                page = B.first_page(ctx)
                page.goto(B.TT + "/", wait_until="domcontentloaded", timeout=45000)
                B.pause(2, 3)
                good = B.logged_in(ctx)
                acc = B.read_account(page) if good else None
            finally:
                ctx.close()
        store.update_state(lambda s: s.update(logged_in=good, account=(acc or s.get("account")) if good else None))
    if good:
        out(f"✅ Session OK{' (@' + acc['user'] + ')' if acc else ''}.")
        return EXIT_OK
    err("❌ Not logged in. Run: tiktok-flame login")
    return EXIT_LOGGED_OUT


def edit_friends(store, users, fn):
    cfg = store.config()
    changed = []
    for raw in users:
        u = clean_user(raw)
        if not u:
            err(f"Invalid username: {raw} (letters, digits, '.' and '_', 2 to 24 characters)")
            continue
        if fn(cfg, u):
            changed.append(u)
    store.save_config(cfg)
    return changed


def cmd_add(a, store):
    def add(cfg, u):
        f = next((f for f in cfg["friends"] if f["user"] == u), None)
        if f:
            f["on"] = True
        elif len(cfg["friends"]) >= MAX_FRIENDS:
            err(f"Limit reached ({MAX_FRIENDS} friends).")
            return False
        else:
            cfg["friends"].append({"user": u, "on": True})
        if a.message:
            next(f for f in cfg["friends"] if f["user"] == u)["message"] = clean_message(a.message)
        return True
    done = edit_friends(store, a.users, add)
    if done:
        out("Added: " + ", ".join("@" + u for u in done) + ". Both accounts must follow each other.")
    return EXIT_OK if done else EXIT_FAILED


def cmd_remove(a, store):
    def rm(cfg, u):
        n = len(cfg["friends"])
        cfg["friends"] = [f for f in cfg["friends"] if f["user"] != u]
        return len(cfg["friends"]) < n
    done = edit_friends(store, a.users, rm)
    out("Removed: " + ", ".join("@" + u for u in done) if done else "Nobody removed (not in the list).")
    return EXIT_OK if done else EXIT_FAILED


def toggle(on):
    def run(a, store):
        def t(cfg, u):
            f = next((f for f in cfg["friends"] if f["user"] == u), None)
            if f:
                f["on"] = on
            return bool(f)
        done = edit_friends(store, a.users, t)
        out(("Resumed: " if on else "Paused: ") + ", ".join("@" + u for u in done) if done else "Not in the list.")
        return EXIT_OK if done else EXIT_FAILED
    return run


def cmd_message(a, store):
    cfg = store.config()
    if a.user:
        u = clean_user(a.user)
        f = next((f for f in cfg["friends"] if f["user"] == u), None)
        if not f:
            err(f"@{u} is not in your list.")
            return EXIT_FAILED
        if a.text is None or a.reset:
            f.pop("message", None)
        else:
            f["message"] = clean_message(a.text) or cfg["message"]
        store.save_config(cfg)
        out(f"Message for @{u}: {f.get('message') or cfg['message'] + ' (default)'}")
        return EXIT_OK
    if a.text is None:
        out(f"Default message: {cfg['message']}")
        return EXIT_OK
    cfg["message"] = clean_message(a.text) or cfg["message"]
    store.save_config(cfg)
    out(f"Default message: {cfg['message']}")
    return EXIT_OK


def cmd_webhook(a, store):
    cfg = store.config()
    if a.url in ("off", "none", ""):
        cfg["webhook"] = None
    elif not a.url.startswith(("https://", "http://")):
        err("The webhook must be an http(s) URL, or 'off'.")
        return EXIT_FAILED
    else:
        cfg["webhook"] = a.url
    store.save_config(cfg)
    out("Webhook disabled." if not cfg["webhook"] else "Webhook saved: problems will be posted there.")
    if cfg["webhook"] and a.test:
        notify(cfg, "🔥 tiktok-flame: webhook test")
    return EXIT_OK


def cmd_status(a, store):
    cfg, st = store.config(), store.state()
    today = date.today().isoformat()
    if a.json:
        print(json.dumps({"config": cfg, "state": {k: v for k, v in st.items() if k != "running"},
                          "today": today}, ensure_ascii=False, indent=1))
        return EXIT_OK
    acc = st.get("account")
    out(f"Account : {'@' + acc['user'] if acc else '-'} ({'logged in' if st['logged_in'] else 'not logged in'})")
    out(f"Message : {cfg['message']}")
    if st.get("running"):
        out(f"Running : {st['running']['what']} since {when(st['running']['since'])}")
    if not cfg["friends"]:
        out("Friends : none yet. Add one: tiktok-flame add <username>")
        return EXIT_OK
    last = st["runs"][0] if st["runs"] else None
    errors = {}
    if last and datetime.fromtimestamp(last["at"]).date() == date.today():
        errors = {r["user"]: r.get("error") for r in last["results"] if not r["ok"]}
    active = [f for f in cfg["friends"] if f["on"]]
    done = sum(1 for f in active if st["sent"].get(f["user"]) == today)
    out(f"Today   : {done}/{len(active)} flame(s) sent\n")
    for f in cfg["friends"]:
        if not f["on"]:
            s = "⏸  paused"
        elif st["sent"].get(f["user"]) == today:
            s = "🔥 sent today"
        elif f["user"] in errors:
            s = "❌ " + errors[f["user"]]
        else:
            s = "⏳ not sent yet today"
        extra = f"  [{f['message']}]" if f.get("message") else ""
        out(f"  @{f['user']:<24} {s}{extra}")
    if last:
        ok = sum(1 for r in last["results"] if r["ok"])
        out(f"\nLast run: {when(last['at'])}, {ok}/{len(last['results'])} sent" + (f", stopped: {last['error']}" if last.get("error") else ""))
    return EXIT_OK


def cmd_send(a, store):
    from . import browser as B
    cfg = store.config()
    if not cfg["friends"]:
        err("Nobody to message. Add a friend: tiktok-flame add <username>")
        return EXIT_FAILED
    if a.jitter:
        delay = random.uniform(0, a.jitter * 60)
        out(f"Waiting {int(delay // 60)} min {int(delay % 60)} s (--jitter)...", a.quiet)
        time.sleep(delay)
    with store.lock("send", wait=900 if a.jitter else 0) as ok:
        if not ok:
            err("Another tiktok-flame command is running.")
            return EXIT_BUSY
        today = date.today().isoformat()
        cfg = store.config()                      # may have changed while waiting
        st = store.state()
        if a.only:                                # named explicitly: paused friends included
            wanted = {clean_user(u) for u in a.only}
            todo = [(f["user"], f.get("message") or cfg["message"]) for f in cfg["friends"] if f["user"] in wanted]
            if not todo:
                err("None of these users is in your list (tiktok-flame add <username>).")
                return EXIT_FAILED
        else:
            todo = [(f["user"], f.get("message") or cfg["message"]) for f in cfg["friends"] if f["on"]]
        if not (a.force or a.dry_run):            # never two flames the same day by accident
            todo = [(u, m) for u, m in todo if st["sent"].get(u) != today]
        if not todo:
            out("Nothing to do: every flame has already been sent today. 🔥", a.quiet)
            return EXIT_OK
        mode = " (dry run: type, then erase, never press Enter)" if a.dry_run else ""
        out(f"Sending to {len(todo)} friend(s){mode}...", a.quiet)
        results, fatal = [], None
        with playwright() as p:
            ctx = B.launch(p, store.profile, headless=not a.window)
            try:
                page = B.first_page(ctx)
                page.goto(B.TT + "/", wait_until="domcontentloaded", timeout=45000)
                B.pause(2, 4)
                if not B.logged_in(ctx):
                    fatal = "logged out"
                else:
                    for i, (user, msg) in enumerate(todo):
                        if i:
                            B.pause(6, 20)
                        try:
                            B.send_one(page, user, msg, dry_run=a.dry_run)
                            results.append({"user": user, "ok": True})
                            out(f"  🔥 @{user}" + (" (dry run OK)" if a.dry_run else ""), a.quiet)
                            if not a.dry_run:
                                store.update_state(lambda s, u=user: s["sent"].__setitem__(u, today))
                        except Exception as e:
                            reason = B.describe(e)
                            results.append({"user": user, "ok": False, "error": reason})
                            shot = B.debug_dump(store, page, user)
                            out(f"  ❌ @{user}: {reason}" + (f"\n     screenshot: {shot}" if shot and a.verbose else ""), a.quiet)
                            if "captcha" in reason:
                                fatal = "captcha"
                                break
            except Exception as e:
                fatal = f"browser ({type(e).__name__})"
            finally:
                ctx.close()

        if not a.dry_run:
            def record(s):
                s["runs"] = ([{"at": int(time.time()), "results": results, "error": fatal}] + s["runs"])[:MAX_RUNS]
                s["sent"] = {u: d for u, d in s["sent"].items() if d == today}
                if fatal == "logged out":
                    s["logged_in"] = False
            store.update_state(record)

    bad = [r for r in results if not r["ok"]]
    if fatal == "logged out":
        msg = "⚠️ tiktok-flame: your TikTok session has expired, no flame was sent. Run: tiktok-flame login"
        err(msg)
        notify(cfg, msg)
        return EXIT_LOGGED_OUT
    if fatal or bad:
        lines = [f"🔥 tiktok-flame: {len(results) - len(bad)}/{len(todo)} flame(s) sent."]
        lines += [f"• @{r['user']}: {r['error']}" for r in bad]
        if fatal and fatal != "captcha":
            lines.append(f"• stopped: {fatal}")
        err("\n".join(lines))
        if not a.dry_run:
            notify(cfg, "\n".join(lines))
        return EXIT_FAILED
    out(f"Done: {len(results)}/{len(todo)} 🔥" + (" (dry run, nothing was sent)" if a.dry_run else ""), a.quiet)
    return EXIT_OK


def cmd_schedule(a, store):
    exe = shutil.which("tiktok-flame") or f"{sys.executable} -m tiktok_flame"
    out("Run it once a day. A random delay (--jitter, in minutes) makes it look less robotic.\n")
    out("Linux / macOS, cron (`crontab -e`), between 11:45 and 12:15:")
    out(f"  45 11 * * * {exe} send --jitter 30 --quiet\n")
    out("Windows, Task Scheduler (PowerShell):")
    out(f'  schtasks /Create /SC DAILY /ST 11:45 /TN tiktok-flame /TR "{exe} send --jitter 30 --quiet"\n')
    out("Get told when something goes wrong (Discord/Slack webhook):")
    out("  tiktok-flame webhook https://discord.com/api/webhooks/... --test")
    return EXIT_OK


# ---------- parser ----------

def build_parser():
    ap = argparse.ArgumentParser(prog="tiktok-flame",
                                 description="Keep your TikTok streaks 🔥 alive: one DM a day to the friends you pick.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--home", help="data folder (default: per-user app data, or $TIKTOK_FLAME_HOME)")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="<command>")

    sub.add_parser("setup", help="download the Chromium browser used by tiktok-flame").set_defaults(fn=cmd_setup)
    s = sub.add_parser("login", help="log in to TikTok (QR code in the terminal, or --window)")
    s.add_argument("--window", action="store_true", help="open a visible browser window and log in any way you like")
    s.add_argument("--invert", action="store_true", help="invert the QR code colours (light terminal themes)")
    s.add_argument("--timeout", type=int, default=300, help="seconds to wait for the login (default 300)")
    s.set_defaults(fn=cmd_login)
    sub.add_parser("logout", help="delete the saved TikTok session").set_defaults(fn=cmd_logout)
    sub.add_parser("check", help="check that the saved session still works").set_defaults(fn=cmd_check)

    s = sub.add_parser("add", help="add friends (username, @username or profile link)")
    s.add_argument("users", nargs="+")
    s.add_argument("-m", "--message", help="custom message for these friends")
    s.set_defaults(fn=cmd_add)
    s = sub.add_parser("remove", help="remove friends")
    s.add_argument("users", nargs="+")
    s.set_defaults(fn=cmd_remove)
    s = sub.add_parser("pause", help="stop messaging friends without removing them")
    s.add_argument("users", nargs="+")
    s.set_defaults(fn=toggle(False))
    s = sub.add_parser("resume", help="message paused friends again")
    s.add_argument("users", nargs="+")
    s.set_defaults(fn=toggle(True))
    s = sub.add_parser("message", help="show or set the message (default 🔥)")
    s.add_argument("text", nargs="?")
    s.add_argument("--user", help="set a message for one friend only")
    s.add_argument("--reset", action="store_true", help="with --user: back to the default message")
    s.set_defaults(fn=cmd_message)
    s = sub.add_parser("webhook", help="post problems to a Discord/Slack webhook ('off' to disable)")
    s.add_argument("url")
    s.add_argument("--test", action="store_true", help="send a test message")
    s.set_defaults(fn=cmd_webhook)
    s = sub.add_parser("status", aliases=["list"], help="friends, today's progress, last run")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("send", help="send today's flames to friends not served yet")
    s.add_argument("--dry-run", action="store_true", help="do everything but press Enter (type, then erase)")
    s.add_argument("--jitter", type=int, default=0, metavar="MIN", help="wait a random 0..MIN minutes first")
    s.add_argument("--only", nargs="+", metavar="USER", help="only these friends")
    s.add_argument("--force", action="store_true", help="send even to friends already served today")
    s.add_argument("--window", action="store_true", help="show the browser window (debugging)")
    s.add_argument("-q", "--quiet", action="store_true", help="print only problems (for cron)")
    s.add_argument("-v", "--verbose", action="store_true", help="print debug screenshot paths on failure")
    s.set_defaults(fn=cmd_send)
    sub.add_parser("schedule", help="how to run it every day (cron, Task Scheduler)").set_defaults(fn=cmd_schedule)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    store = Store(a.home)
    try:
        return a.fn(a, store)
    except KeyboardInterrupt:
        err("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
