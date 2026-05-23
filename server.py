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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Paths (same as gui.py) ────────────────────────────────────────────────────
DATASETS_DIR        = Path("datasets")
SAVED_SEARCHES_FILE = DATASETS_DIR / "saved_searches.json"
CONFIG_FILE         = DATASETS_DIR / "config.json"
BARGAINS_FILE       = DATASETS_DIR / "bargains.json"
MISSED_FILE         = DATASETS_DIR / "missed_deals.json"
THRESHOLDS_FILE     = DATASETS_DIR / "thresholds.json"
PYTHON              = sys.executable

DATASETS_DIR.mkdir(exist_ok=True)

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