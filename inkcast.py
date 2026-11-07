#!/usr/bin/env python3
"""
InkCast - draw live on your Windows screen from any tablet (iPad or Android).

The PC shows an invisible, click-through ink layer on top of everything.
The tablet opens a web page that shows a live view of the PC screen; whatever
you draw there (pencil or finger) appears on the PC instantly. Your mouse and
keyboard keep working normally the whole time.

Run:  python inkcast.py      (or double-click "Start InkCast.bat")
"""
import asyncio
import base64
import ctypes
from ctypes import wintypes
import hashlib
import io
import ipaddress
import json
import math
import os
import secrets
import signal
import socket
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from aiohttp import web
from PySide6.QtCore import Qt, QObject, Signal, QPointF, QRectF, QTimer, QBuffer, QByteArray, QIODevice
from PySide6.QtGui import (QAction, QColor, QFont, QGuiApplication, QIcon, QImage, QPainter,
                           QPainterPath, QPen, QPixmap, QPolygonF, QRegion)
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QInputDialog, QLabel, QMenu, QPushButton,
                               QSystemTrayIcon, QVBoxLayout, QWidget)

APP = "InkCast"


# --------------------------------------------------------------------------- Win32 Mouse Input
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]

class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]

class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", INPUT_UNION),
    ]

def _send_single_input(flags, dx=0, dy=0, data=0) -> bool:
    if sys.platform != "win32":
        return True
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi.dx = int(dx)
    inp.u.mi.dy = int(dy)
    inp.u.mi.mouseData = int(data) & 0xFFFFFFFF
    inp.u.mi.dwFlags = flags
    inp.u.mi.time = 0
    inp.u.mi.dwExtraInfo = 0
    res = ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    return res == 1

def inject_mouse_event(action: str, **kwargs) -> bool:
    if sys.platform != "win32":
        return True
    if action == "move_rel":
        dx = kwargs.get("dx", 0)
        dy = kwargs.get("dy", 0)
        return _send_single_input(MOUSEEVENTF_MOVE, dx=dx, dy=dy)
    elif action == "move_abs":
        # x, y normalized [0.0..1.0] -> [0..65535]
        nx = max(0.0, min(1.0, float(kwargs.get("x", 0.0))))
        ny = max(0.0, min(1.0, float(kwargs.get("y", 0.0))))
        ax = int(nx * 65535.0)
        ay = int(ny * 65535.0)
        return _send_single_input(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, dx=ax, dy=ay)
    elif action == "click":
        btn = kwargs.get("btn", "left")
        if btn == "right":
            _send_single_input(MOUSEEVENTF_RIGHTDOWN)
            return _send_single_input(MOUSEEVENTF_RIGHTUP)
        else:
            _send_single_input(MOUSEEVENTF_LEFTDOWN)
            return _send_single_input(MOUSEEVENTF_LEFTUP)
    elif action == "down":
        btn = kwargs.get("btn", "left")
        flag = MOUSEEVENTF_RIGHTDOWN if btn == "right" else MOUSEEVENTF_LEFTDOWN
        return _send_single_input(flag)
    elif action == "up":
        btn = kwargs.get("btn", "left")
        flag = MOUSEEVENTF_RIGHTUP if btn == "right" else MOUSEEVENTF_LEFTUP
        return _send_single_input(flag)
    elif action == "scroll":
        dy = kwargs.get("dy", 0)
        return _send_single_input(MOUSEEVENTF_WHEEL, data=dy)
    return False

class InputExecutor:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1)

    def submit_mouse(self, action: str, **kwargs):
        return self.executor.submit(inject_mouse_event, action, **kwargs)

    def shutdown(self):
        self.executor.shutdown(wait=False)


# --------------------------------------------------------------------------- Settings & Auth
def get_settings_path() -> str:
    target_dir = os.path.expandvars(r"%LOCALAPPDATA%\InkCast")
    os.makedirs(target_dir, exist_ok=True)
    return os.path.join(target_dir, "settings.json")

def load_settings() -> dict:
    defaults = {"remote_pin": "", "mouse_enabled": True}
    path = get_settings_path()
    if not os.path.isfile(path):
        return defaults
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
            return {**defaults, **d}
    except Exception as e:
        print(f"Warning: could not read settings.json ({e}), using defaults")
        return defaults

def save_settings(data: dict):
    path = get_settings_path()
    tmp_path = path + f".tmp-{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception as e:
        print(f"Error saving settings: {e}")
        if os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except OSError: pass

def hash_pin(pin: str) -> str:
    if not pin:
        return ""
    return hashlib.sha256(pin.strip().encode("utf-8")).hexdigest()

def is_remote_request(request) -> bool:
    if request.headers.get("CF-Connecting-IP"):
        return True
    host = (request.host or "").split(":")[0].lower()
    if not host or host in ("localhost", "127.0.0.1", "::1"):
        return False
    try:
        ipaddress.ip_address(host)
        return False  # direct LAN IP
    except ValueError:
        return True   # domain name or tunnel hostname

def get_client_ip(request) -> str:
    return request.headers.get("CF-Connecting-IP") or request.remote or "unknown"

class AuthManager:
    def __init__(self, pin_hash: str = ""):
        self.pin_hash = pin_hash
        self.tokens = set()
        self.failed_attempts = {} # ip -> [timestamps]

    def set_pin(self, pin: str):
        self.pin_hash = hash_pin(pin)
        self.revoke_tokens()

    def revoke_tokens(self):
        self.tokens.clear()

    def validate_token(self, token: str) -> bool:
        return bool(token and token in self.tokens)

    def issue_token(self) -> str:
        token = secrets.token_hex(16)
        self.tokens.add(token)
        return token

    def is_rate_limited(self, ip: str) -> bool:
        now = time.time()
        attempts = [t for t in self.failed_attempts.get(ip, []) if now - t < 60]
        self.failed_attempts[ip] = attempts
        return len(attempts) >= 5

    def record_failed_attempt(self, ip: str):
        now = time.time()
        attempts = [t for t in self.failed_attempts.get(ip, []) if now - t < 60]
        attempts.append(now)
        self.failed_attempts[ip] = attempts

    def validate_pin(self, pin: str, client_ip: str) -> tuple[bool, str]:
        if not self.pin_hash:
            return False, "Remote access is locked: host has not set a PIN."
        if self.is_rate_limited(client_ip):
            return False, "Too many failed attempts. Locked for 60 seconds."
        if hash_pin(pin) == self.pin_hash:
            token = self.issue_token()
            return True, token
        else:
            self.record_failed_attempt(client_ip)
            return False, "Incorrect PIN."



class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

def get_normalized_cursor(screen_w: int, screen_h: int) -> tuple[float, float]:
    if sys.platform != "win32" or screen_w <= 0 or screen_h <= 0:
        return 0.5, 0.5
    pt = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    x = max(0.0, min(1.0, pt.x / float(screen_w)))
    y = max(0.0, min(1.0, pt.y / float(screen_h)))
    return round(x, 4), round(y, 4)


