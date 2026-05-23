#!/usr/bin/env python3
"""
Unifi Web Server
FastAPI backend that mirrors gui.py's process management over HTTP + WebSocket.

Run:
    uvicorn server:app --host 0.0.0.0 --port 8000 --reload

Then open http://localhost:8000 in your browser.
"""

import asyncio
import csv
import io
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Google OAuth + session auth ───────────────────────────────────────────────
import httpx
from itsdangerous import TimestampSigner, BadSignature

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
SECRET_KEY           = os.environ.get("SECRET_KEY", "dev-secret-please-change")
APP_URL              = os.environ.get("APP_URL", "http://localhost:8000")

_signer         = TimestampSigner(SECRET_KEY)
_pending_signer = TimestampSigner(SECRET_KEY + "-pending")

def _make_session(email: str) -> str:
    return _signer.sign(email.encode()).decode()

def _verify_session(cookie: str) -> "str | None":
    try:
        return _signer.unsign(cookie.encode(), max_age=60*60*24*30).decode()
    except BadSignature:
        return None

def _make_pending(email: str) -> str:
    return _pending_signer.sign(email.encode()).decode()

def _verify_pending(cookie: str) -> "str | None":
    try:
        return _pending_signer.unsign(cookie.encode(), max_age=600).decode()
    except BadSignature:
        return None

OPEN_PATHS = {"/", "/auth/login", "/auth/callback", "/auth/beta", "/api/auth/verify"}

