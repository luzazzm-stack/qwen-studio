#!/usr/bin/env python3
"""Qwen Control — integrated desktop shell (WebView2).

The whole UI is shell.html rendered in a native WebView2 window; this module is the
backend behind it. Collapsed it is a VPN-style switch; when the session is READY the
window grows and the chat UI is embedded in a frame.

Run:  pythonw qwen_shell.py      (or start-shell.bat)
"""
import atexit, json, os, queue, re, shutil, signal, subprocess, sys, threading, time, webbrowser
from pathlib import Path

import requests
import webview

try:
    import pystray
    from PIL import Image, ImageDraw
    HAVE_TRAY = True
except Exception:
    HAVE_TRAY = False

# ------------------------------------------------------------------ config
if getattr(sys, "frozen", False):
    HERE = Path(sys.executable).resolve().parent.parent.parent   # desktop/app/QwenStudio/Qwen Studio.exe -> desktop/
else:
    HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
os.environ["QWEN_APP_HOME"] = str(HERE)
if sys.stdout is None or sys.stderr is None:
    try:
        sys.stdout = sys.stderr = open(HERE / "app.log", "a", buffering=1, encoding="utf-8")
    except Exception:
        import io as _io
        sys.stdout = sys.stderr = _io.StringIO()
sys.path.insert(0, str(HERE))
import kaggle_ctl  # in-process Kaggle API: no CLI start-up cost, no browser
KERNEL_DIR = ROOT / "kernel"
AUTH_FILE = ROOT / "gateway" / "auth.json"
ENDPOINT_FILE = ROOT / "app" / "endpoint.txt"
OPENCODE_CFG = Path.home() / ".config" / "opencode" / "opencode.jsonc"
KERNEL_ID = "luzamhj/qwen-3-8-27b-uncensered"
KERNEL_URL = f"https://www.kaggle.com/code/{KERNEL_ID}"
FIXED_URL = "https://luzaqwen.loca.lt"

SMALL = (470, 320)   # fits the caption buttons + activity log
LARGE = (1080, 720)
POLL_EVERY = 8
BOOT_BUDGET = 15 * 60
QUEUE_BUDGET = 2 * 3600   # how long to sit waiting for Kaggle to hand out a 2xT4
QUEUE_POLL = 5
BEAT_EVERY = 60
WATCH_TTL = 10800
SAFETY_HOURS = 4          # local auto-stop; the kernel's own cap is 12 h
PREFS_FILE = HERE / "shell_prefs.json"    # file:// localStorage is unreliable in WebView2
ICON_ICO = HERE / "qwen.ico"
ICON_PNG = HERE / "qwen_256.png"

CFG, API_KEY, NTFY_TOPIC = {}, "", ""


def reload_cfg():
    """(Re)read gateway/auth.json - also called right after first-run setup creates it."""
    global CFG, API_KEY, NTFY_TOPIC, FIXED_URL, KERNEL_ID, KERNEL_URL
    CFG = json.loads(AUTH_FILE.read_text(encoding="utf-8")) if AUTH_FILE.exists() else {}
    API_KEY = CFG.get("apikey", "")
    NTFY_TOPIC = CFG.get("ntfy_topic", "")
    # A reserved ngrok domain / Cloudflare named tunnel never moves. Installed copies have
    # neither: their address is announced on the private ntfy topic instead.
    _perm = CFG.get("ngrok_domain") or CFG.get("cf_hostname")
    if _perm:
        FIXED_URL = "https://" + _perm.strip().rstrip("/")
    elif NTFY_TOPIC:
        FIXED_URL = ""
    if CFG:
        KERNEL_ID = kaggle_ctl.kernel_id()
        KERNEL_URL = f"https://www.kaggle.com/code/{KERNEL_ID}"


def ntfy_latest(since=None):
    """Newest server address announced on our ntfy topic (optionally only after `since`)."""
    if not NTFY_TOPIC:
        return None
    try:
        r = requests.get(f"https://ntfy.sh/{NTFY_TOPIC}/json",
                         params={"poll": "1", "since": str(int(since)) if since else "12h"}, timeout=15)
        url = None
        for line in r.text.splitlines():
            try:
                m = json.loads(line).get("message", "").strip()
            except Exception:
                continue
            if m.startswith("https://"):
                url = m
        return url
    except Exception:
        return None


reload_cfg()
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

OFF, CONNECTING, ON, STOPPING = "off", "connecting", "on", "stopping"

# Kept OUT of the Api instance on purpose: pywebview walks the js_api object and would
# poke WebView2 COM members from a non-UI thread, hanging the window.
_WIN = {"w": None}


def read_endpoint():
    if NTFY_TOPIC and not ENDPOINT_FILE.exists():
        return ntfy_latest() or ""
    """Last known endpoint, or the fixed tunnel. Never raises: a locked, deleted or
    non-UTF-8 endpoint.txt used to kill the start thread and strand the app in
    'Starting…' with no way back short of killing the process."""
    try:
        if ENDPOINT_FILE.exists():
            return (ENDPOINT_FILE.read_text(encoding="utf-8").strip() or FIXED_URL)
    except Exception:
        pass
    return FIXED_URL


def sh(args, timeout=180):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, **NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"


