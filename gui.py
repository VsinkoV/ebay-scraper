#!/usr/bin/env python3
"""
Unifi — Unified Marketplace Watcher
Dark-themed task manager for running multiple search watches simultaneously.

Usage:
    python3 gui.py
"""

import csv
import io
import json
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

try:
    from PIL import Image, ImageTk
    _PIL = True
except ImportError:
    _PIL = False

DATASETS_DIR        = Path("datasets")
SAVED_SEARCHES_FILE = DATASETS_DIR / "saved_searches.json"
PYTHON              = sys.executable

# ─── Palette ──────────────────────────────────────────────────────────────────
C = {
    "bg":         "#0b0f18",
    "bg2":        "#111827",
    "bg3":        "#1c2433",
    "bg4":        "#243044",
    "border":     "#1e2d42",
    "border2":    "#2e3f58",
    "text":       "#e2e8f0",
    "muted":      "#64748b",
    "faint":      "#334155",
    "green":      "#22c55e",
    "green_dim":  "#14532d",
    "red":        "#ef4444",
    "red_dim":    "#450a0a",
    "blue":       "#3b82f6",
    "blue_dim":   "#1e3a5f",
    "yellow":     "#f59e0b",
    "yellow_dim": "#451a03",
    "purple":     "#a78bfa",
    "teal":       "#2dd4bf",
    "orange":     "#f97316",
    "ebay":       "#e53238",
    "vinted":     "#09b1ba",
    "depop":      "#ff4040",
}

_IS_MAC  = sys.platform == "darwin"
FONT     = lambda s, b=False: (("SF Pro Display", s, "bold" if b else "") if _IS_MAC
                                else ("Segoe UI",   s, "bold" if b else ""))
MONO     = lambda s: ("Menlo", s) if _IS_MAC else ("Consolas", s)

PLAT_ICON   = {"ebay": "🛒", "vinted": "👗", "depop": "📦"}
PLAT_COLOUR = {"ebay": C["ebay"], "vinted": C["vinted"], "depop": C["depop"]}


# Tab accent colours
TAB_COLOURS = {
    "tasks":    "#3b82f6",   # blue
    "bargains": "#f59e0b",   # amber
    "missed":   "#ef4444",   # red
    "alerts":   "#f97316",   # orange
    "data":     "#2dd4bf",   # teal
    "log":      "#a78bfa",   # purple
    "settings": "#22c55e",   # green
}

CONFIG_FILE     = DATASETS_DIR / "config.json"
BARGAINS_FILE   = DATASETS_DIR / "bargains.json"
MISSED_FILE     = DATASETS_DIR / "missed_deals.json"
THRESHOLDS_FILE = DATASETS_DIR / "thresholds.json"

CONFIG_DEFAULTS = {
    "ebay_app_id":      "",
    "ebay_cert_id":     "",
    "discord_webhook":  "",
}

# ─── Colour math ──────────────────────────────────────────────────────────────
def _hex_to_rgb(h: str):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

def _rgb_to_hex(r, g, b):
    return f"#{int(r):02x}{int(g):02x}{int(b):02x}"

def _interp(c1: str, c2: str, t: float) -> str:
    r1, g1, b1 = _hex_to_rgb(c1)
    r2, g2, b2 = _hex_to_rgb(c2)
    return _rgb_to_hex(r1 + (r2 - r1) * t, g1 + (g2 - g1) * t, b1 + (b2 - b1) * t)