class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if not GOOGLE_CLIENT_ID:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in OPEN_PATHS or path.startswith("/ws"):
            await self.app(scope, receive, send)
            return
        # Parse session cookie from headers
        cookies = {}
        for name, value in scope.get("headers", []):
            if name == b"cookie":
                for part in value.decode().split(";"):
                    if "=" in part:
                        k, v = part.strip().split("=", 1)
                        cookies[k.strip()] = v.strip()
        session = cookies.get("session", "")
        if not _verify_session(session):
            accept = ""
            for name, value in scope.get("headers", []):
                if name == b"accept":
                    accept = value.decode()
            if accept.startswith("text/html"):
                response = RedirectResponse("/auth/login")
            else:
                response = JSONResponse({"detail": "Not authenticated"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

# ── Paths (same as gui.py) ────────────────────────────────────────────────────
DATASETS_DIR        = Path("datasets")
SAVED_SEARCHES_FILE = DATASETS_DIR / "saved_searches.json"
CONFIG_FILE         = DATASETS_DIR / "config.json"
BARGAINS_FILE       = DATASETS_DIR / "bargains.json"
MISSED_FILE         = DATASETS_DIR / "missed_deals.json"
THRESHOLDS_FILE     = DATASETS_DIR / "thresholds.json"
APPROVED_EMAILS_FILE = DATASETS_DIR / "approved_emails.json"
USED_CODES_FILE      = DATASETS_DIR / "used_codes.json"
PYTHON              = sys.executable

DATASETS_DIR.mkdir(exist_ok=True)


def _load_approved_emails() -> set:
    if not APPROVED_EMAILS_FILE.exists():
        return set()
    try:
        return set(json.loads(APPROVED_EMAILS_FILE.read_text()))
    except Exception:
        return set()


def _approve_email(email: str) -> None:
    emails = _load_approved_emails()
    emails.add(email)
    DATASETS_DIR.mkdir(exist_ok=True)
    APPROVED_EMAILS_FILE.write_text(json.dumps(list(emails)))


def _load_used_codes() -> set:
    if not USED_CODES_FILE.exists():
        return set()
    try:
        return set(json.loads(USED_CODES_FILE.read_text()))
    except Exception:
        return set()


def _burn_code(code: str) -> None:
    used = _load_used_codes()
    used.add(code.upper())
    DATASETS_DIR.mkdir(exist_ok=True)
    USED_CODES_FILE.write_text(json.dumps(list(used)))


def _valid_beta_code(code: str) -> bool:
    codes = {c.strip().upper() for c in os.environ.get("BETA_CODES", "").split(",") if c.strip()}
    return bool(code) and code.upper() in codes and code.upper() not in _load_used_codes()

CONFIG_DEFAULTS = {
    "ebay_app_id":     "",
    "ebay_cert_id":    "",
    "discord_webhook": "",
}

# ── File helpers (mirrors gui.py) ─────────────────────────────────────────────
def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    DATASETS_DIR.mkdir(exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_searches() -> list:
    return _read_json(SAVED_SEARCHES_FILE, [])


def save_searches(searches: list) -> None:
    _write_json(SAVED_SEARCHES_FILE, searches)


def load_config() -> dict:
    return {**CONFIG_DEFAULTS, **_read_json(CONFIG_FILE, {})}


def save_config(cfg: dict) -> None:
    _write_json(CONFIG_FILE, cfg)


def load_bargains() -> list:
    return _read_json(BARGAINS_FILE, [])


def save_bargains(bargains: list) -> None:
    _write_json(BARGAINS_FILE, bargains)


def load_missed() -> list:
    return _read_json(MISSED_FILE, [])


def save_missed(missed: list) -> None:
    _write_json(MISSED_FILE, missed)


def load_thresholds() -> dict:
    return _read_json(THRESHOLDS_FILE, {})


def save_thresholds(data: dict) -> None:
    _write_json(THRESHOLDS_FILE, data)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _parse_price(val) -> Optional[float]:
    try:
        return float(str(val or "").replace("£", "").replace(",", "").strip())
    except ValueError:
        return None


def _load_sold_median(query: str) -> Optional[float]:
    path = DATASETS_DIR / f"{_slug(query)}_sold.csv"
    if not path.exists():
        return None
    prices = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            p = _parse_price(row.get("price", ""))
            if p and p > 0:
                prices.append(p)
    if not prices:
        return None
    prices.sort()
    return prices[len(prices) // 2]


def _load_platform_prices(query: str, platform: str) -> list:
    path = DATASETS_DIR / f"{_slug(query)}_{platform}_new.csv"
    if not path.exists():
        return []
    prices = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            p = _parse_price(row.get("price", ""))
            if p and p > 0:
                prices.append(p)
    return prices


def _price_stats(prices: list) -> dict:
    if not prices:
        return {}
    arr = sorted(prices)
    n = len(arr)
    return {
        "count":  n,
        "min":    round(arr[0], 2),
        "mean":   round(sum(arr) / n, 2),
        "median": round(arr[n // 2], 2),
        "max":    round(arr[-1], 2),
    }


# ── Process manager ───────────────────────────────────────────────────────────
class ProcessManager:
    """
    Manages listing_watcher.py subprocesses and broadcasts their output
    to all connected WebSocket clients.

    Each subprocess stdout line is either:
      - "BARGAIN_ITEM:{json}" → parsed, saved to bargains.json, broadcast as bargain event
      - anything else         → broadcast as a log line
    """

    def __init__(self):
        self.instances: dict = {}   # name → {process, thread, queue, started_at, interval_min}
        self.bargains:  list = load_bargains()
        self.missed:    list = load_missed()
        self._clients:  set  = set()
        self._loop:     asyncio.AbstractEventLoop | None = None
        self._async_q:  asyncio.Queue | None = None

    # ── WebSocket client registry ─────────────────────────────────────────────
    def register(self, ws: WebSocket) -> None:
        self._clients.add(ws)

    def unregister(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._async_q = asyncio.Queue()

    def _enqueue(self, msg: dict) -> None:
        """Thread-safe push into the asyncio queue."""
        if self._loop and self._async_q:
            self._loop.call_soon_threadsafe(self._async_q.put_nowait, msg)

    async def broadcast_loop(self) -> None:
        """Drain the async queue and push messages to all WebSocket clients."""
        while True:
            msg = await self._async_q.get()
            dead = set()
            for ws in list(self._clients):
                try:
                    await ws.send_json(msg)
                except Exception:
                    dead.add(ws)
            self._clients -= dead

    # ── Subprocess management ─────────────────────────────────────────────────
    def start(self, s: dict) -> None:
        name = s["name"]
        if name in self.instances and self.instances[name]["process"].poll() is None:
            return

        excl_str = ",".join(s.get("exclude_keywords") or [])
        cfg = load_config()
        env = {**os.environ}
        if cfg.get("ebay_app_id"):     env["UNIFI_EBAY_APP_ID"]     = cfg["ebay_app_id"]
        if cfg.get("ebay_cert_id"):    env["UNIFI_EBAY_CERT_ID"]    = cfg["ebay_cert_id"]
        if cfg.get("discord_webhook"): env["UNIFI_DISCORD_WEBHOOK"] = cfg["discord_webhook"]

        cmd = [
            PYTHON, "listing_watcher.py", "--watch",
            "--name",      s["name"],
            "--query",     s["query"],
            "--platforms", ",".join(s["platforms"]),
            "--interval",  str(s["interval_min"]),
            "--exclude",   excl_str,
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                cwd=str(Path(__file__).parent),
                env=env,
            )
        except Exception as e:
            self._enqueue({"type": "log", "name": name,
                           "line": f"Failed to start: {e}",
                           "ts": _ts()})
            return

        q: queue.Queue = queue.Queue()

        def _reader():
            for line in proc.stdout:
                q.put(line.rstrip())
            q.put(f"__EXIT__{proc.wait()}")

        t = threading.Thread(target=_reader, daemon=True)
        t.start()

        self.instances[name] = {
            "process":      proc,
            "thread":       t,
            "queue":        q,
            "started_at":   time.time(),
            "interval_min": s["interval_min"],
        }
        self._enqueue({"type": "log", "name": name,
                       "line": f"Started (PID {proc.pid})", "ts": _ts()})
        self._enqueue({"type": "status"})

    def stop(self, name: str) -> None:
        if name not in self.instances:
            return
        proc = self.instances[name]["process"]
        if proc.poll() is None:
            proc.terminate()
        del self.instances[name]
        self._enqueue({"type": "log", "name": name,
                       "line": "Stopped", "ts": _ts()})
        self._enqueue({"type": "status"})

    def stop_all(self) -> None:
        for name in list(self.instances):
            self.stop(name)

    def status(self) -> dict:
        running = []
        for name, inst in list(self.instances.items()):
            if inst["process"].poll() is None:
                elapsed = time.time() - inst["started_at"]
                interval_s = inst["interval_min"] * 60
                remaining = int(interval_s - (elapsed % interval_s))
                running.append({"name": name, "countdown_s": remaining})
        return {"running": running}

    # ── Background poller (called from asyncio task) ─────────────────────────
    def poll_queues(self) -> None:
        """
        Called every 250 ms from an asyncio background task.
        Drains subprocess stdout queues and enqueues WebSocket messages.
        """
        for name in list(self.instances):
            inst = self.instances.get(name)
            if not inst:
                continue
            try:
                while True:
                    line = inst["queue"].get_nowait()

                    if line.startswith("__EXIT__"):
                        self._enqueue({"type": "log", "name": name,
                                       "line": f"Process ended (exit {line[8:]})",
                                       "ts": _ts()})
                        self.instances.pop(name, None)
                        self._enqueue({"type": "status"})
                        break

                    # Bargain line from the watcher
                    if line.startswith("BARGAIN_ITEM:"):
                        try:
                            data = json.loads(line[len("BARGAIN_ITEM:"):])
                            data["time"] = datetime.now().strftime("%H:%M")
                            self.bargains.append(data)
                            save_bargains(self.bargains)
                            self._enqueue({"type": "bargain", "item": data})
                        except Exception:
                            pass
                        continue

                    self._enqueue({"type": "log", "name": name,
                                   "line": line, "ts": _ts()})

            except queue.Empty:
                pass

        # Reap dead processes
        for name in list(self.instances):
            if self.instances[name]["process"].poll() is not None:
                self.instances.pop(name, None)
                self._enqueue({"type": "status"})


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(title="Unifi")
app.add_middleware(AuthMiddleware)
pm  = ProcessManager()


@app.on_event("startup")
async def _startup():
    loop = asyncio.get_event_loop()
    pm.set_loop(loop)
    asyncio.create_task(pm.broadcast_loop())
    asyncio.create_task(_poll_task())


async def _poll_task():
    while True:
        pm.poll_queues()
        await asyncio.sleep(0.25)


# ── Pydantic models ───────────────────────────────────────────────────────────
class SearchIn(BaseModel):
    name:             str
    query:            str
    platforms:        list[str]
    interval_min:     int = 5
    exclude_keywords: list[str] = []


class ThresholdIn(BaseModel):
    mode:        str   = "percent"   # "percent" | "fixed"
    percent:     int   = 50
    fixed:       Optional[float] = None
    min_records: int   = 20


class SettingsIn(BaseModel):
    ebay_app_id:     str = ""
    ebay_cert_id:    str = ""
    discord_webhook: str = ""


# ── Auth routes ───────────────────────────────────────────────────────────────
@app.get("/ping")
def ping():
    return HTMLResponse("pong-v6")

@app.get("/auth/login")
def auth_login():
    if not GOOGLE_CLIENT_ID:
        return RedirectResponse("/")
    import urllib.parse, secrets
    state  = secrets.token_urlsafe(16)
    params = urllib.parse.urlencode({
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  f"{APP_URL}/auth/callback",
        "response_type": "code",
        "scope":         "openid email profile",
        "state":         state,
        "access_type":   "online",
    })
    r = RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")
    r.set_cookie("oauth_state", state, max_age=600, httponly=True, samesite="lax")
    return r


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = "", state: str = ""):
    import traceback as _tb
    try:
        saved_state = request.cookies.get("oauth_state", "")
        if state != saved_state:
            return HTMLResponse(f"<pre>State mismatch\ngot: {state!r}\nexpected: {saved_state!r}</pre>", status_code=400)

        async with httpx.AsyncClient() as client:
            token_r = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code":          code,
                    "client_id":     GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "redirect_uri":  f"{APP_URL}/auth/callback",
                    "grant_type":    "authorization_code",
                },
            )
            if not token_r.is_success:
                return HTMLResponse(f"<pre>Token exchange failed {token_r.status_code}:\n{token_r.text}</pre>", status_code=500)
            access_token = token_r.json()["access_token"]

            info_r = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            info_r.raise_for_status()
            email = info_r.json().get("email", "unknown")

        if email in _load_approved_emails():
            r = RedirectResponse("/")
            r.set_cookie("session", _make_session(email),
                         max_age=60*60*24*30, httponly=True, samesite="lax")
            r.delete_cookie("oauth_state")
            return r

        r = RedirectResponse("/auth/beta")
        r.set_cookie("pending_email", _make_pending(email),
                     max_age=600, httponly=True, samesite="lax")
        r.delete_cookie("oauth_state")
        return r
    except Exception as e:
        return HTMLResponse(f"<pre>Callback error: {type(e).__name__}: {e}\n\n{_tb.format_exc()}</pre>", status_code=500)


_BETA_PAGE = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Unifi — Beta Access</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:#0b0f18;color:#e2e8f0;font-family:system-ui,sans-serif;
          display:flex;align-items:center;justify-content:center;height:100vh}}
    .card{{background:#161c2d;border:1px solid #2d3748;border-radius:12px;
           padding:40px;width:360px}}
    h1{{font-size:1.4rem;margin-bottom:8px}}
    p{{color:#94a3b8;font-size:.9rem;margin-bottom:24px}}
    .email{{background:#0b0f18;border-radius:6px;padding:8px 12px;
            color:#7dd3fc;font-size:.85rem;margin-bottom:20px}}
    input{{width:100%;padding:10px 14px;background:#0b0f18;
           border:1px solid #2d3748;border-radius:8px;color:#e2e8f0;
           font-size:1rem;letter-spacing:.1em;margin-bottom:16px}}
    input:focus{{outline:none;border-color:#3b82f6}}
    button{{width:100%;padding:11px;background:#3b82f6;color:#fff;
            border:none;border-radius:8px;font-size:1rem;cursor:pointer}}
    button:hover{{background:#2563eb}}
    .err{{color:#f87171;font-size:.85rem;margin-bottom:12px}}
    a{{color:#94a3b8;font-size:.8rem;display:block;text-align:center;margin-top:16px}}
  </style>
</head>
<body>
  <div class="card">
    <h1>Beta Access</h1>
    <p>You're signed in but need a beta code to use Unifi.</p>
    <div class="email">{email}</div>
    {error}
    <form method="post">
      <input name="code" placeholder="Enter beta code" autocomplete="off" autofocus>
      <button type="submit">Activate</button>
    </form>
    <a href="/auth/logout">Sign out</a>
  </div>
</body>
</html>"""


@app.get("/auth/beta")
async def auth_beta_get(request: Request):
    try:
        email = _verify_pending(request.cookies.get("pending_email", ""))
        if not email:
            return RedirectResponse("/auth/login")
        return HTMLResponse(_BETA_PAGE.format(email=email, error=""))
    except Exception as e:
        import traceback
        return HTMLResponse(f"<pre>ERROR: {type(e).__name__}: {e}\n\n{traceback.format_exc()}</pre>", status_code=500)


@app.post("/auth/beta")
async def auth_beta_post(request: Request):
    try:
     email = _verify_pending(request.cookies.get("pending_email", ""))
    except Exception as e:
        import traceback
        return HTMLResponse(f"<pre>POST ERROR: {type(e).__name__}: {e}\n\n{traceback.format_exc()}</pre>", status_code=500)
    if not email:
        return RedirectResponse("/auth/login")

    form = await request.form()
    code = (form.get("code") or "").strip()

    if not _valid_beta_code(code):
        err = '<p class="err">Invalid or already-used code — try again.</p>'
        return HTMLResponse(_BETA_PAGE.format(email=email, error=err), status_code=400)

    _burn_code(code)
    _approve_email(email)

    r = RedirectResponse("/", status_code=303)
    r.set_cookie("session", _make_session(email),
                 max_age=60*60*24*30, httponly=True, samesite="lax")
    r.delete_cookie("pending_email")
    return r


@app.get("/auth/me")
def auth_me(request: Request):
    session = request.cookies.get("session", "")
    email   = _verify_session(session)
    if not email:
        raise HTTPException(401, "Not authenticated")
    return {"email": email}


@app.get("/auth/logout")
def auth_logout():
    r = RedirectResponse("/")
    r.delete_cookie("session")
    return r


@app.post("/api/auth/verify")
def api_verify_legacy():
    return {"ok": True}


# ── REST: Searches ────────────────────────────────────────────────────────────
@app.get("/api/searches")
def api_get_searches():
    searches = load_searches()
    running  = {n for n, i in pm.instances.items() if i["process"].poll() is None}
    for s in searches:
        s["running"] = s["name"] in running
    return searches


@app.post("/api/searches", status_code=201)
def api_create_search(body: SearchIn):
    searches = [s for s in load_searches()
                if s["name"].lower() != body.name.lower()]
    searches.append(body.model_dump())
    save_searches(searches)
    return {"ok": True}


@app.put("/api/searches/{name}")
def api_update_search(name: str, body: SearchIn):
    searches = [s for s in load_searches()
                if s["name"].lower() != name.lower()]
    searches.append(body.model_dump())
    save_searches(searches)
    return {"ok": True}


@app.delete("/api/searches/{name}")
def api_delete_search(name: str):
    if name in pm.instances and pm.instances[name]["process"].poll() is None:
        raise HTTPException(400, "Stop the watcher first")
    searches = [s for s in load_searches() if s["name"] != name]
    save_searches(searches)
    return {"ok": True}


@app.post("/api/searches/{name}/start")
def api_start_search(name: str):
    s = next((x for x in load_searches() if x["name"] == name), None)
    if not s:
        raise HTTPException(404, "Search not found")
    pm.start(s)
    return {"ok": True}


@app.post("/api/searches/{name}/stop")
def api_stop_search(name: str):
    pm.stop(name)
    return {"ok": True}


@app.post("/api/start-all")
def api_start_all():
    for s in load_searches():
        pm.start(s)
    return {"ok": True}


@app.post("/api/stop-all")
def api_stop_all():
    pm.stop_all()
    return {"ok": True}


@app.get("/api/status")
def api_status():
    return pm.status()


# ── REST: Bargains ────────────────────────────────────────────────────────────
@app.get("/api/bargains")
def api_get_bargains():
    return pm.bargains


@app.delete("/api/bargains")
def api_clear_bargains():
    pm.bargains.clear()
    save_bargains([])
    return {"ok": True}


# ── REST: Missed Deals ────────────────────────────────────────────────────────
@app.get("/api/missed")
def api_get_missed():
    return pm.missed


@app.delete("/api/missed")
def api_clear_missed():
    pm.missed.clear()
    save_missed([])
    return {"ok": True}


# ── REST: Thresholds ─────────────────────────────────────────────────────────
@app.get("/api/thresholds")
def api_get_thresholds():
    return load_thresholds()


@app.put("/api/thresholds/{name}")
def api_set_threshold(name: str, body: ThresholdIn):
    th = load_thresholds()
    th[name] = body.model_dump()
    save_thresholds(th)
    return {"ok": True}


# ── REST: Settings ────────────────────────────────────────────────────────────
@app.get("/api/settings")
def api_get_settings():
    cfg = load_config()
    # Mask secrets in response — send length instead of value
    return {
        "ebay_app_id":        cfg["ebay_app_id"],
        "ebay_cert_id_set":   bool(cfg["ebay_cert_id"]),
        "discord_webhook_set": bool(cfg["discord_webhook"]),
    }


@app.post("/api/settings")
def api_save_settings(body: SettingsIn):
    cfg = load_config()
    if body.ebay_app_id:
        cfg["ebay_app_id"] = body.ebay_app_id
    if body.ebay_cert_id:
        cfg["ebay_cert_id"] = body.ebay_cert_id
    if body.discord_webhook:
        cfg["discord_webhook"] = body.discord_webhook
    save_config(cfg)
    return {"ok": True}


# ── REST: Data stats ──────────────────────────────────────────────────────────
@app.get("/api/data")
def api_get_data(query: str = ""):
    if not query:
        return []
    rows = []
    platforms = ["ebay", "vinted", "depop"]
    sold_median = _load_sold_median(query)
    for plat in platforms:
        prices = _load_platform_prices(query, plat)
        if not prices:
            continue
        s = _price_stats(prices)
        s["platform"]     = plat
        s["sold_median"]  = sold_median
        s["threshold_50"] = round(sold_median * 0.50, 2) if sold_median else None
        rows.append(s)
    return rows


@app.get("/api/queries")
def api_get_queries():
    """Return list of queries that have CSV data on disk."""
    slugs = {}
    for f in DATASETS_DIR.glob("*_new.csv"):
        for plat in ("ebay", "vinted", "depop"):
            suffix = f"_{plat}_new.csv"
            if f.name.endswith(suffix):
                slug = f.name[: -len(suffix)]
                slugs[slug] = slug.replace("_", " ")
    for s in load_searches():
        sl = _slug(s["query"])
        slugs[sl] = s["query"]
    return list(slugs.values())


# ── WebSocket ─────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    pm.register(ws)
    # Send current state on connect
    await ws.send_json({"type": "init", "bargains": pm.bargains, "missed": pm.missed})
    try:
        while True:
            await ws.receive_text()   # keep alive — client sends pings
    except WebSocketDisconnect:
        pm.unregister(ws)


# ── Frontend (served from index.html next to server.py) ──────────────────────
@app.get("/")
def serve_frontend():
    html = Path(__file__).parent / "index.html"
    if html.exists():
        return HTMLResponse(html.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Put index.html next to server.py</h1>")