class Api:
    """Everything the HTML can call, plus the session lifecycle behind it."""

    def __init__(self):
        self.mode = OFF
        self.url = ""
        self.status = "Tap to start the server"
        self.sub = ""
        self.deadline = 0
        self.expanded = False
        self.closing = False
        self.cancel_ev = threading.Event()
        self._tray = None
        self._tray_thread = None
        self._quitting = False
        self._stop_sent = False
        self._life = threading.Lock()
        self._probing = True          # blocks Start until _detect_existing has answered
        self._started = False         # ready() is idempotent; only the first call probes
        self._worker = None           # the live start/wait thread, so a dead one can be spotted
        self._jsq = queue.Queue()
        threading.Thread(target=self._js_pump, daemon=True).start()

    # -------------------------------------------------- js bridge helpers
    def _js(self, code):
        """Queue JS for the dispatcher thread.

        evaluate_js() must never be called synchronously from inside a js_api method:
        the WebView thread is blocked waiting for that method to return, so it would
        deadlock waiting on itself (the window shows as Not Responding).
        """
        if self.closing or not _WIN["w"]:
            return
        self._jsq.put(code)

    def _js_pump(self):
        while True:
            code = self._jsq.get()
            if code is None:        # retirement sentinel; `continue` made it dead code
                return
            if self.closing:
                continue
            try:
                _WIN["w"].evaluate_js(code)
            except Exception:
                pass

    def log(self, msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        self._js("window.pushLog(%s)" % json.dumps(line))

    def toast(self, msg):
        self._js("window.showToast(%s)" % json.dumps(msg))

    def push(self, expand=False):
        payload = {"mode": self.mode, "url": self.url, "status": self.status,
                   "sub": self.sub, "deadline": self.deadline, "expand": expand}
        if expand and self.mode == ON:
            # local copy needs no login; fall back to the hosted page if staging failed
            payload["frameUrl"] = self.local_ui_url() or self.signin_url()
        self._js("window.pushState(%s)" % json.dumps(payload))
        self._paint_tray()

    def set(self, mode=None, status=None, sub=None, expand=False):
        if mode is not None:
            self.mode = mode
        if status is not None:
            self.status = status
        if sub is not None:
            self.sub = sub
        self.push(expand=expand)

    # -------------------------------------------------- called from JS
    def save_theme(self, theme):
        try:
            PREFS_FILE.write_text(json.dumps({"theme": theme}), encoding="utf-8")
        except Exception:
            pass
        return True

    def _load_theme(self):
        try:
            return json.loads(PREFS_FILE.read_text(encoding="utf-8")).get("theme", "warm")
        except Exception:
            return "warm"

    def ready(self):
        """Called on every 'pywebviewready'. That fires again on any page reload, so
        this must be idempotent: the old version reset a LIVE session to OFF and
        re-probed, which showed up as the app spontaneously disconnecting."""
        self.apply_window_icon()
        self._js("window.applyTheme(%s)" % json.dumps(self._load_theme()))
        if self._started:
            self.push(expand=(self.mode == ON and self.expanded))
            return True
        self._started = True
        self.set(OFF, "Checking for a running server…", "")
        self.log("Qwen Control ready.")
        threading.Thread(target=self._detect_existing, daemon=True).start()
        return True

    def start(self):
        if self._probing:
            self.log("Still checking whether a server is already running - one moment.")
            return False
        if self.mode != OFF:
            # A CONNECTING mode whose worker thread has died (an unhandled error in a
            # daemon thread is silent under pythonw) used to wedge the app forever:
            # Start refused because mode != OFF and nothing was left to change it.
            w = self._worker
            if self.mode == CONNECTING and (w is None or not w.is_alive()):
                self.log("Previous attempt ended unexpectedly - resetting.")
                self.url = ""
                self.set(OFF, "Tap to start the server", "")
            else:
                return False
        self.cancel_ev.clear()
        self._worker = threading.Thread(target=self._guarded_start, daemon=True)
        self._worker.start()
        return True

    def _spawn_wait(self, state, url):
        """Adopt an existing run on a tracked, exception-guarded thread, so a failure
        cannot leave the app stuck in CONNECTING with a Start button that refuses."""
        def run():
            try:
                self.wait_for_server(state, url)
            except Exception as e:
                self.log("Wait failed: %s: %s" % (type(e).__name__, e))
                self.url = ""
                self.set(OFF, "Connection failed", "see the log, then try again")
        self._worker = threading.Thread(target=run, daemon=True)
        self._worker.start()

    def _guarded_start(self):
        """Never let an exception kill the thread silently - that is what left the
        app stuck on 'Starting…' with a Start button that did nothing."""
        try:
            self.start_flow()
        except Exception as e:
            self.log("Start failed: %s: %s" % (type(e).__name__, e))
            self.url = ""
            self.set(OFF, "Start failed", "see the log, then try again")

    def stop(self):
        if self.mode != ON:
            return False
        threading.Thread(target=self.stop_flow, daemon=True).start()
        return True

    def cancel(self):
        self.cancel_ev.set()
        self.log("Cancelling…")
        return True

    # -------------------------------------------------- native window helpers
    def apply_window_icon(self):
        """WM_SETICON on our HWND: fixes the taskbar and Alt-Tab entry, which would
        otherwise show the generic pythonw.exe icon."""
        try:
            import win32api, win32con, win32gui
            h = self._hwnd()
            if not h or not ICON_ICO.exists():
                return False
            for size, which in ((32, win32con.ICON_BIG), (16, win32con.ICON_SMALL)):
                hic = win32gui.LoadImage(0, str(ICON_ICO), win32con.IMAGE_ICON,
                                         size, size, win32con.LR_LOADFROMFILE)
                win32api.SendMessage(h, win32con.WM_SETICON, which, hic)
            # also set the AppUserModelID so Windows groups/pins it as its own app
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "theev.QwenControl")
            except Exception:
                pass
            return True
        except Exception:
            return False

    def _hwnd(self):
        """OUR window's handle - never a title-only FindWindow.

        A bare FindWindow(None, "Qwen Control") matches any window with that title:
        a second instance, or the old qwen_control.py app. Acting on a foreign HWND
        set the wrong icon, read the wrong maximise state, and a SendMessage to a
        hung foreign window would block this thread forever. Filter by our PID and
        cache the answer."""
        h = getattr(self, "_hwnd_cache", 0)
        if h:
            return h
        try:
            import win32gui, win32process
            pid = os.getpid()
            found = []
            def cb(hw, _):
                if win32gui.GetWindowText(hw) == "Qwen Control":
                    _, p = win32process.GetWindowThreadProcessId(hw)
                    if p == pid:
                        found.append(hw)
                return True
            win32gui.EnumWindows(cb, None)
            if found:
                self._hwnd_cache = found[0]
                return found[0]
        except Exception:
            pass
        return 0

    def _is_maximized(self):
        """True / False / None - None means 'cannot tell' (no pywin32, no HWND).

        The caller must treat None as 'do not touch the window': guessing False here
        is what let a resize yank a maximised window back to floating."""
        try:
            import win32con, win32gui
            h = self._hwnd()
            if not h:
                return None
            pl = win32gui.GetWindowPlacement(h)
            return pl[1] == win32con.SW_SHOWMAXIMIZED
        except Exception:
            return None

    def frame_url(self):
        """Called by the page when it expands on its own (the 'open' button).

        None (not the bare URL) outside ON: mid-boot the bare URL is a login form or
        a 503, and the page's iframe latch would keep showing it after connect."""
        if self.mode != ON:
            return None
        return self.local_ui_url() or self.signin_url()

    def set_expanded(self, on):
        on = bool(on)
        if on == self.expanded:
            return True          # no transition, no resize - never fight a user resize
        self.expanded = on
        # A maximised (or unknowable) window is the user's business - leave it alone.
        if self._is_maximized() is not False:
            return True
        w, h = LARGE if on else SMALL
        try:
            _WIN["w"].resize(w, h)
        except Exception:
            pass
        return True

    # -------------------------------------------------- first-run setup (installed copies)
    def setup_state(self):
        """needs: no gateway/auth.json yet (fresh install). user: Kaggle account signed in on this PC."""
        return {"needs": not AUTH_FILE.exists(),
                "user": kaggle_ctl.oauth_user() or (kaggle_ctl._creds()[0] if Path(kaggle_ctl.KAGGLE_JSON).exists() else "")}

    def open_url(self, url):
        if isinstance(url, str) and url.startswith("https://"):
            webbrowser.open(url)
        return True

    def kaggle_signin(self):
        """Blocks until the user clicks Allow on Kaggle's page (runs on pywebview's API thread)."""
        try:
            user = kaggle_ctl.oauth_login()
            self.log("Signed in to Kaggle as " + user)
            return {"ok": True, "user": user}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def finish_setup(self):
        """Create this install's private server settings, then check Kaggle accepts us."""
        import secrets, hashlib
        user = kaggle_ctl.oauth_user() or kaggle_ctl._creds()[0]
        if not user:
            return {"ok": False, "error": "Sign in to Kaggle first."}
        if not AUTH_FILE.exists():
            salt = secrets.token_hex(16)
            pw = secrets.token_urlsafe(12)
            cfg = {"user": user, "kaggle_user": user, "salt": salt,
                   "pwhash": hashlib.sha256((salt + pw).encode()).hexdigest(),
                   "apikey": "qs-" + secrets.token_urlsafe(24), "secret": secrets.token_hex(32),
                   "tunnel": "cloudflared", "ntfy_topic": "qwen-" + secrets.token_hex(12),
                   "kernel_slug": "qwen-studio-server"}
            AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
            AUTH_FILE.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
            (ROOT / "CREDENTIALS.txt").write_text(
                "Qwen Studio - website login (only needed in a normal browser)\n"
                f"user: {user}\npassword: {pw}\nAPI key (for other apps): {cfg['apikey']}\n", encoding="utf-8")
        reload_cfg()
        try:
            kaggle_ctl.reset()
            kaggle_ctl.check()
        except Exception as e:
            return {"ok": False, "error": "Kaggle did not accept the sign-in: " + str(e)[:160]}
        self.log("Setup finished. Press the switch to start your server.")
        return {"ok": True, "user": user}

    def copy_key(self):
        if not API_KEY:
            self.toast("no API key in gateway/auth.json")
            return False
        try:
            import ctypes
            CF_UNICODETEXT = 13
            u = ctypes.windll.user32
            k = ctypes.windll.kernel32
            u.OpenClipboard(0)
            u.EmptyClipboard()
            buf = ctypes.create_unicode_buffer(API_KEY)
            size = ctypes.sizeof(buf)
            h = k.GlobalAlloc(0x2000, size)
            ctypes.memmove(k.GlobalLock(h), buf, size)
            k.GlobalUnlock(h)
            u.SetClipboardData(CF_UNICODETEXT, h)
            u.CloseClipboard()
        except Exception as e:
            self.toast("clipboard failed: " + type(e).__name__)
            return False
        self.log("API key copied to clipboard.")
        return True

    def local_ui_url(self):
        """URL of a locally-served copy of the chat UI, pre-wired to the gateway.

        Loading the remote page inside the app means the login cookie is set in a
        cross-site iframe, which Chromium refuses to store - the login bounces forever.
        Serving our own copy and handing it the API key sidesteps the cookie entirely,
        so the app needs no login at all (the website still does).
        """
        if not (self.url and API_KEY):
            return None
        try:
            src = ROOT / "app" / "chat.html"
            dst = HERE / "chat_local.html"
            if src.exists():
                shutil.copyfile(src, dst)     # always ship the current UI
            if not dst.exists():
                return None
        except Exception as e:
            self.log("could not stage the local UI: " + type(e).__name__)
            return None
        from urllib.parse import quote
        return "chat_local.html?endpoint=%s&key=%s" % (quote(self.url, safe=""), quote(API_KEY, safe=""))

    def signin_url(self):
        """Return a URL the embedded browser can open already signed in.

        The app knows the API key, so it trades it for a one-shot 60 s ticket rather
        than showing you a login form you have already implicitly passed by launching
        the app. Falls back to the plain URL (which shows the login) if anything fails.
        """
        if not (self.url and API_KEY):
            return self.url
        try:
            # short timeout: this can sit on the path of a UI action, and a slow tunnel
            # must degrade to the login page rather than feel like a hang
            r = requests.post(self.url.rstrip("/") + "/authticket",
                              headers={"Authorization": "Bearer " + API_KEY}, timeout=8)
            if r.status_code == 200:
                t = r.json().get("ticket")
                if t:
                    return self.url.rstrip("/") + "/autologin?t=" + t
            self.log("auto sign-in unavailable (%s) - the login page will show." % r.status_code)
        except Exception as e:
            self.log("auto sign-in failed (%s) - the login page will show." % type(e).__name__)
        return self.url

    def open_external(self):
        if self.url:
            webbrowser.open(self.url)
        return True

    def tray_alive(self):
        """A tray icon the user can actually reach - not just an object we created.
        Hiding the window behind a tray icon that failed to appear leaves the app
        unreachable, and after this conversion the tray menu is where Exit lives."""
        return bool(HAVE_TRAY and self._tray and self._tray_thread
                    and self._tray_thread.is_alive())

    def to_tray(self):
        if self.tray_alive():
            try:
                _WIN["w"].hide()
            except Exception:
                pass
            self.log("Minimised to the tray.")
        else:
            try:
                _WIN["w"].minimize()
            except Exception:
                pass
        return True

    def quit(self):
        threading.Thread(target=self.quit_app, daemon=True).start()
        return True

    # -------------------------------------------------- kaggle log reading
    def read_log(self):
        js = ("(() => { const b=document.body.innerText;"
              "const u=b.match(/https:\\/\\/[a-z0-9-]+\\.(?:loca\\.lt|trycloudflare\\.com)/g);"
              "const g=b.match(/GPUs: \\[[^\\]]*\\][^\\n]*/g);"
              "const m=b.match(/MULTI-GPU OK[^\\n]*|SINGLE GPU ONLY[^\\n]*/g);"
              "const r=b.match(/RESIDENCY:[^\\n]*/g);"
              "const p=b.match(/LT PASSWORD:[^\\n]*/g);"
              "return JSON.stringify({url:(u||[]).slice(-1)[0]||null,"
              "gpus:(g||[]).slice(-1)[0]||null, multi:(m||[]).slice(-1)[0]||null,"
              "res:(r||[]).slice(-1)[0]||null, pw:(p||[]).slice(-1)[0]||null,"
              "ready:/READY\\. Model is loaded/.test(b), pulling:/Pulling huihui/.test(b),"
              "fatal:(b.match(/FATAL[^\\n]*/g)||[]).slice(-1)[0]||null}); })()")
        rc, out = sh(["playwright-cli", "-s=chrome", "eval", js, "--raw"], timeout=90)
        m = re.search(r'\{.*\}', out.replace('\\"', '"'))
        if not m:
            return None
        for candidate in (m.group(0), m.group(0).encode().decode('unicode_escape')):
            try:
                return json.loads(candidate)
            except Exception:
                continue
        return None

    def ensure_browser(self):
        rc, out = sh(["playwright-cli", "-s=chrome", "eval", "location.host", "--raw"], timeout=60)
        if "kaggle.com" in out:
            return True
        rc, out = sh(["playwright-cli", "attach", "--cdp=chrome"], timeout=90)
        if "created" not in out.lower() and "attached" not in out.lower():
            self.log("Cannot reach Chrome. Open Chrome, then chrome://inspect/#remote-debugging")
            self.log("-> tick 'Allow remote debugging', then try again.")
            return False
        sh(["playwright-cli", "-s=chrome", "goto", KERNEL_URL], timeout=90)
        time.sleep(4)
        sh(["playwright-cli", "-s=chrome", "eval",
            "(()=>{const t=[...document.querySelectorAll('a,button,[role=tab]')]"
            ".find(x=>/^Logs$/i.test((x.textContent||'').trim())); if(t) t.click(); return 1;})()",
            "--raw"], timeout=60)
        return True

    # -------------------------------------------------- start
    def kernel_status(self):
        """'queued' | 'running' | 'idle' | 'unknown' for our Kaggle run.

        A queued run already holds one of the two session slots even though nothing is
        served yet, so pushing another gets refused. Checking this first is the
        difference between "waiting for a GPU" and a bogus "session limit reached".
        """
        try:
            tok = kaggle_ctl.status().lower().replace("_", "")
        except Exception:
            return "unknown"
        if tok == "none":
            return "idle"
        return {"queued": "queued", "running": "running"}.get(
            tok, "idle" if tok in ("complete", "error", "cancelrequested", "cancelacknowledged", "newscript")
            else "unknown")

    def discover_url(self):
        if NTFY_TOPIC:
            u = ntfy_latest()
            return u if u and u.rstrip("/") != (self.url or "").rstrip("/") else None
        if "ngrok" in (FIXED_URL or ""):
            return None          # fixed ngrok domain: the server is always at FIXED_URL
        return self._discover_url_from_log()

    def _discover_url_from_log(self):
        """Read the tunnel address out of the Kaggle log.

        The fixed subdomain is best-effort: when it is on cooldown at boot the kernel
        falls back to a random cloudflared name, and then NOTHING we own points at the
        server - the app, opencode and the saved endpoint all poll a dead address while
        the session runs fine somewhere else. Scraping the log is the only way to learn
        the real one. Needs Chrome, so it degrades to None rather than failing.
        """
        try:
            if not self.ensure_browser():
                return None
            info = self.read_log()
            u = (info or {}).get("url")
            if u and u.rstrip("/") != (self.url or "").rstrip("/"):
                return u.rstrip("/")
        except Exception as e:
            self.log("could not read the Kaggle log (%s)." % type(e).__name__)
        return None

    def wait_for_server(self, state="queued", url=None):
        """Sit on an already-queued/running run until its endpoint serves the model.

        Polls the URL - no browser, no log scraping. `url` is the endpoint the caller
        already verified; it must not be replaced with FIXED_URL, because a run that
        fell back to a cloudflared tunnel lives somewhere else entirely and we would
        spend the whole budget polling a dead host while the real server is up.
        """
        target = (url or self.url or FIXED_URL or "").rstrip("/")
        since = getattr(self, "_pushed_at", 0) or None
        # A run that already holds a GPU is booting, not queueing: give it the same
        # 15 min the push path allows, not the 2 h reserved for Kaggle's queue.
        budget = QUEUE_BUDGET if state == "queued" else BOOT_BUDGET
        self.set(CONNECTING,
                 "Queued for a GPU…" if state == "queued" else "Starting up…",
                 "waiting for a free 2xT4" if state == "queued" else target)
        end = time.time() + budget
        announced = (state == "running")
        weights_msg = False
        last_status = 0.0
        shown = None
        probed_log = False
        t0 = time.time()
        while time.time() < end:
            if self.cancel_ev.is_set():
                self.url = ""
                self.set(OFF, "Cancelled", "")
                return
            if NTFY_TOPIC:
                u = ntfy_latest(since)
                if u and u.rstrip("/") != target:
                    target = u.rstrip("/")
                    self.log("Server address: " + target)
            self.url = target
            alive = bool(target) and not self.endpoint_dead()
            if alive and self.models_ready():
                self.log("Model READY.")
                self.deadline = time.time() + SAFETY_HOURS * 3600
                self.save_endpoint(self.url)
                self.arm_watchdog()
                self.set(ON, "Connected", self.url, expand=True)
                return
            # The status check runs on a wall-clock cadence in BOTH states. Gating it on
            # "endpoint dead" missed the case that actually strands the app: the gateway
            # is a separate, supervisor-restarted process, so /healthz keeps answering
            # while Ollama is wedged - the run dies and nothing ever notices.
            if time.time() - last_status > 30:
                last_status = time.time()
                st = self.kernel_status()
                if st == "idle":
                    self.url = ""
                    self.log("The Kaggle run ended before the model was serving.")
                    self.set(OFF, "Kernel stopped", "press Start to try again")
                    return
                if st == "running" and not announced:
                    announced = True
                    self.log("Got a GPU. Booting Ollama and loading weights…")
                # Running for a while but the address never answers: the fixed
                # subdomain was probably lost at boot. Find where it actually is.
                if st == "running" and not probed_log and time.time() - t0 > 150:
                    probed_log = True
                    self.log("Address not responding - reading the real one from Kaggle…")
                    found = self.discover_url()
                    if found:
                        self.log("Server is actually at: " + found)
                        target = found
                        self.save_endpoint(found)   # app + opencode follow it
            if alive:
                if not weights_msg:
                    weights_msg = True
                    self.log("Server is up - loading the model (a few minutes).")
                if shown != "loading":      # repainting identical state churns the tray
                    shown = "loading"
                    self.set(CONNECTING, "Loading model weights…", target)
            time.sleep(QUEUE_POLL)
        self.url = ""      # leaving a URL behind makes atexit /shutdown a run we adopted
        if state == "queued":
            self.log("Still queued after %d min - Kaggle's GPU queue is busy." % (budget // 60))
            self.log("The run is still in line. Press Start again to keep waiting.")
            self.set(OFF, "Still queued on Kaggle", "press Start to keep waiting")
        else:
            self.log("The run has a GPU but never finished loading after %d min." % (budget // 60))
            self.set(OFF, "Model never loaded", "check the Kaggle page")

    def start_flow(self):
        self.set(CONNECTING, "Starting…", "checking for a running server")
        self.log("=" * 40)
        # A server may already be up (the URL is fixed now, so this is cheap and reliable).
        # Pushing a second kernel in that case just trips Kaggle's 2-session limit.
        probe = read_endpoint()          # keep it: the wait loop must poll THIS host
        self.url = probe
        if not self.endpoint_dead():
            if self.models_ready():
                self.log("A server is already running - connecting to it instead of starting another.")
                self.deadline = time.time() + SAFETY_HOURS * 3600
                self.save_endpoint(self.url)     # keep endpoint.txt + opencode in step
                self.arm_watchdog()
                self.set(ON, "Connected", self.url, expand=True)
                return
            self.log("A server is running but is still loading the model - waiting for it.")
            self.wait_for_server("running", self.url)
            return
        self.url = ""
        # Nothing is answering - but a run may already be queued or booting. Pushing in
        # that case is what produced the misleading "session limit reached".
        self.set(CONNECTING, "Starting…", "checking Kaggle")
        state = self.kernel_status()
        if state == "unknown":
            # Failing open here is what produced the bogus "session limit reached":
            # push, get refused for a slot our own queued run is holding. One retry
            # costs a few seconds and turns most transient CLI/network blips into a
            # definite answer.
            self.log("Could not read the Kaggle status - retrying once.")
            time.sleep(4)
            state = self.kernel_status()
        if state in ("queued", "running"):
            self.log("A run is already %s on Kaggle - waiting for that one." % state)
            self.wait_for_server(state, probe)
            return
        if state == "unknown":
            self.log("Still cannot read the Kaggle status. Pushing anyway - if this is")
            self.log("refused, an earlier run of this notebook is probably still queued.")
        self.set(CONNECTING, "Starting…", "pushing kernel to Kaggle")
        self.log("Booting Kaggle kernel…")
        try:
            self._pushed_at = time.time() - 30
            r = kaggle_ctl.push()          # rebuilds kernel.py from app/ + gateway/, then pushes
            self.log("Pushed to Kaggle (version %s)." % r.get("version"))
        except Exception as e:
            msg = str(e)
            self.log("Kaggle refused the push: " + msg[:200])
            if "session" in msg.lower():
                self.log("Kaggle has 2 sessions open already. Stop one on the Kaggle page, then press Start again.")
                try:
                    webbrowser.open(KERNEL_URL)
                except Exception:
                    pass
                self.set(OFF, "Kaggle session limit reached", "stop an old session, then retry")
            else:
                self.set(OFF, "Could not start", "see the log")
            return
        # The server always lives at the fixed ngrok address, so just wait on it -
        # no Chrome, no log scraping.
        self.wait_for_server("queued", FIXED_URL)

    def save_endpoint(self, url):
        try:
            ENDPOINT_FILE.write_text(url, encoding="utf-8")
        except Exception:
            pass
        try:
            if OPENCODE_CFG.exists():
                t = OPENCODE_CFG.read_text(encoding="utf-8")
                n = re.sub(r'"baseURL": "https://[^"]*"', f'"baseURL": "{url}/v1"', t, count=1)
                if n != t:
                    OPENCODE_CFG.write_text(n, encoding="utf-8")
                    self.log("opencode endpoint updated.")
        except Exception:
            pass

    # -------------------------------------------------- watchdog
    def arm_watchdog(self):
        if not (self.url and API_KEY):
            return
        try:
            r = requests.post(self.url.rstrip("/") + "/watchdog",
                              headers={"Authorization": "Bearer " + API_KEY},
                              json={"ttl": WATCH_TTL}, timeout=20)
            if r.status_code == 200:
                self.log(f"Safety net armed ({WATCH_TTL//60} min).")
        except Exception:
            pass

    def beat_loop(self):
        misses = 0
        while not self.closing:
            if self.mode == ON and self.url and API_KEY:
                alive = False
                try:
                    r = requests.post(self.url.rstrip("/") + "/watchdog",
                                      headers={"Authorization": "Bearer " + API_KEY},
                                      json={"ttl": WATCH_TTL}, timeout=15)
                    alive = r.status_code == 200
                except Exception:
                    alive = False
                # The tunnel drops transient 502s constantly, so one miss means
                # nothing - but silently showing "Connected" through a dead link is
                # how the chat ends up failing with no explanation anywhere.
                if alive:
                    if misses:
                        self.log("Connection restored.")
                        self.set(ON, "Connected", self.url)
                    misses = 0
                else:
                    misses += 1
                    if misses == 3:
                        self.log("Lost contact with the server (the tunnel usually")
                        self.log("rebuilds itself within ~10 min). Still holding the session.")
                        self.set(ON, "Reconnecting…", self.url)
                    elif misses == 12:
                        found = self.discover_url()
                        if found:
                            self.log("Server moved to: " + found)
                            self.url = found
                            self.save_endpoint(found)
                            self.set(ON, "Connected", found)
                            misses = 0
                if self.deadline and time.time() > self.deadline:
                    self.log("Safety timer expired - stopping the session.")
                    self.stop_flow()
            for _ in range(BEAT_EVERY):
                if self.closing:
                    return
                time.sleep(1)

    # -------------------------------------------------- stop
    def stop_flow(self):
        self.set(STOPPING, "Stopping…", "asking the kernel to shut down")
        self.deadline = 0
        ok = False
        if self.url and API_KEY:
            try:
                self.log("POST /shutdown …")
                r = requests.post(self.url.rstrip("/") + "/shutdown",
                                  headers={"Authorization": "Bearer " + API_KEY}, timeout=25)
                self.log(f"gateway replied {r.status_code}")
                ok = r.status_code == 200
            except Exception as e:
                self.log("shutdown call failed: " + type(e).__name__)
        if ok:
            time.sleep(8)
            if self.endpoint_dead():
                self.log("Session stopped.")
                self.url = ""
                self.set(OFF, "Disconnected", "")
                return
            self.log("Still responding - using the Kaggle fallback.")
        self.log("Stopping the session from Kaggle…")
        if self.kaggle_stop():
            self.log("Session stopped from Kaggle.")
            self.url = ""
            self.set(OFF, "Disconnected", "")
        else:
            self.log("Could not stop automatically - use the Kaggle page.")
            self.set(OFF, "Stop failed", "stop it on kaggle.com")

    def models_ready(self):
        """True once Ollama actually holds the weights.

        /healthz answers as soon as the gateway binds, which is several minutes before
        the 16 GB download finishes - reporting "Connected" then sends you into a chat
        that cannot answer.
        """
        if not self.url:
            return False
        base = self.url.rstrip("/")
        try:
            # /readyz is unauthenticated, reports VRAM residency, and does NOT feed the
            # kernel's dead-man switch - polling it cannot keep a forgotten session alive.
            r = requests.get(base + "/readyz", timeout=20)
            if r.status_code == 200:
                return bool((r.json() or {}).get("ready"))
        except Exception:
            pass        # fall through - a timeout is "cannot tell", not "not loaded"
        # Older gateway without /readyz: fall back to the authenticated model list. A key
        # problem must not read as "still loading" forever, so an auth failure means
        # "cannot tell" - let the connection through and surface real errors in the chat.
        try:
            r = requests.get(base + "/v1/models",
                             headers={"Authorization": "Bearer " + API_KEY} if API_KEY else {},
                             timeout=20)
            if r.status_code in (401, 403):
                # Cannot verify, so do not report "still loading" forever - but say so:
                # a stale key means the chat will 401 and the watchdog will not arm.
                self.log("WARNING: the server rejected our API key. Connecting anyway,")
                self.log("but chat and the safety timer will fail - check CREDENTIALS.md.")
                return True
            return r.status_code == 200 and bool((r.json() or {}).get("data"))
        except Exception:
            return False

    def endpoint_dead(self):
        try:
            r = requests.get(self.url.rstrip("/") + "/healthz", timeout=12)
            return r.status_code != 200 or r.text.strip() != "ok"
        except Exception:
            return True

    def kaggle_stop(self):
        try:
            kaggle_ctl.delete()        # removes the run; Kaggle frees the GPU at once
            for _ in range(10):
                time.sleep(3)
                if not self.url or self.endpoint_dead():
                    return True
        except Exception as e:
            self.log("Kaggle stop failed: " + type(e).__name__)
        return self._kaggle_stop_browser()

    def _kaggle_stop_browser(self):
        if not self.ensure_browser():
            return False
        sh(["playwright-cli", "-s=chrome", "goto", KERNEL_URL], timeout=90)
        time.sleep(5)
        steps = [
            "(()=>{const b=[...document.querySelectorAll('button,[role=button]')]"
            ".find(x=>/View Active Events/i.test(x.getAttribute('aria-label')||x.textContent||''));"
            "if(b){b.click();return 'panel';}return 'no panel';})()",
            "(()=>{const m=[...document.querySelectorAll('button,[role=button]')]"
            ".filter(b=>/More options for/i.test(b.getAttribute('aria-label')||''));"
            "if(!m.length)return 'none';m[0].click();return 'menu';})()",
            "(()=>{const e=[...document.querySelectorAll('li,[role=menuitem],button,div')]"
            ".find(x=>/Stop Session/.test((x.textContent||'').trim())&&(x.textContent||'').trim().length<30);"
            "if(!e)return 'no item';e.click();return 'stopped';})()",
        ]
        for js in steps:
            rc, out = sh(["playwright-cli", "-s=chrome", "eval", js, "--raw"], timeout=90)
            if "none" in out or "no panel" in out:
                return False
            time.sleep(3)
        time.sleep(6)
        return self.endpoint_dead() if self.url else True

    # -------------------------------------------------- tray
    def _tray_image(self, on):
        """The real app icon; dimmed to ~45% while the server is off.

        Cached: state pushes repaint the tray, and rebuilding the image from disk
        (open + LANCZOS + per-pixel alpha) on every push was measurable jank."""
        cache = getattr(self, "_tray_imgs", None)
        if cache is None:
            cache = self._tray_imgs = {}
        if on in cache:
            return cache[on]
        try:
            img = Image.open(ICON_PNG).convert("RGBA").resize((64, 64), Image.LANCZOS)
            if not on:
                faded = img.copy()
                alpha = faded.getchannel("A").point(lambda a: int(a * 0.45))
                img = faded
                img.putalpha(alpha)
        except Exception:
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            col = (124, 58, 237, 255) if on else (122, 115, 106, 255)
            d.ellipse((6, 6, 58, 58), outline=col, width=6)
            d.rectangle((29, 12, 35, 34), fill=col)
        cache[on] = img
        return img

    def start_tray(self):
        if not HAVE_TRAY:
            return
        def show(*_):
            try:
                _WIN["w"].show()
                _WIN["w"].restore()
            except Exception:
                pass
        def toggle(*_):
            # cover every state: the old ON/not-ON split made this item a silent
            # no-op during CONNECTING (start refuses) and STOPPING (stop refuses)
            if self.mode == ON:
                self.stop()
            elif self.mode == OFF:
                self.start()
            elif self.mode == CONNECTING:
                self.cancel()
        def toggle_label(_):
            return {ON: "Turn OFF", OFF: "Turn ON",
                    CONNECTING: "Cancel start"}.get(self.mode, "Stopping…")
        menu = pystray.Menu(
            pystray.MenuItem("Show Qwen Control", show, default=True),
            pystray.MenuItem(toggle_label, toggle),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit (stops the server)", lambda *_: self.quit()),
        )
        self._tray = pystray.Icon("qwen", self._tray_image(False), "Qwen Control", menu)
        self._tray_thread = threading.Thread(target=self._tray.run, daemon=True)
        self._tray_thread.start()

    def _paint_tray(self):
        if not self._tray:
            return
        if getattr(self, "_tray_mode", None) == self.mode:
            return          # nothing changed; repainting per push is pointless churn
        self._tray_mode = self.mode
        try:
            self._tray.icon = self._tray_image(self.mode == ON)
            self._tray.title = "Qwen Control - " + self.mode.upper()
            self._tray.update_menu()
        except Exception:
            pass

    # -------------------------------------------------- lifecycle
    def _detect_existing(self):
        self._probing = True
        try:
            self._detect_existing_inner()
        finally:
            self._probing = False

    def _detect_existing_inner(self):
        url = read_endpoint()
        try:
            r = requests.get(url.rstrip("/") + "/healthz", timeout=10)
            if r.status_code == 200 and r.text.strip() == "ok":
                self.url = url
                if self.models_ready():
                    self.deadline = time.time() + SAFETY_HOURS * 3600
                    self.save_endpoint(url)
                    self.set(ON, "Connected", url, expand=True)   # show the chat straight away
                    self.log("Found a server already running.")
                    self.arm_watchdog()
                    return
                self.log("Found a server, still loading the model - waiting for it.")
                # Claim the mode BEFORE spawning the waiter: _detect_existing clears
                # _probing the moment this returns, and a Start click landing before the
                # new thread flipped the mode would run a second start_flow alongside it.
                self.mode = CONNECTING
                self._spawn_wait("running", url)
                return
        except Exception:
            pass
        # Nothing served yet - but if a run is already in Kaggle's queue, latch onto it
        # so the app reports "queued" instead of inviting a start that cannot succeed.
        st = self.kernel_status()
        if st in ("queued", "running"):
            self.log("A Kaggle run is already %s - waiting for it to come up." % st)
            if st == "queued":
                self.log("The loca.lt password, if the browser asks for one, is on the Kaggle page.")
            self.mode = CONNECTING          # before the thread starts; see note above
            self._spawn_wait(st, url)
            return
        self.log("No server running. Tap the switch to start one.")

    def quit_app(self):
        with self._life:
            if self._quitting:
                return
            self._quitting = True
        self.cancel_ev.set()
        if self.mode != OFF or self.url:
            self.log("Exiting - stopping the Kaggle session first…")
            try:
                self.stop_flow()
            except Exception:
                pass
            # The window is usually hidden in the tray at this point, so a "stop
            # failed" warning pushed to it dies unseen. Surface the failure the one
            # way that survives our exit: open the Kaggle page for the user.
            if "fail" in (self.status or "").lower():
                try:
                    webbrowser.open(KERNEL_URL)
                except Exception:
                    pass
                time.sleep(1.0)
        self.closing = True
        if self._tray:
            try:
                self._tray.visible = False
                self._tray.stop()
            except Exception:
                pass
            if self._tray_thread:
                self._tray_thread.join(timeout=2)
        os._exit(0)

    def emergency_stop(self, *_):
        with self._life:
            if self._stop_sent or not self.url or not API_KEY:
                return
            self._stop_sent = True
        try:
            requests.post(self.url.rstrip("/") + "/shutdown",
                          headers={"Authorization": "Bearer " + API_KEY}, timeout=3)
        except Exception:
            pass

    def install_handlers(self):
        atexit.register(self.emergency_stop)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: self.quit())
            except Exception:
                pass
        if os.name == "nt":
            threading.Thread(target=self._session_end_hook, daemon=True).start()

    def _session_end_hook(self):
        """Windows logoff / restart / shutdown reaches a GUI app as WM_QUERYENDSESSION."""
        try:
            import win32api, win32con, win32gui
        except Exception:
            return
        def on_end(hwnd, msg, wp, lp):
            self.emergency_stop()
            return True
        try:
            wc = win32gui.WNDCLASS()
            wc.hInstance = win32api.GetModuleHandle(None)
            wc.lpszClassName = "QwenShellSessionEnd"
            wc.lpfnWndProc = {win32con.WM_QUERYENDSESSION: on_end,
                              win32con.WM_ENDSESSION: on_end}
            cls = win32gui.RegisterClass(wc)
            win32gui.CreateWindow(cls, "qwen-session-hook", win32con.WS_OVERLAPPED,
                                  0, 0, 0, 0, 0, 0, wc.hInstance, None)
            win32gui.PumpMessages()
        except Exception:
            pass


def main():
    api = Api()
    # A normal framed window: Windows draws the title bar, caption buttons and resize
    # borders, and handles drag/snap/shake natively. The earlier frameless window
    # re-implemented all of that in HTML and it hung, glitched on resize and sometimes
    # could not be moved - fake chrome is strictly worse than the real thing.
    win = webview.create_window(
        "Qwen Control", str(HERE / "shell.html"), js_api=api,
        width=SMALL[0], height=SMALL[1], resizable=True,
        background_color="#1c1a18",
        min_size=(380, 300))
    _WIN["w"] = win

    # The native X hides to the tray and keeps the session alive; Exit (which stops
    # the session) lives in the tray menu. This also catches Alt+F4/taskbar close.
    # WITHOUT a working tray there is no Exit anywhere, so vetoing the close would
    # make the app unkillable except via Task Manager - in that case let the close
    # through; atexit's emergency_stop still sends /shutdown to the session.
    def _on_closing():
        if api.tray_alive():
            api.to_tray()
            return False      # veto: window hides, session keeps running
        return True           # no tray: allow a real close
    win.events.closing += _on_closing

    # Expanding while maximised skips the resize (correctly); restoring later would
    # otherwise land on the old collapsed bounds with the chat crammed into them.
    # Deliberately narrow: only grow when the restored bounds are still collapsed-size
    # while the chat is showing - anything else is a size the user chose, keep it.
    def _on_restored():
        try:
            if api.expanded and win.width < SMALL[0] + 60:
                win.resize(*LARGE)
        except Exception:
            pass
    try:
        win.events.restored += _on_restored
    except Exception:
        pass                  # older pywebview without the event: cosmetic loss only

    api.start_tray()
    api.install_handlers()
    threading.Thread(target=api.beat_loop, daemon=True).start()
    # user_agent: loca.tt shows a "Tunnel website ahead" interstitial to anything that
    #   looks like a browser. A non-browser UA is let straight through (verified: 200 vs 511).
    # private_mode=False + storage_path: keep cookies on disk so the login survives restarts.
    webview.start(debug=False,
                  http_server=True,          # serve the folder so the UI copy is same-origin
                  user_agent="QwenControl/1.0 (+desktop shell)",
                  private_mode=False,
                  storage_path=str(HERE / "webview_data"))


if __name__ == "__main__":
    main()