def resource_dir():
    """Folder containing files shipped next to the executable.
    A standalone build keeps the camera helper writable beside InkCast.exe.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


PORT = int(os.environ.get("INKCAST_PORT", "8765"))
REF = 1920.0                      # sizes are defined relative to a 1920px-wide screen
STROKES = {"pen", "marker", "hl", "neon", "laser", "farrow"}
SHAPES = {"line", "arrow", "rect", "ellipse"}
LASER_HOLD, LASER_FADE = 0.6, 0.6  # seconds a laser line stays, then fades
PREVIEW_WIDTH = 1280               # width of the screen preview sent to the tablet
PREVIEW_QUALITY = 55
MAX_IMAGE = 1600                   # pasted/inserted images are scaled down to this
BGS = ["white", "grid", "black", "green"]
BGC = {"white": "#ffffff", "grid": "#ffffff", "black": "#141517", "green": "#1f3a2c"}
GEOM_KEYS = ("pts", "w", "a", "b", "x", "y", "fs", "tw", "th", "h")

# Text fonts: the PC serves the same font files to the tablet, so text looks
# (and measures) identical on both. First file found in C:\Windows\Fonts wins.
FONT_OPTIONS = {
    "sans":  [("segoeuib.ttf", "Segoe UI", True)],
    "hand":  [("inkfree.ttf", "Ink Free", False), ("comicbd.ttf", "Comic Sans MS", True)],
    "serif": [("georgiab.ttf", "Georgia", True)],
}

# Global shortcuts: Ctrl + Alt + <key>
HOTKEYS = [
    ("Z", "undo", "Undo"),
    ("Y", "redo", "Redo"),
    ("C", "clear", "Clear ink"),
    ("H", "hide", "Hide / show ink"),
    ("L", "laser", "Laser pointer on / off"),
    ("W", "board", "Whiteboard on / off"),
    ("B", "bg", "Change whiteboard background"),
    ("V", "paste", "Paste image from clipboard"),
]


def resolve_fonts():
    fontdir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    out = {}
    for name, options in FONT_OPTIONS.items():
        chosen = None
        for fname, family, bold in options:
            path = os.path.join(fontdir, fname)
            if os.path.isfile(path):
                chosen = (path, family, bold)
                break
        if chosen is None:
            _f, family, bold = options[-1]
            chosen = (None, family, bold)
        out[name] = chosen
    return out


FONTS = resolve_fonts()


def geom_of(it):
    g = {}
    for k in GEOM_KEYS:
        if k in it:
            v = it[k]
            if k == "pts":
                v = [list(p) for p in v]
            elif isinstance(v, list):
                v = list(v)
            g[k] = v
    return g


def xf_geom(g, f, tx, ty):
    """x' = f*x + tx, y' = f*y + ty; sizes scale by f (uniform in screen pixels)."""
    out = {}
    if "pts" in g:
        out["pts"] = [[x * f + tx, y * f + ty] for x, y in g["pts"]]
    for k in ("a", "b"):
        if k in g:
            out[k] = [g[k][0] * f + tx, g[k][1] * f + ty]
    if "x" in g:
        out["x"] = g["x"] * f + tx
        out["y"] = g["y"] * f + ty
    for k in ("w", "h", "fs", "tw", "th"):
        if k in g:
            out[k] = g[k] * f
    return out


def encode_image_bytes(data):
    """Any image bytes -> (data URL, width, height), scaled down to MAX_IMAGE."""
    from PIL import Image, ImageOps
    im = Image.open(io.BytesIO(data))
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:
        pass
    im.thumbnail((MAX_IMAGE, MAX_IMAGE))
    has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    buf = io.BytesIO()
    if has_alpha:
        im.convert("RGBA").save(buf, "PNG")
        mime = "image/png"
    else:
        im.convert("RGB").save(buf, "JPEG", quality=90)
        mime = "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode(), im.width, im.height


def qimage_to_dataurl(img):
    if img.width() > MAX_IMAGE or img.height() > MAX_IMAGE:
        img = img.scaled(MAX_IMAGE, MAX_IMAGE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    if img.hasAlphaChannel():
        img.save(buf, "PNG")
        mime = "image/png"
    else:
        img.save(buf, "JPG", 90)
        mime = "image/jpeg"
    buf.close()
    return f"data:{mime};base64," + base64.b64encode(bytes(ba)).decode(), img.width(), img.height()


# --------------------------------------------------------------------------- overlay (PC side)

class Bridge(QObject):
    ops = Signal(str)       # raw JSON text from the tablet, parsed on the GUI thread
    hotkey = Signal(str)    # name of a pressed global shortcut
    tablet = Signal()       # a tablet connected


def smooth_path(pts):
    path = QPainterPath(pts[0])
    if len(pts) < 3:
        for q in pts[1:]:
            path.lineTo(q)
        return path
    for i in range(1, len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        path.quadTo(a, QPointF((a.x() + b.x()) / 2.0, (a.y() + b.y()) / 2.0))
    path.lineTo(pts[-1])
    return path


class Overlay(QWidget):
    def __init__(self):
        flags = (Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool |
                 Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setGeometry(QGuiApplication.primaryScreen().geometry())
        self.pages = {"screen": {}, "board": {}}   # id -> item, in z-order, per page
        self.page = "screen"
        self.bg = "white"
        self.live = set()     # ids being drawn / moved (painted live, not cached)
        self.snap = {}        # id -> geometry when a move/resize started
        self.lasers = {}      # id -> laser stroke (temporary)
        self.cache = None
        self.dirty = True     # cache needs a full rebuild
        self.pending = []     # finished items to paint on top of the cache
        self.region = QRegion()
        self.full = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(16)

    @property
    def items(self):
        return self.pages[self.page]

    # ---- dirty-region tracking (repainting only what changed keeps it fast)
    def _mark(self, r):
        if r is not None:
            self.region = self.region.united(r.toAlignedRect())

    def _rect_of(self, it, pts=None):
        W, H = self.width(), self.height()
        s = W / REF
        k = it.get("k")
        m = 4.0
        if k in STROKES:
            P = pts if pts is not None else it.get("pts", [])
            if not P:
                return None
            xs = [p[0] for p in P]
            ys = [p[1] for p in P]
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
            w = float(it.get("w", 6)) * s
            m += w * (1.7 if k in ("neon", "laser") else 0.7)
            if k == "farrow":
                m += max(14 * s, w * 3.2) + w
        elif k in SHAPES:
            x0, x1 = sorted((it["a"][0], it["b"][0]))
            y0, y1 = sorted((it["a"][1], it["b"][1]))
            w = float(it.get("w", 6)) * s
            m += w + (max(14 * s, w * 3.2) if k == "arrow" else 0)
        elif k == "text":
            x0, y0 = it["x"], it["y"]
            x1, y1 = x0 + it.get("tw", 0.1), y0 + it.get("th", 0.05)
            m += float(it.get("fs", 30)) * s * 0.5
        elif k == "img":
            x0, y0 = it["x"], it["y"]
            x1, y1 = x0 + it["w"], y0 + it["h"]
        else:
            return None
        return QRectF(x0 * W - m, y0 * H - m, (x1 - x0) * W + 2 * m, (y1 - y0) * H + 2 * m)

    # ---- incoming ops
    def on_ops(self, text):
        try:
            d = json.loads(text)
        except ValueError:
            return
        for op in (d.get("ops", []) if d.get("t") == "b" else [d]):
            try:
                self._apply(op)
            except Exception as e:  # never let one bad op kill the overlay
                print("op error:", e)
        self._flush_region()

    def _flush_region(self):
        if self.full:
            self.update()
        elif not self.region.isEmpty():
            self.update(self.region)
        self.region = QRegion()
        self.full = False

    def _prep(self, it):
        it["_c"] = QColor(it.get("c", "#ff3b30"))
        if it.get("k") == "img":
            q = QImage()
            src = it.get("src", "")
            try:
                q.loadFromData(base64.b64decode(src.split(",", 1)[1]))
            except Exception:
                pass
            it["_img"] = q
        if it.get("k") == "laser":
            it["_t0"] = time.monotonic()
            it["_end"] = None
        return it

    def _finish(self, it):
        """Item stopped being live: paint it onto the cache cheaply if it's on top."""
        ids = list(self.items.keys())
        if not self.dirty and self.cache is not None and ids and ids[-1] == it["id"]:
            self.pending.append(it)
        else:
            self.dirty = True

    def _ids(self, d):
        if "ids" in d:
            return list(d["ids"])
        return [d["id"]] if "id" in d else []

    def _apply(self, d):
        t = d.get("t")
        items = self.items
        if t == "add":
            it = self._prep(dict(d["item"]))
            if it["k"] == "laser":
                self.lasers[it["id"]] = it
            else:
                items[it["id"]] = it
                if d.get("done"):
                    self._finish(it)
                else:
                    self.live.add(it["id"])
            self._mark(self._rect_of(it))
        elif t == "pts":
            it = items.get(d["id"]) or self.lasers.get(d["id"])
            if it and d.get("pts"):
                seg = ([it["pts"][-1]] if it["pts"] else []) + d["pts"]
                it["pts"].extend(d["pts"])
                if it["k"] == "farrow":       # the arrow head moves with the end of the line
                    seg = it["pts"][-12:] if len(it["pts"]) > 12 else it["pts"]
                self._mark(self._rect_of(it, seg))
        elif t == "upd":
            it = items.get(d["id"])
            if it:
                self._mark(self._rect_of(it))
                for k, v in d.items():
                    if k not in ("t", "id"):
                        it[k] = v
                if "c" in d:
                    it["_c"] = QColor(d["c"])
                self._mark(self._rect_of(it))
                if d["id"] not in self.live:
                    self.dirty = True
        elif t == "grab":
            for i in self._ids(d):
                it = items.get(i)
                if it:
                    self.live.add(i)
                    self.snap[i] = geom_of(it)
                    self.dirty = True
                    self._mark(self._rect_of(it))
        elif t == "xf":
            f, tx, ty = float(d.get("f", 1)), float(d.get("tx", 0)), float(d.get("ty", 0))
            for i in self._ids(d):
                it = items.get(i)
                if not it:
                    continue
                g = self.snap.get(i) or geom_of(it)
                self.snap.setdefault(i, g)
                self._mark(self._rect_of(it))
                it.update(xf_geom(g, f, tx, ty))
                self._mark(self._rect_of(it))
        elif t == "end":
            for i in self._ids(d):
                self.snap.pop(i, None)
                if i in self.lasers:
                    self.lasers[i]["_end"] = time.monotonic()
                if i in self.live:
                    self.live.discard(i)
                    it = items.get(i)
                    if it:
                        if len(self._ids(d)) == 1 and "id" in d:
                            self._finish(it)
                        else:
                            self.dirty = True
                        self._mark(self._rect_of(it))
        elif t == "del":
            it = items.pop(d["id"], None)
            if it is not None:
                self.live.discard(d["id"])
                self.dirty = True
                self._mark(self._rect_of(it))
        elif t == "clear":
            items.clear(); self.live.clear(); self.lasers.clear(); self.snap.clear()
            self.dirty = True
            self.full = True
        elif t == "page":
            if d.get("p") in self.pages:
                self.page = d["p"]
            if d.get("bg") in BGC:
                self.bg = d["bg"]
            self.live.clear(); self.snap.clear()
            self.dirty = True
            self.full = True
        elif t == "sync":
            self.live.clear(); self.snap.clear()
            pages = d.get("pages", {})
            for name in self.pages:
                self.pages[name] = {}
                for it in pages.get(name, []):
                    it = self._prep(dict(it))
                    self.pages[name][it["id"]] = it
            if d.get("page") in self.pages:
                self.page = d["page"]
            if d.get("bg") in BGC:
                self.bg = d["bg"]
            self.dirty = True
            self.full = True

    def add_local(self, item):
        self._apply({"t": "add", "item": item, "done": True})
        self._flush_region()

    # ---- painting
    def _tick(self):
        if not self.lasers:
            return
        now = time.monotonic()
        for i, it in list(self.lasers.items()):
            self._mark(self._rect_of(it))
            end = it["_end"]
            if (end is not None and now - end > LASER_HOLD + LASER_FADE) or now - it["_t0"] > 60:
                del self.lasers[i]
        self._flush_region()

    def _new_cache(self):
        dpr = self.devicePixelRatioF()
        img = QImage(max(1, int(self.width() * dpr)), max(1, int(self.height() * dpr)),
                     QImage.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(Qt.transparent)
        return img

    def _painter(self, dev):
        p = QPainter(dev)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        return p

    def _update_cache(self):
        if self.dirty or self.cache is None:
            self.cache = self._new_cache()
            p = self._painter(self.cache)
            for i, it in self.items.items():
                if i not in self.live:
                    self._draw(p, it)
            p.end()
            self.dirty = False
            self.pending.clear()
        elif self.pending:
            p = self._painter(self.cache)
            for it in self.pending:
                if it["id"] in self.items and it["id"] not in self.live:
                    self._draw(p, it)
            p.end()
            self.pending.clear()

    def _paint_board(self, p):
        W, H = self.width(), self.height()
        p.fillRect(self.rect(), QColor(BGC.get(self.bg, "#ffffff")))
        if self.bg == "grid":
            s = W / REF
            step = 48 * s
            p.setPen(QPen(QColor("#dfe3e8"), max(1.0, s)))
            x = step
            while x < W:
                p.drawLine(QPointF(x, 0), QPointF(x, H)); x += step
            y = step
            while y < H:
                p.drawLine(QPointF(0, y), QPointF(W, y)); y += step

    def paintEvent(self, _e):
        self._update_cache()
        p = self._painter(self)
        if self.page == "board":
            self._paint_board(p)
        p.drawImage(QPointF(0, 0), self.cache)
        for i, it in self.items.items():
            if i in self.live:
                self._draw(p, it)
        now = time.monotonic()
        for it in self.lasers.values():
            fade = 1.0
            if it["_end"] is not None:
                age = now - it["_end"] - LASER_HOLD
                if age > 0:
                    fade = max(0.0, 1.0 - age / LASER_FADE)
            if fade > 0:
                self._draw(p, it, fade)
        p.end()

    def _pen(self, p, color, width, cap=Qt.RoundCap, alpha=1.0):
        c = QColor(color)
        c.setAlphaF(max(0.0, min(1.0, c.alphaF() * alpha)))
        p.setPen(QPen(c, max(0.5, width), Qt.SolidLine, cap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)

    def _head(self, p, tip, ux, uy, c, w, s, fade=1.0):
        hl = max(14 * s, w * 3.2)
        hw = hl * 0.55
        bx, by = tip.x() - ux * hl, tip.y() - uy * hl
        col = QColor(c)
        col.setAlphaF(col.alphaF() * fade)
        p.setPen(QPen(col, max(0.5, w * 0.5), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(col)
        p.drawPolygon(QPolygonF([tip, QPointF(bx - uy * hw, by + ux * hw), QPointF(bx + uy * hw, by - ux * hw)]))

    def _draw(self, p, it, fade=1.0):
        W, H = self.width(), self.height()
        s = W / REF
        k = it["k"]
        c = it["_c"]
        if k in STROKES:
            w = float(it.get("w", 6)) * s
            pts = [QPointF(x * W, y * H) for x, y in it["pts"]]
            if not pts:
                return
            path = smooth_path(pts)

            def stroke(color, width, cap=Qt.RoundCap, alpha=1.0):
                self._pen(p, color, width, cap, alpha * fade)
                if len(pts) == 1:
                    p.drawPoint(pts[0])
                else:
                    p.drawPath(path)

            if k in ("neon", "laser"):
                stroke(c, w * 3.0, alpha=0.12)
                stroke(c, w * 1.8, alpha=0.25)
                stroke(c, w)
                stroke(QColor(255, 255, 255), w * 0.4, alpha=0.85)
            elif k == "hl":
                stroke(c, w, Qt.SquareCap, 0.35)
            else:
                stroke(c, w)
            if k == "farrow" and len(pts) >= 2:
                last = pts[-1]
                hl = max(14 * s, w * 3.2)
                j = len(pts) - 2
                while j > 0 and math.hypot(pts[j].x() - last.x(), pts[j].y() - last.y()) < hl * 0.8:
                    j -= 1
                dx, dy = last.x() - pts[j].x(), last.y() - pts[j].y()
                d = math.hypot(dx, dy)
                if d >= 0.5:
                    ux, uy = dx / d, dy / d
                    tip = QPointF(last.x() + ux * w * 0.5, last.y() + uy * w * 0.5)
                    self._head(p, tip, ux, uy, c, w, s, fade)
        elif k in SHAPES:
            w = float(it.get("w", 6)) * s
            a = QPointF(it["a"][0] * W, it["a"][1] * H)
            b = QPointF(it["b"][0] * W, it["b"][1] * H)
            self._pen(p, c, w)
            if k == "line":
                p.drawLine(a, b)
            elif k == "arrow":
                dx, dy = b.x() - a.x(), b.y() - a.y()
                L = math.hypot(dx, dy)
                if L < 1:
                    return
                ux, uy = dx / L, dy / L
                hl = min(max(14 * s, w * 3.2), L)
                p.drawLine(a, QPointF(b.x() - ux * hl * 0.8, b.y() - uy * hl * 0.8))
                hw = hl * 0.55
                bx, by = b.x() - ux * hl, b.y() - uy * hl
                p.setPen(QPen(c, max(0.5, w * 0.5), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                p.setBrush(c)
                p.drawPolygon(QPolygonF([b, QPointF(bx - uy * hw, by + ux * hw), QPointF(bx + uy * hw, by - ux * hw)]))
            else:
                r = QRectF(a, b).normalized()
                if k == "rect":
                    p.drawRect(r)
                else:
                    p.drawEllipse(r)
        elif k == "text":
            fs = max(1.0, float(it.get("fs", 30)) * s)
            _path, family, bold = FONTS.get(it.get("f", "sans"), FONTS["sans"])
            f = QFont(family)
            f.setPixelSize(max(1, int(round(fs))))
            f.setBold(bold)
            p.setFont(f)
            p.setPen(QPen(c))
            x, y = it["x"] * W, it["y"] * H
            for i, line in enumerate(str(it.get("txt", "")).split("\n")):
                p.drawText(QPointF(x, y + fs * 0.92 + i * fs * 1.2), line)
        elif k == "img":
            q = it.get("_img")
            if q is not None and not q.isNull():
                p.drawImage(QRectF(it["x"] * W, it["y"] * H, it["w"] * W, it["h"] * H), q)


# --------------------------------------------------------------------------- server (network side)

class Server:
    def __init__(self, bridge, aspect):
        self.bridge = bridge
        self.aspect = aspect
        self.screen_w = 1920
        self.screen_h = 1080
        self.input_executor = InputExecutor()
        self.auth_manager = AuthManager(pin_hash=load_settings().get("remote_pin", ""))
        self.mouse_enabled = load_settings().get("mouse_enabled", True)
        self.pages = {"screen": {}, "board": {}}   # canonical items, so a reloaded tablet page gets them back
        self.page = "screen"
        self.bg = "white"
        self.snap = {}
        self.clients = set()
        self.exec = ThreadPoolExecutor(max_workers=2)
        self.tl = threading.local()
        self.loop = None
        self.grab_failed = False

    # ---- state mirror
    def apply(self, d):
        t = d.get("t")
        items = self.pages[self.page]
        ids = list(d["ids"]) if "ids" in d else ([d["id"]] if "id" in d else [])
        if t == "b":
            for op in d.get("ops", []):
                self.apply(op)
        elif t == "add":
            it = d.get("item", {})
            if it.get("k") != "laser":
                items[it["id"]] = it
        elif t == "pts":
            it = items.get(d.get("id"))
            if it and "pts" in it:
                it["pts"].extend(d.get("pts", []))
        elif t == "upd":
            it = items.get(d.get("id"))
            if it:
                for k, v in d.items():
                    if k not in ("t", "id"):
                        it[k] = v
        elif t == "grab":
            for i in ids:
                if i in items:
                    self.snap[i] = geom_of(items[i])
        elif t == "xf":
            f, tx, ty = float(d.get("f", 1)), float(d.get("tx", 0)), float(d.get("ty", 0))
            for i in ids:
                it = items.get(i)
                if it:
                    g = self.snap.setdefault(i, geom_of(it))
                    it.update(xf_geom(g, f, tx, ty))
        elif t == "end":
            for i in ids:
                self.snap.pop(i, None)
        elif t == "del":
            items.pop(d.get("id"), None)
        elif t == "clear":
            items.clear()
        elif t == "page":
            if d.get("p") in self.pages:
                self.page = d["p"]
            if d.get("bg") in BGC:
                self.bg = d["bg"]
        elif t == "sync":
            pages = d.get("pages", {})
            self.pages = {n: {it["id"]: it for it in pages.get(n, [])} for n in ("screen", "board")}
            if d.get("page") in self.pages:
                self.page = d["page"]
            if d.get("bg") in BGC:
                self.bg = d["bg"]

    # ---- screen preview
    def grab(self):
        from PIL import Image
        if not hasattr(self.tl, "sct"):
            import mss
            try:
                import mss.windows as mw
                mw.CAPTUREBLT = 0   # keeps our own ink layer out of the preview (no double ink on the tablet)
            except Exception:
                pass
            self.tl.sct = mss.mss()
            mons = self.tl.sct.monitors[1:]
            self.tl.mon = next((m for m in mons if m["left"] == 0 and m["top"] == 0), mons[0])
        shot = self.tl.sct.grab(self.tl.mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        if img.width > PREVIEW_WIDTH:
            img = img.resize((PREVIEW_WIDTH, round(img.height * PREVIEW_WIDTH / img.width)), Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=PREVIEW_QUALITY)
        return buf.getvalue()

    async def send_frame(self, ws):
        try:
            data = await self.loop.run_in_executor(self.exec, self.grab)
            if not ws.closed:
                await ws.send_bytes(data)
                cx, cy = get_normalized_cursor(self.screen_w, self.screen_h)
                await ws.send_str(json.dumps({"t": "cur", "x": cx, "y": cy}))
        except Exception as e:
            if not self.grab_failed:
                print("Screen preview unavailable:", e)
                self.grab_failed = True

    def broadcast_mouse_state(self, enabled: bool):
        if self.loop:
            asyncio.run_coroutine_threadsafe(
                self.broadcast(json.dumps({"t": "mouse_state", "enabled": enabled})),
                self.loop
            )

    # ---- images from the web / clipboard
    async def download_image(self, url):
        if url.startswith("data:image/"):
            data = base64.b64decode(url.split(",", 1)[1])
        else:
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}) as s:
                async with s.get(url) as r:
                    if r.status != 200:
                        raise ValueError(f"HTTP {r.status}")
                    data = await r.content.read(25 * 1024 * 1024 + 1)
                    if len(data) > 25 * 1024 * 1024:
                        raise ValueError("image is too large")
        return await self.loop.run_in_executor(self.exec, encode_image_bytes, data)

    async def fetch_for_tablet(self, ws, url):
        try:
            src, _w, _h = await self.download_image(url)
            await ws.send_str(json.dumps({"t": "cmd", "c": "img", "src": src}))
        except Exception as e:
            print("Couldn't load image:", e)
            await ws.send_str(json.dumps({"t": "toast", "msg": "Couldn't load that image. Try saving it to Photos and use Image."}))

    def add_image(self, src, iw, ih):
        """Called from the GUI thread with an image from the PC clipboard."""
        if self.loop:
            self.loop.call_soon_threadsafe(self._add_image, src, iw, ih)

    def fetch_for_pc(self, url):
        async def run():
            try:
                src, iw, ih = await self.download_image(url)
                self._add_image(src, iw, ih)
            except Exception as e:
                print("Couldn't load image from clipboard link:", e)
        if self.loop:
            self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(run()))

    def _add_image(self, src, iw, ih):
        if self.clients:
            # the tablet adds it (so it's selected, resizable and undoable)
            asyncio.ensure_future(self.broadcast({"t": "cmd", "c": "img", "src": src}))
            return
        w = 0.3
        h = w * self.aspect * ih / max(1, iw)
        if h > 0.6:
            w *= 0.6 / h
            h = 0.6
        item = {"id": uuid.uuid4().hex[:8], "k": "img", "src": src, "c": "#000",
                "x": (1 - w) / 2, "y": (1 - h) / 2, "w": w, "h": h}
        op = {"t": "add", "item": item, "done": True}
        self.apply(op)
        self.bridge.ops.emit(json.dumps(op))

    def handle_mouse_msg(self, d: dict, is_authorized: bool) -> bool:
        if not self.mouse_enabled or not is_authorized:
            return False
        action = d.get("action")
        if not action:
            return False
        if action == "move_rel":
            dx = float(d.get("dx", 0.0)) * self.screen_w
            dy = float(d.get("dy", 0.0)) * self.screen_h
            self.input_executor.submit_mouse("move_rel", dx=dx, dy=dy)
            return True
        elif action == "move_abs":
            x = float(d.get("x", 0.0))
            y = float(d.get("y", 0.0))
            self.input_executor.submit_mouse("move_abs", x=x, y=y)
            return True
        elif action in ("click", "down", "up"):
            btn = d.get("btn", "left")
            self.input_executor.submit_mouse(action, btn=btn)
            return True
        elif action == "scroll":
            dy = int(d.get("dy", 0))
            self.input_executor.submit_mouse("scroll", dy=dy)
            return True
        return False

    def slide(self, direction):
        if direction in ("next", "prev", "enter", "space"):
            self.loop.run_in_executor(self.exec, trigger_slide, direction)

    # ---- http / websocket
    async def index(self, _request):
        return web.Response(text=PAGE, content_type="text/html",
                            headers={"Cache-Control": "no-store"})

    async def manifest(self, _request):
        return web.json_response({"name": APP, "short_name": APP, "start_url": "/", "display": "fullscreen",
                                  "background_color": "#000000", "theme_color": "#1c1c1e",
                                  "orientation": "any"})

    async def font(self, request):
        f = FONTS.get(request.match_info["name"])
        if not f or not f[0]:
            raise web.HTTPNotFound()
        return web.FileResponse(f[0], headers={"Content-Type": "font/ttf",
                                               "Cache-Control": "max-age=86400"})

    async def ws_handler(self, request):
        ws = web.WebSocketResponse(max_msg_size=0, heartbeat=15, compress=False)
        await ws.prepare(request)
        sock = request.transport.get_extra_info("socket") if request.transport else None
        if sock is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass

        remote = is_remote_request(request)
        client_ip = get_client_ip(request)
        is_authorized = not remote

        self.clients.add(ws)
        print(f"Tablet connected ({'Remote' if remote else 'LAN'} from {client_ip})")
        self.bridge.tablet.emit()
        await ws.send_str(json.dumps({
            "t": "hello", "aspect": self.aspect, "page": self.page, "bg": self.bg,
            "mouse_enabled": self.mouse_enabled,
            "auth_required": remote and bool(self.auth_manager.pin_hash),
            "screen_w": self.screen_w, "screen_h": self.screen_h,
            "pages": {n: list(v.values()) for n, v in self.pages.items()}}))
        try:
            async for m in ws:
                if m.type != web.WSMsgType.TEXT:
                    continue
                if m.data == '{"t":"f"}':
                    if not remote or is_authorized:
                        asyncio.ensure_future(self.send_frame(ws))
                    continue
                try:
                    d = json.loads(m.data)
                except ValueError:
                    continue

                t = d.get("t")
                if t == "auth":
                    pin = d.get("pin")
                    token = d.get("token")
                    if token and self.auth_manager.validate_token(token):
                        is_authorized = True
                        await ws.send_str(json.dumps({"t": "auth_ok", "token": token}))
                    elif pin:
                        ok, res = self.auth_manager.validate_pin(str(pin), client_ip)
                        if ok:
                            is_authorized = True
                            await ws.send_str(json.dumps({"t": "auth_ok", "token": res}))
                        else:
                            await ws.send_str(json.dumps({"t": "auth_err", "msg": res}))
                    else:
                        await ws.send_str(json.dumps({"t": "auth_err", "msg": "Invalid auth request"}))
                    continue
                if t == "mouse":
                    self.handle_mouse_msg(d, is_authorized)
                    continue
                if remote and not is_authorized:
                    continue

                if t == "slide":
                    self.slide(d.get("dir", "next"))
                    continue
                if t == "fetch":
                    url = str(d.get("url", ""))
                    if url.startswith(("http://", "https://", "data:image/")):
                        asyncio.ensure_future(self.fetch_for_tablet(ws, url))
                    continue
                self.apply(d)
                self.bridge.ops.emit(m.data)
        finally:
            self.clients.discard(ws)
            print("Tablet disconnected")
        return ws

    async def broadcast(self, obj):
        text = json.dumps(obj)
        for ws in list(self.clients):
            try:
                await ws.send_str(text)
            except Exception:
                pass

    # ---- commands from PC shortcuts / tray
    def command(self, name):
        if self.loop:
            self.loop.call_soon_threadsafe(self._command, name)

    def _command(self, name):
        if self.clients:
            # the tablet does the work, so it stays undoable and in sync
            asyncio.ensure_future(self.broadcast({"t": "cmd", "c": name}))
            return
        op = None
        if name == "clear":
            op = {"t": "clear"}
        elif name == "board":
            op = {"t": "page", "p": "board" if self.page == "screen" else "screen", "bg": self.bg}
        elif name == "bg":
            op = {"t": "page", "p": "board", "bg": BGS[(BGS.index(self.bg) + 1) % len(BGS)]}
        if op:
            self.apply(op)
            self.bridge.ops.emit(json.dumps(op))

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_get("/", self.index)
        app.router.add_get("/manifest.json", self.manifest)
        app.router.add_get("/font/{name}", self.font)
        app.router.add_get("/ws", self.ws_handler)
        runner = web.AppRunner(app, access_log=None)
        self.loop.run_until_complete(runner.setup())
        try:
            self.loop.run_until_complete(web.TCPSite(runner, "0.0.0.0", PORT).start())
        except OSError as e:
            print(f"\n!! Could not open port {PORT}: {e}\n   Is {APP} already running?")
            os._exit(1)
        self.loop.run_forever()


# --------------------------------------------------------------------------- global shortcuts (Windows)

def start_hotkeys(emit):
    """Registers Ctrl+Alt+<key> shortcuts. Returns the keys that could not be registered."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes
    failed = []
    ready = threading.Event()

    def run():
        user32 = ctypes.windll.user32
        MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, WM_HOTKEY = 0x0001, 0x0002, 0x4000, 0x0312
        for i, (key, _name, _label) in enumerate(HOTKEYS, start=1):
            if not user32.RegisterHotKey(None, i, MOD_ALT | MOD_CONTROL | MOD_NOREPEAT, ord(key)):
                failed.append(key)
        ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and 1 <= msg.wParam <= len(HOTKEYS):
                emit(HOTKEYS[msg.wParam - 1][1])

    threading.Thread(target=run, daemon=True).start()
    ready.wait(2)
    return failed


def trigger_slide(direction: str):
    """Trigger slide advance / back.
    Works with PowerPoint (full screen or window) and any active presentation app.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW("screenClass", None)
        if not hwnd:
            hwnd = user32.FindWindowW("PPTFrameClass", None)
        if hwnd:
            try:
                user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
            time.sleep(0.02)
        # Page Down (0x22) for next, Page Up (0x21) for previous, Return (0x0D) for enter
        if direction == "enter":
            vk = 0x0D
        elif direction == "space":
            vk = 0x20
        elif direction == "prev":
            vk = 0x21
        else:
            vk = 0x22
        KEYEVENTF_KEYUP = 0x0002
        KEYEVENTF_EXTENDEDKEY = 0x0001
        user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
        time.sleep(0.02)
        user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
    except Exception as e:
        print(f"Slide trigger error: {e}")


# --------------------------------------------------------------------------- startup

def all_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def tray_icon():
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#ff3b30"))
    p.setPen(Qt.NoPen)
    p.drawEllipse(6, 6, 52, 52)
    p.setPen(QPen(QColor("white"), 6, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(20, 44, 44, 20)
    p.end()
    return QIcon(pm)


def qr_pixmap(text, module=7):
    """QR code for text as a crisp pixmap, or None if the qrcode package isn't installed."""
    try:
        import qrcode
    except ImportError:
        return None
    qr = qrcode.QRCode(border=3, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    m = qr.get_matrix()
    dpr = QGuiApplication.primaryScreen().devicePixelRatio()
    s = max(1, round(module * dpr))          # whole device pixels per module keeps it sharp
    pm = QPixmap(len(m) * s, len(m) * s)
    pm.fill(Qt.white)
    p = QPainter(pm)
    p.setPen(Qt.NoPen)
    p.setBrush(Qt.black)
    for y, row in enumerate(m):
        for x, dark in enumerate(row):
            if dark:
                p.drawRect(x * s, y * s, s, s)
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def get_cloudflared_install_path():
    """Returns a writable path for cloudflared.exe."""
    rdir = resource_dir()
    try:
        test_file = os.path.join(rdir, ".write_test")
        with open(test_file, "w") as f:
            f.write("1")
        os.remove(test_file)
        return os.path.join(rdir, "cloudflared.exe")
    except Exception:
        target_dir = os.path.expandvars(r"%LOCALAPPDATA%\InkCast")
        os.makedirs(target_dir, exist_ok=True)
        return os.path.join(target_dir, "cloudflared.exe")


class TunnelManager(QObject):
    started = Signal(str)        # remote_url
    stopped = Signal()
    error = Signal(str)
    status_changed = Signal(str) # "starting", "downloading", "running", "stopped"

    def __init__(self, port):
        super().__init__()
        self.port = port
        self.proc = None
        self.remote_url = None
        self.status = "stopped"

    def find_cloudflared(self):
        import shutil
        p = shutil.which("cloudflared")
        if p:
            return p
        beside = os.path.join(resource_dir(), "cloudflared.exe")
        if os.path.isfile(beside):
            return beside
        for candidate in [
            os.path.expandvars(r"%LOCALAPPDATA%\InkCast\cloudflared.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\cloudflared\cloudflared.exe"),
            os.path.expandvars(r"%ProgramFiles%\cloudflared\cloudflared.exe"),
        ]:
            if os.path.isfile(candidate):
                return candidate
        return None

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        self.status = "starting"
        self.status_changed.emit("starting")

        def run():
            exe = self.find_cloudflared()
            if not exe:
                self.status = "downloading"
                self.status_changed.emit("downloading")
                target_exe = get_cloudflared_install_path()
                try:
                    import urllib.request
                    url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
                    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=60) as resp, open(target_exe, "wb") as out:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            out.write(chunk)
                    exe = target_exe
                except Exception as e:
                    self.status = "stopped"
                    self.status_changed.emit("stopped")
                    self.error.emit(f"Could not download cloudflared: {e}")
                    return

            try:
                import re, subprocess
                creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                self.proc = subprocess.Popen(
                    [exe, "tunnel", "--url", f"http://127.0.0.1:{self.port}"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    creationflags=creation_flags
                )
                found = False
                for line in self.proc.stdout:
                    m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
                    if m:
                        self.remote_url = m.group(0)
                        self.status = "running"
                        self.status_changed.emit("running")
                        self.started.emit(self.remote_url)
                        found = True
                        break
                if not found and self.proc.poll() is not None:
                    self.status = "stopped"
                    self.status_changed.emit("stopped")
                    self.error.emit("Tunnel process exited without URL")
            except Exception as e:
                self.status = "stopped"
                self.status_changed.emit("stopped")
                self.error.emit(str(e))

        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None
        self.remote_url = None
        self.status = "stopped"
        self.stopped.emit()
        self.status_changed.emit("stopped")


class ConnectWindow(QWidget):
    """Shows the tablet address as a QR code and as text to type."""
    def __init__(self, url, tunnel_mgr=None):
        super().__init__(None, Qt.Window | Qt.WindowStaysOnTopHint)
        self.local_url = url
        self.remote_url = None
        self.current_url = url
        self.tunnel_mgr = tunnel_mgr
        self.setWindowTitle(f"{APP} - connect your tablet")
        self.setWindowIcon(tray_icon())
        self.setStyleSheet("QWidget{background:#1c1c1e;color:#f2f2f7;font-family:'Segoe UI'}"
                           "#qr{background:#fff;border-radius:12px;padding:10px}"
                           "#url{font-size:20px;font-weight:700;color:#fff}"
                           "#mut{color:#8e8e93;font-size:13px}"
                           "QPushButton{background:#2c2c2e;color:#f2f2f7;border:1px solid #3a3a3c;"
                           "border-radius:8px;padding:6px 14px;font-size:13px;font-weight:600}"
                           "QPushButton:hover{background:#3a3a3c}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 22, 28, 22)
        lay.setSpacing(10)

        def label(text, name=None, size=None):
            w = QLabel(text)
            w.setAlignment(Qt.AlignCenter)
            w.setWordWrap(True)
            if name:
                w.setObjectName(name)
            if size:
                w.setStyleSheet(f"font-size:{size}px;font-weight:600")
            lay.addWidget(w)
            return w

        pm = qr_pixmap(url)
        self.title_label = label(
            "Scan with your tablet's camera" if pm is not None else "Type this into the tablet's browser:",
            size=17,
        )
        if pm is not None:
            self.qr_label = QLabel()
            self.qr_label.setObjectName("qr")
            self.qr_label.setPixmap(pm)
            self.qr_label.setAlignment(Qt.AlignCenter)
            lay.addWidget(self.qr_label, 0, Qt.AlignCenter)

        self.sub_label = label("or type this into the tablet's browser:", "mut")
        self.url_label = label(url, "url")
        self.url_label.setWordWrap(False)
        self.url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        btn_box = QWidget()
        btn_lay = QHBoxLayout(btn_box)
        btn_lay.setContentsMargins(0, 4, 0, 4)
        btn_lay.setSpacing(8)

        self.copy_btn = QPushButton("Copy Link")
        self.copy_btn.clicked.connect(self.copy_current_url)
        btn_lay.addWidget(self.copy_btn)

        self.toggle_btn = QPushButton("Start Remote Tunnel")
        self.toggle_btn.clicked.connect(self.toggle_tunnel_mode)
        btn_lay.addWidget(self.toggle_btn)

        lay.addWidget(btn_box)

        self.hint_label = label(
            "The tablet must be on the same WiFi as this PC (or use Remote Tunnel).\n"
            "This window closes when your tablet connects.\n"
            "Open it again from the tray icon.", "mut")
        self.setMinimumWidth(400)
        self.adjustSize()
        self.setFixedSize(self.size())

        if self.tunnel_mgr:
            self.tunnel_mgr.started.connect(self.on_tunnel_started)
            self.tunnel_mgr.stopped.connect(self.on_tunnel_stopped)
            self.tunnel_mgr.status_changed.connect(self.on_tunnel_status)

    def copy_current_url(self):
        QGuiApplication.clipboard().setText(self.current_url)
        self.copy_btn.setText("Copied!")
        QTimer.singleShot(1500, lambda: self.copy_btn.setText("Copy Link"))

    def toggle_tunnel_mode(self):
        if not self.tunnel_mgr:
            return
        if self.remote_url:
            if self.current_url == self.remote_url:
                self.show_url(self.local_url, is_remote=False)
            else:
                self.show_url(self.remote_url, is_remote=True)
        else:
            self.toggle_btn.setEnabled(False)
            self.toggle_btn.setText("Starting Tunnel...")
            self.tunnel_mgr.start()

    def show_url(self, target_url, is_remote=False):
        self.current_url = target_url
        self.url_label.setText(target_url)
        pm = qr_pixmap(target_url)
        if pm is not None and self.qr_label is not None:
            self.qr_label.setPixmap(pm)
        if is_remote:
            self.title_label.setText("Scan for Remote Access (Anywhere)")
            self.sub_label.setText("Works over mobile data or any Wi-Fi network.")
            self.toggle_btn.setText("Switch to Local Wi-Fi QR")
            self.toggle_btn.setEnabled(True)
        else:
            self.title_label.setText("Scan with your tablet's camera")
            self.sub_label.setText("The tablet must be on the same WiFi as this PC.")
            if self.remote_url:
                self.toggle_btn.setText("Switch to Cloudflare Remote QR")
                self.toggle_btn.setEnabled(True)
            else:
                self.toggle_btn.setText("Start Remote Tunnel")
                self.toggle_btn.setEnabled(True)

    def on_tunnel_started(self, remote_url):
        self.remote_url = remote_url
        self.show_url(remote_url, is_remote=True)

    def on_tunnel_stopped(self):
        self.remote_url = None
        self.show_url(self.local_url, is_remote=False)

    def on_tunnel_status(self, status):
        if status == "starting":
            self.toggle_btn.setEnabled(False)
            self.toggle_btn.setText("Starting Tunnel...")
        elif status == "downloading":
            self.toggle_btn.setEnabled(False)
            self.toggle_btn.setText("Downloading cloudflared...")
        elif status == "stopped":
            if not self.remote_url:
                self.toggle_btn.setEnabled(True)
                self.toggle_btn.setText("Start Remote Tunnel")

    def show_centered(self):
        self.move(QGuiApplication.primaryScreen().availableGeometry().center() - self.rect().center())
        self.show()
        self.raise_()
        self.activateWindow()


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)   # Ctrl+C in the console quits
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    bridge = Bridge()
    overlay = Overlay()
    bridge.ops.connect(overlay.on_ops)
    overlay.show()

    g = overlay.geometry()
    server = Server(bridge, g.width() / max(1, g.height()))
    server.screen_w = max(1, g.width())
    server.screen_h = max(1, g.height())
    threading.Thread(target=server.run, daemon=True).start()

    url = f"http://{local_ip()}:{PORT}"
    settings = load_settings()

    tray = QSystemTrayIcon(tray_icon())
    tray.setToolTip(f"{APP} - open {url} on your tablet")

    def paste_from_pc():
        cb = QApplication.clipboard()
        img = cb.image()
        md = cb.mimeData()
        if img.isNull() and md is not None and md.hasUrls():
            for u in md.urls():
                if u.isLocalFile():
                    q = QImage(u.toLocalFile())
                    if not q.isNull():
                        img = q
                        break
        if not img.isNull():
            src, iw, ih = qimage_to_dataurl(img)
            server.add_image(src, iw, ih)
            return
        txt = (cb.text() or "").strip()
        if txt.startswith(("http://", "https://")):
            server.fetch_for_pc(txt)
            return
        tray.showMessage(APP, "No image in the clipboard. Right-click an image and choose Copy image first.")

    tunnel_mgr = TunnelManager(PORT)
    connect_win = ConnectWindow(url, tunnel_mgr)
    bridge.tablet.connect(connect_win.hide)

    menu = QMenu()
    info = QAction(f"Connect a tablet (QR code)...\t{url}")
    info.triggered.connect(connect_win.show_centered)
    tunnel_start = QAction("Start Cloudflare Tunnel...")
    tunnel_copy = QAction("Copy Cloudflare Link")
    tunnel_stop = QAction("Stop Cloudflare Tunnel")
    tunnel_copy.setVisible(False)
    tunnel_stop.setVisible(False)

    def on_start_tunnel():
        tunnel_start.setEnabled(False)
        tunnel_start.setText("Starting Cloudflare Tunnel...")
        tunnel_mgr.start()

    def on_copy_tunnel():
        if tunnel_mgr.remote_url:
            QGuiApplication.clipboard().setText(tunnel_mgr.remote_url)
            tray.showMessage(APP, "Copied Cloudflare remote link to clipboard!")

    tunnel_start.triggered.connect(on_start_tunnel)
    tunnel_stop.triggered.connect(tunnel_mgr.stop)
    tunnel_copy.triggered.connect(on_copy_tunnel)

    def on_tunnel_status(status):
        if status == "starting":
            tunnel_start.setEnabled(False)
            tunnel_start.setText("Starting Cloudflare Tunnel...")
        elif status == "downloading":
            tunnel_start.setEnabled(False)
            tunnel_start.setText("Downloading cloudflared (~20MB)...")
        elif status == "running":
            tunnel_start.setVisible(False)
            tunnel_copy.setText(f"Copy Cloudflare Link\t{tunnel_mgr.remote_url}")
            tunnel_copy.setVisible(True)
            tunnel_stop.setVisible(True)
        elif status == "stopped":
            tunnel_start.setEnabled(True)
            tunnel_start.setText("Start Cloudflare Tunnel...")
            tunnel_start.setVisible(True)
            tunnel_copy.setVisible(False)
            tunnel_stop.setVisible(False)

    def on_tunnel_started(remote_url):
        tray.showMessage(APP, f"Cloudflare Tunnel active!\nLink copied to clipboard:\n{remote_url}")
        QGuiApplication.clipboard().setText(remote_url)
        tray.setToolTip(f"{APP}\nWiFi: {url}\nRemote: {remote_url}")
        if not settings.get("remote_pin"):
            QTimer.singleShot(2000, lambda: tray.showMessage(APP, "Warning: No remote PIN set! Remote access is locked until you set a PIN in tray."))

    def on_tunnel_error(err):
        tray.showMessage(APP, f"Cloudflare Tunnel: {err}")

    tunnel_mgr.status_changed.connect(on_tunnel_status)
    tunnel_mgr.started.connect(on_tunnel_started)
    tunnel_mgr.error.connect(on_tunnel_error)
    app.aboutToQuit.connect(tunnel_mgr.stop)

    clear = QAction("Clear ink\tCtrl+Alt+C")
    clear.triggered.connect(lambda: server.command("clear"))
    board = QAction("Whiteboard on / off\tCtrl+Alt+W")
    board.triggered.connect(lambda: server.command("board"))
    paste = QAction("Paste image\tCtrl+Alt+V")
    paste.triggered.connect(paste_from_pc)
    toggle = QAction("Hide ink\tCtrl+Alt+H")

    def toggle_ink():
        overlay.setVisible(not overlay.isVisible())
        toggle.setText(("Hide ink" if overlay.isVisible() else "Show ink") + "\tCtrl+Alt+H")

    toggle.triggered.connect(toggle_ink)
    cam_bat = os.path.join(resource_dir(), "camera", "InkCast Camera.bat")
    camera = QAction("Phone camera (USB)...")

    def start_camera():
        try:
            os.startfile(cam_bat)   # opens the launcher in its own window
        except (AttributeError, OSError) as e:
            tray.showMessage(APP, f"Couldn't start the phone camera: {e}")

    camera.triggered.connect(start_camera)
    camera.setVisible(os.path.isfile(cam_bat))
    mouse_toggle = QAction("Allow remote mouse", checkable=True)
    mouse_toggle.setChecked(settings.get("mouse_enabled", True))
    pin_action = QAction("Remote access PIN…")

    def on_mouse_toggle():
        enabled = mouse_toggle.isChecked()
        settings["mouse_enabled"] = enabled
        save_settings(settings)
        server.mouse_enabled = enabled
        server.broadcast_mouse_state(enabled)

    def on_set_pin():
        pin, ok = QInputDialog.getText(None, "Remote Access PIN", 
                                       "Enter a 6-8 digit PIN for remote access\n(leave blank to lock remote access):")
        if ok:
            pin = pin.strip()
            settings["remote_pin"] = hash_pin(pin)
            save_settings(settings)
            server.auth_manager.set_pin(pin)
            if pin:
                tray.showMessage(APP, "Remote PIN updated. Active remote sessions revoked.")
            else:
                tray.showMessage(APP, "Remote PIN cleared. Remote access is now locked.")

    mouse_toggle.triggered.connect(on_mouse_toggle)
    pin_action.triggered.connect(on_set_pin)

    quit_ = QAction(f"Quit {APP}")
    quit_.triggered.connect(app.quit)
    menu.addAction(info)
    menu.addSeparator()
    menu.addAction(tunnel_start)
    menu.addAction(tunnel_copy)
    menu.addAction(tunnel_stop)
    menu.addSeparator()
    for a in (paste, clear, board, toggle):
        menu.addAction(a)
    menu.addSeparator()
    menu.addAction(mouse_toggle)
    menu.addAction(pin_action)
    menu.addSeparator()
    menu.addAction(camera)
    menu.addSeparator()
    menu.addAction(quit_)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: connect_win.show_centered()
                           if reason == QSystemTrayIcon.Trigger else None)
    tray.show()
    connect_win.show_centered()

    def on_hotkey(name):
        if name == "hide":
            toggle_ink()
        elif name == "paste":
            paste_from_pc()
        else:
            server.command(name)

    bridge.hotkey.connect(on_hotkey)
    failed = start_hotkeys(bridge.hotkey.emit)

    print("=" * 62)
    print(f"  {APP} is running")
    print()
    print("  On your tablet (iPad or Android), scan the QR code in the InkCast")
    print("  window, or open the browser and go to:")
    print(f"     {url}")
    others = [ip for ip in all_ips() if ip not in url]
    if others:
        print("  If that doesn't open, try:", ", ".join(f"http://{ip}:{PORT}" for ip in others))
    print()
    print("  PC shortcuts (Ctrl + Alt + key):")
    for key, _name, label in HOTKEYS:
        mark = "   (taken by another app)" if key in failed else ""
        print(f"     Ctrl+Alt+{key}   {label}{mark}")
    print()
    print("  Tablet and PC must be on the same WiFi / network.")
    print("  (Or use Cloudflare Tunnel to connect remotely from anywhere: 'Start Cloudflare Tunnel.bat')")
    print("  If Windows asks about the firewall, click Allow.")
    print("  Close this window or press Ctrl+C to quit.")
    print("=" * 62)
    sys.exit(app.exec())


# --------------------------------------------------------------------------- tablet page

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="theme-color" content="#1c1c1e">
<link rel="manifest" href="/manifest.json">
<title>InkCast</title>
<style>
@font-face{font-family:InkSans;src:url(/font/sans);font-weight:700;font-display:swap}
@font-face{font-family:InkHand;src:url(/font/hand);font-weight:700;font-display:swap}
@font-face{font-family:InkSerif;src:url(/font/serif);font-weight:700;font-display:swap}
:root{--bar:#1c1c1e;--btn:#2c2c2e;--on:#0a84ff;--fg:#f2f2f7;--mut:#8e8e93}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;height:100%;background:#000;color:var(--fg);overflow:hidden;overscroll-behavior:none;
  touch-action:none;-webkit-user-select:none;user-select:none;-webkit-touch-callout:none;
  font:13px -apple-system,BlinkMacSystemFont,Roboto,"Segoe UI",sans-serif}
#side{position:fixed;left:0;top:0;bottom:0;width:62px;background:var(--bar);display:flex;flex-direction:column;
  align-items:center;gap:2px;padding:6px 0;overflow-y:auto;z-index:2;padding-left:env(safe-area-inset-left);touch-action:pan-y}
#top{position:fixed;left:62px;right:0;top:0;height:54px;background:var(--bar);display:flex;align-items:center;gap:4px;
  padding:0 8px;overflow-x:auto;z-index:2;touch-action:pan-x;scrollbar-width:none}
#top::-webkit-scrollbar{display:none}
.tb{width:44px;min-height:46px;border-radius:10px;border:0;background:transparent;color:var(--fg);display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:1px;font-size:9px;padding:3px 0;flex:none;font-family:inherit}
.tb svg{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.tb.on{background:var(--on);color:#fff}
.tb:disabled{opacity:.3}
.sep{width:36px;height:1px;background:#3a3a3c;margin:3px 0;flex:none}
.vsep{width:1px;height:30px;background:#3a3a3c;margin:0 3px;flex:none}
.sw{width:26px;height:26px;border-radius:50%;flex:none;border:2px solid #48484a;padding:0;position:relative;overflow:hidden}
.sw.on{outline:3px solid #fff;outline-offset:2px}
#pal{display:contents}
#addc{width:26px;height:26px;border-radius:50%;border:2px dashed #636366;color:var(--fg);flex:none;position:relative;overflow:hidden;
  display:flex;align-items:center;justify-content:center;font-size:20px;line-height:1}
#addc input,.bgo input{position:absolute;inset:-8px;width:calc(100% + 16px);height:calc(100% + 16px);opacity:0;border:0;padding:0}
#bgmenu,#palmenu{position:fixed;z-index:7;display:none;min-width:170px;background:#2c2c2e;border:1px solid #3a3a3c;border-radius:12px;
  padding:6px;box-shadow:0 10px 30px rgba(0,0,0,.5)}
#bgmenu.show,#palmenu.show{display:block}
.bgo{display:flex;align-items:center;gap:10px;width:100%;height:42px;border:0;border-radius:8px;background:transparent;
  color:var(--fg);font:inherit;font-size:15px;padding:0 10px;text-align:left;position:relative;overflow:hidden}
.bgo.on{background:var(--on);color:#fff}
.bgo:disabled{opacity:.35}
#palDel{color:#ff453a}
.bgo .bgSw{width:22px;height:22px;border-radius:6px;border:2px solid #636366;flex:none}
#custom{width:26px;height:26px;border-radius:50%;border:2px solid #48484a;padding:0;overflow:hidden;flex:none;position:relative;
  background:conic-gradient(red,yellow,lime,cyan,blue,magenta,red)}
#custom.on{outline:3px solid #fff;outline-offset:2px}
#custom input{position:absolute;inset:-8px;width:50px;height:50px;opacity:0;border:0;padding:0}
#size{width:84px;flex:none;accent-color:var(--on)}
#dot{width:38px;height:44px;flex:none;display:flex;align-items:center;justify-content:center}
#fonts{display:flex;gap:2px;flex:none}
.fb{width:34px;height:38px;border-radius:9px;border:0;background:var(--btn);color:var(--fg);font-size:18px;flex:none;padding:0}
.fb.on{background:var(--on);color:#fff}
#status{margin-left:auto;display:flex;align-items:center;gap:6px;color:var(--mut);white-space:nowrap;flex:none;padding:0 4px 0 8px;font-size:12px}
#led{width:9px;height:9px;border-radius:50%;background:#ff453a}
#led.ok{background:#30d158}
#stage{position:fixed;background:#1a1a1a;overflow:hidden}
#bg,#cv{position:absolute;left:0;top:0;width:100%;height:100%}
#bg{display:block;visibility:hidden}
#ed{position:absolute;display:none;z-index:1;margin:0;padding:0 2px;border:1.5px dashed #0a84ff;border-radius:4px;outline:none;
  background:rgba(10,132,255,.08);resize:none;overflow:hidden;white-space:pre;line-height:1.2;font-weight:700;
  -webkit-user-select:text;user-select:text;touch-action:auto;-webkit-touch-callout:default}
#hint{position:fixed;left:62px;right:0;bottom:14px;text-align:center;color:var(--mut);pointer-events:none;font-size:14px;
  text-shadow:0 1px 3px #000}
#toast{position:fixed;left:50%;bottom:44px;transform:translateX(-50%);background:#2c2c2e;color:#fff;padding:10px 16px;border-radius:12px;
  font-size:14px;max-width:80vw;text-align:center;box-shadow:0 6px 24px rgba(0,0,0,.4);opacity:0;transition:opacity .2s;pointer-events:none;z-index:5}
#toast.show{opacity:1}
#pastebox{position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);z-index:6;display:none;width:min(420px,80vw);
  background:#1c1c1e;border:1px solid #3a3a3c;border-radius:16px;padding:18px;box-shadow:0 10px 40px rgba(0,0,0,.5)}
#pastebox.show{display:block}
#pastearea{min-height:110px;border:2px dashed #0a84ff;border-radius:12px;display:flex;align-items:center;justify-content:center;
  text-align:center;padding:12px;color:#c7c7cc;font-size:15px;outline:none;-webkit-user-select:text;user-select:text;touch-action:auto;caret-color:transparent;-webkit-touch-callout:default}
#pasteclose{margin-top:12px;width:100%;height:40px;border-radius:10px;border:0;background:var(--btn);color:var(--fg);font-size:15px}
input[type=file]{display:none}
#mouse-cursor{display:none;position:absolute;left:0;top:0;pointer-events:none;z-index:90;transform:translate(-1px,-1px);will-change:left,top}
#mouse-cursor svg{filter:drop-shadow(0 1.5px 3.5px rgba(0,0,0,0.8));display:block}
.click-ripple{position:absolute;width:26px;height:26px;margin-left:-13px;margin-top:-13px;border-radius:50%;pointer-events:none;z-index:92;animation:click-ripple-anim .35s cubic-bezier(0.1,0.8,0.3,1) forwards}
.ripple-left{border:2.5px solid #0a84ff;background:rgba(10,132,255,0.35)}
.ripple-right{border:2.5px solid #ff9f0a;background:rgba(255,159,10,0.35)}
@keyframes click-ripple-anim{0%{transform:scale(0.2);opacity:1}100%{transform:scale(1.6);opacity:0}}
#tp-topbar{position:fixed;top:10px;left:50%;transform:translateX(-50%);z-index:95;display:flex;align-items:center;gap:3px;background:rgba(28,28,30,0.88);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);padding:3px 6px;border-radius:20px;border:1px solid rgba(255,255,255,0.18);box-shadow:0 6px 24px rgba(0,0,0,0.5);touch-action:none}
@media (orientation: landscape) {
  #tp-topbar{top:12px;right:16px;left:auto;transform:none}
}
.tp-btn{background:transparent;border:none;color:#fff;font-size:12px;font-weight:600;padding:5px 9px;border-radius:12px;cursor:pointer;display:flex;align-items:center;gap:4px;user-select:none;-webkit-user-select:none;touch-action:manipulation}
.tp-btn:active{background:rgba(255,255,255,0.2);transform:scale(0.96)}
.tp-btn.active{background:rgba(10,132,255,0.85);color:#fff}
.tp-drag-handle{color:rgba(255,255,255,0.45);cursor:grab;padding:0 5px;font-size:13px;letter-spacing:-1px;user-select:none;-webkit-user-select:none;touch-action:none}
.tp-mode-btn{background:rgba(255,255,255,0.1);padding:5px 8px}
.tp-exit-btn{color:#ff453a;font-size:13px;padding:5px 8px}
.tp-vsep{width:1px;height:16px;background:rgba(255,255,255,0.2);margin:0 1px}
.modal-wrap{position:fixed;inset:0;background:rgba(0,0,0,0.65);z-index:100;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px)}
.modal-box{background:#1e2023;border:1px solid rgba(255,255,255,0.2);border-radius:12px;padding:20px;width:280px;text-align:center;color:#fff;box-shadow:0 8px 32px rgba(0,0,0,0.5)}
.modal-err{color:#ff453a;font-size:12px;min-height:16px;margin:4px 0}
#pininput{width:100%;box-sizing:border-box;padding:10px;font-size:20px;text-align:center;letter-spacing:4px;border-radius:8px;border:1px solid #444;background:#121315;color:#fff;margin-bottom:12px}
.modal-actions{display:flex;gap:8px}
.modal-btn-pri{flex:1;background:#0a84ff;border:none;padding:10px;border-radius:8px;color:#fff;font-weight:600;cursor:pointer}
.modal-btn-sec{flex:1;background:#3a3a3c;border:none;padding:10px;border-radius:8px;color:#fff;cursor:pointer}
.crd-modal-box{background:#1e2023;border:1px solid rgba(255,255,255,0.18);border-radius:16px;width:min(340px,90vw);padding:20px;color:#fff;box-shadow:0 12px 40px rgba(0,0,0,0.65);font-family:inherit}
.crd-modal-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
.crd-modal-title{margin:0;font-size:18px;font-weight:600}
.crd-close-btn{background:transparent;border:none;color:#8e8e93;font-size:18px;cursor:pointer;padding:4px 8px;border-radius:8px}
.crd-close-btn:active{background:rgba(255,255,255,0.1);color:#fff}
.crd-option{display:flex;align-items:center;gap:12px;padding:10px 8px;border-radius:10px;cursor:pointer;user-select:none;-webkit-user-select:none}
.crd-option:active{background:rgba(255,255,255,0.08)}
.crd-option input[type=radio]{display:none}
.crd-radio-dot{width:20px;height:20px;border-radius:50%;border:2px solid #8e8e93;position:relative;flex-shrink:0;box-sizing:border-box}
.crd-option input[type=radio]:checked + .crd-radio-dot{border-color:#0a84ff}
.crd-option input[type=radio]:checked + .crd-radio-dot::after{content:'';position:absolute;inset:3px;background:#0a84ff;border-radius:50%}
.crd-option-label{font-size:16px;font-weight:500}
.crd-sep{height:1px;background:rgba(255,255,255,0.12);margin:14px 0 12px}
.crd-desc{margin:0;font-size:13px;line-height:1.45;color:#8e8e93}
</style></head><body>
<div id="side"></div>
<div id="top"></div>
<div id="stage"><img id="bg" alt=""><canvas id="cv"></canvas><div id="mouse-cursor"><svg viewBox="0 0 24 24" width="16" height="16"><path d="M1 1l13 10.2-6.2 1.2 3.6 6.8-2.6 1.4-3.6-6.8L1 18.5V1z" fill="#ffffff" stroke="#1c1c1e" stroke-width="1.4" stroke-linejoin="round"/></svg></div><textarea id="ed" rows="1" autocapitalize="sentences" spellcheck="false"></textarea></div>
<div id="hint"></div>
<div id="toast"></div>
<div id="tp-topbar" style="display:none;">
  <div class="tp-drag-handle" id="tpDrag" title="Drag to move">⋮⋮</div>
  <button class="tp-btn" id="tpPrev" title="Previous Slide">◀</button>
  <button class="tp-btn" id="tpNext" title="Next Slide">▶</button>
  <button class="tp-btn" id="tpEnter" title="Enter / Confirm">↵</button>
  <div class="tp-vsep"></div>
  <button class="tp-btn" id="tpInkBtn" title="Toggle Draw / Annotate">✏️</button>
  <button class="tp-btn" id="tpUndo" title="Undo Annotation" style="display:none;">↶</button>
  <div class="tp-vsep"></div>
  <button class="tp-btn tp-mode-btn" id="tpConfig" title="Configure Touch Input">
    <span id="tpModeIcon">🖱️</span>
    <span style="font-size:9px;opacity:0.7;margin-left:1px;">▾</span>
  </button>
  <div class="tp-vsep"></div>
  <button class="tp-btn tp-exit-btn" id="tpExit" title="Exit Remote Control">✕</button>
</div>
<div id="touch-config-modal" class="modal-wrap" style="display:none;">
  <div class="crd-modal-box">
    <div class="crd-modal-header">
      <h3 class="crd-modal-title">Configure Touch Input</h3>
      <button class="crd-close-btn" id="closeTouchConfig">✕</button>
    </div>
    <div class="crd-modal-body">
      <label class="crd-option" id="optRel">
        <input type="radio" name="touch-mode" value="rel" id="radioRel">
        <span class="crd-radio-dot"></span>
        <span class="crd-option-label">Trackpad mode</span>
      </label>
      <label class="crd-option" id="optAbs">
        <input type="radio" name="touch-mode" value="abs" id="radioAbs">
        <span class="crd-radio-dot"></span>
        <span class="crd-option-label">Direct touch mode</span>
      </label>
      <div class="crd-sep"></div>
      <p id="crd-desc" class="crd-desc">
        Drag anywhere to move the mouse pointer. Tap to left-click, tap with two fingers to right-click, swipe with two fingers to scroll.
      </p>
    </div>
  </div>
</div>
<div id="pinmodal" class="modal-wrap" style="display:none;">
  <div class="modal-box">
    <h3 style="margin-top:0;">Enter Host PIN</h3>
    <p id="pinerr" class="modal-err"></p>
    <input type="password" id="pininput" maxlength="8" pattern="[0-9]*" inputmode="numeric" placeholder="6-digit PIN">
    <div class="modal-actions">
      <button id="pinsubmit" class="modal-btn-pri">Unlock</button>
      <button id="pincancel" class="modal-btn-sec">Cancel</button>
    </div>
  </div>
</div>
<div id="pastebox"><div id="pastearea" contenteditable="true">Long-press here and tap <b>&nbsp;Paste&nbsp;</b><br>(or press Ctrl/⌘+V)</div>
  <button id="pasteclose">Cancel</button></div>
<div id="bgmenu"></div>
<div id="palmenu">
  <label class="bgo"><span class="bgSw" id="palSw"></span>Change color<input type="color" id="palpick"></label>
  <button class="bgo" id="palDel"><span class="bgSw" style="border-style:dashed"></span>Delete color</button>
  <button class="bgo" id="palReset"><span class="bgSw" style="background:conic-gradient(red,yellow,lime,cyan,blue,magenta,red)"></span>Reset palette</button>
</div>
<input type="file" id="file" accept="image/*">
<script>
"use strict";
const I = {
 pen:'<path d="M3 21l3.5-1L19 7.5 16.5 5 4 17.5z"/><path d="M14.5 7l2.5 2.5"/>',
 marker:'<path d="M4 20h5L20 9l-5-5L4 15z" stroke-width="2.6"/>',
 hl:'<path d="M9 14l-3 3v4h5l2-3"/><path d="M9 14L17 3l4 3-8 12z"/>',
 neon:'<path d="M12 2v5M12 17v5M2 12h5M17 12h5M5 5l3.5 3.5M15.5 15.5L19 19M5 19l3.5-3.5M15.5 8.5L19 5"/>',
 laser:'<circle cx="12" cy="12" r="4" fill="currentColor"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>',
 line:'<path d="M5 19L19 5"/>',
 arrow:'<path d="M5 19L19 5M10 5h9v9"/>',
 farrow:'<path d="M4 19c1-7 6-12 14-12"/><path d="M14 3.5L18.5 7 15 11.5"/>',
 rect:'<rect x="4" y="6" width="16" height="12" rx="1"/>',
 ellipse:'<ellipse cx="12" cy="12" rx="9" ry="6.5"/>',
 text:'<path d="M5 7V4h14v3M12 4v16M9 20h6"/>',
 select:'<path d="M5 3l5.5 17 2.6-7.4L20.5 10z"/>',
 eraser:'<path d="M8 20h12"/><path d="M3.5 14.5l9-9a2 2 0 0 1 2.8 0l3.2 3.2a2 2 0 0 1 0 2.8L10 20H7.5z"/>',
 image:'<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 16l5-5 4 4 3-3 6 6"/><circle cx="15.5" cy="9" r="1.5"/>',
 paste:'<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V2.5h6V4M9 10h6M9 14h4"/>',
 undo:'<path d="M9 14L4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-3"/>',
 redo:'<path d="M15 14l5-5-5-5"/><path d="M20 9H10a6 6 0 0 0 0 12h3"/>',
 trash:'<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
 del:'<path d="M18 6L6 18M6 6l12 12"/>',
 screen:'<rect x="3" y="4" width="18" height="12" rx="1.5"/><path d="M8 20h8M12 16v4"/>',
 board:'<rect x="3" y="4" width="18" height="13" rx="1"/><path d="M8 21l4-4 4 4"/>',
 eye:'<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
 hand:'<path d="M8 13V5a1.5 1.5 0 0 1 3 0v6M11 11V4a1.5 1.5 0 0 1 3 0v7M14 11V5.5a1.5 1.5 0 0 1 3 0V14a7 7 0 0 1-7 7h-1a6 6 0 0 1-5-3l-2-4a1.5 1.5 0 0 1 2.5-1.5L8 15"/>',
 prev:'<path d="M15 18l-6-6 6-6"/>',
 next:'<path d="M9 18l6-6-6-6"/>',
 full:'<path d="M3 8V5a2 2 0 0 1 2-2h3m11 0h3a2 2 0 0 1 2 2v3m0 8v3a2 2 0 0 1-2 2h-3M8 21H5a2 2 0 0 1-2-2v-3"/>',
 exitfull:'<path d="M8 3v3a2 2 0 0 1-2 2H3m18 0h-3a2 2 0 0 1-2-2V3m0 18v-3a2 2 0 0 1 2-2h3M3 16h3a2 2 0 0 1 2 2v3"/>',
 trackpad:'<rect x="4" y="3" width="16" height="18" rx="2.5"/><line x1="4" y1="14" x2="20" y2="14"/><line x1="12" y1="14" x2="12" y2="21"/>'
};
const svg = n => '<svg viewBox="0 0 24 24">'+I[n]+'</svg>';
const TOOLS = [['pen','Pen'],['marker','Marker'],['hl','Highlight'],['neon','Neon'],['laser','Laser'],'|',
  ['line','Line'],['arrow','Arrow'],['farrow','Curve'],['rect','Box'],['ellipse','Circle'],['text','Text'],'|',
  ['select','Select'],['eraser','Eraser'],['image','Image'],['paste','Paste'],'|',
  ['trackpad','Trackpad']];
const COLORS = ['#ff3b30','#ff9500','#ffcc00','#34c759','#00c7ff','#0a84ff','#bf5af2','#ff2d92','#ffffff','#000000'];
const STROKE = {pen:1,marker:1,hl:1,neon:1,laser:1,farrow:1}, SHAPE = {line:1,arrow:1,rect:1,ellipse:1};
const WMUL = {pen:1,marker:2.2,hl:4,neon:1.2,laser:1.2,farrow:1};
const BGS = ['white','grid','black','green'];
const BGC = {white:'#ffffff', grid:'#ffffff', black:'#141517', green:'#1f3a2c'};
const BGN = {white:'White', grid:'Grid', black:'Black', green:'Chalkboard'};
const bgCss = b => b==='grid'
  ? 'linear-gradient(#b8bec6 1px,transparent 1px) 0 0/6px 6px,linear-gradient(90deg,#b8bec6 1px,transparent 1px) 0 0/6px 6px,#fff'
  : BGC[b];
const FONTS = {
  sans:  'InkSans,-apple-system,Roboto,"Segoe UI",sans-serif',
  hand:  'InkHand,"Ink Free","Comic Sans MS","Chalkboard SE","Comic Neue",cursive',
  serif: 'InkSerif,Georgia,"Noto Serif",serif'
};
const fontOf = (f, px) => '700 ' + px + 'px ' + (FONTS[f] || FONTS.sans);

let S = {tool:'pen', color:'#ff3b30', size:6, fingers:true, fingersSet:false, live:true, bg:'white', font:'sans'};
try { Object.assign(S, JSON.parse(localStorage.getItem('inkcast.s')||'{}')); } catch(e){}
if (!TOOLS.some(t => t[0]===S.tool) || S.tool==='image' || S.tool==='paste' || S.tool==='trackpad') S.tool = 'pen';
if (!BGS.includes(S.bg)) S.bg = 'white';
if (!FONTS[S.font]) S.font = 'sans';
const isHex = c => typeof c==='string' && /^#[0-9a-f]{6}$/i.test(c);
const MAXPAL = 16;
if (!Array.isArray(S.palette) || !S.palette.length || S.palette.length>MAXPAL || !S.palette.every(isHex)) S.palette = COLORS.slice();
const save = () => { try { localStorage.setItem('inkcast.s', JSON.stringify(S)); } catch(e){} };

let pages = {screen:[], board:[]}, page = 'screen', items = pages.screen;
const HIST = {screen:{h:[], r:[]}, board:{h:[], r:[]}};
let hist = HIST.screen.h, redoS = HIST.screen.r;
let lasers = [], cur = null, pid = null, drag = null, erase = null, sel = [], marq = null, lassoOut = null;
let editor = null, downPt = null, sizeBefore = null, prevTool = 'pen', crect = null;
let aspect = 16/9, sw = 1, sh = 1;
const $ = id => document.getElementById(id);
const stage = $('stage'), cv = $('cv'), ctx = cv.getContext('2d'), bg = $('bg'), ed = $('ed');
const base = document.createElement('canvas'), bctx = base.getContext('2d');
const mctx = document.createElement('canvas').getContext('2d');
let baseDirty = true, needDraw = true, haveFrame = false;
const appendList = [];
const uid = () => Math.random().toString(36).slice(2,10);
const rnd = v => Math.round(v*10000)/10000;
const strip = (k,v) => (k && k[0]==='_') ? undefined : v;
const clone = o => JSON.parse(JSON.stringify(o, strip));

// ---------------- toolbar
const side = $('side'), topBar = $('top');
for (const t of TOOLS) {
  if (t==='|') { side.insertAdjacentHTML('beforeend','<div class="sep"></div>'); continue; }
  const b = document.createElement('button'); b.className='tb'; b.dataset.tool=t[0];
  b.innerHTML = svg(t[0])+'<span>'+t[1]+'</span>';
  b.onclick = () => {
    if (t[0]==='image') { $('file').click(); return; }
    if (t[0]==='paste') { openPaste(); return; }
    setTool(t[0]);
  };
  side.appendChild(b);
}
// palette: tap a color to use it; tap the selected color again to change or delete it; + adds one (remembered on this tablet)
topBar.insertAdjacentHTML('beforeend',
 '<div id="pal"></div><label id="addc" title="Add a color">+<input type="color" id="addpick"></label>');
const pal = $('pal'), palMenu = $('palmenu');
let palIdx = -1, addIdx = -1;
function renderPalette(){
  pal.innerHTML = '';
  S.palette.forEach((c, i) => {
    const b = document.createElement('div'); b.className='sw'; b.title='Tap again to change or delete this color';
    b.onclick = () => {
      if (S.color===S.palette[i]) { (palMenu.classList.contains('show') && palIdx===i) ? closePalMenu() : openPalMenu(i, b); return; }
      closePalMenu(); setColor(S.palette[i]);
      if (!S.palTip) { S.palTip = true; save(); toast('Tip: tap the selected color again to change or delete it'); }
    };
    pal.appendChild(b);
  });
  $('addc').style.display = S.palette.length>=MAXPAL ? 'none' : '';
  paintPalette();
}
function paintPalette(){
  pal.querySelectorAll('.sw').forEach((b, i) => { const c = S.palette[i]; b.style.background = c; b.dataset.c = c; });
}
// one trip through the picker adds exactly one color (nothing if cancelled)
$('addc').addEventListener('pointerdown', () => { addIdx = -1; $('addpick').value = S.color; });
const addColor = v => {
  if (!isHex(v)) return;
  v = v.toLowerCase();
  if (addIdx<0) {
    if (S.palette.length>=MAXPAL) return;
    S.palette.push(v); addIdx = S.palette.length-1; renderPalette();
  } else { S.palette[addIdx] = v; paintPalette(); }
  save(); setColor(v);
};
$('addpick').oninput = e => addColor(e.target.value);
$('addpick').onchange = e => { addColor(e.target.value); addIdx = -1; };
function placeMenu(m, anchor){
  const r = anchor.getBoundingClientRect();
  m.classList.add('show');
  m.style.top = (r.bottom + 6) + 'px';
  m.style.left = Math.max(8, Math.min(r.left, innerWidth - m.offsetWidth - 8)) + 'px';
}
function openPalMenu(i, b){
  palIdx = i;
  $('palpick').value = S.palette[i]; $('palSw').style.background = S.palette[i];
  $('palDel').disabled = S.palette.length<=1;
  placeMenu(palMenu, b);
}
function closePalMenu(){ palMenu.classList.remove('show'); }
$('palpick').oninput = e => {
  const v = e.target.value.toLowerCase();
  if (palIdx<0 || !isHex(v)) return;
  S.palette[palIdx] = v; save(); paintPalette(); $('palSw').style.background = v; setColor(v);
};
$('palDel').onclick = () => {
  if (S.palette.length<=1) return;
  const [gone] = S.palette.splice(palIdx, 1);
  closePalMenu(); save(); renderPalette();
  if (S.color===gone && !S.palette.includes(gone)) setColor(S.palette[Math.min(palIdx, S.palette.length-1)]);
  else ui();
};
$('palReset').onclick = () => {
  closePalMenu();
  if (!confirm('Reset the palette to the original colors?')) return;
  S.palette = COLORS.slice(); save(); renderPalette(); ui();
};
renderPalette();
const tbtn = (icon,label,id) => '<button class="tb" id="'+id+'">'+svg(icon)+'<span>'+label+'</span></button>';
topBar.insertAdjacentHTML('beforeend',
 '<label id="custom" title="Custom color"><input type="color" id="cpick"></label><div class="vsep"></div>'+
 '<div id="dot"></div><input type="range" id="size" min="1" max="40" step="1">'+
 '<div id="fonts"><div class="vsep"></div><button class="fb" data-f="sans">Aa</button><button class="fb" data-f="hand">Aa</button><button class="fb" data-f="serif">Aa</button></div>'+
 '<div class="vsep"></div>'+tbtn('undo','Undo','undo')+tbtn('redo','Redo','redo')+tbtn('trash','Clear','clear')+tbtn('del','Delete','delB')+
 '<div class="vsep"></div>'+tbtn('screen','Screen','screenB')+tbtn('board','Board','boardB')+
 '<div class="vsep"></div>'+tbtn('prev','Prev','prevB')+tbtn('next','Next','nextB')+
 '<div class="vsep"></div>'+tbtn('eye','Preview','liveB')+tbtn('hand','Fingers','fingB')+tbtn('full','Full','fullB')+
 '<div id="status"><span id="led"></span><span id="stxt">Connecting</span></div>');
document.querySelectorAll('.fb').forEach(b => { b.style.font = fontOf(b.dataset.f, 16); b.onclick = () => setFont(b.dataset.f); });
$('cpick').oninput = e => setColor(e.target.value);
$('size').value = S.size;
$('size').oninput = e => {
  S.size = +e.target.value; save();
  if (sel.length && S.tool==='select') {
    if (!sizeBefore) sizeBefore = new Map(sel.map(it => [it, styleOf(it)]));
    for (const it of sel) applySize(it);
  }
  ui();
};
$('size').onchange = () => {
  if (sizeBefore) {
    const its = [...sizeBefore].filter(([it]) => items.includes(it)).map(([it, before]) => ({it, before, after:styleOf(it)}));
    if (its.length) pushHist({a:'upd', its});
  }
  sizeBefore = null;
};
$('undo').onclick = undo; $('redo').onclick = redo; $('clear').onclick = clearAll; $('delB').onclick = deleteSel;
$('screenB').onclick = () => setPage('screen');
$('prevB').onclick = () => sendSlide('prev');
$('nextB').onclick = () => sendSlide('next');
function sendSlide(dir, quiet=false){
  if (!connected) { toast('Offline: cannot change slide'); return; }
  sendNow({t:'slide', dir});
  if (!quiet) toast(dir==='next' ? 'Next Slide ▶' : '◀ Prev Slide');
}
function isFullscreen(){
  return !!(document.fullscreenElement || document.webkitFullscreenElement || document.mozFullScreenElement || document.msFullscreenElement);
}
function updateFsUI(){
  const fs = isFullscreen();
  const btn = $('fullB');
  if (!btn) return;
  btn.classList.toggle('on', fs);
  btn.innerHTML = svg(fs ? 'exitfull' : 'full') + '<span>' + (fs ? 'Exit' : 'Full') + '</span>';
}
function toggleFullscreen(){
  const doc = document;
  const docEl = document.documentElement;
  if (!isFullscreen()) {
    const req = docEl.requestFullscreen || docEl.webkitRequestFullscreen || docEl.mozRequestFullScreen || docEl.msRequestFullscreen;
    if (req) {
      Promise.resolve(req.call(docEl)).then(() => { updateFsUI(); layout(); }).catch(e => {
        toast('Tip: Add to Home Screen for fullscreen view');
      });
    } else {
      toast('Tip: Add to Home Screen for fullscreen view');
    }
  } else {
    const exit = doc.exitFullscreen || doc.webkitExitFullscreen || doc.mozCancelFullScreen || doc.msExitFullscreen;
    if (exit) {
      Promise.resolve(exit.call(doc)).then(() => { updateFsUI(); layout(); }).catch(e => console.warn(e));
    }
  }
}
$('fullB').onclick = toggleFullscreen;
['fullscreenchange', 'webkitfullscreenchange', 'mozfullscreenchange', 'MSFullscreenChange'].forEach(evt => {
  document.addEventListener(evt, () => { updateFsUI(); layout(); });
});
// Board opens a menu of whiteboards
const bgMenu = $('bgmenu');
bgMenu.innerHTML = BGS.map(b => '<button class="bgo" data-bg="'+b+'"><span class="bgSw" style="background:'+bgCss(b)+'"></span>'+BGN[b]+'</button>').join('');
bgMenu.querySelectorAll('.bgo').forEach(o => { o.onclick = () => { closeBgMenu(); setBoard(o.dataset.bg); }; });
function openBgMenu(){ closePalMenu(); placeMenu(bgMenu, $('boardB')); ui(); }
function closeBgMenu(){ bgMenu.classList.remove('show'); }
$('boardB').onclick = () => bgMenu.classList.contains('show') ? closeBgMenu() : openBgMenu();
// a tap outside a menu only closes it (it doesn't draw)
document.addEventListener('pointerdown', e => {
  let closed = false;
  for (const [m, a] of [[bgMenu, $('boardB')], [palMenu, pal]]) {
    if (!m.classList.contains('show') || m.contains(e.target) || a.contains(e.target)) continue;
    m.classList.remove('show'); closed = true;
  }
  if (closed && e.target===cv) { e.preventDefault(); e.stopPropagation(); }
}, true);
const closeMenus = () => { closeBgMenu(); closePalMenu(); };
$('top').addEventListener('scroll', closeMenus);
addEventListener('resize', closeMenus);
$('liveB').onclick = () => { S.live = !S.live; save(); updBg(); ui(); reqFrame(); toast(S.live ? 'Live PC preview on' : 'Live PC preview off'); };
$('fingB').onclick = () => {
  S.fingers = !S.fingers; S.fingersSet = true; save(); ui();
  toast(S.fingers ? 'Fingers can draw' : 'Only the pencil draws (palm rejection on)');
};

function toast(msg){
  const t = $('toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove('show'), 2600);
}
function setTool(t){
  if (editor) commitEditor();
  if (t!=='select') sel = [];
  if (t!=='laser' && t!=='trackpad') prevTool = t;
  const changed = (S.tool !== t);
  if (t === 'trackpad') {
    tpAction = 'mouse';
    const inkBtn = $('tpInkBtn'); if (inkBtn) inkBtn.classList.remove('active');
    const undoBtn = $('tpUndo'); if (undoBtn) undoBtn.style.display = 'none';
    if (page !== 'screen') setPage('screen');
    if (!S.live) { S.live = true; save(); updBg(); reqFrame(); }
  }
  S.tool = t; save();
  if (changed) layout();
  ui(); needDraw = true;
}
function setColor(c){
  S.color = c; save();
  if (sel.length && S.tool==='select') {
    const its = sel.filter(it => it.k!=='img' && it.c!==c).map(it => ({it, before:{c:it.c}, after:{c}}));
    its.forEach(o => setFields(o.it, o.after));
    if (its.length) pushHist({a:'upd', its});
  }
  if (editor) ed.style.color = c;
  ui();
}
function setFont(f){
  S.font = f; save();
  if (editor) { editor.f = f; placeEditor(); }
  if (sel.length && S.tool==='select') {
    const its = sel.filter(it => it.k==='text' && (it.f||'sans')!==f).map(it => {
      const m = measure(it.txt, it.fs, f);
      return {it, before:{f:it.f||'sans', tw:it.tw, th:it.th}, after:{f, tw:m.tw, th:m.th}};
    });
    its.forEach(o => setFields(o.it, o.after));
    if (its.length) pushHist({a:'upd', its});
  }
  ui();
}
function ui(){
  document.querySelectorAll('.tb[data-tool]').forEach(b => b.classList.toggle('on', b.dataset.tool===S.tool));
  document.querySelectorAll('.sw').forEach(b => b.classList.toggle('on', b.dataset.c===S.color));
  $('custom').classList.toggle('on', !S.palette.includes(S.color));
  const d = Math.max(3, Math.min(32, S.size*(WMUL[S.tool]||1)*0.9));
  $('dot').innerHTML = '<div style="width:'+d+'px;height:'+d+'px;border-radius:50%;background:'+S.color+
    (S.tool==='hl'?';opacity:.45':'')+'"></div>';
  const selText = sel.find(it => it.k==='text');
  const showFonts = S.tool==='text' || !!editor || (S.tool==='select' && !!selText);
  $('fonts').style.display = showFonts ? 'flex' : 'none';
  const curFont = (S.tool==='select' && selText) ? (selText.f||'sans') : S.font;
  document.querySelectorAll('.fb').forEach(b => b.classList.toggle('on', b.dataset.f===curFont));
  $('undo').disabled = !hist.length; $('redo').disabled = !redoS.length;
  $('delB').style.display = (sel.length && S.tool==='select') ? '' : 'none';
  $('screenB').classList.toggle('on', page==='screen');
  $('boardB').classList.toggle('on', page==='board');
  $('liveB').classList.toggle('on', S.live);
  $('fingB').classList.toggle('on', S.fingers);
  document.querySelectorAll('.bgo').forEach(o => o.classList.toggle('on', page==='board' && o.dataset.bg===S.bg));
  let h = '';
  if (S.tool==='trackpad') {
    side.style.display = 'none';
    topBar.style.display = 'none';
    if (authRequired && !authToken) {
      $('tp-topbar').style.display = 'none';
      promptPin();
    } else {
      $('tp-topbar').style.display = 'flex';
    }
    if (tpAction === 'ink') {
      h = 'Inking active · draw on slides with finger or stylus · tap ✏️ for mouse control';
    } else {
      h = tpMode === 'rel' ? 'Trackpad: drag to move · tap to left-click · 2-finger tap right-click · 2-finger scroll'
                           : 'Direct touch: tap screen to click · touch & hold to right-click';
    }
  } else {
    side.style.display = 'flex';
    topBar.style.display = 'flex';
    $('tp-topbar').style.display = 'none';
    const mc = $('mouse-cursor'); if (mc) mc.style.display = 'none';
    if (S.tool==='text') h = 'Tap where you want text · tap a text to edit it';
    else if (S.tool==='select') h = sel.length
      ? (sel.length>1 ? sel.length+' selected · ' : '') + 'drag to move · corner dots to resize · tap a color, size or font to change it'
      : 'Tap something, or draw a loop around things to select them';
    else if (S.tool==='farrow') h = 'Draw a curved line — the arrow head goes where you lift the pen';
  }
  $('hint').textContent = h;
  updateCursorUI();
}

// ---------------- trackpad, touch config & pin auth
let authRequired = false, authToken = localStorage.getItem('inkcast.auth_token') || '', mouseEnabled = true;
let tpMode = localStorage.getItem('inkcast.tp_mode') || 'rel';
let tpAction = 'mouse';
let tpNormX = 0.5, tpNormY = 0.5;
let tpStart = null, tpLast = null, tpDist = 0, tpMoved = false;
let tpTwoStart = null, tpTwoLast = null;
let tpLongPressTimer = null;
let tpPendingMove = null, tpRaf = null;

const DESCS = {
  rel: 'Drag anywhere to move the mouse pointer. Tap to left-click, tap with two fingers to right-click, swipe with two fingers to scroll.',
  abs: 'Tap directly on the desktop to click at that point. Drag to move pointer directly, touch and hold to right-click.'
};

function setTpMode(mode) {
  tpMode = (mode === 'abs' ? 'abs' : 'rel');
  try { localStorage.setItem('inkcast.tp_mode', tpMode); } catch(e){}
  $('radioRel').checked = (tpMode === 'rel');
  $('radioAbs').checked = (tpMode === 'abs');
  $('crd-desc').textContent = DESCS[tpMode] || DESCS.rel;
  $('tpModeIcon').textContent = (tpMode === 'rel' ? '🖱️' : '👆');
  ui();
}
$('radioRel').onchange = () => setTpMode('rel');
$('radioAbs').onchange = () => setTpMode('abs');
$('tpConfig').onclick = () => { $('touch-config-modal').style.display = 'flex'; };
$('closeTouchConfig').onclick = () => { $('touch-config-modal').style.display = 'none'; };
$('touch-config-modal').onclick = e => {
  if (e.target === $('touch-config-modal')) $('touch-config-modal').style.display = 'none';
};
setTpMode(tpMode);

function setTpAction(act) {
  tpAction = act;
  const inkBtn = $('tpInkBtn');
  const undoBtn = $('tpUndo');
  if (inkBtn) inkBtn.classList.toggle('active', tpAction === 'ink');
  if (undoBtn) undoBtn.style.display = (tpAction === 'ink' ? 'flex' : 'none');
  const curEl = $('mouse-cursor');
  if (curEl && tpAction === 'ink') curEl.style.display = 'none';
  else if (curEl && S.tool === 'trackpad') curEl.style.display = 'block';
  toast(tpAction === 'ink' ? 'Draw mode active (tap ✏️ for mouse)' : 'Mouse control active');
  ui();
}
$('tpInkBtn').onclick = () => setTpAction(tpAction === 'ink' ? 'mouse' : 'ink');
$('tpUndo').onclick = () => undo();

function getStageRect() {
  const r = stage.getBoundingClientRect();
  return { left: r.left, top: r.top, width: Math.max(1, r.width), height: Math.max(1, r.height) };
}

function updateCursorUI() {
  const curEl = $('mouse-cursor');
  if (!curEl) return;
  if (S.tool === 'trackpad' && tpAction !== 'ink') {
    curEl.style.display = 'block';
    curEl.style.left = (tpNormX * sw) + 'px';
    curEl.style.top = (tpNormY * sh) + 'px';
  } else {
    curEl.style.display = 'none';
  }
}

function showClickRipple(xNorm, yNorm, type='left') {
  const r = document.createElement('div');
  r.className = 'click-ripple ripple-' + type;
  r.style.left = (xNorm * sw) + 'px';
  r.style.top = (yNorm * sh) + 'px';
  stage.appendChild(r);
  setTimeout(() => { if (r.parentNode) r.parentNode.removeChild(r); }, 400);
}

function promptPin() {
  $('pinmodal').style.display = 'flex';
  $('pininput').value = '';
  $('pinerr').textContent = '';
  $('pininput').focus();
}

function flushMouseMove() {
  if (tpPendingMove) {
    send(tpPendingMove);
    tpPendingMove = null;
  }
  tpRaf = null;
}

function queueMouseMove(msg) {
  tpPendingMove = msg;
  if (!tpRaf) tpRaf = requestAnimationFrame(flushMouseMove);
}

$('tpPrev').onclick = () => sendSlide('prev', true);
$('tpNext').onclick = () => sendSlide('next', true);
$('tpEnter').onclick = () => sendSlide('enter', true);
$('tpExit').onclick = () => setTool(prevTool || 'pen');

const tpBar = $('tp-topbar');
let tpBarDragging = false, tpBarStartX = 0, tpBarStartY = 0;
$('tpDrag').addEventListener('touchstart', e => {
  if (e.touches.length === 1) {
    tpBarDragging = true;
    const t = e.touches[0];
    const r = tpBar.getBoundingClientRect();
    tpBarStartX = t.clientX - r.left;
    tpBarStartY = t.clientY - r.top;
    e.preventDefault();
  }
}, { passive: false });
document.addEventListener('touchmove', e => {
  if (!tpBarDragging) return;
  const t = e.touches[0];
  let x = t.clientX - tpBarStartX;
  let y = t.clientY - tpBarStartY;
  x = Math.max(4, Math.min(innerWidth - tpBar.offsetWidth - 4, x));
  y = Math.max(4, Math.min(innerHeight - tpBar.offsetHeight - 4, y));
  tpBar.style.left = x + 'px';
  tpBar.style.top = y + 'px';
  tpBar.style.right = 'auto';
  tpBar.style.transform = 'none';
  e.preventDefault();
}, { passive: false });
document.addEventListener('touchend', () => { tpBarDragging = false; });

$('pincancel').onclick = () => {
  $('pinmodal').style.display = 'none';
  setTool(prevTool || 'pen');
};
$('pinsubmit').onclick = () => {
  const pin = $('pininput').value.trim();
  if (!pin) return;
  sendNow({ t: 'auth', pin: pin });
};

function isOverlayTouch(e) {
  return !!(e.target.closest && e.target.closest('#tp-topbar, #touch-config-modal, #pinmodal, #toast'));
}

document.addEventListener('touchstart', e => {
  if (S.tool !== 'trackpad' || isOverlayTouch(e)) return;
  if (tpAction === 'ink') return; // let canvas pointer events handle drawing
  if (e.touches.length === 1) {
    const t = e.touches[0];
    tpStart = { x: t.clientX, y: t.clientY, time: Date.now() };
    tpLast = { x: t.clientX, y: t.clientY };
    tpDist = 0;
    tpMoved = false;
    clearTimeout(tpLongPressTimer);
    if (tpMode === 'abs') {
      tpLongPressTimer = setTimeout(() => {
        if (!tpMoved && tpMode === 'abs') {
          const rect = getStageRect();
          tpNormX = Math.max(0, Math.min(1, (tpStart.x - rect.left) / rect.width));
          tpNormY = Math.max(0, Math.min(1, (tpStart.y - rect.top) / rect.height));
          updateCursorUI();
          showClickRipple(tpNormX, tpNormY, 'right');
          sendNow({ t: 'mouse', action: 'move_abs', x: tpNormX, y: tpNormY });
          sendNow({ t: 'mouse', action: 'click', btn: 'right' });
          tpMoved = true;
        }
      }, 500);
    }
  } else if (e.touches.length === 2) {
    clearTimeout(tpLongPressTimer);
    tpTwoStart = { y: (e.touches[0].clientY + e.touches[1].clientY) / 2, time: Date.now() };
    tpTwoLast = tpTwoStart.y;
  }
}, { passive: false });

document.addEventListener('touchmove', e => {
  if (S.tool !== 'trackpad' || isOverlayTouch(e)) return;
  if (tpAction === 'ink') return; // let canvas pointer events handle drawing
  e.preventDefault();
  if (e.touches.length === 1 && tpLast) {
    const t = e.touches[0];
    const dx = t.clientX - tpLast.x;
    const dy = t.clientY - tpLast.y;
    tpDist += Math.hypot(dx, dy);
    if (tpDist > 5) {
      tpMoved = true;
      clearTimeout(tpLongPressTimer);
    }
    tpLast = { x: t.clientX, y: t.clientY };

    const rect = getStageRect();
    if (tpMode === 'rel') {
      const sens = 1.35;
      const ndx = (dx * sens) / rect.width;
      const ndy = (dy * sens) / rect.height;
      tpNormX = Math.max(0, Math.min(1, tpNormX + ndx));
      tpNormY = Math.max(0, Math.min(1, tpNormY + ndy));
      updateCursorUI();
      queueMouseMove({ t: 'mouse', action: 'move_rel', dx: ndx, dy: ndy });
    } else if (tpMode === 'abs') {
      tpNormX = Math.max(0, Math.min(1, (t.clientX - rect.left) / rect.width));
      tpNormY = Math.max(0, Math.min(1, (t.clientY - rect.top) / rect.height));
      updateCursorUI();
      queueMouseMove({ t: 'mouse', action: 'move_abs', x: tpNormX, y: tpNormY });
    }
  } else if (e.touches.length === 2 && tpTwoLast !== null) {
    clearTimeout(tpLongPressTimer);
    const cy = (e.touches[0].clientY + e.touches[1].clientY) / 2;
    const dScroll = cy - tpTwoLast;
    tpTwoLast = cy;
    queueMouseMove({ t: 'mouse', action: 'scroll', dy: Math.round(dScroll * 3) });
  }
}, { passive: false });

document.addEventListener('touchend', e => {
  if (S.tool !== 'trackpad' || isOverlayTouch(e)) return;
  if (tpAction === 'ink') return; // let canvas pointer events handle drawing
  clearTimeout(tpLongPressTimer);
  if (e.touches.length === 0 && tpStart) {
    const elapsed = Date.now() - tpStart.time;
    if (!tpMoved && elapsed < 350) {
      if (tpMode === 'rel') {
        showClickRipple(tpNormX, tpNormY, 'left');
        sendNow({ t: 'mouse', action: 'click', btn: 'left' });
      } else if (tpMode === 'abs') {
        const rect = getStageRect();
        tpNormX = Math.max(0, Math.min(1, (tpStart.x - rect.left) / rect.width));
        tpNormY = Math.max(0, Math.min(1, (tpStart.y - rect.top) / rect.height));
        updateCursorUI();
        showClickRipple(tpNormX, tpNormY, 'left');
        sendNow({ t: 'mouse', action: 'move_abs', x: tpNormX, y: tpNormY });
        sendNow({ t: 'mouse', action: 'click', btn: 'left' });
      }
    }
    tpStart = null;
    tpLast = null;
  } else if (e.touches.length === 0 && tpTwoStart) {
    const elapsed = Date.now() - tpTwoStart.time;
    if (Math.abs(tpTwoLast - tpTwoStart.y) < 15 && elapsed < 350) {
      showClickRipple(tpNormX, tpNormY, 'right');
      sendNow({ t: 'mouse', action: 'click', btn: 'right' });
    }
    tpTwoStart = null;
    tpTwoLast = null;
  }
});

// ---------------- layout
function layout(){
  const isTp = (S.tool === 'trackpad');
  const L = isTp ? 0 : 62, T = isTp ? 0 : 54;
  const W = innerWidth - L, Hh = innerHeight - T;
  let w = W, h = W/aspect; if (h > Hh) { h = Hh; w = Hh*aspect; }
  sw = w; sh = h;
  Object.assign(stage.style, {left:(L+(W-w)/2)+'px', top:(T+(Hh-h)/2)+'px', width:w+'px', height:h+'px'});
  const dpr = devicePixelRatio || 1;
  cv.width = base.width = Math.round(w*dpr); cv.height = base.height = Math.round(h*dpr);
  if (editor) placeEditor();
  baseDirty = needDraw = true;
  updateCursorUI();
}
addEventListener('resize', layout);

// ---------------- rendering (mirrors the PC renderer)
function pathOf(c, pts, W, H){
  c.beginPath();
  const n = pts.length;
  c.moveTo(pts[0][0]*W, pts[0][1]*H);
  if (n === 1) { c.lineTo(pts[0][0]*W+0.01, pts[0][1]*H); return; }
  if (n < 3) { c.lineTo(pts[1][0]*W, pts[1][1]*H); return; }
  for (let i=1;i<n-1;i++) {
    const x = pts[i][0]*W, y = pts[i][1]*H;
    c.quadraticCurveTo(x, y, (x + pts[i+1][0]*W)/2, (y + pts[i+1][1]*H)/2);
  }
  c.lineTo(pts[n-1][0]*W, pts[n-1][1]*H);
}
function drawBoard(c, W, H){
  c.fillStyle = BGC[S.bg]; c.fillRect(0,0,W,H);
  if (S.bg==='grid') {
    const s = W/1920, step = 48*s; c.strokeStyle = '#dfe3e8'; c.lineWidth = Math.max(1, s); c.beginPath();
    for (let x=step; x<W; x+=step) { c.moveTo(x,0); c.lineTo(x,H); }
    for (let y=step; y<H; y+=step) { c.moveTo(0,y); c.lineTo(W,y); }
    c.stroke();
  }
}
function head(c, tx, ty, ux, uy, w, s){
  const hl = Math.max(14*s, w*3.2), hw = hl*.55, bx = tx-ux*hl, by = ty-uy*hl;
  c.beginPath(); c.moveTo(tx,ty); c.lineTo(bx-uy*hw, by+ux*hw); c.lineTo(bx+uy*hw, by-ux*hw); c.closePath();
  c.fill(); c.lineWidth = Math.max(.5, w*.5); c.stroke();
}
function draw(c, it, W, H, fade, extra){
  fade = fade===undefined ? 1 : fade;
  const s = W/1920, k = it.k, w = (it.w||0)*s;
  c.save(); c.lineJoin='round'; c.strokeStyle = it.c; c.fillStyle = it.c;
  if (STROKE[k]) {
    const pts = extra && extra.length ? it.pts.concat(extra) : it.pts;
    if (!pts.length) { c.restore(); return; }
    pathOf(c, pts, W, H);
    if (k==='neon' || k==='laser') {
      c.lineCap='round';
      for (const [m,a] of [[3,.12],[1.8,.25]]) { c.globalAlpha=a*fade; c.lineWidth=w*m; c.stroke(); }
      c.globalAlpha=fade; c.lineWidth=w; c.stroke();
      c.globalAlpha=.85*fade; c.strokeStyle='#fff'; c.lineWidth=w*.4; c.stroke();
    } else {
      c.lineCap = k==='hl' ? 'square' : 'round';
      c.globalAlpha = (k==='hl' ? .35 : 1)*fade; c.lineWidth = Math.max(.5,w); c.stroke();
    }
    if (k==='farrow' && pts.length >= 2) {
      const n = pts.length, lx = pts[n-1][0]*W, ly = pts[n-1][1]*H, hl = Math.max(14*s, w*3.2);
      let j = n-2;
      while (j > 0 && Math.hypot(pts[j][0]*W-lx, pts[j][1]*H-ly) < hl*.8) j--;
      const dx = lx-pts[j][0]*W, dy = ly-pts[j][1]*H, d = Math.hypot(dx,dy);
      if (d >= .5) { const ux = dx/d, uy = dy/d; head(c, lx+ux*w*.5, ly+uy*w*.5, ux, uy, w, s); }
    }
  } else if (SHAPE[k]) {
    const ax=it.a[0]*W, ay=it.a[1]*H, bx=it.b[0]*W, by=it.b[1]*H;
    c.globalAlpha = fade; c.lineCap='round'; c.lineWidth=Math.max(.5,w); c.beginPath();
    if (k==='line') { c.moveTo(ax,ay); c.lineTo(bx,by); c.stroke(); }
    else if (k==='arrow') {
      const dx=bx-ax, dy=by-ay, L=Math.hypot(dx,dy);
      if (L>=1) {
        const ux=dx/L, uy=dy/L, hl=Math.min(Math.max(14*s, w*3.2), L), hw=hl*.55;
        const bxx=bx-ux*hl, byy=by-uy*hl;
        c.moveTo(ax,ay); c.lineTo(bx-ux*hl*.8, by-uy*hl*.8); c.stroke();
        c.beginPath(); c.lineWidth=Math.max(.5,w*.5);
        c.moveTo(bx,by); c.lineTo(bxx-uy*hw, byy+ux*hw); c.lineTo(bxx+uy*hw, byy-ux*hw); c.closePath(); c.fill(); c.stroke();
      }
    } else {
      const x=Math.min(ax,bx), y=Math.min(ay,by), rw=Math.abs(bx-ax), rh=Math.abs(by-ay);
      if (k==='rect') c.rect(x,y,rw,rh); else c.ellipse(x+rw/2,y+rh/2,rw/2,rh/2,0,0,Math.PI*2);
      c.stroke();
    }
  } else if (k==='text') {
    const fs = it.fs*s; c.font = fontOf(it.f, fs); c.textBaseline = 'alphabetic'; c.globalAlpha = fade;
    String(it.txt).split('\n').forEach((ln,i) => c.fillText(ln, it.x*W, it.y*H + fs*.92 + i*fs*1.2));
  } else if (k==='img') {
    if (it._img && it._img.complete && it._img.naturalWidth) c.drawImage(it._img, it.x*W, it.y*H, it.w*W, it.h*H);
  }
  c.restore();
}
function isLive(it){ return it===cur || (drag && drag.set.has(it)); }
function toBase(it){ if (!baseDirty) appendList.push(it); needDraw = true; }
function render(){
  const W = cv.width, Hc = cv.height;
  if (baseDirty) {
    bctx.clearRect(0,0,W,Hc);
    if (page==='board') drawBoard(bctx, W, Hc);
    for (const it of items) if (!isLive(it) && !(editor && editor.it===it)) draw(bctx, it, W, Hc);
    baseDirty = false; appendList.length = 0;
  } else if (appendList.length) {
    for (const it of appendList) if (items.includes(it) && !isLive(it)) draw(bctx, it, W, Hc);
    appendList.length = 0;
  }
  ctx.clearRect(0,0,W,Hc);
  ctx.drawImage(base, 0, 0);
  if (drag) for (const it of items) if (drag.set.has(it)) draw(ctx, it, W, Hc);
  if (cur && cur.k!=='laser') draw(ctx, cur, W, Hc, 1, cur._pred);
  const now = performance.now();
  lasers = lasers.filter(l => !l._end || now - l._end < 1200);
  for (const l of lasers) {
    let f = 1; if (l._end) { const a = now - l._end - 600; if (a > 0) f = Math.max(0, 1 - a/600); }
    draw(ctx, l, W, Hc, f, l===cur ? l._pred : null);
  }
  drawMarq(W, Hc);
  drawSel(W, Hc);
  needDraw = lasers.length > 0;
}
function loop(){ if (needDraw || baseDirty || appendList.length) render(); flush(); requestAnimationFrame(loop); }

// ---------------- selection (normalized coords)
function bboxOf(it){
  let x0, y0, x1, y1, pad = 0;
  if (STROKE[it.k]) {
    x0 = y0 = Infinity; x1 = y1 = -Infinity;
    for (const p of it.pts) { if (p[0]<x0) x0=p[0]; if (p[0]>x1) x1=p[0]; if (p[1]<y0) y0=p[1]; if (p[1]>y1) y1=p[1]; }
    pad = it.w*(sw/1920)/2 + (it.k==='farrow' ? Math.max(14, it.w*3.2)*(sw/1920)/2 : 0);
  } else if (SHAPE[it.k]) {
    x0=Math.min(it.a[0],it.b[0]); x1=Math.max(it.a[0],it.b[0]); y0=Math.min(it.a[1],it.b[1]); y1=Math.max(it.a[1],it.b[1]);
    pad = it.w*(sw/1920)/2;
  } else if (it.k==='text') { x0=it.x; y0=it.y; x1=it.x+it.tw; y1=it.y+it.th; }
  else { x0=it.x; y0=it.y; x1=it.x+it.w; y1=it.y+it.h; }
  return {x0:x0-pad/sw, y0:y0-pad/sh, x1:x1+pad/sw, y1:y1+pad/sh};
}
function groupBox(list){
  let x0=Infinity, y0=Infinity, x1=-Infinity, y1=-Infinity;
  for (const it of list) { const b = bboxOf(it); x0=Math.min(x0,b.x0); y0=Math.min(y0,b.y0); x1=Math.max(x1,b.x1); y1=Math.max(y1,b.y1); }
  const p = 8;
  return {x:x0-p/sw, y:y0-p/sh, w:(x1-x0)+2*p/sw, h:(y1-y0)+2*p/sh};
}
function cornersOfAB(it){
  const x0=Math.min(it.a[0],it.b[0]), x1=Math.max(it.a[0],it.b[0]), y0=Math.min(it.a[1],it.b[1]), y1=Math.max(it.a[1],it.b[1]);
  return [[x0,y0],[x1,y0],[x1,y1],[x0,y1]];
}
const boxCorners = b => [[b.x,b.y],[b.x+b.w,b.y],[b.x+b.w,b.y+b.h],[b.x,b.y+b.h]];
function selKind(){
  if (sel.length===1 && (sel[0].k==='line' || sel[0].k==='arrow')) return 'ends';
  if (sel.length===1 && (sel[0].k==='rect' || sel[0].k==='ellipse')) return 'box';
  return 'group';
}
function handles(){
  const k = selKind(), it = sel[0];
  if (k==='ends') return [{x:it.a[0], y:it.a[1], m:'a'}, {x:it.b[0], y:it.b[1], m:'b'}];
  if (k==='box') return cornersOfAB(it).map((c,i) => ({x:c[0], y:c[1], m:'c'+i}));
  return boxCorners(groupBox(sel)).map((c,i) => ({x:c[0], y:c[1], m:'g'+i}));
}
function drawSel(W, Hc){
  sel = sel.filter(it => items.includes(it));
  if (!sel.length || S.tool!=='select') return;
  const d = devicePixelRatio||1;
  ctx.save(); ctx.strokeStyle='#0a84ff'; ctx.lineWidth=1.5*d;
  const ls = lassoOut && lassoOut.its.length===sel.length && sel.every(it => lassoOut.its.includes(it)) ? lassoOut : null;
  if (ls) strokeLasso(ls.pts, W, Hc, true);   // the loop you drew stays around what it picked
  else if (selKind()!=='ends') {
    const b = groupBox(sel); ctx.setLineDash([6*d,5*d]); ctx.strokeRect(b.x*W, b.y*Hc, b.w*W, b.h*Hc); ctx.setLineDash([]);
  }
  if (sel.length > 1 && !ls) {
    ctx.globalAlpha = .45;
    for (const it of sel) { const b = bboxOf(it); ctx.strokeRect(b.x0*W, b.y0*Hc, (b.x1-b.x0)*W, (b.y1-b.y0)*Hc); }
    ctx.globalAlpha = 1;
  }
  for (const h of handles()) {
    ctx.beginPath(); ctx.arc(h.x*W, h.y*Hc, 8*d, 0, Math.PI*2); ctx.fillStyle='#fff'; ctx.fill(); ctx.lineWidth=2*d; ctx.stroke();
  }
  ctx.restore();
}
function drawMarq(W, Hc){
  if (marq && marq.pts.length > 1) strokeLasso(marq.pts, W, Hc, false);   // open while drawing, like Noteful
}
// dashed blue lasso line; the ends are only joined once the pen lifts
function strokeLasso(pts, W, Hc, close){
  const d = devicePixelRatio||1;
  ctx.save(); ctx.beginPath();
  pts.forEach((q, i) => i ? ctx.lineTo(q[0]*W, q[1]*Hc) : ctx.moveTo(q[0]*W, q[1]*Hc));
  if (close) ctx.closePath();
  ctx.setLineDash([12*d,10*d]); ctx.lineWidth=2.5*d; ctx.lineCap='round'; ctx.lineJoin='round';
  ctx.strokeStyle='#3b82f6'; ctx.stroke(); ctx.restore();
}
function geomOf(it){
  if (STROKE[it.k]) return {pts: it.pts.map(p => p.slice()), w: it.w};
  if (SHAPE[it.k]) return {a: it.a.slice(), b: it.b.slice(), w: it.w};
  if (it.k==='text') return {x:it.x, y:it.y, fs:it.fs, tw:it.tw, th:it.th};
  return {x:it.x, y:it.y, w:it.w, h:it.h};
}
function styleOf(it){ return it.k==='text' ? {fs:it.fs, tw:it.tw, th:it.th} : {w:it.w}; }
function xfGeom(g, f, tx, ty){   // x' = f*x + tx, sizes * f  (same formula as the PC)
  const o = {};
  if (g.pts) o.pts = g.pts.map(q => [rnd(q[0]*f+tx), rnd(q[1]*f+ty)]);
  if (g.a) { o.a = [rnd(g.a[0]*f+tx), rnd(g.a[1]*f+ty)]; o.b = [rnd(g.b[0]*f+tx), rnd(g.b[1]*f+ty)]; }
  if (g.x !== undefined) { o.x = rnd(g.x*f+tx); o.y = rnd(g.y*f+ty); }
  for (const k of ['w','h','fs','tw','th']) if (g[k] !== undefined) o[k] = g[k]*f;
  return o;
}
// lasso: a freehand loop; the ends are joined when the pen lifts
function inPoly(q, P){
  let c = false;
  for (let i = 0, j = P.length-1; i < P.length; j = i++) {
    const a = P[i], b = P[j];
    if ((a[1] > q[1]) !== (b[1] > q[1]) && q[0] < (b[0]-a[0])*(q[1]-a[1])/(b[1]-a[1]) + a[0]) c = !c;
  }
  return c;
}
function keyPts(it){
  if (STROKE[it.k]) { const step = Math.max(1, Math.floor(it.pts.length/40)); return it.pts.filter((_,i) => i%step===0); }
  if (SHAPE[it.k]) return [it.a, it.b, [(it.a[0]+it.b[0])/2, (it.a[1]+it.b[1])/2]];
  const b = bboxOf(it); return [[b.x0,b.y0],[b.x1,b.y0],[b.x1,b.y1],[b.x0,b.y1],[(b.x0+b.x1)/2,(b.y0+b.y1)/2]];
}
function lassoSelect(poly){
  if (poly.length < 3) return [];
  const inside = q => inPoly(q, poly);
  return items.filter(it => {
    if (it.k==='text' || it.k==='img') { const b = bboxOf(it); return inside([(b.x0+b.x1)/2, (b.y0+b.y1)/2]); }
    const P = keyPts(it); let n = 0;
    for (const q of P) if (inside(q)) n++;
    return n >= Math.max(1, P.length*0.5);   // at least half of a line/shape must be inside the loop
  });
}
function hitSelect(p){
  const x = p[0]*sw, y = p[1]*sh;
  if (sel.length) {
    for (const h of handles()) if (Math.hypot(h.x*sw-x, h.y*sh-y) < 24) return {mode:h.m};
    if (selKind()==='ends') { if (hits(sel[0], x, y, 16)) return {mode:'move'}; }
    else { const b = groupBox(sel); if (x>=b.x*sw && x<=(b.x+b.w)*sw && y>=b.y*sh && y<=(b.y+b.h)*sh) return {mode:'move'}; }
  }
  for (let i = items.length-1; i >= 0; i--) if (hits(items[i], x, y, 14)) return {it:items[i], mode:'move'};
  return null;
}
function startDrag(mode, p){
  drag = {mode, p0:p, set:new Set(sel), o:new Map(sel.map(it => [it, geomOf(it)])), b0:groupBox(sel),
          lp: lassoOut && lassoOut.pts.map(q => q.slice())};
  send({t:'grab', ids: sel.map(it => it.id)}); baseDirty = true;
}

// ---------------- network
let ws = null, firstHello = true, outq = [], pend = null, connected = false;
function send(op){ commit(); outq.push(op); }
function commit(){
  if (!pend) return;
  if (pend.pts) { if (pend.pts.length) outq.push({t:'pts', id:pend.id, pts:pend.pts}); }
  else outq.push(pend.op);
  pend = null;
}
function flush(){
  commit();
  if (!outq.length || !connected) return;
  const msg = outq.length===1 ? outq[0] : {t:'b', ops:outq};
  try { ws.send(JSON.stringify(msg, strip)); outq = []; } catch(e) {}
}
let lastReq = 0, frameWaiting = false, frameUrl = null;
function reqFrame(){
  if (!S.live || !connected || frameWaiting || page!=='screen') return;
  // while drawing, the preview slows right down so the pen gets all the WiFi
  const gap = pid !== null ? 400 : 70;
  const wait = Math.max(0, gap - (performance.now() - lastReq));
  frameWaiting = true;
  setTimeout(() => {
    if (pid !== null && performance.now() - lastReq < 400) { frameWaiting = false; reqFrame(); return; }
    lastReq = performance.now();
    try { ws.send('{"t":"f"}'); } catch(e){ frameWaiting = false; }
  }, wait);
}
function updBg(){ bg.style.visibility = (page==='screen' && S.live && haveFrame) ? 'visible' : 'hidden'; }
bg.onload = () => { haveFrame = true; updBg(); frameWaiting = false; reqFrame(); };
bg.onerror = () => { frameWaiting = false; reqFrame(); };
function loadImg(it){ if (it.k==='img' && !it._img) { it._img = new Image(); it._img.onload = () => { baseDirty = true; }; it._img.src = it.src; } return it; }
function setStatus(ok, txt){ $('led').classList.toggle('ok', ok); $('stxt').textContent = txt; }
function runCmd(m){
  const c = m.c;
  if (c==='undo') undo();
  else if (c==='redo') redo();
  else if (c==='clear') clearAll();
  else if (c==='laser') setTool(S.tool==='laser' ? (prevTool==='laser' ? 'pen' : prevTool) : 'laser');
  else if (c==='board') setPage(page==='screen' ? 'board' : 'screen');
  else if (c==='bg') cycleBg();
  else if (c==='img' && m.src) addImageSrc(m.src);
}
function connect(){
  const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
  ws = new WebSocket(proto + location.host + '/ws');
  ws.binaryType = 'blob';
  ws.onopen = () => { connected = true; setStatus(true, 'Live'); flush(); };
  ws.onclose = () => { connected = false; frameWaiting = false; setStatus(false, 'Offline'); setTimeout(connect, 1000); };
  ws.onerror = () => {};
  ws.onmessage = ev => {
    if (typeof ev.data !== 'string') {
      const u = URL.createObjectURL(ev.data); bg.src = u;
      if (frameUrl) URL.revokeObjectURL(frameUrl); frameUrl = u; return;
    }
    const m = JSON.parse(ev.data);
    if (m.t === 'hello') {
      aspect = m.aspect || aspect; layout();
      authRequired = !!m.auth_required;
      mouseEnabled = (m.mouse_enabled !== false);
      if (authRequired && authToken) {
        sendNow({t:'auth', token: authToken});
      }
      if (firstHello) {
        const P = m.pages || {};
        pages = {screen:(P.screen||[]).map(loadImg), board:(P.board||[]).map(loadImg)};
        page = m.page==='board' ? 'board' : 'screen';
        if (BGS.includes(m.bg)) S.bg = m.bg;
        items = pages[page]; hist = HIST[page].h; redoS = HIST[page].r;
        firstHello = false;
      } else send({t:'sync', pages, page, bg:S.bg});
      baseDirty = needDraw = true; updBg(); ui(); reqFrame();
    } else if (m.t === 'auth_ok') {
      if (m.token) { authToken = m.token; localStorage.setItem('inkcast.auth_token', authToken); }
      $('pinmodal').style.display = 'none';
      toast('Unlocked host control');
      if (S.tool === 'trackpad') {
        layout();
        ui();
      }
    } else if (m.t === 'auth_err') {
      $('pinerr').textContent = m.msg || 'Authentication failed';
    } else if (m.t === 'mouse_state') {
      mouseEnabled = !!m.enabled;
      if (!mouseEnabled && S.tool === 'trackpad') toast('Host disabled remote mouse');
    } else if (m.t === 'cur') {
      tpNormX = m.x;
      tpNormY = m.y;
      if (S.tool === 'trackpad') updateCursorUI();
    } else if (m.t === 'cmd') runCmd(m);
    else if (m.t === 'toast') toast(m.msg);
  };
}

// ---------------- pages (screen / whiteboard)
function setPage(p){
  if (editor) commitEditor();
  if (cur) { send({t:'end', id:cur.id}); cur = null; }
  if (drag) { send({t:'end', ids:[...drag.set].map(it => it.id)}); drag = null; }
  erase = null; pid = null; sel = []; marq = null; lassoOut = null;
  const changed = p !== page;
  page = p; items = pages[p]; hist = HIST[p].h; redoS = HIST[p].r;
  send({t:'page', p, bg:S.bg});
  baseDirty = needDraw = true; updBg(); reqFrame(); ui();
  if (changed) toast(p==='board' ? 'Whiteboard' : 'Screen');
}
function setBoard(b){
  S.bg = b; save();
  if (page!=='board') setPage('board');
  else { send({t:'page', p:'board', bg:S.bg}); baseDirty = needDraw = true; ui(); }
}
function cycleBg(){ setBoard(BGS[(BGS.indexOf(S.bg)+1) % BGS.length]); }

// ---------------- text
const fsFor = () => 12 + S.size*3;
function measure(txt, fs, f){
  const px = fs*sw/1920, lines = String(txt).split('\n');
  mctx.font = fontOf(f, px);
  let w = 0; for (const l of lines) w = Math.max(w, mctx.measureText(l).width);
  return {tw: rnd(Math.max(w, 4)/sw), th: rnd(lines.length*px*1.2/sh)};
}
function placeEditor(){
  const fs = (editor.it ? editor.it.fs : fsFor())*sw/1920;
  Object.assign(ed.style, {left:(editor.p[0]*sw - 2)+'px', top:(editor.p[1]*sh - fs*.12)+'px', fontSize:fs+'px',
    font: fontOf(editor.f, fs), lineHeight:'1.2', color: editor.it ? editor.it.c : S.color});
  autosize();
}
function autosize(){
  ed.style.width = '20px'; ed.style.height = '10px';
  ed.style.width = (ed.scrollWidth + 6) + 'px'; ed.style.height = (ed.scrollHeight + 2) + 'px';
}
function openEditor(p, it){
  editor = {it: it||null, p: it ? [it.x, it.y] : p, f: it ? (it.f||'sans') : S.font};
  ed.value = it ? it.txt : '';
  ed.style.display = 'block'; placeEditor(); ed.focus();
  baseDirty = needDraw = true; ui();
}
ed.addEventListener('input', autosize);
ed.addEventListener('keydown', e => {
  if (e.key==='Enter' && !e.shiftKey) { e.preventDefault(); commitEditor(); }
  else if (e.key==='Escape') { editor = null; ed.style.display = 'none'; ed.blur(); baseDirty = needDraw = true; ui(); }
});
ed.addEventListener('blur', () => setTimeout(() => { if (editor && document.activeElement!==ed) commitEditor(); }, 0));
function commitEditor(){
  if (!editor) return;
  const e = editor, txt = ed.value.replace(/\s+$/, '');
  editor = null; ed.style.display = 'none'; ed.blur();
  if (e.it) {
    if (!txt) { const idx = items.indexOf(e.it); remove(e.it); pushHist({a:'del', its:[{it:e.it, idx}]}); }
    else if (txt !== e.it.txt || e.f !== (e.it.f||'sans')) {
      const m = measure(txt, e.it.fs, e.f), before = {txt:e.it.txt, f:e.it.f||'sans', tw:e.it.tw, th:e.it.th};
      const after = {txt, f:e.f, tw:m.tw, th:m.th};
      setFields(e.it, after); pushHist({a:'upd', its:[{it:e.it, before, after}]});
    }
  } else if (txt) {
    const fs = fsFor(), m = measure(txt, fs, e.f);
    const it = {id:uid(), k:'text', c:S.color, f:e.f, fs, x:e.p[0], y:e.p[1], txt, tw:m.tw, th:m.th};
    items.push(it); send({t:'add', item:it, done:true}); pushHist({a:'add', its:[it]}); toBase(it);
  }
  baseDirty = needDraw = true; ui();
}

// ---------------- input
function pt(e){
  const r = crect || cv.getBoundingClientRect();
  return [rnd(Math.min(1,Math.max(0,(e.clientX-r.left)/r.width))), rnd(Math.min(1,Math.max(0,(e.clientY-r.top)/r.height)))];
}
function accept(e){
  if (e.pointerType==='pen' && !S.penSeen) {
    S.penSeen = true;
    if (!S.fingersSet && S.fingers) { S.fingers = false; toast('Pencil detected: fingers won’t draw now (palm rejection). Tap “Fingers” to change.'); }
    save(); ui();
  }
  if (e.pointerType==='touch' && !S.fingers && !(S.tool === 'trackpad' && tpAction === 'ink')) return false;
  if (e.pointerType==='mouse' && e.button!==0) return false;
  return pid===null;
}
function pushHist(h){ hist.push(h); if (hist.length>300) hist.shift(); redoS.length = 0; ui(); }

cv.addEventListener('pointerdown', e => {
  const isTp = (S.tool === 'trackpad');
  if (isTp && tpAction !== 'ink' && e.pointerType !== 'pen') return;
  if (!accept(e)) return;
  e.preventDefault();
  if (editor) { commitEditor(); return; }
  pid = e.pointerId; crect = cv.getBoundingClientRect();
  try { cv.setPointerCapture(pid); } catch(_){}
  const p = pt(e), t = isTp ? (prevTool && prevTool!=='trackpad' ? prevTool : 'pen') : S.tool;
  if (STROKE[t]) {
    cur = {id:uid(), k:t, c:S.color, w:S.size*WMUL[t], pts:[p]};
    if (t==='laser') lasers.push(cur); else items.push(cur);
    send({t:'add', item:{id:cur.id, k:t, c:cur.c, w:cur.w, pts:[p]}});
  } else if (SHAPE[t]) {
    cur = {id:uid(), k:t, c:S.color, w:S.size, a:p, b:p};
    items.push(cur); send({t:'add', item:{id:cur.id, k:t, c:cur.c, w:cur.w, a:p, b:p}});
  } else if (t==='text') {
    downPt = p;
  } else if (t==='eraser') {
    erase = []; eraseAt(p);
  } else if (t==='select') {
    const hit = hitSelect(p);
    if (hit) {
      if (hit.it && !sel.includes(hit.it)) sel = [hit.it];
      startDrag(hit.mode, p);
    } else { sel = []; marq = {pts:[p]}; }
  }
  flush(); needDraw = true; ui();
});
cv.addEventListener('pointermove', e => {
  if (e.pointerId !== pid) return;
  const evs = (e.getCoalescedEvents && e.getCoalescedEvents().length) ? e.getCoalescedEvents() : [e];
  if (cur && STROKE[cur.k]) {
    if (!pend || pend.id !== cur.id || !pend.pts) { commit(); pend = {id:cur.id, pts:[]}; }
    for (const ev of evs) {
      const p = pt(ev), q = cur.pts[cur.pts.length-1];
      if (q && p[0]===q[0] && p[1]===q[1]) continue;
      cur.pts.push(p); pend.pts.push(p);
    }
    // predicted points make the ink feel glued to the pen tip (drawn locally only)
    cur._pred = e.getPredictedEvents ? e.getPredictedEvents().slice(0, 3).map(pt) : null;
    needDraw = true;
  } else if (cur && SHAPE[cur.k]) {
    cur.b = pt(e); commit(); pend = {op:{t:'upd', id:cur.id, b:cur.b}}; needDraw = true;
  } else if (erase) {
    for (const ev of evs) eraseAt(pt(ev));
  } else if (marq) {
    for (const ev of evs) {
      const p = pt(ev), q = marq.pts[marq.pts.length-1];
      if (p[0]!==q[0] || p[1]!==q[1]) marq.pts.push(p);
    }
    needDraw = true;
  } else if (drag) {
    const p = pt(e), m = drag.mode, dx = p[0]-drag.p0[0], dy = p[1]-drag.p0[1];
    if (m==='move' || m[0]==='g') {
      let f = 1, tx = dx, ty = dy;
      if (m[0]==='g') {
        const cs = boxCorners(drag.b0), i = +m[1], c = cs[i], an = cs[(i+2)%4];
        const d0 = Math.hypot((c[0]-an[0])*sw, (c[1]-an[1])*sh), d1 = Math.hypot((c[0]+dx-an[0])*sw, (c[1]+dy-an[1])*sh);
        f = Math.max(0.05, d0 ? d1/d0 : 1); tx = an[0]*(1-f); ty = an[1]*(1-f);
      }
      for (const it of drag.set) Object.assign(it, xfGeom(drag.o.get(it), f, tx, ty));
      if (lassoOut && drag.lp) lassoOut.pts = xfGeom({pts:drag.lp}, f, tx, ty).pts;   // the loop travels with them
      commit(); pend = {op:{t:'xf', ids:[...drag.set].map(it => it.id), f, tx, ty}};
    } else {
      lassoOut = null;   // reshaping one line/box: its own handles take over
      const it = sel[0], o = drag.o.get(it);
      let g;
      if (m==='a' || m==='b') { g = {a:o.a.slice(), b:o.b.slice()}; g[m] = [rnd(o[m][0]+dx), rnd(o[m][1]+dy)]; }
      else { const cs = cornersOfAB(o), i = +m[1], c = cs[i], opp = cs[(i+2)%4]; g = {a: opp.slice(), b: [rnd(c[0]+dx), rnd(c[1]+dy)]}; }
      Object.assign(it, g);
      commit(); pend = {op:Object.assign({t:'upd', id:it.id}, g)};
    }
    needDraw = true;
  }
  flush();   // send right away instead of waiting for the next frame
});
function endPointer(e){
  if (e.pointerId !== pid) return;
  pid = null; crect = null;
  if (cur) {
    const it = cur; cur = null; it._pred = null;
    if (it.k==='laser') { it._end = performance.now(); send({t:'end', id:it.id}); }
    else if (SHAPE[it.k] && Math.hypot((it.b[0]-it.a[0])*sw, (it.b[1]-it.a[1])*sh) < 4) {
      items.splice(items.indexOf(it), 1); send({t:'del', id:it.id});
    } else { send({t:'end', id:it.id}); pushHist({a:'add', its:[it]}); toBase(it); }
  } else if (downPt) {
    const p = downPt; downPt = null;
    if (S.tool==='text') {
      const x = p[0]*sw, y = p[1]*sh;
      let hit = null;
      for (let i = items.length-1; i >= 0; i--) if (items[i].k==='text' && hits(items[i], x, y, 6)) { hit = items[i]; break; }
      openEditor(p, hit);
    }
  } else if (erase) {
    if (erase.length) pushHist({a:'del', its:erase});
    erase = null;
  } else if (marq) {
    const m = marq; marq = null;
    let x0=Infinity, y0=Infinity, x1=-Infinity, y1=-Infinity;
    for (const q of m.pts) { x0=Math.min(x0,q[0]); x1=Math.max(x1,q[0]); y0=Math.min(y0,q[1]); y1=Math.max(y1,q[1]); }
    sel = Math.hypot((x1-x0)*sw, (y1-y0)*sh) > 12 ? lassoSelect(m.pts) : [];   // a tap or tiny scribble just clears
    lassoOut = sel.length ? {pts:m.pts, its:sel.slice()} : null;
  } else if (drag) {
    const d = drag; drag = null;
    send({t:'end', ids:[...d.set].map(it => it.id)});
    const its = [...d.set].map(it => ({it, before:d.o.get(it), after:geomOf(it)}))
      .filter(o => JSON.stringify(o.before) !== JSON.stringify(o.after));
    if (its.length) pushHist({a:'upd', its});
    baseDirty = true;
  }
  flush(); needDraw = true; ui(); reqFrame();
}
cv.addEventListener('pointerup', endPointer);
cv.addEventListener('pointercancel', endPointer);
document.addEventListener('touchmove', e => {
  if (!e.target.closest || !e.target.closest('#top,#side,#ed,#pastebox')) e.preventDefault();
}, {passive:false});
document.addEventListener('gesturestart', e => e.preventDefault());
document.addEventListener('contextmenu', e => { if (e.target!==ed && !e.target.closest('#pastebox')) e.preventDefault(); });
document.addEventListener('keydown', e => {
  if (editor || e.target===ed) return;
  const mod = e.ctrlKey || e.metaKey;
  if (mod && e.key.toLowerCase()==='z') { e.preventDefault(); e.shiftKey ? redo() : undo(); }
  else if (mod && e.key.toLowerCase()==='y') { e.preventDefault(); redo(); }
  else if ((e.key==='Delete' || e.key==='Backspace') && sel.length) { e.preventDefault(); deleteSel(); }
});

// ---------------- eraser / hit-testing (in stage pixels)
function segD(px,py,ax,ay,bx,by){
  const dx=bx-ax, dy=by-ay, l=dx*dx+dy*dy; let t = l ? ((px-ax)*dx+(py-ay)*dy)/l : 0;
  t = Math.max(0,Math.min(1,t)); return Math.hypot(px-ax-t*dx, py-ay-t*dy);
}
function hits(it, x, y, r){
  const s = sw/1920, w = (it.w||0)*s/2 + r;
  if (STROKE[it.k]) {
    const P = it.pts;
    if (P.length===1) return Math.hypot(P[0][0]*sw-x, P[0][1]*sh-y) < w;
    for (let i=1;i<P.length;i++) if (segD(x,y,P[i-1][0]*sw,P[i-1][1]*sh,P[i][0]*sw,P[i][1]*sh) < w) return true;
    return false;
  }
  if (SHAPE[it.k]) {
    const ax=it.a[0]*sw, ay=it.a[1]*sh, bx=it.b[0]*sw, by=it.b[1]*sh;
    if (it.k==='line' || it.k==='arrow') return segD(x,y,ax,ay,bx,by) < w;
    const x0=Math.min(ax,bx), x1=Math.max(ax,bx), y0=Math.min(ay,by), y1=Math.max(ay,by);
    if (it.k==='rect') return Math.min(segD(x,y,x0,y0,x1,y0),segD(x,y,x1,y0,x1,y1),segD(x,y,x1,y1,x0,y1),segD(x,y,x0,y1,x0,y0)) < w;
    const cx=(x0+x1)/2, cy=(y0+y1)/2, rx=Math.max(1,(x1-x0)/2), ry=Math.max(1,(y1-y0)/2);
    const v = Math.sqrt(((x-cx)/rx)**2 + ((y-cy)/ry)**2);
    return Math.abs(v-1)*Math.min(rx,ry) < w;
  }
  if (it.k==='text') return x>=it.x*sw-r && x<=(it.x+it.tw)*sw+r && y>=it.y*sh-r && y<=(it.y+it.th)*sh+r;
  if (it.k==='img') return x>=it.x*sw && x<=(it.x+it.w)*sw && y>=it.y*sh && y<=(it.y+it.h)*sh;
  return false;
}
function eraseAt(p){
  const x = p[0]*sw, y = p[1]*sh;
  for (let i = items.length-1; i >= 0; i--) {
    if (hits(items[i], x, y, 10)) {
      const it = items[i]; items.splice(i,1); erase.push({it, idx:i}); send({t:'del', id:it.id});
      sel = sel.filter(s => s!==it); baseDirty = true;
    }
  }
}

// ---------------- edits, undo / redo / clear
function setFields(it, r){
  const g = clone(r); Object.assign(it, g);
  send(Object.assign({t:'upd', id:it.id}, g)); baseDirty = needDraw = true;
}
function applySize(it){
  if (it.k==='text') { const fs = fsFor(), m = measure(it.txt, fs, it.f); setFields(it, {fs, tw:m.tw, th:m.th}); }
  else if (STROKE[it.k]) setFields(it, {w: S.size*(WMUL[it.k]||1)});
  else if (SHAPE[it.k]) setFields(it, {w: S.size});
}
function readd(it){ items.push(it); send({t:'add', item:it, done:true}); toBase(it); }
function remove(it){
  const i = items.indexOf(it);
  if (i>=0) { items.splice(i,1); send({t:'del', id:it.id}); baseDirty = true; }
  sel = sel.filter(s => s!==it);
}
function deleteSel(){
  const its = sel.map(it => ({it, idx:items.indexOf(it)})).filter(o => o.idx >= 0);
  if (!its.length) { sel = []; ui(); return; }
  its.forEach(o => remove(o.it)); pushHist({a:'del', its}); sel = []; needDraw = true; ui();
}
function applyH(h, back){
  if (h.a==='add') (back ? h.its.forEach(remove) : h.its.forEach(readd));
  else if (h.a==='del' || h.a==='clear') {
    if (back) h.its.slice().sort((a,b)=>a.idx-b.idx).forEach(o => readd(o.it));
    else h.its.forEach(o => remove(o.it));
  } else if (h.a==='upd') h.its.forEach(o => setFields(o.it, back ? o.before : o.after));
  baseDirty = needDraw = true; ui();
}
function undo(){ if (editor) commitEditor(); const h = hist.pop(); if (h) { lassoOut = null; applyH(h, true); redoS.push(h); ui(); } }
function redo(){ const h = redoS.pop(); if (h) { lassoOut = null; applyH(h, false); hist.push(h); ui(); } }
function clearAll(){
  if (editor) commitEditor();
  if (!items.length) return;
  pushHist({a:'clear', its: items.map((it,idx)=>({it,idx}))});
  items.length = 0; sel = []; send({t:'clear'}); baseDirty = needDraw = true; ui();
}

// ---------------- images: picker, paste, drag & drop, PC clipboard
function addImageSrc(src){
  const im = new Image();
  im.onload = () => {
    let w = 0.3, h = w*sw*im.naturalHeight/(im.naturalWidth*sh);
    if (h > 0.6) { w *= 0.6/h; h = 0.6; }
    const it = {id:uid(), k:'img', src, x:rnd((1-w)/2), y:rnd((1-h)/2), w:rnd(w), h:rnd(h), c:'#000', _img:im};
    items.push(it); send({t:'add', item:it, done:true}); pushHist({a:'add', its:[it]}); toBase(it);
    setTool('select'); sel = [it]; needDraw = true; ui();
    toast('Image added: drag to move, corner dots to resize');
  };
  im.onerror = () => toast('Couldn’t read that image');
  im.src = src;
}
function addImageFile(f){
  const r = new FileReader();
  r.onload = () => {
    const im = new Image();
    im.onload = () => {
      const MAX = 1600, k = Math.min(1, MAX/Math.max(im.width, im.height));
      const c = document.createElement('canvas'); c.width = Math.round(im.width*k); c.height = Math.round(im.height*k);
      c.getContext('2d').drawImage(im, 0, 0, c.width, c.height);
      const png = /png|gif|webp|svg/i.test(f.type);
      addImageSrc(png ? c.toDataURL('image/png') : c.toDataURL('image/jpeg', 0.9));
    };
    im.onerror = () => toast('Couldn’t read that image');
    im.src = r.result;
  };
  r.readAsDataURL(f);
}
function sendNow(obj){ if (connected) { try { ws.send(JSON.stringify(obj)); return true; } catch(e){} } return false; }
function imageFromData(dt){
  if (!dt) return false;
  for (const f of (dt.files || [])) if (/^image\//.test(f.type)) { addImageFile(f); return true; }
  for (const item of (dt.items || [])) if (item.kind==='file' && /^image\//.test(item.type)) { const f = item.getAsFile(); if (f) { addImageFile(f); return true; } }
  const html = dt.getData && dt.getData('text/html');
  const m = html && html.match(/<img[^>]+src=["']([^"']+)["']/i);
  let url = m ? m[1].replace(/&amp;/g,'&') : '';
  if (!url) { const t = (dt.getData && (dt.getData('text/uri-list') || dt.getData('text/plain')) || '').trim().split('\n')[0]; if (/^(https?:|data:image\/)/.test(t)) url = t; }
  if (url) {
    if (url.startsWith('data:image/')) addImageSrc(url);
    else if (sendNow({t:'fetch', url})) toast('Loading image…');
    return true;
  }
  return false;
}
function openPaste(){ $('pastebox').classList.add('show'); const a = $('pastearea'); a.focus(); }
function closePaste(){ $('pastebox').classList.remove('show'); $('pastearea').blur(); }
$('pasteclose').onclick = closePaste;
document.addEventListener('paste', e => {
  if (e.target===ed) return;
  e.preventDefault();
  const ok = imageFromData(e.clipboardData);
  closePaste();
  if (!ok) toast('No image found. Long-press the image in your browser, choose Copy, then Paste here.');
});
document.addEventListener('dragover', e => e.preventDefault());
document.addEventListener('drop', e => { e.preventDefault(); if (!imageFromData(e.dataTransfer)) toast('Drop an image to add it'); });
$('file').onchange = e => { const f = e.target.files[0]; e.target.value = ''; if (f) addImageFile(f); };

if (document.fonts && document.fonts.load) {
  Promise.all(Object.keys(FONTS).map(f => document.fonts.load(fontOf(f, 20)).catch(() => null)))
    .then(() => { for (const p of Object.values(pages)) for (const it of p) if (it.k==='text') Object.assign(it, measure(it.txt, it.fs, it.f)); baseDirty = needDraw = true; });
}
layout(); ui(); connect(); requestAnimationFrame(loop);
</script></body></html>
"""

if __name__ == "__main__":
    main()