# ─── Rounded rectangle helper ────────────────────────────────────────────────
def _draw_rounded_rect(canvas, x1, y1, x2, y2, r, **kw):
    """Draw a filled rounded-rectangle on a Canvas."""
    r = min(r, (x2 - x1) // 2, (y2 - y1) // 2)
    canvas.create_arc(x1,     y1,     x1+2*r, y1+2*r, start=90,  extent=90, style=tk.PIESLICE, **kw)
    canvas.create_arc(x2-2*r, y1,     x2,     y1+2*r, start=0,   extent=90, style=tk.PIESLICE, **kw)
    canvas.create_arc(x1,     y2-2*r, x1+2*r, y2,     start=180, extent=90, style=tk.PIESLICE, **kw)
    canvas.create_arc(x2-2*r, y2-2*r, x2,     y2,     start=270, extent=90, style=tk.PIESLICE, **kw)
    canvas.create_rectangle(x1+r, y1,   x2-r, y2,   **kw)
    canvas.create_rectangle(x1,   y1+r, x2,   y2-r, **kw)


# ─── Canvas tab button (rounded, coloured underline when active) ──────────────
class TabBtn(tk.Canvas):
    W, H, R = 110, 42, 8

    def __init__(self, parent, text, accent, command, **kw):
        super().__init__(parent, width=self.W, height=self.H,
                         bg=C["bg2"], highlightthickness=0, cursor="hand2", **kw)
        self._text    = text
        self._accent  = accent
        self._command = command
        self._active  = False
        self._hover   = False
        self._draw()
        self.bind("<Enter>",           self._on_enter)
        self.bind("<Leave>",           self._on_leave)
        self.bind("<ButtonRelease-1>", lambda e: command())

    def _on_enter(self, _=None):
        self._hover = True
        self._draw()

    def _on_leave(self, _=None):
        self._hover = False
        self._draw()

    def set_active(self, active: bool):
        self._active = active
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h, r = self.W, self.H, self.R

        # Pill background when active or hovered
        if self._active:
            bg = _interp(self._accent, "#000000", 0.78)
        elif self._hover:
            bg = C["bg3"]
        else:
            bg = C["bg2"]

        if self._active or self._hover:
            _draw_rounded_rect(self, 4, 4, w - 4, h - 4, r,
                               fill=bg, outline="")

        # Coloured underline bar when active
        if self._active:
            _draw_rounded_rect(self, 4, h - 5, w - 4, h - 1, 2,
                               fill=self._accent, outline="")

        # Label
        fg = self._accent if self._active else (C["text"] if self._hover else C["muted"])
        self.create_text(w // 2, h // 2 - 2, text=self._text,
                         fill=fg,
                         font=FONT(10, self._active))


# ─── Animated hover button ────────────────────────────────────────────────────
class HoverBtn(tk.Label):
    """A label that animates its background on hover and acts as a button."""

    STEPS = 5
    DELAY = 18   # ms per frame

    def __init__(self, parent, text, command, bg=C["bg3"], hover=C["bg4"],
                 fg=C["text"], active_bg=None, padx=14, pady=8,
                 font=None, radius=6, width=None, **kw):
        super().__init__(parent, text=text, bg=bg, fg=fg,
                         font=font or FONT(10),
                         padx=padx, pady=pady,
                         cursor="hand2", relief=tk.FLAT,
                         **({} if width is None else {"width": width}),
                         **kw)
        self._bg      = bg
        self._hover   = hover
        self._active  = active_bg or _interp(hover, "#ffffff", 0.15)
        self._cmd     = command
        self._anim_id = None
        self._t       = 0.0

        self.bind("<Enter>",          self._on_enter)
        self.bind("<Leave>",          self._on_leave)
        self.bind("<ButtonPress-1>",  self._on_press)
        self.bind("<ButtonRelease-1>",self._on_release)

    def _cancel(self):
        if self._anim_id:
            try:
                self.after_cancel(self._anim_id)
            except Exception:
                pass
            self._anim_id = None

    def _tick(self, target: float, step: float):
        self._t = max(0.0, min(1.0, self._t + step))
        try:
            self.configure(bg=_interp(self._bg, self._hover, self._t))
        except tk.TclError:
            return
        if abs(self._t - target) > 0.01:
            self._anim_id = self.after(self.DELAY,
                                       lambda: self._tick(target, step))

    def _on_enter(self, _=None):
        self._cancel()
        step = 1.0 / self.STEPS
        self._tick(1.0, step)

    def _on_leave(self, _=None):
        self._cancel()
        step = -1.0 / self.STEPS
        self._tick(0.0, step)

    def _on_press(self, _=None):
        self._cancel()
        try:
            self.configure(bg=self._active)
        except tk.TclError:
            pass

    def _on_release(self, _=None):
        try:
            self.configure(bg=self._hover)
        except tk.TclError:
            pass
        if self._cmd:
            self._cmd()


# ─── Pill-shaped status badge (Canvas) ───────────────────────────────────────
class Pill(tk.Canvas):
    def __init__(self, parent, text, colour, bg=None, **kw):
        bg = bg or parent["bg"]
        super().__init__(parent, bg=bg, highlightthickness=0,
                         cursor="arrow", **kw)
        self._text   = text
        self._colour = colour
        self._bg     = bg
        self.bind("<Configure>", self._draw)
        # Give it a default size before Configure fires
        self.configure(width=90, height=22)

    def _draw(self, _=None):
        self.delete("all")
        w, h = self.winfo_width() or 90, self.winfo_height() or 22
        r    = h // 2
        dim  = _interp(self._colour, "#000000", 0.72)

        # Filled pill background
        self.create_arc(0, 0, 2*r, h, start=90, extent=180,
                        fill=dim, outline="")
        self.create_arc(w-2*r, 0, w, h, start=270, extent=180,
                        fill=dim, outline="")
        self.create_rectangle(r, 0, w-r, h, fill=dim, outline="")

        # Dot + text
        dot_x, dot_y = r + 6, h // 2
        self.create_oval(dot_x-3, dot_y-3, dot_x+3, dot_y+3,
                         fill=self._colour, outline="")
        self.create_text(dot_x + 8, h // 2, text=self._text,
                         fill=self._colour,
                         font=FONT(9, True), anchor="w")

    def update_status(self, text, colour):
        self._text   = text
        self._colour = colour
        self._draw()


# ─── Scrollable inner frame ───────────────────────────────────────────────────
def scrolled(parent):
    outer  = tk.Frame(parent, bg=C["bg"])
    canvas = tk.Canvas(outer, bg=C["bg"], highlightthickness=0, bd=0)
    sb     = tk.Scrollbar(outer, orient="vertical", command=canvas.yview,
                          bg=C["bg3"], troughcolor=C["bg2"],
                          activebackground=C["bg4"], width=6,
                          relief=tk.FLAT, bd=0)
    inner  = tk.Frame(canvas, bg=C["bg"])
    win    = canvas.create_window((0, 0), window=inner, anchor="nw")
    inner.bind("<Configure>",
               lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.configure(yscrollcommand=sb.set)
    canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    sb.pack(side=tk.RIGHT, fill=tk.Y)

    def _scroll(e):
        delta = -1 * (e.delta // 120) if e.delta else (-1 if e.num == 4 else 1)
        canvas.yview_scroll(delta, "units")
    canvas.bind_all("<MouseWheel>", _scroll)
    canvas.bind_all("<Button-4>",   _scroll)
    canvas.bind_all("<Button-5>",   _scroll)
    return outer, inner, canvas


# ─── Divider ──────────────────────────────────────────────────────────────────
def divider(parent, pady=0):
    tk.Frame(parent, bg=C["border"], height=1).pack(fill=tk.X, pady=pady)


# ─── Platform toggle button ───────────────────────────────────────────────────
class PlatformToggle(tk.Canvas):
    """Clickable card that toggles a platform on/off."""
    W, H, R = 108, 68, 12

    PLATFORMS = [
        ("ebay",     "🛒", "eBay",     C["ebay"],   True),
        ("vinted",   "👗", "Vinted",   C["vinted"], True),
        ("depop",    "📦", "Depop",    C["depop"],  True),
        ("facebook", "f",  "Facebook", C["blue"],   False),  # coming soon
    ]

    def __init__(self, parent, platform, icon, label, colour,
                 enabled=True, selected=False, **kw):
        super().__init__(parent, width=self.W, height=self.H,
                         bg=C["bg"], highlightthickness=0,
                         cursor="hand2" if enabled else "arrow", **kw)
        self._platform = platform
        self._icon     = icon
        self._label    = label
        self._colour   = colour
        self._enabled  = enabled
        self._selected = selected
        self._hover    = False
        self._anim     = None
        self._t        = 1.0 if selected else 0.0
        self._draw()
        if enabled:
            self.bind("<Enter>",           self._on_enter)
            self.bind("<Leave>",           self._on_leave)
            self.bind("<ButtonPress-1>",   self._on_press)
            self.bind("<ButtonRelease-1>", self._on_release)

    # ── Properties ──────────────────────────────────────────────────────────
    @property
    def selected(self):
        return self._selected

    @property
    def platform(self):
        return self._platform

    # ── Events ──────────────────────────────────────────────────────────────
    def _on_enter(self, _=None):
        self._hover = True
        self._draw()

    def _on_leave(self, _=None):
        self._hover = False
        self._draw()

    def _on_press(self, _=None):
        self._selected = not self._selected
        self._animate(1.0 if self._selected else 0.0)

    def _on_release(self, _=None):
        self._draw()

    # ── Animation ────────────────────────────────────────────────────────────
    def _animate(self, target):
        if self._anim:
            try: self.after_cancel(self._anim)
            except Exception: pass
        step = 0.2 * (1 if target > self._t else -1)
        self._tick(target, step)

    def _tick(self, target, step):
        self._t = max(0.0, min(1.0, self._t + step))
        try:
            self._draw()
        except tk.TclError:
            return
        if abs(self._t - target) > 0.05:
            self._anim = self.after(16, lambda: self._tick(target, step))

    # ── Drawing ──────────────────────────────────────────────────────────────
    def _draw(self):
        self.delete("all")
        w, h, r = self.W, self.H, self.R
        t = self._t

        if not self._enabled:
            card_bg = _interp(C["bg"], C["bg2"], 0.4)
            border  = C["faint"]
            icon_c  = C["faint"]
            text_c  = C["faint"]
        else:
            # Interpolate between inactive and active visuals
            active_bg = _interp(self._colour, "#000000", 0.78)
            card_bg   = _interp(C["bg3"] if self._hover else C["bg2"],
                                active_bg, t)
            border    = _interp(C["border2"] if self._hover else C["border"],
                                self._colour, t)
            icon_c    = _interp(C["muted"], self._colour, t)
            text_c    = _interp(C["muted"], self._colour, t)

        # Card background
        _draw_rounded_rect(self, 2, 2, w-2, h-2, r, fill=card_bg, outline="")

        # Border (drawn as arc outline segments)
        for x1, y1, x2, y2, start in [
            (2, 2, 2+2*r, 2+2*r, 90),
            (w-2-2*r, 2, w-2, 2+2*r, 0),
            (2, h-2-2*r, 2+2*r, h-2, 180),
            (w-2-2*r, h-2-2*r, w-2, h-2, 270),
        ]:
            self.create_arc(x1, y1, x2, y2, start=start, extent=90,
                            outline=border, style=tk.ARC, width=1)
        self.create_line(2+r, 2, w-2-r, 2, fill=border, width=1)
        self.create_line(2+r, h-2, w-2-r, h-2, fill=border, width=1)
        self.create_line(2, 2+r, 2, h-2-r, fill=border, width=1)
        self.create_line(w-2, 2+r, w-2, h-2-r, fill=border, width=1)

        # Icon
        font_size = 9 if self._icon == "f" else 18
        icon_font = FONT(font_size, True) if self._icon == "f" else ("Arial", font_size)
        self.create_text(w//2, h//2 - 8, text=self._icon,
                         fill=icon_c, font=icon_font)

        # Label
        self.create_text(w//2, h - 16, text=self._label,
                         fill=text_c, font=FONT(9, self._selected))

        # Checkmark when selected
        if t > 0.5:
            alpha = min(1.0, (t - 0.5) * 2)
            ck_col = _interp(card_bg, self._colour, alpha)
            self.create_text(w - 12, 12, text="✓",
                             fill=ck_col, font=FONT(8, True))

        # "Soon" badge for disabled
        if not self._enabled:
            _draw_rounded_rect(self, w-36, 4, w-4, 18, 4,
                               fill=C["faint"], outline="")
            self.create_text(w-20, 11, text="soon",
                             fill=C["bg"], font=FONT(7, True))


# ─── Add / Edit dialog ────────────────────────────────────────────────────────
class SearchDialog(tk.Toplevel):
    def __init__(self, root, on_save, existing: dict = None):
        super().__init__(root)
        self.title("Unifi — New Search" if not existing else "Unifi — Edit Search")
        self.geometry("520x660")
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(root)
        self._on_save = on_save

        d            = existing or {}
        active_plats = set(d.get("platforms", ["ebay", "vinted", "depop"])
                           if isinstance(d.get("platforms"), list) else
                           d.get("platforms", "ebay,vinted,depop").split(","))

        # ── Header strip ────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=C["bg2"])
        hdr.pack(fill=tk.X)
        tk.Frame(hdr, bg=C["blue"], width=3).pack(side=tk.LEFT, fill=tk.Y)
        # U icon
        ic = tk.Canvas(hdr, width=30, height=30, bg=C["bg2"], highlightthickness=0)
        ic.pack(side=tk.LEFT, padx=(16, 6), pady=14)
        _draw_rounded_rect(ic, 0, 0, 30, 30, 8, fill=C["blue"], outline="")
        ic.create_text(15, 15, text="U", fill="white", font=FONT(12, True))
        tk.Label(hdr, text="Edit Search" if existing else "New Search",
                 bg=C["bg2"], fg=C["text"], font=FONT(13, True),
                 pady=16).pack(side=tk.LEFT)
        tk.Label(hdr, text="· Unifi", bg=C["bg2"], fg=C["muted"],
                 font=FONT(10), pady=16).pack(side=tk.LEFT, padx=4)

        divider(self)

        # ── Body ────────────────────────────────────────────────────────────
        body = tk.Frame(self, bg=C["bg"])
        body.pack(fill=tk.BOTH, expand=True, padx=30, pady=10)

        # Name field
        self._name_var = self._field(body, "Watch Name",
                                     "Give this search a short label",
                                     d.get("name", ""))
        # Query field
        self._query_var = self._field(body, "Search Query",
                                      "e.g.  all saints leather jacket",
                                      d.get("query", ""))

        # Platform toggles
        tk.Label(body, text="Platforms", bg=C["bg"], fg=C["text"],
                 font=FONT(10, True), anchor="w").pack(anchor="w", pady=(10, 2))
        tk.Label(body, text="Click to select · at least one required",
                 bg=C["bg"], fg=C["muted"], font=FONT(9),
                 anchor="w").pack(anchor="w")

        plat_row = tk.Frame(body, bg=C["bg"])
        plat_row.pack(fill=tk.X, pady=(8, 0))
        self._toggles = []
        for plat, icon, label, colour, enabled in PlatformToggle.PLATFORMS:
            tog = PlatformToggle(plat_row, plat, icon, label, colour,
                                 enabled=enabled,
                                 selected=(plat in active_plats and enabled))
            tog.pack(side=tk.LEFT, padx=(0, 8))
            self._toggles.append(tog)

        # Exclude keywords field
        excl_default = ", ".join(d.get("exclude_keywords") or [])
        self._exclude_var = self._field(body, "Exclude Keywords",
                                        "Comma-separated words to block  e.g. broken, damaged, spares",
                                        excl_default)

        # Interval stepper
        tk.Label(body, text="Poll Interval",
                 bg=C["bg"], fg=C["text"],
                 font=FONT(10, True), anchor="w").pack(anchor="w", pady=(14, 2))
        tk.Label(body, text="How often to check for new listings (minutes)",
                 bg=C["bg"], fg=C["muted"], font=FONT(9),
                 anchor="w").pack(anchor="w")

        stepper_frame = tk.Frame(body, bg=C["bg"])
        stepper_frame.pack(anchor="w", pady=(8, 0))
        self._interval_val = tk.IntVar(value=d.get("interval_min", 5))

        HoverBtn(stepper_frame, "−", self._dec_interval,
                 bg=C["bg3"], hover=C["bg4"], fg=C["text"],
                 padx=14, pady=6, font=FONT(13)).pack(side=tk.LEFT)
        self._interval_lbl = tk.Label(stepper_frame, textvariable=self._interval_val,
                                       bg=C["bg3"], fg=C["text"],
                                       font=FONT(14, True), width=4,
                                       relief=tk.FLAT)
        self._interval_lbl.pack(side=tk.LEFT, ipady=5)
        tk.Label(stepper_frame, text="min", bg=C["bg3"], fg=C["muted"],
                 font=FONT(10), padx=6).pack(side=tk.LEFT)
        HoverBtn(stepper_frame, "+", self._inc_interval,
                 bg=C["bg3"], hover=C["bg4"], fg=C["text"],
                 padx=14, pady=6, font=FONT(13)).pack(side=tk.LEFT)

        divider(self)

        # Save + Cancel
        foot = tk.Frame(self, bg=C["bg"])
        foot.pack(fill=tk.X, padx=30, pady=14)
        HoverBtn(foot, "  + Save Watch  ", self._save,
                 bg=C["green"], hover=_interp(C["green"], "#ffffff", 0.15),
                 fg="white", font=FONT(13, True),
                 padx=28, pady=13).pack(side=tk.LEFT)
        HoverBtn(foot, "Cancel", self.destroy,
                 bg=C["bg3"], hover=C["bg4"], fg=C["muted"],
                 padx=16, pady=10, font=FONT(10)).pack(side=tk.LEFT, padx=10)

    # ── Helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _field(parent, label, hint, default):
        grp = tk.Frame(parent, bg=C["bg"])
        grp.pack(fill=tk.X, pady=(0, 4))
        tk.Label(grp, text=label, bg=C["bg"], fg=C["text"],
                 font=FONT(10, True), anchor="w").pack(anchor="w")
        tk.Label(grp, text=hint, bg=C["bg"], fg=C["muted"],
                 font=FONT(9), anchor="w").pack(anchor="w")
        e = tk.Entry(grp, bg=C["bg3"], fg=C["text"],
                     insertbackground=C["text"], relief=tk.FLAT,
                     font=FONT(11),
                     highlightbackground=C["border2"],
                     highlightthickness=1,
                     highlightcolor=C["blue"])
        e.insert(0, default)
        e.pack(fill=tk.X, ipady=9, pady=(4, 0))
        e.bind("<FocusIn>",  lambda ev, w=e: w.configure(highlightbackground=C["blue"]))
        e.bind("<FocusOut>", lambda ev, w=e: w.configure(highlightbackground=C["border2"]))
        return e

    def _inc_interval(self):
        self._interval_val.set(min(120, self._interval_val.get() + 1))

    def _dec_interval(self):
        self._interval_val.set(max(1, self._interval_val.get() - 1))

    def _save(self):
        name  = self._name_var.get().strip()
        query = self._query_var.get().strip()
        plats = [t.platform for t in self._toggles if t.selected]
        if not name or not query or not plats:
            if not name:
                self._name_var.configure(highlightbackground=C["red"])
            return
        excl = [kw.strip() for kw in self._exclude_var.get().split(",") if kw.strip()]
        self._on_save({"name": name, "query": query,
                       "platforms": plats,
                       "interval_min": self._interval_val.get(),
                       "exclude_keywords": excl})
        self.destroy()


# ─── Data / Stats helpers ─────────────────────────────────────────────────────
def _load_thresholds() -> dict:
    if not THRESHOLDS_FILE.exists():
        return {}
    try:
        with open(THRESHOLDS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_thresholds(data: dict) -> None:
    DATASETS_DIR.mkdir(exist_ok=True)
    with open(THRESHOLDS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _parse_price_val(val) -> "float | None":
    try:
        return float(str(val or "").replace("£", "").replace(",", "").strip())
    except ValueError:
        return None


def _load_platform_prices(query: str, platform: str) -> list:
    path = DATASETS_DIR / f"{_slug(query)}_{platform}_new.csv"
    if not path.exists():
        return []
    prices = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            p = _parse_price_val(row.get("price", ""))
            if p is not None and p > 0:
                prices.append(p)
    return prices


def _load_cheapest_row(query: str, platform: str) -> "tuple[dict | None, str | None]":
    """Return (cheapest_row, path_tried) for a given query + platform.
    Tries both slug variants so we match regardless of how the CSV was named."""
    slugs_to_try = sorted({
        _slug(query),
        query.lower().replace(" ", "_"),   # listing_watcher.py uses this
    })
    for slug in slugs_to_try:
        path = DATASETS_DIR / f"{slug}_{platform}_new.csv"
        if not path.exists():
            continue
        best       = None
        best_price = float("inf")
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                p = _parse_price_val(row.get("price", ""))
                if p is not None and 0 < p < best_price:
                    best_price = p
                    best = row
        return best, str(path)
    # Neither slug matched — report the path we tried last
    slug = slugs_to_try[-1]
    return None, str(DATASETS_DIR / f"{slug}_{platform}_new.csv")


def _load_sold_median(query: str) -> "float | None":
    path = DATASETS_DIR / f"{_slug(query)}_sold.csv"
    if not path.exists():
        return None
    prices = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            p = _parse_price_val(row.get("price", ""))
            if p is not None and p > 0:
                prices.append(p)
    if not prices:
        return None
    prices.sort()
    return prices[len(prices) // 2]


def _count_sold_records(query: str) -> int:
    """Return number of sold records on disk for this query."""
    path = DATASETS_DIR / f"{_slug(query)}_sold.csv"
    if not path.exists():
        return 0
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return sum(1 for _ in csv.DictReader(f))
    except Exception:
        return 0


def _is_listing_active(url: str, platform: str) -> bool:
    """
    Return True if the listing URL still appears live.
    Returns True on network errors (assume active — don't remove on flaky connection).
    """
    import ssl
    if not url:
        return True

    # macOS Python often fails SSL cert verification — use an unverified context
    _ctx = ssl._create_unverified_context()

    _UA = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }

    def _open(req_url, extra_headers=None):
        headers = {**_UA, **(extra_headers or {})}
        req = urllib.request.Request(req_url, headers=headers)
        return urllib.request.urlopen(req, timeout=8, context=_ctx)

    try:
        if platform == "vinted":
            # Vinted embeds \"is_closed\":true/false ~2MB into the page.
            # Read the full page and check that value — the API requires auth so is unusable.
            try:
                with _open(url) as resp:
                    if resp.status != 200:
                        return False
                    body = resp.read(3_000_000).decode("utf-8", errors="ignore")
                    if '\\"is_closed\\":true' in body:
                        return False   # sold / closed
                    if '\\"is_closed\\":false' in body:
                        return True    # confirmed active
                    return True        # signal not found — assume active
            except urllib.error.HTTPError as e:
                return e.code not in (404, 410)

        if platform == "depop":
            try:
                with _open(url) as resp:
                    if resp.status != 200:
                        return False
                    body = resp.read(64_000).decode("utf-8", errors="ignore").lower()
                    return not any(p in body for p in [
                        '"sold":true', '"is_sold":true',
                        "this product is not available",
                        "no longer available",
                    ])
            except urllib.error.HTTPError as e:
                return e.code not in (404, 410)

        # eBay (and any other platform)
        with _open(url) as resp:
            if resp.status != 200:
                return False
            body = resp.read(64_000).decode("utf-8", errors="ignore").lower()
            return not any(p in body for p in [
                "this listing has ended",
                "item not found",
                "this item is not available",
                "page not found",
            ])

    except urllib.error.HTTPError as e:
        return e.code not in (404, 410)
    except Exception:
        return True  # network issue — don't remove


def _price_stats(prices: list) -> dict:
    if not prices:
        return {}
    arr = sorted(prices)
    n   = len(arr)
    return {
        "count":  n,
        "min":    arr[0],
        "mean":   sum(arr) / n,
        "median": arr[n // 2],
        "max":    arr[-1],
    }


def _discover_queries() -> list:
    """Return list of unique query slugs that have at least one CSV in datasets/."""
    if not DATASETS_DIR.exists():
        return []
    slugs = set()
    for f in DATASETS_DIR.glob("*_new.csv"):
        # filename: {slug}_{platform}_new.csv
        for plat in ("ebay", "vinted", "depop"):
            suffix = f"_{plat}_new.csv"
            if f.name.endswith(suffix):
                slugs.add(f.name[: -len(suffix)])
    # Also pull queries from saved searches (use name as query hint)
    searches = []
    if SAVED_SEARCHES_FILE.exists():
        try:
            with open(SAVED_SEARCHES_FILE, encoding="utf-8") as fh:
                searches = json.load(fh)
        except Exception:
            pass
    result = {}
    for slug in slugs:
        result[slug] = slug.replace("_", " ")
    for s in searches:
        slug = _slug(s["query"])
        if slug not in result:
            result[slug] = s["query"]
        else:
            result[slug] = s["query"]  # prefer human-readable query text
    return list(result.values())


# ─── Main application ─────────────────────────────────────────────────────────
class App:
    def __init__(self, root: tk.Tk):
        self.root      = root
        self.root.title("Unifi — Unified Marketplace Watcher")
        self.root.geometry("1240x740")
        self.root.minsize(960, 600)
        self.root.configure(bg=C["bg"])

        # Try to hide the default tk icon on Mac
        try:
            self.root.iconbitmap("")
        except Exception:
            pass

        self.instances: dict = {}
        self.bargains:  list = []
        self.missed:    list = []
        self._log_lines = 0
        self._page = "tasks"

        self._build_nav()
        _apply_config_to_watcher()
        self._build_tasks_page()
        self._build_bargains_page()
        self._load_bargains()
        self._build_missed_page()
        self._load_missed()
        self._build_alerts_page()
        self._build_data_page()
        self._build_log_page()
        self._build_settings_page()
        self._build_statusbar()
        self._show_page("tasks")
        self._refresh_tasks()
        self._poll_output()
        self._tick_countdowns()

    # ── Navigation ────────────────────────────────────────────────────────────
    def _build_nav(self):
        self._nav = tk.Frame(self.root, bg=C["bg2"], height=58)
        self._nav.pack(fill=tk.X)
        self._nav.pack_propagate(False)

        # Left gradient accent bar
        tk.Frame(self._nav, bg=C["blue"], width=3).pack(side=tk.LEFT, fill=tk.Y)

        # Logo — "Unifi" wordmark
        logo = tk.Frame(self._nav, bg=C["bg2"])
        logo.pack(side=tk.LEFT, padx=(14, 4), pady=10)
        # Circle icon
        icon_c = tk.Canvas(logo, width=32, height=32, bg=C["bg2"],
                           highlightthickness=0)
        icon_c.pack(side=tk.LEFT)
        _draw_rounded_rect(icon_c, 2, 2, 30, 30, 8,
                           fill=C["blue"], outline="")
        icon_c.create_text(16, 16, text="U", fill="white",
                           font=FONT(13, True))

        tk.Label(logo, text="Unifi", bg=C["bg2"], fg=C["text"],
                 font=FONT(14, True)).pack(side=tk.LEFT, padx=(6, 4))
        tk.Label(logo, text="marketplace", bg=C["bg2"], fg=C["muted"],
                 font=FONT(9)).pack(side=tk.LEFT, padx=(0, 32))

        # Vertical separator
        tk.Frame(self._nav, bg=C["border"], width=1).pack(
            side=tk.LEFT, fill=tk.Y, pady=12)

        # Coloured TabBtn widgets
        self._nav_btns   = {}
        self._nav_badges = {}
        for label, page in [("Tasks", "tasks"), ("Bargains", "bargains"), ("Missed", "missed"), ("Alerts", "alerts"), ("Data", "data"), ("Log", "log"), ("Settings", "settings")]:
            wrap = tk.Frame(self._nav, bg=C["bg2"])
            wrap.pack(side=tk.LEFT, padx=2, pady=8)
            b = TabBtn(wrap, label, TAB_COLOURS[page],
                       command=lambda p=page: self._show_page(p))
            b.pack(side=tk.LEFT)
            # Badge (shown over the tab when bargains arrive)
            badge = tk.Label(wrap, text="", bg=C["yellow"], fg=C["bg"],
                             font=FONT(7, True), padx=4, pady=0)
            self._nav_btns[page]   = b
            self._nav_badges[page] = badge

        # Right — status pill
        self._status_lbl = tk.Label(self._nav, text="● Idle",
                                    bg=C["bg2"], fg=C["muted"],
                                    font=FONT(10))
        self._status_lbl.pack(side=tk.RIGHT, padx=20)

        # Bottom border
        divider(self.root)

    def _show_page(self, page: str):
        pages = {
            "tasks":    self._tasks_frame,
            "bargains": self._bargains_frame,
            "missed":   self._missed_frame,
            "alerts":   self._alerts_frame,
            "data":     self._data_frame,
            "log":      self._log_frame,
            "settings": self._settings_frame,
        }
        for p, frame in pages.items():
            frame.pack_forget()
            self._nav_btns[p].set_active(p == page)
        pages[page].pack(fill=tk.BOTH, expand=True)
        self._page = page

        # Rebind mouse-wheel to whichever canvas is now visible
        _scroll_canvases = {
            "tasks":    getattr(self, "_tasks_canvas",    None),
            "bargains": getattr(self, "_bargains_canvas", None),
            "missed":   getattr(self, "_missed_canvas",   None),
            "alerts":   getattr(self, "_alerts_canvas",   None),
            "data":     getattr(self, "_data_canvas",     None),
            "settings": getattr(self, "_settings_canvas", None),
        }
        cv = _scroll_canvases.get(page)
        if cv:
            def _scroll(e, _cv=cv):
                delta = -1 * (e.delta // 120) if e.delta else (-1 if e.num == 4 else 1)
                _cv.yview_scroll(delta, "units")
            cv.bind_all("<MouseWheel>", _scroll)
            cv.bind_all("<Button-4>",   _scroll)
            cv.bind_all("<Button-5>",   _scroll)

    # ── Tasks page ────────────────────────────────────────────────────────────
    def _build_tasks_page(self):
        self._tasks_frame = tk.Frame(self.root, bg=C["bg"])

        # Column header row
        hdr = tk.Frame(self._tasks_frame, bg=C["bg2"], pady=0)
        hdr.pack(fill=tk.X, padx=0)
        for text, w, pad in [
            ("#",          24,  (24, 0)),
            ("Name",       220, (12, 0)),
            ("Query",      260, (0, 0)),
            ("Platforms",  100, (0, 0)),
            ("Interval",    70, (0, 0)),
            ("Status",      90, (0, 0)),
            ("Actions",    120, (0, 16)),
        ]:
            tk.Label(hdr, text=text, bg=C["bg2"], fg=C["muted"],
                     font=FONT(9), width=w // 7, anchor="w",
                     padx=pad[0], pady=9).pack(side=tk.LEFT)

        divider(self._tasks_frame)

        outer, self._tasks_inner, self._tasks_canvas = scrolled(self._tasks_frame)
        outer.pack(fill=tk.BOTH, expand=True)

    def _refresh_tasks(self):
        for w in self._tasks_inner.winfo_children():
            w.destroy()
        searches = load_searches()
        if not searches:
            empty = tk.Frame(self._tasks_inner, bg=C["bg"])
            empty.pack(fill=tk.BOTH, expand=True, pady=80)
            tk.Label(empty, text="No saved searches yet.",
                     bg=C["bg"], fg=C["muted"], font=FONT(14, True)).pack()
            tk.Label(empty, text="Click  + Create  in the toolbar below to add one.",
                     bg=C["bg"], fg=C["faint"], font=FONT(11)).pack(pady=6)
        else:
            for i, s in enumerate(searches):
                self._task_row(i + 1, s)
        self._update_stats()

    def _task_row(self, num: int, s: dict):
        name       = s["name"]
        inst       = self.instances.get(name)
        is_running = inst is not None and inst["process"].poll() is None
        row_bg     = C["bg3"] if num % 2 == 0 else C["bg"]

        row = tk.Frame(self._tasks_inner, bg=row_bg, pady=0)
        row.pack(fill=tk.X)
        row.pack_propagate(False)
        row.configure(height=58)

        # Green left accent bar for running instances
        accent_col = C["green"] if is_running else row_bg
        tk.Frame(row, bg=accent_col, width=3).pack(side=tk.LEFT, fill=tk.Y)

        # Row number
        tk.Label(row, text=str(num), bg=row_bg, fg=C["faint"],
                 font=FONT(10), width=3).pack(side=tk.LEFT, padx=(8, 4))

        # Name
        tk.Label(row, text=name, bg=row_bg, fg=C["text"],
                 font=FONT(11, True), width=17, anchor="w").pack(side=tk.LEFT, padx=(4, 0))

        # Query (truncated)
        tk.Label(row, text=s["query"][:36], bg=row_bg, fg=C["muted"],
                 font=FONT(10), width=28, anchor="w").pack(side=tk.LEFT, padx=4)

        # Platform icons with platform colours
        p_frame = tk.Frame(row, bg=row_bg, width=110)
        p_frame.pack(side=tk.LEFT)
        p_frame.pack_propagate(False)
        for p in s["platforms"]:
            tk.Label(p_frame, text=PLAT_ICON.get(p, "?"),
                     bg=row_bg, fg=PLAT_COLOUR.get(p, C["muted"]),
                     font=FONT(14)).pack(side=tk.LEFT, padx=2)

        # Interval + countdown
        interval_frame = tk.Frame(row, bg=row_bg)
        interval_frame.pack(side=tk.LEFT, padx=4)
        tk.Label(interval_frame, text=f"{s['interval_min']}m", bg=row_bg, fg=C["muted"],
                 font=FONT(10), width=5).pack(side=tk.LEFT)
        if is_running and inst:
            cd_lbl = tk.Label(interval_frame, text="--:--", bg=row_bg,
                              fg=C["teal"], font=FONT(9, True), width=6)
            cd_lbl.pack(side=tk.LEFT)
            inst["countdown_lbl"] = cd_lbl
        else:
            tk.Label(interval_frame, text="", bg=row_bg, font=FONT(9),
                     width=6).pack(side=tk.LEFT)

        # Status pill
        pill = Pill(row, "RUNNING" if is_running else "IDLE",
                    C["green"] if is_running else C["faint"],
                    bg=row_bg, width=96, height=22)
        pill.pack(side=tk.LEFT, padx=8)

        # Action buttons
        acts = tk.Frame(row, bg=row_bg)
        acts.pack(side=tk.RIGHT, padx=10)

        if is_running:
            HoverBtn(acts, "■", lambda n=name: self._stop_instance(n),
                     bg=C["red_dim"], hover=C["red"], fg=C["red"],
                     padx=10, pady=5, font=FONT(12)).pack(side=tk.LEFT, padx=2)
        else:
            HoverBtn(acts, "▶", lambda ss=s: self._start_instance(ss),
                     bg=C["green_dim"], hover=C["green"], fg=C["green"],
                     padx=10, pady=5, font=FONT(12)).pack(side=tk.LEFT, padx=2)

        HoverBtn(acts, "✎", lambda ss=s: self._edit_dialog(ss),
                 bg=C["bg4"], hover=C["border2"], fg=C["text"],
                 padx=9, pady=5, font=FONT(11)).pack(side=tk.LEFT, padx=2)
        HoverBtn(acts, "🗑", lambda n=name: self._delete_search(n),
                 bg=C["bg4"], hover=C["red_dim"], fg=C["red"],
                 padx=9, pady=5, font=FONT(11)).pack(side=tk.LEFT, padx=2)

        # Hover highlight on entire row
        def _h_enter(e, r=row, bg=row_bg):
            r.configure(bg=C["bg4"])
            for w in r.winfo_children():
                try: w.configure(bg=C["bg4"])
                except Exception: pass
        def _h_leave(e, r=row, bg=row_bg):
            r.configure(bg=bg)
            for w in r.winfo_children():
                try: w.configure(bg=bg)
                except Exception: pass

        row.bind("<Enter>", _h_enter)
        row.bind("<Leave>", _h_leave)

        divider(self._tasks_inner)

    # ── Bargains page ─────────────────────────────────────────────────────────
    def _build_bargains_page(self):
        self._bargains_frame    = tk.Frame(self.root, bg=C["bg"])
        self._bargains_sort     = "newest"
        self._bargain_row_idx   = 0

        # Header
        hdr = tk.Frame(self._bargains_frame, bg=C["bg2"])
        hdr.pack(fill=tk.X)
        for text, w in [("Time",55),("Platform",80),("Title",380),
                        ("Price",70),("Saves",70),("Link",60)]:
            tk.Label(hdr, text=text, bg=C["bg2"], fg=C["muted"],
                     font=FONT(9), width=w//7, anchor="w",
                     padx=10, pady=9).pack(side=tk.LEFT)

        # Right-hand controls: sort buttons + refresh + clear
        HoverBtn(hdr, " Clear ", self._clear_bargains,
                 bg=C["bg3"], hover=C["bg4"], fg=C["muted"],
                 padx=8, pady=5, font=FONT(9)).pack(side=tk.RIGHT, padx=(4, 10), pady=8)
        HoverBtn(hdr, "⟳  Refresh", self._rescan_all_bargains,
                 bg=C["bg3"], hover=C["bg4"], fg=C["orange"],
                 padx=8, pady=5, font=FONT(9)).pack(side=tk.RIGHT, padx=(0, 4), pady=8)

        sort_sep = tk.Frame(hdr, bg=C["border"], width=1)
        sort_sep.pack(side=tk.RIGHT, fill=tk.Y, pady=6)

        self._sort_btns = {}
        for label, key in [("Price ↓", "price_desc"), ("Price ↑", "price_asc"), ("Newest", "newest")]:
            btn = HoverBtn(hdr, label, lambda k=key: self._set_bargains_sort(k),
                           bg=C["bg2"], hover=C["bg3"],
                           fg=C["blue"] if key == "newest" else C["muted"],
                           padx=7, pady=5, font=FONT(9))
            btn.pack(side=tk.RIGHT, padx=2, pady=8)
            self._sort_btns[key] = btn

        divider(self._bargains_frame)

        outer, self._bargains_inner, self._bargains_canvas = scrolled(self._bargains_frame)
        outer.pack(fill=tk.BOTH, expand=True)

        self._no_bargains = tk.Label(
            self._bargains_inner,
            text="🔥  No bargains found yet.\n\nTo detect bargains:\n1. Scrape eBay sold data in the Alerts tab (or set a fixed £ threshold)\n2. Start a watch in the Tasks tab\n3. Bargains will appear here when a listing is below your threshold",
            bg=C["bg"], fg=C["muted"], font=FONT(11), justify=tk.CENTER)
        self._no_bargains.pack(pady=70)

    # ── Missed Deals page ─────────────────────────────────────────────────────
    def _build_missed_page(self):
        self._missed_frame = tk.Frame(self.root, bg=C["bg"])

        hdr = tk.Frame(self._missed_frame, bg=C["bg2"])
        hdr.pack(fill=tk.X)
        tk.Frame(hdr, bg=C["red"], width=3).pack(side=tk.LEFT, fill=tk.Y)
        tk.Label(hdr, text="Missed Deals", bg=C["bg2"], fg=C["text"],
                 font=FONT(13, True), padx=16, pady=14).pack(side=tk.LEFT)
        tk.Label(hdr, text="· Items that sold before you could buy them",
                 bg=C["bg2"], fg=C["muted"], font=FONT(10), pady=14).pack(side=tk.LEFT)
        HoverBtn(hdr, " Clear All ", self._clear_missed,
                 bg=C["bg3"], hover=C["bg4"], fg=C["muted"],
                 padx=8, pady=5, font=FONT(9)).pack(side=tk.RIGHT, padx=10, pady=10)
        divider(self._missed_frame)

        # Column headers
        col_hdr = tk.Frame(self._missed_frame, bg=C["bg2"])
        col_hdr.pack(fill=tk.X)
        for text, w in [("Sold", 65), ("Platform", 80), ("Title", 360),
                        ("Price", 70), ("Saved", 70), ("Link", 60)]:
            tk.Label(col_hdr, text=text, bg=C["bg2"], fg=C["muted"],
                     font=FONT(9), width=w // 7, anchor="w",
                     padx=10, pady=8).pack(side=tk.LEFT)
        divider(self._missed_frame)

        outer, self._missed_inner, self._missed_canvas = scrolled(self._missed_frame)
        outer.pack(fill=tk.BOTH, expand=True)

        self._no_missed = tk.Label(
            self._missed_inner,
            text="✓  Nothing missed yet.\n\nWhen a bargain sells before you buy it,\nit will appear here so you know what to look for next time.",
            bg=C["bg"], fg=C["muted"], font=FONT(11), justify=tk.CENTER)
        self._no_missed.pack(pady=70)

    def _add_missed_row(self, m: dict, idx: int):
        row_bg = C["bg3"] if idx % 2 == 0 else C["bg"]
        row = tk.Frame(self._missed_inner, bg=row_bg)
        row.pack(fill=tk.X)
        row.pack_propagate(False)
        row.configure(height=52)

        # Red left accent (vs gold for bargains)
        tk.Frame(row, bg=C["red"], width=3).pack(side=tk.LEFT, fill=tk.Y)

        # Thumbnail
        img_lbl = tk.Label(row, bg=row_bg, width=0)
        img_lbl.pack(side=tk.LEFT, padx=(4, 0))
        if _PIL and m.get("image_url"):
            def _load_img(url=m["image_url"], lbl=img_lbl, bg=row_bg):
                try:
                    import ssl
                    ctx = ssl._create_unverified_context()
                    with urllib.request.urlopen(url, timeout=5, context=ctx) as resp:
                        data = resp.read()
                    img = Image.open(io.BytesIO(data)).convert("RGB")
                    img.thumbnail((44, 44))
                    photo = ImageTk.PhotoImage(img)
                    lbl.configure(image=photo, width=44)
                    lbl.image = photo
                except Exception:
                    pass
            threading.Thread(target=_load_img, daemon=True).start()

        # Sold-at time
        tk.Label(row, text=m.get("sold_at", m.get("time", "")),
                 bg=row_bg, fg=C["muted"], font=FONT(9), width=7).pack(side=tk.LEFT, padx=8)

        # Platform
        p     = m["platform"]
        p_col = PLAT_COLOUR.get(p, C["muted"])
        tk.Label(row, text=f"{PLAT_ICON.get(p,'?')} {p.capitalize()}",
                 bg=row_bg, fg=p_col, font=FONT(10, True),
                 width=10, anchor="w").pack(side=tk.LEFT, padx=6)

        # Title (dimmed)
        tk.Label(row, text=m["title"][:54], bg=row_bg, fg=C["muted"],
                 font=FONT(10), width=42, anchor="w").pack(side=tk.LEFT, padx=6)

        # Price (red — it's gone)
        tk.Label(row, text=f"£{m['price']}", bg=row_bg, fg=C["red"],
                 font=FONT(11, True), width=7).pack(side=tk.LEFT, padx=6)

        # Savings (what you would have saved)
        tk.Label(row, text=f"-£{float(m['savings']):.0f}",
                 bg=row_bg, fg=C["muted"], font=FONT(11), width=7).pack(side=tk.LEFT, padx=6)

        # Link (still useful to see what it was)
        lnk = HoverBtn(row, "View →", lambda url=m["url"]: webbrowser.open(url),
                       bg=row_bg, hover=C["bg4"], fg=C["muted"],
                       padx=8, pady=4, font=FONT(10))
        lnk.pack(side=tk.LEFT, padx=6)

        divider(self._missed_inner)

    def _render_missed(self):
        for w in self._missed_inner.winfo_children():
            w.destroy()
        self._no_missed = None
        if not self.missed:
            self._no_missed = tk.Label(
                self._missed_inner,
                text="✓  Nothing missed yet.\n\nWhen a bargain sells before you buy it,\nit will appear here so you know what to look for next time.",
                bg=C["bg"], fg=C["muted"], font=FONT(11), justify=tk.CENTER)
            self._no_missed.pack(pady=70)
            self._nav_badges["missed"].pack_forget()
            return
        for idx, m in enumerate(reversed(self.missed)):
            self._add_missed_row(m, idx)
        self._nav_badges["missed"].configure(text=f" {len(self.missed)} ")
        self._nav_badges["missed"].pack(in_=self._nav_btns["missed"].master,
                                        side=tk.LEFT, pady=18)

    def _load_missed(self):
        if not MISSED_FILE.exists():
            return
        try:
            with open(MISSED_FILE, encoding="utf-8") as f:
                self.missed.extend(json.load(f))
            self._render_missed()
        except Exception:
            pass

    def _save_missed(self):
        try:
            DATASETS_DIR.mkdir(exist_ok=True)
            with open(MISSED_FILE, "w", encoding="utf-8") as f:
                json.dump(self.missed, f)
        except Exception:
            pass

    def _clear_missed(self):
        self.missed.clear()
        self._save_missed()
        self._render_missed()

    def _set_bargains_sort(self, key: str):
        self._bargains_sort = key
        for k, btn in self._sort_btns.items():
            btn.configure(fg=C["blue"] if k == key else C["muted"])
        self._render_bargains()

    def _render_bargains(self):
        """Clear and re-render bargain rows according to current sort order."""
        self._bargain_row_idx = 0
        for w in self._bargains_inner.winfo_children():
            w.destroy()
        self._no_bargains = None

        if not self.bargains:
            self._no_bargains = tk.Label(
                self._bargains_inner,
                text="🔥  No bargains found yet.\n\nTo detect bargains:\n1. Scrape eBay sold data in the Alerts tab (or set a fixed £ threshold)\n2. Start a watch in the Tasks tab\n3. Bargains will appear here when a listing is below your threshold",
                bg=C["bg"], fg=C["muted"], font=FONT(11), justify=tk.CENTER)
            self._no_bargains.pack(pady=70)
            self._nav_badges["bargains"].pack_forget()
            return

        sort_key = self._bargains_sort
        if sort_key == "price_asc":
            items = sorted(self.bargains,
                           key=lambda b: _parse_price_val(b.get("price", "")) or float("inf"))
        elif sort_key == "price_desc":
            items = sorted(self.bargains,
                           key=lambda b: _parse_price_val(b.get("price", "")) or 0, reverse=True)
        else:  # newest first = most recently added at top
            items = list(self.bargains)

        for b in items:
            self._add_bargain_row(b)

    def _add_bargain_row(self, b: dict):
        if self._no_bargains and self._no_bargains.winfo_exists():
            self._no_bargains.pack_forget()

        row_bg = C["bg3"] if self._bargain_row_idx % 2 == 0 else C["bg"]
        self._bargain_row_idx += 1

        row = tk.Frame(self._bargains_inner, bg=row_bg)
        row.pack(fill=tk.X)
        row.pack_propagate(False)
        row.configure(height=52)

        # Gold left accent
        tk.Frame(row, bg=C["yellow"], width=3).pack(side=tk.LEFT, fill=tk.Y)

        # Thumbnail (if PIL available and image_url present)
        img_lbl = tk.Label(row, bg=row_bg, width=0)
        img_lbl.pack(side=tk.LEFT, padx=(4, 0))
        if _PIL and b.get("image_url"):
            def _load_img(url=b["image_url"], lbl=img_lbl, bg=row_bg):
                try:
                    with urllib.request.urlopen(url, timeout=5) as resp:
                        data = resp.read()
                    img = Image.open(io.BytesIO(data)).convert("RGB")
                    img.thumbnail((44, 44))
                    photo = ImageTk.PhotoImage(img)
                    lbl.configure(image=photo, width=44)
                    lbl.image = photo  # keep reference
                except Exception:
                    pass
            threading.Thread(target=_load_img, daemon=True).start()

        # Time
        tk.Label(row, text=b["time"], bg=row_bg, fg=C["muted"],
                 font=FONT(9), width=7).pack(side=tk.LEFT, padx=8)

        # Platform
        p     = b["platform"]
        p_col = PLAT_COLOUR.get(p, C["muted"])
        tk.Label(row, text=f"{PLAT_ICON.get(p,'?')} {p.capitalize()}",
                 bg=row_bg, fg=p_col, font=FONT(10, True),
                 width=10, anchor="w").pack(side=tk.LEFT, padx=6)

        # Title
        tk.Label(row, text=b["title"][:54], bg=row_bg, fg=C["text"],
                 font=FONT(10), width=42, anchor="w").pack(side=tk.LEFT, padx=6)

        # Price (green)
        tk.Label(row, text=f"£{b['price']}", bg=row_bg, fg=C["green"],
                 font=FONT(11, True), width=7).pack(side=tk.LEFT, padx=6)

        # Savings (gold)
        tk.Label(row, text=f"-£{float(b['savings']):.0f}",
                 bg=row_bg, fg=C["yellow"], font=FONT(11, True),
                 width=7).pack(side=tk.LEFT, padx=6)

        # View link
        lnk = HoverBtn(row, "View →", lambda url=b["url"]: webbrowser.open(url),
                       bg=row_bg, hover=C["blue_dim"], fg=C["blue"],
                       padx=8, pady=4, font=FONT(10))
        lnk.pack(side=tk.LEFT, padx=6)

        divider(self._bargains_inner)

        # Update nav badge
        self._nav_badges["bargains"].configure(text=f" {len(self.bargains)} ")
        self._nav_badges["bargains"].pack(in_=self._nav_btns["bargains"].master,
                                          side=tk.LEFT, pady=18)

    # ── Alerts / Thresholds page ───────────────────────────────────────────────
    def _build_alerts_page(self):
        self._alerts_frame = tk.Frame(self.root, bg=C["bg"])

        hdr = tk.Frame(self._alerts_frame, bg=C["bg2"])
        hdr.pack(fill=tk.X)
        tk.Frame(hdr, bg=C["orange"], width=3).pack(side=tk.LEFT, fill=tk.Y)
        tk.Label(hdr, text="Alert Thresholds", bg=C["bg2"], fg=C["text"],
                 font=FONT(13, True), padx=16, pady=14).pack(side=tk.LEFT)
        tk.Label(hdr, text="· Set bargain price per search  —  % of eBay median or fixed £",
                 bg=C["bg2"], fg=C["muted"], font=FONT(10), pady=14).pack(side=tk.LEFT)
        HoverBtn(hdr, "⟳  Rescan Bargains", self._rescan_all_bargains,
                 bg=C["bg3"], hover=C["bg4"], fg=C["orange"],
                 padx=10, pady=5, font=FONT(9)).pack(side=tk.RIGHT, padx=6, pady=10)
        HoverBtn(hdr, "⟳  Refresh", self._refresh_alerts,
                 bg=C["bg3"], hover=C["bg4"], fg=C["muted"],
                 padx=10, pady=5, font=FONT(9)).pack(side=tk.RIGHT, padx=(10, 2), pady=10)
        divider(self._alerts_frame)

        outer, self._alerts_inner, self._alerts_canvas = scrolled(self._alerts_frame)
        outer.pack(fill=tk.BOTH, expand=True)

        self._refresh_alerts()

    def _rescan_all_bargains(self):
        """
        1. Check every existing bargain URL — remove any that are sold/gone.
        2. Scan CSVs for new items below threshold.
        Runs URL checks in a background thread to keep the UI responsive.
        """
        self._append_log("Alerts", "Rescan started — checking existing listings...")

        def _run():
            # ── Step 1: check existing bargains ──────────────────────────────
            to_remove = []
            for b in list(self.bargains):
                url      = b.get("url", "")
                platform = b.get("platform", "")
                if url and not _is_listing_active(url, platform):
                    to_remove.append(b)

            def _apply_removals():
                missed_urls = {m.get("url", "") for m in self.missed if m.get("url")}
                for b in to_remove:
                    if b in self.bargains:
                        self.bargains.remove(b)
                        # Archive to Missed Deals — only if not already there
                        url = b.get("url", "")
                        if not url or url not in missed_urls:
                            missed_entry = {**b, "sold_at": datetime.now().strftime("%H:%M")}
                            self.missed.append(missed_entry)
                            if url:
                                missed_urls.add(url)
                if to_remove:
                    self._render_bargains()
                    self._save_bargains()
                    self._render_missed()
                    self._save_missed()
                    self._append_log(
                        "Alerts",
                        f"Moved {len(to_remove)} sold listing(s) to Missed Deals"
                    )

                # ── Step 2: scan CSVs for new bargains ────────────────────────
                thresholds  = _load_thresholds()
                searches    = load_searches()
                ran         = 0
                before      = len(self.bargains)
                for s in searches:
                    cfg = thresholds.get(s["name"], {})
                    if not cfg:
                        continue
                    mode = cfg.get("mode", "percent")
                    if mode == "fixed":
                        effective = cfg.get("fixed")
                    else:
                        median    = _load_sold_median(s["query"])
                        pct       = cfg.get("percent", 80)
                        effective = round(median * pct / 100, 2) if median else None
                    if effective:
                        self._check_existing_bargains(s["query"], s["platforms"], effective)
                        ran += 1

                new_found = len(self.bargains) - before
                if new_found:
                    summary = f"{new_found} new bargain(s) found"
                else:
                    summary = "No new bargains found"
                self._append_log(
                    "Alerts",
                    f"Rescan complete — {ran} alert(s) checked · {summary}"
                    + (f" · {len(to_remove)} listing(s) removed as sold" if to_remove else "")
                )

            self.root.after(0, _apply_removals)

        threading.Thread(target=_run, daemon=True).start()

    def _refresh_alerts(self):
        for w in self._alerts_inner.winfo_children():
            w.destroy()
        searches = load_searches()
        if not searches:
            tk.Label(self._alerts_inner,
                     text="No saved searches yet.\nAdd one in the Tasks tab.",
                     bg=C["bg"], fg=C["muted"], font=FONT(12),
                     justify=tk.CENTER).pack(pady=70)
            return
        thresholds = _load_thresholds()
        for s in searches:
            self._alert_row(s, thresholds.get(s["name"], {}))

    def _alert_row(self, s: dict, cfg: dict):
        name   = s["name"]
        query  = s["query"]
        median = _load_sold_median(query)
        sold_count = _count_sold_records(query)
        min_records = cfg.get("min_records", 20)

        card = tk.Frame(self._alerts_inner, bg=C["bg2"])
        card.pack(fill=tk.X, pady=(0, 1))
        tk.Frame(card, bg=C["orange"], width=3).pack(side=tk.LEFT, fill=tk.Y)

        body = tk.Frame(card, bg=C["bg2"])
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=12)

        # Title row
        title_row = tk.Frame(body, bg=C["bg2"])
        title_row.pack(fill=tk.X)
        tk.Label(title_row, text=name, bg=C["bg2"], fg=C["text"],
                 font=FONT(11, True)).pack(side=tk.LEFT)
        tk.Label(title_row, text=f" · {query}", bg=C["bg2"], fg=C["muted"],
                 font=FONT(10)).pack(side=tk.LEFT)
        if median is not None:
            tk.Label(title_row, text=f"   eBay sold median: £{median:.2f}",
                     bg=C["bg2"], fg=C["teal"], font=FONT(9)).pack(side=tk.LEFT, padx=10)
            count_col = C["green"] if sold_count >= min_records else C["orange"]
            tk.Label(title_row, text=f"({sold_count} records)",
                     bg=C["bg2"], fg=count_col, font=FONT(9)).pack(side=tk.LEFT)
        else:
            tk.Label(title_row, text="   No eBay sold data",
                     bg=C["bg2"], fg=C["muted"], font=FONT(9)).pack(side=tk.LEFT, padx=10)

        # Options row
        opts = tk.Frame(body, bg=C["bg2"])
        opts.pack(fill=tk.X, pady=(10, 0))

        mode_var = tk.StringVar(value=cfg.get("mode", "percent"))

        # — Percent option —
        pf = tk.Frame(opts, bg=C["bg2"])
        pf.pack(side=tk.LEFT, padx=(0, 20))
        tk.Radiobutton(pf, text="eBay Median  ", variable=mode_var, value="percent",
                       bg=C["bg2"], fg=C["text"], selectcolor=C["bg3"],
                       activebackground=C["bg2"], font=FONT(10)).pack(side=tk.LEFT)
        pct_var = tk.StringVar(value=str(cfg.get("percent", 50)))
        tk.Entry(pf, textvariable=pct_var, bg=C["bg3"], fg=C["text"],
                 insertbackground=C["text"], relief=tk.FLAT, font=FONT(11), width=4,
                 highlightbackground=C["border2"], highlightthickness=1,
                 highlightcolor=C["blue"]).pack(side=tk.LEFT)
        tk.Label(pf, text=" %", bg=C["bg2"], fg=C["muted"], font=FONT(10)).pack(side=tk.LEFT)

        # — Fixed option —
        ff = tk.Frame(opts, bg=C["bg2"])
        ff.pack(side=tk.LEFT, padx=(0, 20))
        tk.Radiobutton(ff, text="Fixed price  £", variable=mode_var, value="fixed",
                       bg=C["bg2"], fg=C["text"], selectcolor=C["bg3"],
                       activebackground=C["bg2"], font=FONT(10)).pack(side=tk.LEFT)
        fix_var = tk.StringVar(value=str(cfg.get("fixed", "") or ""))
        tk.Entry(ff, textvariable=fix_var, bg=C["bg3"], fg=C["text"],
                 insertbackground=C["text"], relief=tk.FLAT, font=FONT(11), width=7,
                 highlightbackground=C["border2"], highlightthickness=1,
                 highlightcolor=C["blue"]).pack(side=tk.LEFT)

        # — Scrape eBay Sold controls (defined early so vars are captured in _save_threshold) —
        scrape_frame = tk.Frame(opts, bg=C["bg2"])

        pages_var = tk.StringVar(value=str(cfg.get("pages", 5)))
        min_var   = tk.StringVar(value=str(cfg.get("min_records", 20)))

        # — Save button —
        def _save_threshold(n=name, mv=mode_var, pv=pct_var, fv=fix_var,
                            q=query, plats=s["platforms"], med=median,
                            mnv=min_var):
            th = _load_thresholds()
            try:
                pct = max(1, min(100, int(pv.get())))
            except ValueError:
                pct = 50
            try:
                fixed = float(fv.get().replace("£", "").strip()) if fv.get().strip() else None
            except ValueError:
                fixed = None
            try:
                min_rec = max(1, int(mnv.get()))
            except ValueError:
                min_rec = 20
            th[n] = {"mode": mv.get(), "percent": pct, "fixed": fixed, "min_records": min_rec}
            _save_thresholds(th)
            if mv.get() == "fixed" and fixed and fixed > 0:
                effective = fixed
            elif med is not None:
                effective = round(med * pct / 100, 2)
            else:
                effective = None
            if effective:
                self._check_existing_bargains(q, plats, effective)

        HoverBtn(opts, "  Save  ", _save_threshold,
                 bg=C["blue"], hover=_interp(C["blue"], "#ffffff", 0.2),
                 fg="white", padx=12, pady=5, font=FONT(10)).pack(side=tk.LEFT)

        # — Now lay out scrape controls —
        scrape_frame.pack(side=tk.LEFT, padx=(16, 0))

        pg_frame = tk.Frame(scrape_frame, bg=C["bg2"])
        pg_frame.pack(side=tk.LEFT, padx=(0, 6))
        tk.Label(pg_frame, text="Pages:", bg=C["bg2"], fg=C["muted"],
                 font=FONT(9)).pack(side=tk.LEFT)
        tk.Entry(pg_frame, textvariable=pages_var, bg=C["bg3"], fg=C["text"],
                 insertbackground=C["text"], relief=tk.FLAT, font=FONT(10), width=3,
                 highlightbackground=C["border2"], highlightthickness=1,
                 highlightcolor=C["blue"]).pack(side=tk.LEFT, padx=(4, 0))

        min_frame = tk.Frame(scrape_frame, bg=C["bg2"])
        min_frame.pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(min_frame, text="  Min records:", bg=C["bg2"], fg=C["muted"],
                 font=FONT(9)).pack(side=tk.LEFT)
        tk.Entry(min_frame, textvariable=min_var, bg=C["bg3"], fg=C["text"],
                 insertbackground=C["text"], relief=tk.FLAT, font=FONT(10), width=4,
                 highlightbackground=C["border2"], highlightthickness=1,
                 highlightcolor=C["blue"]).pack(side=tk.LEFT, padx=(4, 0))

        def _scrape_sold(q=query, nm=name):
            try:
                pages = max(1, int(pages_var.get()))
            except ValueError:
                pages = 5
            try:
                min_rec = max(1, int(min_var.get()))
            except ValueError:
                min_rec = 20
            cmd = [PYTHON, "listing_watcher.py", "--scrape-sold",
                   "--query", q, "--pages", str(pages), "--min-records", str(min_rec)]
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, cwd=str(Path(__file__).parent))
                qu = queue.Queue()
                def _r():
                    for ln in proc.stdout:
                        qu.put(ln.rstrip())
                    qu.put(f"__EXIT__{proc.wait()}")
                threading.Thread(target=_r, daemon=True).start()
                self.instances[f"__sold_{nm}"] = {"process": proc, "queue": qu}
                self._append_log(f"eBay Sold [{nm}]",
                                 f"Scraping sold listings for '{q}' ({pages} pages, min {min_rec} records)...")
                self._show_page("log")
            except Exception as e:
                self._append_log(f"eBay Sold [{nm}]", f"Failed: {e}")

        HoverBtn(scrape_frame, "  Scrape eBay Sold  ", _scrape_sold,
                 bg=C["bg3"], hover=C["bg4"], fg=C["teal"],
                 padx=12, pady=5, font=FONT(10)).pack(side=tk.LEFT)

        # — Delete data button —
        def _delete_data(q=query, nm=name):
            slug = re.sub(r"[^a-z0-9]+", "_", q.lower()).strip("_")
            deleted = []
            for pattern in [f"{slug}_*.csv"]:
                for f in DATASETS_DIR.glob(pattern):
                    f.unlink()
                    deleted.append(f.name)
            self._append_log(nm, f"Deleted {len(deleted)} file(s): {', '.join(deleted) or 'none found'}")
            self._refresh_alerts()

        HoverBtn(opts, "  Delete Data  ", _delete_data,
                 bg=C["bg3"], hover=C["red_dim"], fg=C["red"],
                 padx=12, pady=5, font=FONT(10)).pack(side=tk.LEFT, padx=(8, 0))

        divider(self._alerts_inner)

    # ── Data page ─────────────────────────────────────────────────────────────
    def _build_data_page(self):
        self._data_frame = tk.Frame(self.root, bg=C["bg"])

        # Top toolbar
        bar = tk.Frame(self._data_frame, bg=C["bg2"])
        bar.pack(fill=tk.X)

        tk.Label(bar, text="Query:", bg=C["bg2"], fg=C["muted"],
                 font=FONT(10), padx=12, pady=10).pack(side=tk.LEFT)

        self._data_query_var = tk.StringVar()
        self._data_query_combo = tk.Entry(
            bar, textvariable=self._data_query_var,
            bg=C["bg3"], fg=C["text"],
            insertbackground=C["text"], relief=tk.FLAT,
            font=FONT(11), width=30,
            highlightbackground=C["border2"], highlightthickness=1,
            highlightcolor=C["teal"])
        self._data_query_combo.pack(side=tk.LEFT, ipady=7, padx=(0, 8), pady=8)
        self._data_query_combo.bind("<FocusIn>",  lambda e: self._data_query_combo.configure(highlightbackground=C["teal"]))
        self._data_query_combo.bind("<FocusOut>", lambda e: self._data_query_combo.configure(highlightbackground=C["border2"]))

        HoverBtn(bar, "  Refresh  ", self._refresh_data,
                 bg=C["teal"], hover=_interp(C["teal"], "#ffffff", 0.2),
                 fg=C["bg"], font=FONT(10, True),
                 padx=14, pady=7).pack(side=tk.LEFT, padx=4, pady=8)

        HoverBtn(bar, "Generate Chart", self._open_chart,
                 bg=C["bg3"], hover=C["bg4"], fg=C["muted"],
                 padx=12, pady=7, font=FONT(10)).pack(side=tk.LEFT, padx=4, pady=8)

        # Saved search quick-select buttons
        self._data_query_btns_frame = tk.Frame(bar, bg=C["bg2"])
        self._data_query_btns_frame.pack(side=tk.LEFT, padx=8)

        # Right: last-refreshed label
        self._data_refresh_lbl = tk.Label(bar, text="", bg=C["bg2"], fg=C["muted"],
                                           font=FONT(9))
        self._data_refresh_lbl.pack(side=tk.RIGHT, padx=14)

        divider(self._data_frame)

        # Column headers
        cols = [("#",10),("Query",180),("Platform",90),("Listings",70),
                ("Min",80),("Mean",80),("Median",80),("Max",80),("eBay Sold",90),("Threshold",90)]
        hdr = tk.Frame(self._data_frame, bg=C["bg2"])
        hdr.pack(fill=tk.X)
        for text, w in cols:
            tk.Label(hdr, text=text, bg=C["bg2"], fg=C["muted"],
                     font=FONT(9), width=max(w//7,1), anchor="w",
                     padx=8, pady=8).pack(side=tk.LEFT)
        divider(self._data_frame)

        outer, self._data_inner, self._data_canvas = scrolled(self._data_frame)
        outer.pack(fill=tk.BOTH, expand=True)

        self._data_empty = tk.Label(
            self._data_inner,
            text="Enter a query above and click Refresh.\nOr run a watch first to collect data.",
            bg=C["bg"], fg=C["muted"], font=FONT(12), justify=tk.CENTER)
        self._data_empty.pack(pady=70)

        self._populate_query_shortcuts()

    def _populate_query_shortcuts(self):
        for w in self._data_query_btns_frame.winfo_children():
            w.destroy()
        for q in _discover_queries()[:6]:
            wrap = tk.Frame(self._data_query_btns_frame, bg=C["bg2"])
            wrap.pack(side=tk.LEFT, padx=2, pady=8)
            HoverBtn(wrap, q,
                     lambda q=q: (self._data_query_var.set(q), self._refresh_data()),
                     bg=C["bg3"], hover=_interp(C["teal"], "#000000", 0.6),
                     fg=C["muted"], padx=8, pady=4, font=FONT(9)
                     ).pack(side=tk.LEFT)
            HoverBtn(wrap, "×", lambda q=q: self._delete_query_data(q),
                     bg=C["bg3"], hover=C["red_dim"], fg=C["red"],
                     padx=5, pady=4, font=FONT(9)
                     ).pack(side=tk.LEFT)

    def _delete_query_data(self, query: str):
        slug = _slug(query)
        deleted = []
        for f in DATASETS_DIR.glob(f"{slug}_*.csv"):
            f.unlink()
            deleted.append(f.name)
        self._append_log("Data", f"Deleted {len(deleted)} file(s) for '{query}'")
        if self._data_query_var.get().strip().lower() == query.lower():
            self._data_query_var.set("")
            for w in self._data_inner.winfo_children():
                w.destroy()
            tk.Label(self._data_inner,
                     text="Enter a query above and click Refresh.\nOr run a watch first to collect data.",
                     bg=C["bg"], fg=C["muted"], font=FONT(12), justify=tk.CENTER
                     ).pack(pady=70)
        self._populate_query_shortcuts()

    def _refresh_data(self):
        query = self._data_query_var.get().strip()
        for w in self._data_inner.winfo_children():
            w.destroy()

        if not query:
            tk.Label(self._data_inner, text="Enter a query above and click Refresh.",
                     bg=C["bg"], fg=C["muted"], font=FONT(12)).pack(pady=70)
            return

        platforms = ["ebay", "vinted", "depop"]
        sold_median = _load_sold_median(query)
        bargain_threshold = round(sold_median * 0.50, 2) if sold_median else None

        rows_added = 0
        for i, plat in enumerate(platforms):
            prices = _load_platform_prices(query, plat)
            if not prices:
                continue
            s = _price_stats(prices)
            row_bg = C["bg3"] if rows_added % 2 == 0 else C["bg"]

            row = tk.Frame(self._data_inner, bg=row_bg, height=54)
            row.pack(fill=tk.X)
            row.pack_propagate(False)

            # Colour accent left bar
            tk.Frame(row, bg=PLAT_COLOUR.get(plat, C["muted"]), width=3).pack(side=tk.LEFT, fill=tk.Y)

            vals = [
                (str(rows_added + 1),      10, C["faint"],  False),
                (query[:24],               180, C["text"],   False),
                (f"{PLAT_ICON.get(plat,'?')} {plat.capitalize()}", 90, PLAT_COLOUR.get(plat, C["muted"]), True),
                (str(s["count"]),           70, C["text"],   False),
                (f"£{s['min']:.2f}",        80, C["muted"],  False),
                (f"£{s['mean']:.2f}",       80, C["muted"],  False),
                (f"£{s['median']:.2f}",     80, C["text"],   True),
                (f"£{s['max']:.2f}",        80, C["muted"],  False),
                (f"£{sold_median:.2f}" if sold_median else "—", 90, C["yellow"], False),
                (f"£{bargain_threshold:.2f}" if bargain_threshold else "—", 90, C["green"], False),
            ]
            for text, w, fg, bold in vals:
                tk.Label(row, text=text, bg=row_bg, fg=fg,
                         font=FONT(10, bold), width=max(w//7, 1),
                         anchor="w", padx=8).pack(side=tk.LEFT)

            divider(self._data_inner)
            rows_added += 1

        if rows_added == 0:
            tk.Label(self._data_inner,
                     text=f"No CSV data found for  \"{query}\".\nRun a watch first to collect listing data.",
                     bg=C["bg"], fg=C["muted"], font=FONT(12), justify=tk.CENTER).pack(pady=70)
        else:
            # Summary footer
            tk.Frame(self._data_inner, bg=C["bg"], height=12).pack()
            foot = tk.Frame(self._data_inner, bg=C["bg2"],
                            highlightbackground=C["border"], highlightthickness=1)
            foot.pack(fill=tk.X, padx=24, pady=8)
            info_parts = [f"Query: \"{query}\"  ·  {rows_added} platform(s) with data"]
            if sold_median:
                info_parts.append(f"eBay sold median: £{sold_median:.2f}")
            if bargain_threshold:
                info_parts.append(f"Bargain threshold: £{bargain_threshold:.2f} (50%)")
            tk.Label(foot, text="   ·   ".join(info_parts),
                     bg=C["bg2"], fg=C["muted"], font=FONT(9),
                     padx=16, pady=10).pack(anchor="w")

        self._data_refresh_lbl.configure(
            text=f"Updated {datetime.now().strftime('%H:%M:%S')}")
        self._populate_query_shortcuts()

    def _open_chart(self):
        query = self._data_query_var.get().strip()
        if not query:
            return
        chart_path = DATASETS_DIR / f"{_slug(query)}_price_comparison.png"
        # Generate chart via visualise.py in background
        def _gen():
            try:
                subprocess.run(
                    [PYTHON, "visualise.py", query],
                    cwd=str(Path(__file__).parent),
                    capture_output=True, timeout=30)
                if chart_path.exists():
                    import os
                    if sys.platform == "darwin":
                        os.system(f'open "{chart_path}"')
                    elif sys.platform.startswith("win"):
                        os.startfile(str(chart_path))
                    else:
                        os.system(f'xdg-open "{chart_path}"')
            except Exception:
                pass
        threading.Thread(target=_gen, daemon=True).start()

    def _load_bargains(self):
        if not BARGAINS_FILE.exists():
            return
        try:
            with open(BARGAINS_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            self.bargains.extend(saved)
            self._render_bargains()
            self._update_stats()
        except Exception:
            pass

    def _save_bargains(self):
        try:
            DATASETS_DIR.mkdir(exist_ok=True)
            with open(BARGAINS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.bargains, f)
        except Exception:
            pass

    def _check_existing_bargains(self, query: str, platforms: list, threshold: float):
        """Check existing CSV data and add ALL items below threshold to Bargains."""
        added   = 0
        parts   = []
        slugs_to_try = sorted({_slug(query), query.lower().replace(" ", "_")})
        now_str = datetime.now().strftime("%H:%M")
        # Block URLs already in bargains OR already in missed — prevents re-adding sold items
        # and prevents the same item appearing in both tabs
        existing_urls = (
            {b.get("url", "") for b in self.bargains if b.get("url")} |
            {m.get("url", "") for m in self.missed   if m.get("url")}
        )

        for platform in platforms:
            # Find the CSV path (try both slug variants)
            path = None
            for slug in slugs_to_try:
                candidate = DATASETS_DIR / f"{slug}_{platform}_new.csv"
                if candidate.exists():
                    path = candidate
                    break

            if path is None:
                slug = slugs_to_try[-1]
                parts.append(f"{platform}: no CSV found ({slug}_{platform}_new.csv)")
                continue

            # Load all rows below threshold
            platform_added = 0
            try:
                with open(path, newline="", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        price_val = _parse_price_val(row.get("price", ""))
                        if price_val is None or price_val <= 0:
                            continue
                        if price_val >= threshold:
                            continue
                        url = row.get("url", "")
                        if url and url in existing_urls:
                            continue  # already shown, skip duplicate
                        savings = round(threshold - price_val, 2)
                        bargain = {
                            "platform":  platform,
                            "title":     row.get("title", ""),
                            "price":     row.get("price", ""),
                            "url":       url,
                            "image_url": row.get("image_url", ""),
                            "savings":   savings,
                            "query":     query,
                            "time":      now_str,
                        }
                        if url:
                            existing_urls.add(url)
                        self.bargains.append(bargain)
                        self._add_bargain_row(bargain)
                        self._notify_bargain(bargain)
                        platform_added += 1
                        added += 1
            except Exception as exc:
                parts.append(f"{platform}: error reading CSV ({exc})")
                continue

            if platform_added:
                parts.append(f"{platform}: ✓ {platform_added} item(s) added")
            else:
                parts.append(f"{platform}: 0 items below £{threshold:.2f} in {path.name}")

        self._append_log(
            "Alerts",
            f"Threshold £{threshold:.2f} scan for '{query}': {' | '.join(parts) or 'no platforms checked'}"
        )
        if added:
            self._save_bargains()
            self._update_stats()

    def _clear_bargains(self):
        self.bargains.clear()
        self._save_bargains()
        self._render_bargains()
        self._update_stats()

    # ── Log page ──────────────────────────────────────────────────────────────
    def _build_log_page(self):
        self._log_frame = tk.Frame(self.root, bg=C["bg"])

        bar = tk.Frame(self._log_frame, bg=C["bg2"])
        bar.pack(fill=tk.X)
        tk.Label(bar, text="Live Output", bg=C["bg2"], fg=C["muted"],
                 font=FONT(10), padx=16, pady=8).pack(side=tk.LEFT)
        HoverBtn(bar, "Clear", self._clear_log,
                 bg=C["bg3"], hover=C["bg4"], fg=C["muted"],
                 padx=10, pady=5, font=FONT(9)).pack(side=tk.RIGHT, padx=10, pady=7)
        divider(self._log_frame)

        self.log_text = tk.Text(
            self._log_frame, bg="#060d18", fg="#8b9db5",
            font=MONO(10), wrap=tk.WORD,
            state=tk.DISABLED, selectbackground=C["bg4"],
            relief=tk.FLAT, padx=16, pady=12,
            insertbackground=C["text"],
        )
        sb = tk.Scrollbar(self._log_frame, command=self.log_text.yview,
                          bg=C["bg3"], troughcolor=C["bg2"],
                          activebackground=C["bg4"], width=6,
                          relief=tk.FLAT, bd=0)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # Tags
        self.log_text.tag_configure("ts",      foreground="#2d4a6a")
        self.log_text.tag_configure("name",    foreground=C["purple"])
        self.log_text.tag_configure("bargain", foreground=C["yellow"],  font=MONO(10))
        self.log_text.tag_configure("new",     foreground=C["green"])
        self.log_text.tag_configure("error",   foreground=C["red"])
        self.log_text.tag_configure("info",    foreground=C["blue"])
        self.log_text.tag_configure("default", foreground="#8b9db5")

    def _append_log(self, name: str, line: str):
        tag = "default"
        l = line.lower()
        if "bargain" in l or "🔥" in line:
            tag = "bargain"
        elif "✅" in line or "new" in l or "seeded" in l:
            tag = "new"
        elif "❌" in line or "error" in l or "fail" in l:
            tag = "error"
        elif "ready" in l or "[watch]" in l or "threshold" in l:
            tag = "info"

        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{ts}] ", "ts")
        self.log_text.insert(tk.END, f"[{name}] ", "name")
        self.log_text.insert(tk.END, f"{line}\n", tag)
        self.log_text.configure(state=tk.DISABLED)
        self.log_text.see(tk.END)
        self._log_lines += 1
        if self._log_lines > 2500:
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.delete("1.0", "600.0")
            self.log_text.configure(state=tk.DISABLED)
            self._log_lines -= 600

    def _clear_log(self):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)
        self._log_lines = 0

    # ── Status bar ────────────────────────────────────────────────────────────
    # ── Settings page ─────────────────────────────────────────────────────────
    def _build_settings_page(self):
        self._settings_frame = tk.Frame(self.root, bg=C["bg"])

        outer, inner, self._settings_canvas = scrolled(self._settings_frame)
        outer.pack(fill=tk.BOTH, expand=True)

        def section(title, colour=C["green"]):
            hdr = tk.Frame(inner, bg=C["bg"])
            hdr.pack(fill=tk.X, padx=32, pady=(24, 6))
            tk.Frame(hdr, bg=colour, width=3).pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
            tk.Label(hdr, text=title, bg=C["bg"], fg=C["text"],
                     font=FONT(12, True)).pack(side=tk.LEFT)

        def setting_row(parent, label, hint, key, secret=False):
            row = tk.Frame(parent, bg=C["bg2"],
                           highlightbackground=C["border"],
                           highlightthickness=1)
            row.pack(fill=tk.X, padx=32, pady=3)
            left = tk.Frame(row, bg=C["bg2"], width=220)
            left.pack(side=tk.LEFT, fill=tk.Y, padx=(18, 0), pady=12)
            left.pack_propagate(False)
            tk.Label(left, text=label, bg=C["bg2"], fg=C["text"],
                     font=FONT(10, True), anchor="w").pack(anchor="w")
            tk.Label(left, text=hint, bg=C["bg2"], fg=C["muted"],
                     font=FONT(9), anchor="w", wraplength=200,
                     justify=tk.LEFT).pack(anchor="w", pady=(2, 0))
            val = cfg.get(key, "")
            e = tk.Entry(row, bg=C["bg3"], fg=C["text"],
                         insertbackground=C["text"], relief=tk.FLAT,
                         font=FONT(10),
                         highlightbackground=C["border"],
                         highlightthickness=1,
                         highlightcolor=C["green"],
                         show="●" if secret else "")
            e.insert(0, val)
            e.pack(side=tk.LEFT, fill=tk.X, expand=True,
                   ipady=10, padx=16, pady=12)
            e.bind("<FocusIn>",  lambda ev, w=e: w.configure(
                highlightbackground=C["green"]))
            e.bind("<FocusOut>", lambda ev, w=e: w.configure(
                highlightbackground=C["border"]))
            # Eye toggle for secret fields
            if secret:
                showing = [False]
                def _toggle_show(w=e, s=showing):
                    s[0] = not s[0]
                    w.configure(show="" if s[0] else "●")
                HoverBtn(row, "👁", _toggle_show,
                         bg=C["bg3"], hover=C["bg4"], fg=C["muted"],
                         padx=8, pady=6, font=FONT(11)).pack(
                    side=tk.LEFT, padx=(0, 8), pady=12)
            self._setting_entries[key] = e

        cfg = _load_config()
        self._setting_entries = {}

        # ── eBay API ────────────────────────────────────────────────────────
        section("eBay Browse API", C["ebay"])
        tk.Label(inner,
                 text="Get free keys at  developer.ebay.com  →  My Keys  →  Production",
                 bg=C["bg"], fg=C["muted"], font=FONT(9)).pack(
            anchor="w", padx=32, pady=(0, 4))

        ebay_block = tk.Frame(inner, bg=C["bg"])
        ebay_block.pack(fill=tk.X)
        setting_row(ebay_block, "App ID (Client ID)",
                    "ebay developer App ID", "ebay_app_id", secret=False)
        setting_row(ebay_block, "Cert ID (Client Secret)",
                    "ebay developer Cert ID", "ebay_cert_id", secret=True)

        # ── Discord ─────────────────────────────────────────────────────────
        section("Discord Notifications", C["purple"])
        tk.Label(inner,
                 text="Server Settings  →  Integrations  →  Webhooks  →  New Webhook  →  Copy URL",
                 bg=C["bg"], fg=C["muted"], font=FONT(9)).pack(
            anchor="w", padx=32, pady=(0, 4))

        disc_block = tk.Frame(inner, bg=C["bg"])
        disc_block.pack(fill=tk.X)
        setting_row(disc_block, "Webhook URL",
                    "Paste your Discord channel webhook URL",
                    "discord_webhook", secret=True)

        # ── Save button ──────────────────────────────────────────────────────
        tk.Frame(inner, bg=C["bg"], height=10).pack()
        divider(inner)
        save_row = tk.Frame(inner, bg=C["bg"])
        save_row.pack(fill=tk.X, padx=32, pady=18)

        self._save_status = tk.Label(save_row, text="",
                                     bg=C["bg"], fg=C["green"],
                                     font=FONT(10))
        self._save_status.pack(side=tk.RIGHT, padx=12)

        HoverBtn(save_row, "  Save Settings  ", self._save_settings,
                 bg=C["green"], hover=_interp(C["green"], "#ffffff", 0.2),
                 fg=C["bg"], font=FONT(11, True),
                 padx=22, pady=10).pack(side=tk.LEFT)

        HoverBtn(save_row, "Test Discord", self._test_discord,
                 bg=C["bg3"], hover=C["bg4"], fg=C["muted"],
                 padx=14, pady=10, font=FONT(10)).pack(side=tk.LEFT, padx=10)

    def _save_settings(self):
        cfg = {k: e.get().strip() for k, e in self._setting_entries.items()}
        _save_config(cfg)
        # Patch listing_watcher module live if imported
        try:
            import listing_watcher as lw
            if cfg["ebay_app_id"]:
                lw.EBAY_APP_ID  = cfg["ebay_app_id"]
            if cfg["ebay_cert_id"]:
                lw.EBAY_CERT_ID = cfg["ebay_cert_id"]
            if cfg["discord_webhook"]:
                lw.DISCORD_WEBHOOK = cfg["discord_webhook"]
        except Exception:
            pass
        self._save_status.configure(text="✓  Saved", fg=C["green"])
        self.root.after(3000, lambda: self._save_status.configure(text=""))

    def _test_discord(self):
        cfg = _load_config()
        webhook = self._setting_entries.get("discord_webhook")
        url = webhook.get().strip() if webhook else cfg.get("discord_webhook", "")
        if not url:
            self._save_status.configure(text="No webhook URL set", fg=C["red"])
            return
        import threading, requests as _req
        def _send():
            try:
                r = _req.post(url, json={"embeds": [{"title": "✅ Unifi — Test",
                    "description": "Discord notifications are working!",
                    "color": 0x22c55e}]}, timeout=8)
                msg = "✓  Discord OK" if r.status_code == 204 else f"⚠  HTTP {r.status_code}"
                col = C["green"] if r.status_code == 204 else C["yellow"]
            except Exception as e:
                msg, col = f"✗  {e}", C["red"]
            self.root.after(0, lambda: self._save_status.configure(text=msg, fg=col))
        self._save_status.configure(text="Sending…", fg=C["muted"])
        threading.Thread(target=_send, daemon=True).start()

    def _build_statusbar(self):
        divider(self.root)
        bar = tk.Frame(self.root, bg=C["bg2"], height=56)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        bar.pack_propagate(False)

        tk.Frame(bar, bg=C["red"], width=3).pack(side=tk.LEFT, fill=tk.Y)

        HoverBtn(bar, "  + Create  ", self._add_dialog,
                 bg=C["red"], hover=_interp(C["red"], "#ffffff", 0.18),
                 fg="white", font=FONT(10, True),
                 padx=16, pady=10).pack(side=tk.LEFT, padx=(14, 0), pady=10)

        # Separator
        tk.Frame(bar, bg=C["border"], width=1).pack(side=tk.LEFT,
                                                     fill=tk.Y, pady=12, padx=14)

        # Stats
        self._stat_running  = self._stat_lbl(bar, "0  Running",  C["muted"])
        self._stat_bargains = self._stat_lbl(bar, "0  Bargains", C["muted"])
        self._stat_tasks    = self._stat_lbl(bar, "0  Tasks",    C["muted"])

        # Right controls
        tk.Frame(bar, bg=C["border"], width=1).pack(side=tk.RIGHT,
                                                     fill=tk.Y, pady=12, padx=8)
        HoverBtn(bar, "■  Stop All", self._stop_all,
                 bg=C["bg3"], hover=C["bg4"], fg=C["muted"],
                 padx=14, pady=9, font=FONT(10)).pack(side=tk.RIGHT, padx=4, pady=10)
        HoverBtn(bar, "▶  Start All", self._start_all,
                 bg=C["green_dim"], hover=C["green"], fg=C["green"],
                 padx=14, pady=9, font=FONT(10)).pack(side=tk.RIGHT, padx=4, pady=10)

    @staticmethod
    def _stat_lbl(parent, text, colour):
        lbl = tk.Label(parent, text=text, bg=C["bg2"], fg=colour, font=FONT(10))
        lbl.pack(side=tk.LEFT, padx=16)
        return lbl

    def _update_stats(self):
        running = sum(1 for i in self.instances.values() if i["process"].poll() is None)
        tasks   = len(load_searches())
        bcount  = len(self.bargains)

        self._stat_running.configure(
            text=f"{running}  Running",
            fg=C["green"] if running else C["muted"])
        self._stat_bargains.configure(
            text=f"{bcount}  Bargains",
            fg=C["yellow"] if bcount else C["muted"])
        self._stat_tasks.configure(text=f"{tasks}  Tasks")
        self._status_lbl.configure(
            text="● Watching" if running else "● Idle",
            fg=C["green"] if running else C["muted"])

    # ── CRUD ──────────────────────────────────────────────────────────────────
    def _add_dialog(self):
        SearchDialog(self.root, self._persist_search)

    def _edit_dialog(self, existing: dict):
        SearchDialog(self.root, self._persist_search, existing=existing)

    def _persist_search(self, data: dict):
        searches = [s for s in load_searches()
                    if s["name"].lower() != data["name"].lower()]
        searches.append(data)
        save_searches(searches)
        self._refresh_tasks()

    def _delete_search(self, name: str):
        if name in self.instances and self.instances[name]["process"].poll() is None:
            return
        save_searches([s for s in load_searches() if s["name"] != name])
        self._refresh_tasks()

    # ── Instances ─────────────────────────────────────────────────────────────
    def _start_instance(self, s: dict):
        name = s["name"]
        if name in self.instances and self.instances[name]["process"].poll() is None:
            return

        excl_str = ",".join(s.get("exclude_keywords") or [])
        cmd = [PYTHON, "listing_watcher.py", "--watch",
               "--name",      s["name"],
               "--query",     s["query"],
               "--platforms", ",".join(s["platforms"]),
               "--interval",  str(s["interval_min"]),
               "--exclude",   excl_str]
        cfg = _load_config()
        env = {**__import__("os").environ}
        if cfg.get("ebay_app_id"):      env["UNIFI_EBAY_APP_ID"]      = cfg["ebay_app_id"]
        if cfg.get("ebay_cert_id"):     env["UNIFI_EBAY_CERT_ID"]     = cfg["ebay_cert_id"]
        if cfg.get("discord_webhook"):  env["UNIFI_DISCORD_WEBHOOK"]  = cfg["discord_webhook"]

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, cwd=str(Path(__file__).parent), env=env)
        except Exception as e:
            self._append_log(name, f"Failed to start: {e}")
            return

        q = queue.Queue()

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
            "countdown_lbl": None,
        }
        self._append_log(name, f"Started  (PID {proc.pid})")
        self._refresh_tasks()
        self._update_stats()

    def _stop_instance(self, name: str):
        if name not in self.instances:
            return
        proc = self.instances[name]["process"]
        if proc.poll() is None:
            proc.terminate()
        del self.instances[name]
        self._append_log(name, "Stopped")
        self._refresh_tasks()
        self._update_stats()

    def _start_all(self):
        for s in load_searches():
            self._start_instance(s)

    def _stop_all(self):
        for name in list(self.instances.keys()):
            self._stop_instance(name)

    # ── Countdown tickers ─────────────────────────────────────────────────────
    def _tick_countdowns(self):
        now = time.time()
        for inst in self.instances.values():
            lbl = inst.get("countdown_lbl")
            if lbl is None:
                continue
            try:
                if not lbl.winfo_exists():
                    inst["countdown_lbl"] = None
                    continue
            except Exception:
                continue
            started  = inst.get("started_at", now)
            interval = inst.get("interval_min", 5) * 60
            elapsed  = now - started
            remaining = int(interval - (elapsed % interval))
            mins, secs = divmod(remaining, 60)
            lbl.configure(text=f"{mins:02d}:{secs:02d}")
        self.root.after(1000, self._tick_countdowns)

    # ── Bargain notifications ─────────────────────────────────────────────────
    def _notify_bargain(self, data: dict):
        """Play a sound + send macOS notification + show in-app toast."""
        title   = (data.get("title") or "")[:60]
        price   = data.get("price", "?")
        plat    = (data.get("platform") or "").capitalize()
        savings = data.get("savings", "")
        msg     = f"£{price} on {plat} — saves £{float(savings):.0f}" if savings else f"£{price} on {plat}"

        # macOS native notification with sound
        if _IS_MAC:
            try:
                subprocess.Popen([
                    "osascript", "-e",
                    f'display notification "{msg}" with title "🔥 Unifi Bargain" subtitle "{title}" sound name "Ping"'
                ])
            except Exception:
                pass
        else:
            # Non-mac fallback: system bell
            try:
                self.root.bell()
            except Exception:
                pass

        # In-app toast (works on all platforms)
        self._show_toast(f"🔥  {title}", msg)

    def _show_toast(self, heading: str, body: str, duration: int = 4000):
        """Slide-in toast notification anchored to the bottom-right of the window."""
        toast = tk.Frame(self.root, bg=C["yellow"], padx=2, pady=2)

        inner = tk.Frame(toast, bg=C["bg2"], padx=14, pady=10)
        inner.pack(fill=tk.BOTH, expand=True)

        tk.Label(inner, text=heading, bg=C["bg2"], fg=C["yellow"],
                 font=FONT(10, True), anchor="w", wraplength=280).pack(fill=tk.X)
        tk.Label(inner, text=body, bg=C["bg2"], fg=C["text"],
                 font=FONT(9), anchor="w", wraplength=280).pack(fill=tk.X, pady=(2, 0))

        def _dismiss():
            try:
                toast.destroy()
            except Exception:
                pass

        tk.Label(inner, text="✕", bg=C["bg2"], fg=C["muted"],
                 font=FONT(9), cursor="hand2").pack(anchor="ne")
        inner.winfo_children()[-1].bind("<Button-1>", lambda _: _dismiss())

        # Place at bottom-right
        self.root.update_idletasks()
        rw = self.root.winfo_width()
        rh = self.root.winfo_height()
        tw, th = 310, 90
        x = rw - tw - 16
        y = rh - th - 40

        toast.place(x=x, y=y, width=tw, height=th)
        toast.lift()

        # Auto-dismiss after duration
        self.root.after(duration, _dismiss)

    # ── Output poll ───────────────────────────────────────────────────────────
    def _poll_output(self):
        needs_refresh = False

        for name, inst in list(self.instances.items()):
            try:
                while True:
                    line = inst["queue"].get_nowait()

                    if line.startswith("__EXIT__"):
                        self._append_log(name, f"Process ended  (exit {line[8:]})")
                        del self.instances[name]
                        needs_refresh = True
                        break

                    if line.startswith("BARGAIN_ITEM:"):
                        try:
                            data = json.loads(line[len("BARGAIN_ITEM:"):])
                            data["time"] = datetime.now().strftime("%H:%M")
                            self.bargains.append(data)
                            self._add_bargain_row(data)
                            self._save_bargains()
                            self._update_stats()
                            # Flash the nav button
                            self._nav_btns["bargains"].configure(fg=C["yellow"])
                            self.root.after(3000, lambda:
                                self._nav_btns["bargains"].configure(
                                    fg=C["text"] if self._page == "bargains" else C["muted"]))
                            self._notify_bargain(data)
                        except Exception:
                            pass
                        continue

                    self._append_log(name, line)

            except queue.Empty:
                pass

        # Reap dead processes
        for name in list(self.instances.keys()):
            if self.instances[name]["process"].poll() is not None:
                del self.instances[name]
                needs_refresh = True

        if needs_refresh:
            self._refresh_tasks()
            self._update_stats()

        self.root.after(250, self._poll_output)

    # ── Shutdown ──────────────────────────────────────────────────────────────
    def on_close(self):
        for inst in self.instances.values():
            try:
                inst["process"].terminate()
            except Exception:
                pass
        self.root.destroy()


# ─── Config helpers ───────────────────────────────────────────────────────────
def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        return dict(CONFIG_DEFAULTS)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {**CONFIG_DEFAULTS, **data}
    except Exception:
        return dict(CONFIG_DEFAULTS)


def _save_config(cfg: dict) -> None:
    DATASETS_DIR.mkdir(exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def _apply_config_to_watcher() -> None:
    """Inject saved API keys into listing_watcher at runtime."""
    cfg = _load_config()
    try:
        import listing_watcher as lw
        if cfg.get("ebay_app_id"):
            lw.EBAY_APP_ID      = cfg["ebay_app_id"]
        if cfg.get("ebay_cert_id"):
            lw.EBAY_CERT_ID     = cfg["ebay_cert_id"]
        if cfg.get("discord_webhook"):
            lw.DISCORD_WEBHOOK  = cfg["discord_webhook"]
    except Exception:
        pass


# ─── Helpers ──────────────────────────────────────────────────────────────────
def load_searches() -> list[dict]:
    if not SAVED_SEARCHES_FILE.exists():
        return []
    try:
        with open(SAVED_SEARCHES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []




def save_searches(searches: list[dict]) -> None:
    DATASETS_DIR.mkdir(exist_ok=True)
    with open(SAVED_SEARCHES_FILE, "w", encoding="utf-8") as f:
        json.dump(searches, f, indent=2)


# ─── Entry point ──────────────────────────────────────────────────────────────
def main():
    root = tk.Tk()
    app  = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
