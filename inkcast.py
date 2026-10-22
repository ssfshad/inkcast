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
import io
import json
import math
import os
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
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

APP = "InkCast"
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
        except Exception as e:
            if not self.grab_failed:
                print("Screen preview unavailable:", e)
                self.grab_failed = True

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
        self.clients.add(ws)
        print("Tablet connected:", request.remote)
        await ws.send_str(json.dumps({
            "t": "hello", "aspect": self.aspect, "page": self.page, "bg": self.bg,
            "pages": {n: list(v.values()) for n, v in self.pages.items()}}))
        try:
            async for m in ws:
                if m.type != web.WSMsgType.TEXT:
                    continue
                if m.data == '{"t":"f"}':
                    asyncio.ensure_future(self.send_frame(ws))
                    continue
                try:
                    d = json.loads(m.data)
                except ValueError:
                    continue
                if d.get("t") == "fetch":
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
    threading.Thread(target=server.run, daemon=True).start()

    url = f"http://{local_ip()}:{PORT}"

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

    menu = QMenu()
    info = QAction(f"Tablet address: {url}")
    info.setEnabled(False)
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
    cam_bat = os.path.join(os.path.dirname(os.path.abspath(__file__)), "camera", "InkCast Camera.bat")
    camera = QAction("Phone camera (USB)...")

    def start_camera():
        try:
            os.startfile(cam_bat)   # opens the launcher in its own window
        except (AttributeError, OSError) as e:
            tray.showMessage(APP, f"Couldn't start the phone camera: {e}")

    camera.triggered.connect(start_camera)
    camera.setVisible(os.path.isfile(cam_bat))
    quit_ = QAction(f"Quit {APP}")
    quit_.triggered.connect(app.quit)
    menu.addAction(info)
    menu.addSeparator()
    for a in (paste, clear, board, toggle):
        menu.addAction(a)
    menu.addSeparator()
    menu.addAction(camera)
    menu.addSeparator()
    menu.addAction(quit_)
    tray.setContextMenu(menu)
    tray.show()

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
    print("  On your tablet (iPad or Android), open the browser and go to:")
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
.sw{width:26px;height:26px;border-radius:50%;flex:none;border:2px solid #48484a;padding:0}
.sw.on{outline:3px solid #fff;outline-offset:2px}
#custom{width:26px;height:26px;border-radius:50%;border:2px solid #48484a;padding:0;overflow:hidden;flex:none;position:relative;
  background:conic-gradient(red,yellow,lime,cyan,blue,magenta,red)}
#custom.on{outline:3px solid #fff;outline-offset:2px}
#custom input{position:absolute;inset:-8px;width:50px;height:50px;opacity:0;border:0;padding:0}
#size{width:84px;flex:none;accent-color:var(--on)}
#dot{width:38px;height:44px;flex:none;display:flex;align-items:center;justify-content:center}
#fonts{display:flex;gap:2px;flex:none}
.fb{width:34px;height:38px;border-radius:9px;border:0;background:var(--btn);color:var(--fg);font-size:18px;flex:none;padding:0}
.fb.on{background:var(--on);color:#fff}
#bgSw{width:22px;height:22px;border-radius:6px;border:2px solid #636366;display:block}
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
</style></head><body>
<div id="side"></div>
<div id="top"></div>
<div id="stage"><img id="bg" alt=""><canvas id="cv"></canvas><textarea id="ed" rows="1" autocapitalize="sentences" spellcheck="false"></textarea></div>
<div id="hint"></div>
<div id="toast"></div>
<div id="pastebox"><div id="pastearea" contenteditable="true">Long-press here and tap <b>&nbsp;Paste&nbsp;</b><br>(or press Ctrl/⌘+V)</div>
  <button id="pasteclose">Cancel</button></div>
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
 hand:'<path d="M8 13V5a1.5 1.5 0 0 1 3 0v6M11 11V4a1.5 1.5 0 0 1 3 0v7M14 11V5.5a1.5 1.5 0 0 1 3 0V14a7 7 0 0 1-7 7h-1a6 6 0 0 1-5-3l-2-4a1.5 1.5 0 0 1 2.5-1.5L8 15"/>'
};
const svg = n => '<svg viewBox="0 0 24 24">'+I[n]+'</svg>';
const TOOLS = [['pen','Pen'],['marker','Marker'],['hl','Highlight'],['neon','Neon'],['laser','Laser'],'|',
  ['line','Line'],['arrow','Arrow'],['farrow','Curve'],['rect','Box'],['ellipse','Circle'],['text','Text'],'|',
  ['select','Select'],['eraser','Eraser'],['image','Image'],['paste','Paste']];
const COLORS = ['#ff3b30','#ff9500','#ffcc00','#34c759','#00c7ff','#0a84ff','#bf5af2','#ff2d92','#ffffff','#000000'];
const STROKE = {pen:1,marker:1,hl:1,neon:1,laser:1,farrow:1}, SHAPE = {line:1,arrow:1,rect:1,ellipse:1};
const WMUL = {pen:1,marker:2.2,hl:4,neon:1.2,laser:1.2,farrow:1};
const BGS = ['white','grid','black','green'];
const BGC = {white:'#ffffff', grid:'#ffffff', black:'#141517', green:'#1f3a2c'};
const FONTS = {
  sans:  'InkSans,-apple-system,Roboto,"Segoe UI",sans-serif',
  hand:  'InkHand,"Ink Free","Comic Sans MS","Chalkboard SE","Comic Neue",cursive',
  serif: 'InkSerif,Georgia,"Noto Serif",serif'
};
const fontOf = (f, px) => '700 ' + px + 'px ' + (FONTS[f] || FONTS.sans);

let S = {tool:'pen', color:'#ff3b30', size:6, fingers:true, fingersSet:false, live:true, bg:'white', font:'sans'};
try { Object.assign(S, JSON.parse(localStorage.getItem('inkcast.s')||'{}')); } catch(e){}
if (!TOOLS.some(t => t[0]===S.tool) || S.tool==='image' || S.tool==='paste') S.tool = 'pen';
if (!BGS.includes(S.bg)) S.bg = 'white';
if (!FONTS[S.font]) S.font = 'sans';
const save = () => { try { localStorage.setItem('inkcast.s', JSON.stringify(S)); } catch(e){} };

let pages = {screen:[], board:[]}, page = 'screen', items = pages.screen;
const HIST = {screen:{h:[], r:[]}, board:{h:[], r:[]}};
let hist = HIST.screen.h, redoS = HIST.screen.r;
let lasers = [], cur = null, pid = null, drag = null, erase = null, sel = [], lasso = null;
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
for (const c of COLORS) {
  const b = document.createElement('button'); b.className='sw'; b.style.background=c; b.dataset.c=c;
  b.onclick = () => setColor(c); topBar.appendChild(b);
}
const tbtn = (icon,label,id) => '<button class="tb" id="'+id+'">'+svg(icon)+'<span>'+label+'</span></button>';
topBar.insertAdjacentHTML('beforeend',
 '<label id="custom" title="Custom color"><input type="color" id="cpick"></label><div class="vsep"></div>'+
 '<div id="dot"></div><input type="range" id="size" min="1" max="40" step="1">'+
 '<div id="fonts"><div class="vsep"></div><button class="fb" data-f="sans">Aa</button><button class="fb" data-f="hand">Aa</button><button class="fb" data-f="serif">Aa</button></div>'+
 '<div class="vsep"></div>'+tbtn('undo','Undo','undo')+tbtn('redo','Redo','redo')+tbtn('trash','Clear','clear')+tbtn('del','Delete','delB')+
 '<div class="vsep"></div>'+tbtn('screen','Screen','screenB')+tbtn('board','Board','boardB')+
 '<button class="tb" id="bgB"><span id="bgSw"></span><span>BG</span></button>'+
 '<div class="vsep"></div>'+tbtn('eye','Preview','liveB')+tbtn('hand','Fingers','fingB')+
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
$('boardB').onclick = () => setPage('board');
$('bgB').onclick = () => cycleBg();
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
  if (t!=='laser') prevTool = t;
  S.tool = t; save(); ui(); needDraw = true;
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
  $('custom').classList.toggle('on', !COLORS.includes(S.color));
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
  $('bgSw').style.background = S.bg==='grid'
    ? 'linear-gradient(#b8bec6 1px,transparent 1px) 0 0/6px 6px,linear-gradient(90deg,#b8bec6 1px,transparent 1px) 0 0/6px 6px,#fff'
    : BGC[S.bg];
  let h = '';
  if (S.tool==='text') h = 'Tap where you want text · tap a text to edit it';
  else if (S.tool==='select') h = sel.length
    ? (sel.length>1 ? sel.length+' selected · ' : '') + 'drag to move · corner dots to resize · tap a color, size or font to change it'
    : 'Tap something, or draw a loop around things to select them';
  else if (S.tool==='farrow') h = 'Draw a curved line — the arrow head goes where you lift the pen';
  $('hint').textContent = h;
}

// ---------------- layout
function layout(){
  const L = 62, T = 54, W = innerWidth - L, Hh = innerHeight - T;
  let w = W, h = W/aspect; if (h > Hh) { h = Hh; w = Hh*aspect; }
  sw = w; sh = h;
  Object.assign(stage.style, {left:(L+(W-w)/2)+'px', top:(T+(Hh-h)/2)+'px', width:w+'px', height:h+'px'});
  const dpr = devicePixelRatio || 1;
  cv.width = base.width = Math.round(w*dpr); cv.height = base.height = Math.round(h*dpr);
  if (editor) placeEditor();
  baseDirty = needDraw = true;
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
  drawLasso(W, Hc);
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
  if (selKind()!=='ends') {
    const b = groupBox(sel); ctx.setLineDash([6*d,5*d]); ctx.strokeRect(b.x*W, b.y*Hc, b.w*W, b.h*Hc); ctx.setLineDash([]);
  }
  if (sel.length > 1) {
    ctx.globalAlpha = .45;
    for (const it of sel) { const b = bboxOf(it); ctx.strokeRect(b.x0*W, b.y0*Hc, (b.x1-b.x0)*W, (b.y1-b.y0)*Hc); }
    ctx.globalAlpha = 1;
  }
  for (const h of handles()) {
    ctx.beginPath(); ctx.arc(h.x*W, h.y*Hc, 8*d, 0, Math.PI*2); ctx.fillStyle='#fff'; ctx.fill(); ctx.lineWidth=2*d; ctx.stroke();
  }
  ctx.restore();
}
function drawLasso(W, Hc){
  if (!lasso || lasso.length < 2) return;
  const d = devicePixelRatio||1;
  ctx.save(); ctx.beginPath(); ctx.moveTo(lasso[0][0]*W, lasso[0][1]*Hc);
  for (const p of lasso) ctx.lineTo(p[0]*W, p[1]*Hc);
  ctx.closePath(); ctx.fillStyle='rgba(10,132,255,.10)'; ctx.fill();
  ctx.setLineDash([7*d,6*d]); ctx.lineWidth=2*d; ctx.strokeStyle='#0a84ff'; ctx.stroke(); ctx.restore();
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
function inPoly(x, y, P){
  let c = false;
  for (let i=0, j=P.length-1; i<P.length; j=i++) {
    const xi=P[i][0]*sw, yi=P[i][1]*sh, xj=P[j][0]*sw, yj=P[j][1]*sh;
    if (((yi>y)!==(yj>y)) && (x < (xj-xi)*(y-yi)/(yj-yi)+xi)) c = !c;
  }
  return c;
}
function keyPts(it){
  if (STROKE[it.k]) { const step = Math.max(1, Math.floor(it.pts.length/40)); return it.pts.filter((_,i) => i%step===0); }
  if (SHAPE[it.k]) return [it.a, it.b, [(it.a[0]+it.b[0])/2, (it.a[1]+it.b[1])/2]];
  const b = bboxOf(it); return [[b.x0,b.y0],[b.x1,b.y0],[b.x1,b.y1],[b.x0,b.y1],[(b.x0+b.x1)/2,(b.y0+b.y1)/2]];
}
function lassoSelect(L){
  return items.filter(it => {
    if (it.k==='text' || it.k==='img') { const b = bboxOf(it); return inPoly((b.x0+b.x1)/2*sw, (b.y0+b.y1)/2*sh, L); }
    const P = keyPts(it); let n = 0;
    for (const q of P) if (inPoly(q[0]*sw, q[1]*sh, L)) n++;
    return n >= Math.max(1, P.length*0.6);   // most of a line/shape must be inside the loop
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
  drag = {mode, p0:p, set:new Set(sel), o:new Map(sel.map(it => [it, geomOf(it)])), b0:groupBox(sel)};
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
  ws = new WebSocket('ws://' + location.host + '/ws');
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
      if (firstHello) {
        const P = m.pages || {};
        pages = {screen:(P.screen||[]).map(loadImg), board:(P.board||[]).map(loadImg)};
        page = m.page==='board' ? 'board' : 'screen';
        if (BGS.includes(m.bg)) S.bg = m.bg;
        items = pages[page]; hist = HIST[page].h; redoS = HIST[page].r;
        firstHello = false;
      } else send({t:'sync', pages, page, bg:S.bg});
      baseDirty = needDraw = true; updBg(); ui(); reqFrame();
    } else if (m.t === 'cmd') runCmd(m);
    else if (m.t === 'toast') toast(m.msg);
  };
}

// ---------------- pages (screen / whiteboard)
function setPage(p){
  if (editor) commitEditor();
  if (cur) { send({t:'end', id:cur.id}); cur = null; }
  if (drag) { send({t:'end', ids:[...drag.set].map(it => it.id)}); drag = null; }
  erase = null; pid = null; sel = []; lasso = null;
  const changed = p !== page;
  page = p; items = pages[p]; hist = HIST[p].h; redoS = HIST[p].r;
  send({t:'page', p, bg:S.bg});
  baseDirty = needDraw = true; updBg(); reqFrame(); ui();
  if (changed) toast(p==='board' ? 'Whiteboard' : 'Screen');
}
function cycleBg(){
  S.bg = BGS[(BGS.indexOf(S.bg)+1) % BGS.length]; save();
  if (page!=='board') setPage('board');
  else { send({t:'page', p:'board', bg:S.bg}); baseDirty = needDraw = true; ui(); }
}

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
  if (e.pointerType==='touch' && !S.fingers) return false;
  if (e.pointerType==='mouse' && e.button!==0) return false;
  return pid===null;
}
function pushHist(h){ hist.push(h); if (hist.length>300) hist.shift(); redoS.length = 0; ui(); }

cv.addEventListener('pointerdown', e => {
  if (!accept(e)) return;
  e.preventDefault();
  if (editor) { commitEditor(); return; }
  pid = e.pointerId; crect = cv.getBoundingClientRect();
  try { cv.setPointerCapture(pid); } catch(_){}
  const p = pt(e), t = S.tool;
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
    } else { sel = []; lasso = [p]; }
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
  } else if (lasso) {
    for (const ev of evs) lasso.push(pt(ev));
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
      commit(); pend = {op:{t:'xf', ids:[...drag.set].map(it => it.id), f, tx, ty}};
    } else {
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
  } else if (lasso) {
    const L = lasso; lasso = null;
    let len = 0; for (let i=1;i<L.length;i++) len += Math.hypot((L[i][0]-L[i-1][0])*sw, (L[i][1]-L[i-1][1])*sh);
    sel = len > 12 ? lassoSelect(L) : [];
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
function undo(){ if (editor) commitEditor(); const h = hist.pop(); if (h) { applyH(h, true); redoS.push(h); ui(); } }
function redo(){ const h = redoS.pop(); if (h) { applyH(h, false); hist.push(h); ui(); } }
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
