"""Paths, config, state and locking. No browser code in this module."""
import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path

APP = "tiktok-flame"
USER_RE = re.compile(r"^[a-z0-9._]{2,24}$")
DEFAULT_MESSAGE = "🔥"
MAX_FRIENDS = 50
MAX_MESSAGE = 200
MAX_RUNS = 30


def data_dir():
    """Where config, state and the browser profile live. Override with $TIKTOK_FLAME_HOME."""
    env = os.environ.get("TIKTOK_FLAME_HOME")
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP


# ---------- pure helpers ----------

def clean_user(s):
    """'@Name', 'name' or a profile link (tiktok.com/@name) -> 'name' (lowercase), or None if invalid."""
    s = str(s or "").strip()
    m = re.search(r"tiktok\.com/@([^/?#\s]+)", s)
    if m:
        s = m.group(1)
    s = s.lstrip("@").strip().lower()
    return s if USER_RE.match(s) else None


def clean_message(s):
    s = str(s or "").strip()
    return s[:MAX_MESSAGE] if s else None


def normalize_config(raw):
    raw = raw if isinstance(raw, dict) else {}
    friends, seen = [], set()
    for f in raw.get("friends") or []:
        d = f if isinstance(f, dict) else {"user": f}
        u = clean_user(d.get("user"))
        if not u or u in seen:
            continue
        seen.add(u)
        item = {"user": u, "on": bool(d.get("on", True))}
        if clean_message(d.get("message")):
            item["message"] = clean_message(d.get("message"))
        friends.append(item)
    webhook = str(raw.get("webhook") or "").strip()
    return {"friends": friends[:MAX_FRIENDS],
            "message": clean_message(raw.get("message")) or DEFAULT_MESSAGE,
            "webhook": webhook if webhook.startswith(("https://", "http://")) else None}


def pending(cfg, state, today):
    """[(user, message)] still to send today: enabled friends not already served."""
    sent = state.get("sent") or {}
    return [(f["user"], f.get("message") or cfg["message"])
            for f in cfg["friends"] if f["on"] and sent.get(f["user"]) != today]


# ---------- files ----------

def save_json(path, obj):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _lock(f):
    if os.name == "nt":
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(f):
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


class Store:
    """Everything on disk. The browser profile (= your TikTok session) never leaves `profile/`."""

    def __init__(self, root=None):
        self.root = Path(root) if root else data_dir()
        self.config_path = self.root / "config.json"
        self.state_path = self.root / "state.json"
        self.profile = self.root / "profile"
        self.debug = self.root / "debug"
        self.lock_path = self.root / "run.lock"
        self.qr_path = self.root / "login-qr.png"

    def ensure(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.root, 0o700)

    # config
    def config(self):
        return normalize_config(load_json(self.config_path, {}))

    def save_config(self, cfg):
        self.ensure()
        save_json(self.config_path, normalize_config(cfg))

    # state (written only while holding the lock, or by single-shot commands)
    def state(self):
        st = load_json(self.state_path, {})
        st = st if isinstance(st, dict) else {}
        st.setdefault("logged_in", False)
        st.setdefault("account", None)
        st.setdefault("sent", {})
        st.setdefault("runs", [])
        st.setdefault("running", None)
        return st

    def update_state(self, fn):
        self.ensure()
        st = self.state()
        fn(st)
        save_json(self.state_path, st)
        return st

    @contextlib.contextmanager
    def lock(self, what, wait=0):
        """One browser at a time. Yields False if another run holds the lock after `wait` seconds."""
        self.ensure()
        f = open(self.lock_path, "a+")
        end = time.time() + wait
        while True:
            try:
                _lock(f)
                break
            except OSError:
                if time.time() >= end:
                    f.close()
                    yield False
                    return
                time.sleep(2)
        self.update_state(lambda s: s.update(running={"what": what, "pid": os.getpid(), "since": int(time.time())}))
        try:
            yield True
        finally:
            self.update_state(lambda s: s.update(running=None))
            _unlock(f)
            f.close()
