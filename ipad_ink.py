#!/usr/bin/env python3
"""
iPad Ink - draw live on your Windows screen from an iPad.

The PC shows an invisible, click-through ink layer on top of everything.
The iPad opens a web page (Safari) that shows a live view of the PC screen;
whatever you draw there with the pencil appears on the PC instantly.
Your mouse and keyboard keep working normally the whole time.

Run:  python ipad_ink.py      (or double-click "Start iPad Ink.bat")
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
from concurrent.futures import ThreadPoolExecutor

from aiohttp import web
from PySide6.QtCore import Qt, QObject, Signal, QPointF, QRectF, QTimer
from PySide6.QtGui import (QAction, QColor, QFont, QGuiApplication, QIcon, QImage, QPainter,
                           QPainterPath, QPen, QPixmap, QPolygonF)
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

PORT = int(os.environ.get("IPAD_INK_PORT", "8765"))
REF = 1920.0                      # sizes are defined relative to a 1920px-wide screen
STROKES = {"pen", "marker", "hl", "neon", "laser"}
SHAPES = {"line", "arrow", "rect", "ellipse"}
LASER_HOLD, LASER_FADE = 0.6, 0.6  # seconds a laser line stays, then fades
PREVIEW_WIDTH = 1280               # width of the screen preview sent to the iPad
PREVIEW_QUALITY = 55
BGS = ["white", "grid", "black", "green"]
BGC = {"white": "#ffffff", "grid": "#ffffff", "black": "#141517", "green": "#1f3a2c"}
TEXT_FONT = "Segoe UI"

# Global shortcuts: Ctrl + Alt + <key>
HOTKEYS = [
    ("Z", "undo", "Undo"),
    ("Y", "redo", "Redo"),
    ("C", "clear", "Clear ink"),
    ("H", "hide", "Hide / show ink"),
    ("L", "laser", "Laser pointer on / off"),
    ("W", "board", "Whiteboard on / off"),
    ("B", "bg", "Change whiteboard background"),
]


# --------------------------------------------------------------------------- overlay (PC side)

class Bridge(QObject):
    ops = Signal(str)       # raw JSON text from the iPad, parsed on the GUI thread
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
        self.live = set()   # ids currently being drawn / moved (painted live, not cached)
        self.lasers = {}    # id -> laser stroke (temporary)
        self.cache = None
        self.dirty = True
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(16)

    @property
    def items(self):
        return self.pages[self.page]

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
        self.update()

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

    def _apply(self, d):
        t = d.get("t")
        if t == "add":
            it = self._prep(dict(d["item"]))
            if it["k"] == "laser":
                self.lasers[it["id"]] = it
            else:
                self.items[it["id"]] = it
                if d.get("done"):
                    self.dirty = True
                else:
                    self.live.add(it["id"])
        elif t == "pts":
            it = self.items.get(d["id"]) or self.lasers.get(d["id"])
            if it:
                it["pts"].extend(d["pts"])
        elif t == "upd":
            it = self.items.get(d["id"])
            if it:
                for k, v in d.items():
                    if k not in ("t", "id"):
                        it[k] = v
                if "c" in d:
                    it["_c"] = QColor(d["c"])
                if d["id"] not in self.live:
                    self.dirty = True
        elif t == "grab":
            if d["id"] in self.items:
                self.live.add(d["id"])
                self.dirty = True
        elif t == "end":
            i = d["id"]
            if i in self.lasers:
                self.lasers[i]["_end"] = time.monotonic()
            if i in self.live:
                self.live.discard(i)
                self.dirty = True
        elif t == "del":
            if self.items.pop(d["id"], None) is not None:
                self.live.discard(d["id"])
                self.dirty = True
        elif t == "clear":
            self.items.clear(); self.live.clear(); self.lasers.clear()
            self.dirty = True
        elif t == "page":
            if d.get("p") in self.pages:
                self.page = d["p"]
            if d.get("bg") in BGC:
                self.bg = d["bg"]
            self.live.clear()
            self.dirty = True
        elif t == "sync":
            self.live.clear()
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

    # ---- painting
    def _tick(self):
        if not self.lasers:
            return
        now = time.monotonic()
        for i, it in list(self.lasers.items()):
            end = it["_end"]
            if (end is not None and now - end > LASER_HOLD + LASER_FADE) or now - it["_t0"] > 60:
                del self.lasers[i]
        self.update()

    def _rebuild(self):
        dpr = self.devicePixelRatioF()
        img = QImage(max(1, int(self.width() * dpr)), max(1, int(self.height() * dpr)),
                     QImage.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        for i, it in self.items.items():
            if i not in self.live:
                self._draw(p, it)
        p.end()
        self.cache = img
        self.dirty = False

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
        if self.dirty or self.cache is None:
            self._rebuild()
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
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
                hw = hl * 0.55
                base = QPointF(b.x() - ux * hl, b.y() - uy * hl)
                shaft_end = QPointF(b.x() - ux * hl * 0.8, b.y() - uy * hl * 0.8)
                p.drawLine(a, shaft_end)
                left = QPointF(base.x() - uy * hw, base.y() + ux * hw)
                right = QPointF(base.x() + uy * hw, base.y() - ux * hw)
                p.setPen(QPen(c, max(0.5, w * 0.5), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                p.setBrush(c)
                p.drawPolygon(QPolygonF([b, left, right]))
            else:
                r = QRectF(a, b).normalized()
                if k == "rect":
                    p.drawRect(r)
                else:
                    p.drawEllipse(r)
        elif k == "text":
            fs = max(1.0, float(it.get("fs", 30)) * s)
            f = QFont(TEXT_FONT)
            f.setPixelSize(max(1, int(round(fs))))
            f.setBold(True)
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
        self.pages = {"screen": {}, "board": {}}   # canonical items, so a reloaded iPad page gets them back
        self.page = "screen"
        self.bg = "white"
        self.clients = set()
        self.exec = ThreadPoolExecutor(max_workers=1)
        self.tl = threading.local()
        self.loop = None
        self.grab_failed = False

    # ---- state mirror
    def apply(self, d):
        t = d.get("t")
        items = self.pages[self.page]
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
                mw.CAPTUREBLT = 0   # keeps our own ink layer out of the preview (no double ink on iPad)
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

    # ---- http / websocket
    async def index(self, _request):
        return web.Response(text=PAGE, content_type="text/html",
                            headers={"Cache-Control": "no-store"})

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
        print("iPad connected:", request.remote)
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
                self.apply(d)
                self.bridge.ops.emit(m.data)
        finally:
            self.clients.discard(ws)
            print("iPad disconnected")
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
            # the iPad does the work, so it stays undoable and in sync
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
        app.router.add_get("/ws", self.ws_handler)
        runner = web.AppRunner(app, access_log=None)
        self.loop.run_until_complete(runner.setup())
        try:
            self.loop.run_until_complete(web.TCPSite(runner, "0.0.0.0", PORT).start())
        except OSError as e:
            print(f"\n!! Could not open port {PORT}: {e}\n   Is iPad Ink already running?")
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
    tray.setToolTip(f"iPad Ink - open {url} on your iPad")
    menu = QMenu()
    info = QAction(f"iPad address: {url}")
    info.setEnabled(False)
    clear = QAction("Clear ink\tCtrl+Alt+C")
    clear.triggered.connect(lambda: server.command("clear"))
    board = QAction("Whiteboard on / off\tCtrl+Alt+W")
    board.triggered.connect(lambda: server.command("board"))
    toggle = QAction("Hide ink\tCtrl+Alt+H")

    def toggle_ink():
        overlay.setVisible(not overlay.isVisible())
        toggle.setText(("Hide ink" if overlay.isVisible() else "Show ink") + "\tCtrl+Alt+H")

    toggle.triggered.connect(toggle_ink)
    quit_ = QAction("Quit iPad Ink")
    quit_.triggered.connect(app.quit)
    menu.addAction(info)
    menu.addSeparator()
    for a in (clear, board, toggle):
        menu.addAction(a)
    menu.addSeparator()
    menu.addAction(quit_)
    tray.setContextMenu(menu)
    tray.show()

    def on_hotkey(name):
        if name == "hide":
            toggle_ink()
        else:
            server.command(name)

    bridge.hotkey.connect(on_hotkey)
    failed = start_hotkeys(bridge.hotkey.emit)

    print("=" * 60)
    print("  iPad Ink is running")
    print()
    print("  On your iPad, open Safari and go to:")
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
    print("  iPad and PC must be on the same WiFi / network.")
    print("  If Windows asks about the firewall, click Allow.")
    print("  Close this window or press Ctrl+C to quit.")
    print("=" * 60)
    sys.exit(app.exec())


# --------------------------------------------------------------------------- iPad page

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<title>iPad Ink</title>
<style>
:root{--bar:#1c1c1e;--btn:#2c2c2e;--on:#0a84ff;--fg:#f2f2f7;--mut:#8e8e93}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;height:100%;background:#000;color:var(--fg);overflow:hidden;overscroll-behavior:none;
  touch-action:none;-webkit-user-select:none;user-select:none;-webkit-touch-callout:none;
  font:13px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
#side{position:fixed;left:0;top:0;bottom:0;width:62px;background:var(--bar);display:flex;flex-direction:column;
  align-items:center;gap:3px;padding:8px 0;overflow-y:auto;z-index:2;padding-left:env(safe-area-inset-left);touch-action:pan-y}
#top{position:fixed;left:62px;right:0;top:0;height:54px;background:var(--bar);display:flex;align-items:center;gap:5px;
  padding:0 10px;overflow-x:auto;z-index:2;touch-action:pan-x}
.tb{width:50px;min-height:46px;border-radius:10px;border:0;background:transparent;color:var(--fg);display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:1px;font-size:9px;padding:3px 0;flex:none}
.tb svg{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.tb.on{background:var(--on);color:#fff}
.tb:disabled{opacity:.3}
.sep{width:36px;height:1px;background:#3a3a3c;margin:3px 0;flex:none}
.vsep{width:1px;height:30px;background:#3a3a3c;margin:0 3px;flex:none}
.sw{width:30px;height:30px;border-radius:50%;flex:none;border:2px solid #48484a;padding:0}
.sw.on{outline:3px solid #fff;outline-offset:2px}
#custom{width:30px;height:30px;border-radius:50%;border:2px solid #48484a;padding:0;overflow:hidden;flex:none;position:relative;
  background:conic-gradient(red,yellow,lime,cyan,blue,magenta,red)}
#custom.on{outline:3px solid #fff;outline-offset:2px}
#custom input{position:absolute;inset:-8px;width:50px;height:50px;opacity:0;border:0;padding:0}
#size{width:100px;flex:none;accent-color:var(--on)}
#dot{width:40px;height:44px;flex:none;display:flex;align-items:center;justify-content:center}
#bgSw{width:22px;height:22px;border-radius:6px;border:2px solid #636366;display:block}
#status{margin-left:auto;display:flex;align-items:center;gap:6px;color:var(--mut);white-space:nowrap;flex:none;padding-left:8px}
#led{width:9px;height:9px;border-radius:50%;background:#ff453a}
#led.ok{background:#30d158}
#stage{position:fixed;background:#1a1a1a;overflow:hidden}
#bg,#cv{position:absolute;left:0;top:0;width:100%;height:100%}
#bg{display:block;visibility:hidden}
#ed{position:absolute;display:none;z-index:1;margin:0;padding:0 2px;border:1.5px dashed #0a84ff;border-radius:4px;outline:none;
  background:rgba(10,132,255,.08);resize:none;overflow:hidden;white-space:pre;line-height:1.2;font-weight:bold;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-user-select:text;user-select:text;touch-action:auto}
#hint{position:fixed;left:62px;right:0;bottom:16px;text-align:center;color:var(--mut);pointer-events:none;font-size:14px}
input[type=file]{display:none}
</style></head><body>
<div id="side"></div>
<div id="top"></div>
<div id="stage"><img id="bg" alt=""><canvas id="cv"></canvas><textarea id="ed" rows="1" autocapitalize="sentences" spellcheck="false"></textarea></div>
<div id="hint"></div>
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
 rect:'<rect x="4" y="6" width="16" height="12" rx="1"/>',
 ellipse:'<ellipse cx="12" cy="12" rx="9" ry="6.5"/>',
 text:'<path d="M5 7V4h14v3M12 4v16M9 20h6"/>',
 select:'<path d="M5 3l5.5 17 2.6-7.4L20.5 10z"/>',
 eraser:'<path d="M8 20h12"/><path d="M3.5 14.5l9-9a2 2 0 0 1 2.8 0l3.2 3.2a2 2 0 0 1 0 2.8L10 20H7.5z"/>',
 image:'<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 16l5-5 4 4 3-3 6 6"/><circle cx="15.5" cy="9" r="1.5"/>',
 undo:'<path d="M9 14L4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-3"/>',
 redo:'<path d="M15 14l5-5-5-5"/><path d="M20 9H10a6 6 0 0 0 0 12h3"/>',
 trash:'<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
 del:'<path d="M18 6L6 18M6 6l12 12"/>',
 eye:'<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
 board:'<rect x="3" y="4" width="18" height="13" rx="1"/><path d="M8 21l4-4 4 4"/>',
 hand:'<path d="M8 13V5a1.5 1.5 0 0 1 3 0v6M11 11V4a1.5 1.5 0 0 1 3 0v7M14 11V5.5a1.5 1.5 0 0 1 3 0V14a7 7 0 0 1-7 7h-1a6 6 0 0 1-5-3l-2-4a1.5 1.5 0 0 1 2.5-1.5L8 15"/>'
};
const svg = n => '<svg viewBox="0 0 24 24">'+I[n]+'</svg>';
const TOOLS = [['pen','Pen'],['marker','Marker'],['hl','Highlight'],['neon','Neon'],['laser','Laser'],'|',
  ['line','Line'],['arrow','Arrow'],['rect','Box'],['ellipse','Circle'],['text','Text'],'|',
  ['select','Select'],['eraser','Eraser'],['image','Image']];
const COLORS = ['#ff3b30','#ff9500','#ffcc00','#34c759','#00c7ff','#0a84ff','#bf5af2','#ff2d92','#ffffff','#000000'];
const STROKE = {pen:1,marker:1,hl:1,neon:1,laser:1}, SHAPE = {line:1,arrow:1,rect:1,ellipse:1};
const WMUL = {pen:1,marker:2.2,hl:4,neon:1.2,laser:1.2};
const BGS = ['white','grid','black','green'];
const BGC = {white:'#ffffff', grid:'#ffffff', black:'#141517', green:'#1f3a2c'};
const FONT = '-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif';

let S = {tool:'pen', color:'#ff3b30', size:6, fingers:false, live:true, bg:'white'};
try { Object.assign(S, JSON.parse(localStorage.getItem('ink.s')||'{}')); } catch(e){}
if (!TOOLS.some(t => t[0]===S.tool) || S.tool==='image') S.tool = 'pen';
if (!BGS.includes(S.bg)) S.bg = 'white';
const save = () => { try { localStorage.setItem('ink.s', JSON.stringify(S)); } catch(e){} };

let pages = {screen:[], board:[]}, page = 'screen', items = pages.screen;
const HIST = {screen:{h:[], r:[]}, board:{h:[], r:[]}};
let hist = HIST.screen.h, redoS = HIST.screen.r;
let lasers = [], cur = null, pid = null, drag = null, erase = null, sel = null;
let editor = null, downPt = null, sizeBefore = null, prevTool = 'pen';
let aspect = 16/9, sw = 1, sh = 1;
const $ = id => document.getElementById(id);
const stage = $('stage'), cv = $('cv'), ctx = cv.getContext('2d'), bg = $('bg'), ed = $('ed');
const base = document.createElement('canvas'), bctx = base.getContext('2d');
const mctx = document.createElement('canvas').getContext('2d');
let baseDirty = true, needDraw = true, haveFrame = false;
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
  b.onclick = () => { if (t[0]==='image') { $('file').click(); return; } setTool(t[0]); };
  side.appendChild(b);
}
for (const c of COLORS) {
  const b = document.createElement('button'); b.className='sw'; b.style.background=c; b.dataset.c=c;
  b.onclick = () => setColor(c); topBar.appendChild(b);
}
const tbtn = (icon,label,id) => '<button class="tb" id="'+id+'">'+svg(icon)+'<span>'+label+'</span></button>';
topBar.insertAdjacentHTML('beforeend',
 '<label id="custom" title="Custom color"><input type="color" id="cpick"></label><div class="vsep"></div>'+
 '<div id="dot"></div><input type="range" id="size" min="1" max="40" step="1"><div class="vsep"></div>'+
 tbtn('undo','Undo','undo')+tbtn('redo','Redo','redo')+tbtn('trash','Clear','clear')+tbtn('del','Delete','delB')+
 '<div class="vsep"></div>'+tbtn('eye','Screen','liveB')+tbtn('board','Board','boardB')+
 '<button class="tb" id="bgB"><span id="bgSw"></span><span>BG</span></button>'+tbtn('hand','Fingers','fingB')+
 '<div id="status"><span id="led"></span><span id="stxt">Connecting…</span></div>');
$('cpick').oninput = e => setColor(e.target.value);
$('size').value = S.size;
$('size').oninput = e => {
  S.size = +e.target.value; save();
  if (sel && S.tool==='select' && sel.k!=='img') { if (!sizeBefore) sizeBefore = styleOf(sel); applySize(sel); baseDirty = needDraw = true; }
  ui();
};
$('size').onchange = () => {
  if (sel && sizeBefore) pushHist({a:'upd', it:sel, before:sizeBefore, after:styleOf(sel)});
  sizeBefore = null;
};
$('undo').onclick = undo; $('redo').onclick = redo; $('clear').onclick = clearAll; $('delB').onclick = deleteSel;
$('liveB').onclick = () => { S.live = !S.live; save(); updBg(); ui(); reqFrame(); };
$('fingB').onclick = () => { S.fingers = !S.fingers; save(); ui(); };
$('boardB').onclick = () => togglePage();
$('bgB').onclick = () => cycleBg();

function setTool(t){
  if (editor) commitEditor();
  if (t!=='select') sel = null;
  if (t!=='laser') prevTool = t;
  S.tool = t; save(); ui(); needDraw = true;
}
function setColor(c){
  S.color = c; save();
  if (sel && S.tool==='select' && sel.k!=='img' && sel.c!==c) {
    const before = {c:sel.c}; setFields(sel, {c}); pushHist({a:'upd', it:sel, before, after:{c}});
  }
  if (editor) ed.style.color = c;
  ui();
}
function ui(){
  document.querySelectorAll('.tb[data-tool]').forEach(b => b.classList.toggle('on', b.dataset.tool===S.tool));
  document.querySelectorAll('.sw').forEach(b => b.classList.toggle('on', b.dataset.c===S.color));
  $('custom').classList.toggle('on', !COLORS.includes(S.color));
  const d = Math.max(3, Math.min(34, S.size*(WMUL[S.tool]||1)*0.9));
  $('dot').innerHTML = '<div style="width:'+d+'px;height:'+d+'px;border-radius:50%;background:'+S.color+
    (S.tool==='hl'?';opacity:.45':'')+'"></div>';
  $('undo').disabled = !hist.length; $('redo').disabled = !redoS.length;
  $('delB').style.display = (sel && S.tool==='select') ? '' : 'none';
  $('liveB').classList.toggle('on', S.live && page==='screen');
  $('boardB').classList.toggle('on', page==='board');
  $('fingB').classList.toggle('on', S.fingers);
  $('bgSw').style.background = S.bg==='grid'
    ? 'linear-gradient(#b8bec6 1px,transparent 1px) 0 0/6px 6px,linear-gradient(90deg,#b8bec6 1px,transparent 1px) 0 0/6px 6px,#fff'
    : BGC[S.bg];
  let h = '';
  if (S.tool==='text') h = 'Tap where you want text · tap a text to edit it';
  else if (S.tool==='select') h = sel ? 'Drag to move · handles to resize · tap a color or the size slider to change it' : 'Tap anything to select it';
  else if (!S.fingers && !items.length) h = 'Draw with your pencil · fingers are ignored (palm-safe)';
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
  const P = pts.map(p => [p[0]*W, p[1]*H]);
  c.moveTo(P[0][0], P[0][1]);
  if (P.length === 1) { c.lineTo(P[0][0]+0.01, P[0][1]); return; }
  if (P.length < 3) { for (let i=1;i<P.length;i++) c.lineTo(P[i][0],P[i][1]); return; }
  for (let i=1;i<P.length-1;i++) c.quadraticCurveTo(P[i][0],P[i][1],(P[i][0]+P[i+1][0])/2,(P[i][1]+P[i+1][1])/2);
  const l = P[P.length-1]; c.lineTo(l[0], l[1]);
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
function draw(c, it, W, H, fade){
  fade = fade===undefined ? 1 : fade;
  const s = W/1920, k = it.k, w = (it.w||0)*s;
  c.save(); c.lineJoin='round'; c.strokeStyle = it.c; c.fillStyle = it.c;
  if (STROKE[k]) {
    if (!it.pts.length) { c.restore(); return; }
    pathOf(c, it.pts, W, H);
    if (k==='neon' || k==='laser') {
      c.lineCap='round';
      for (const [m,a] of [[3,.12],[1.8,.25]]) { c.globalAlpha=a*fade; c.lineWidth=w*m; c.stroke(); }
      c.globalAlpha=fade; c.lineWidth=w; c.stroke();
      c.globalAlpha=.85*fade; c.strokeStyle='#fff'; c.lineWidth=w*.4; c.stroke();
    } else {
      c.lineCap = k==='hl' ? 'square' : 'round';
      c.globalAlpha = (k==='hl' ? .35 : 1)*fade; c.lineWidth = Math.max(.5,w); c.stroke();
    }
  } else if (SHAPE[k]) {
    const ax=it.a[0]*W, ay=it.a[1]*H, bx=it.b[0]*W, by=it.b[1]*H;
    c.lineCap='round'; c.lineWidth=Math.max(.5,w); c.beginPath();
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
    const fs = it.fs*s; c.font = 'bold '+fs+'px '+FONT; c.textBaseline = 'alphabetic'; c.globalAlpha = fade;
    String(it.txt).split('\n').forEach((ln,i) => c.fillText(ln, it.x*W, it.y*H + fs*.92 + i*fs*1.2));
  } else if (k==='img') {
    if (it._img && it._img.complete && it._img.naturalWidth) c.drawImage(it._img, it.x*W, it.y*H, it.w*W, it.h*H);
  }
  c.restore();
}
function isLive(it){ return (cur && it===cur) || (drag && it===drag.it); }
function render(){
  const W = cv.width, Hc = cv.height;
  if (baseDirty) {
    bctx.clearRect(0,0,W,Hc);
    if (page==='board') drawBoard(bctx, W, Hc);
    for (const it of items) if (!isLive(it) && !(editor && editor.it===it)) draw(bctx, it, W, Hc);
    baseDirty = false;
  }
  ctx.clearRect(0,0,W,Hc);
  ctx.drawImage(base, 0, 0);
  for (const it of items) if (isLive(it)) draw(ctx, it, W, Hc);
  const now = performance.now();
  lasers = lasers.filter(l => !l._end || now - l._end < 1200);
  for (const l of lasers) {
    let f = 1; if (l._end) { const a = now - l._end - 600; if (a > 0) f = Math.max(0, 1 - a/600); }
    draw(ctx, l, W, Hc, f);
  }
  drawSel(W, Hc);
  needDraw = lasers.length > 0;
}
function loop(){ if (needDraw || baseDirty) render(); flush(); requestAnimationFrame(loop); }

// ---------------- selection geometry (normalized coords)
function bboxOf(it){
  let x0, y0, x1, y1, pad = 0;
  if (STROKE[it.k]) {
    x0 = y0 = Infinity; x1 = y1 = -Infinity;
    for (const p of it.pts) { x0=Math.min(x0,p[0]); y0=Math.min(y0,p[1]); x1=Math.max(x1,p[0]); y1=Math.max(y1,p[1]); }
    pad = it.w*(sw/1920)/2;
  } else if (SHAPE[it.k]) {
    x0=Math.min(it.a[0],it.b[0]); x1=Math.max(it.a[0],it.b[0]); y0=Math.min(it.a[1],it.b[1]); y1=Math.max(it.a[1],it.b[1]);
    pad = it.w*(sw/1920)/2;
  } else if (it.k==='text') { x0=it.x; y0=it.y; x1=it.x+it.tw; y1=it.y+it.th; }
  else { x0=it.x; y0=it.y; x1=it.x+it.w; y1=it.y+it.h; }
  pad += 6;
  return {x:x0-pad/sw, y:y0-pad/sh, w:(x1-x0)+2*pad/sw, h:(y1-y0)+2*pad/sh};
}
function cornersOf(it){
  const x0=Math.min(it.a[0],it.b[0]), x1=Math.max(it.a[0],it.b[0]), y0=Math.min(it.a[1],it.b[1]), y1=Math.max(it.a[1],it.b[1]);
  return [[x0,y0],[x1,y0],[x1,y1],[x0,y1]];
}
function handlesOf(it){
  if (it.k==='line' || it.k==='arrow') return [{x:it.a[0], y:it.a[1], m:'a'}, {x:it.b[0], y:it.b[1], m:'b'}];
  if (it.k==='rect' || it.k==='ellipse') return cornersOf(it).map((c,i) => ({x:c[0], y:c[1], m:'c'+i}));
  const b = bboxOf(it); return [{x:b.x+b.w, y:b.y+b.h, m:'scale'}];
}
function drawSel(W, Hc){
  if (!sel || S.tool!=='select' || !items.includes(sel)) return;
  const d = devicePixelRatio||1;
  ctx.save(); ctx.strokeStyle='#0a84ff'; ctx.lineWidth=1.5*d;
  if (!(sel.k==='line' || sel.k==='arrow')) {
    const b = bboxOf(sel); ctx.setLineDash([6*d,5*d]); ctx.strokeRect(b.x*W, b.y*Hc, b.w*W, b.h*Hc); ctx.setLineDash([]);
  }
  for (const h of handlesOf(sel)) {
    ctx.beginPath(); ctx.arc(h.x*W, h.y*Hc, 8*d, 0, Math.PI*2); ctx.fillStyle='#fff'; ctx.fill(); ctx.lineWidth=2*d; ctx.stroke();
  }
  ctx.restore();
}
function geomOf(it){
  if (STROKE[it.k]) return {pts: it.pts.map(p => p.slice()), w: it.w};
  if (SHAPE[it.k]) return {a: it.a.slice(), b: it.b.slice()};
  if (it.k==='text') return {x:it.x, y:it.y, fs:it.fs, tw:it.tw, th:it.th};
  return {x:it.x, y:it.y, w:it.w, h:it.h};
}
function styleOf(it){ return it.k==='text' ? {fs:it.fs, tw:it.tw, th:it.th} : {w:it.w}; }
function translate(o, k, dx, dy){
  if (STROKE[k]) return {pts: o.pts.map(q => [rnd(q[0]+dx), rnd(q[1]+dy)])};
  if (SHAPE[k]) return {a:[rnd(o.a[0]+dx), rnd(o.a[1]+dy)], b:[rnd(o.b[0]+dx), rnd(o.b[1]+dy)]};
  return {x: rnd(o.x+dx), y: rnd(o.y+dy)};
}
function scaleG(o, k, b0, dx){
  const f = Math.max(0.1, (b0.w + dx) / b0.w), ox = b0.x, oy = b0.y;
  if (STROKE[k]) return {pts: o.pts.map(q => [rnd(ox+(q[0]-ox)*f), rnd(oy+(q[1]-oy)*f)]), w: Math.max(.5, o.w*f)};
  if (k==='text') return {fs: Math.max(4, o.fs*f), tw: o.tw*f, th: o.th*f};
  return {w: rnd(o.w*f), h: rnd(o.h*f)};
}
function hitSelect(p){
  const x = p[0]*sw, y = p[1]*sh;
  if (sel && items.includes(sel)) {
    for (const h of handlesOf(sel)) if (Math.hypot(h.x*sw-x, h.y*sh-y) < 22) return {it:sel, mode:h.m};
    const b = bboxOf(sel);
    if (sel.k!=='line' && sel.k!=='arrow' && x>=b.x*sw && x<=(b.x+b.w)*sw && y>=b.y*sh && y<=(b.y+b.h)*sh) return {it:sel, mode:'move'};
  }
  for (let i = items.length-1; i >= 0; i--) if (hits(items[i], x, y, 14)) return {it:items[i], mode:'move'};
  return null;
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
  const wait = Math.max(0, 70 - (performance.now() - lastReq));
  frameWaiting = true;
  setTimeout(() => { lastReq = performance.now(); try { ws.send('{"t":"f"}'); } catch(e){ frameWaiting=false; } }, wait);
}
function updBg(){ bg.style.visibility = (page==='screen' && S.live && haveFrame) ? 'visible' : 'hidden'; }
bg.onload = () => { haveFrame = true; updBg(); frameWaiting = false; reqFrame(); };
bg.onerror = () => { frameWaiting = false; reqFrame(); };
function loadImg(it){ if (it.k==='img' && !it._img) { it._img = new Image(); it._img.onload = () => { baseDirty = true; }; it._img.src = it.src; } return it; }
function setStatus(ok, txt){ $('led').classList.toggle('ok', ok); $('stxt').textContent = txt; }
function runCmd(c){
  if (c==='undo') undo();
  else if (c==='redo') redo();
  else if (c==='clear') clearAll();
  else if (c==='laser') setTool(S.tool==='laser' ? (prevTool==='laser' ? 'pen' : prevTool) : 'laser');
  else if (c==='board') togglePage();
  else if (c==='bg') cycleBg();
}
function connect(){
  ws = new WebSocket('ws://' + location.host + '/ws');
  ws.binaryType = 'blob';
  ws.onopen = () => { connected = true; setStatus(true, 'Connected'); };
  ws.onclose = () => { connected = false; frameWaiting = false; setStatus(false, 'Reconnecting…'); setTimeout(connect, 1000); };
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
    } else if (m.t === 'cmd') runCmd(m.c);
    else if (m.t === 'clear') { items.length = 0; hist.length = 0; redoS.length = 0; sel = null; baseDirty = true; ui(); }
  };
}

// ---------------- pages (screen / whiteboard)
function setPage(p){
  if (editor) commitEditor();
  if (cur) { send({t:'end', id:cur.id}); cur = null; }
  if (drag) { send({t:'end', id:drag.it.id}); drag = null; }
  erase = null; pid = null; sel = null;
  page = p; items = pages[p]; hist = HIST[p].h; redoS = HIST[p].r;
  send({t:'page', p, bg:S.bg});
  baseDirty = needDraw = true; updBg(); reqFrame(); ui();
}
function togglePage(){ setPage(page==='screen' ? 'board' : 'screen'); }
function cycleBg(){
  S.bg = BGS[(BGS.indexOf(S.bg)+1) % BGS.length]; save();
  if (page!=='board') setPage('board');
  else { send({t:'page', p:'board', bg:S.bg}); baseDirty = needDraw = true; ui(); }
}

// ---------------- text
const fsFor = () => 12 + S.size*3;
function measure(txt, fs){
  const px = fs*sw/1920, lines = String(txt).split('\n');
  mctx.font = 'bold '+px+'px '+FONT;
  let w = 0; for (const l of lines) w = Math.max(w, mctx.measureText(l).width);
  return {tw: rnd(Math.max(w, 4)/sw), th: rnd(lines.length*px*1.2/sh)};
}
function placeEditor(){
  const fs = (editor.it ? editor.it.fs : fsFor())*sw/1920;
  Object.assign(ed.style, {left:(editor.p[0]*sw - 2)+'px', top:(editor.p[1]*sh - fs*.12)+'px', fontSize:fs+'px',
    color: editor.it ? editor.it.c : S.color});
  autosize();
}
function autosize(){
  ed.style.width = '20px'; ed.style.height = '10px';
  ed.style.width = (ed.scrollWidth + 6) + 'px'; ed.style.height = (ed.scrollHeight + 2) + 'px';
}
function openEditor(p, it){
  editor = {it: it||null, p: it ? [it.x, it.y] : p};
  ed.value = it ? it.txt : '';
  ed.style.display = 'block'; placeEditor(); ed.focus();
  baseDirty = needDraw = true;
}
ed.addEventListener('input', autosize);
ed.addEventListener('keydown', e => {
  if (e.key==='Enter' && !e.shiftKey) { e.preventDefault(); commitEditor(); }
  else if (e.key==='Escape') { editor = null; ed.style.display = 'none'; ed.blur(); baseDirty = needDraw = true; }
});
ed.addEventListener('blur', () => commitEditor());
function commitEditor(){
  if (!editor) return;
  const e = editor, txt = ed.value.replace(/\s+$/, '');
  editor = null; ed.style.display = 'none'; ed.blur();
  if (e.it) {
    if (!txt) { const idx = items.indexOf(e.it); remove(e.it); pushHist({a:'del', its:[{it:e.it, idx}]}); }
    else if (txt !== e.it.txt) {
      const m = measure(txt, e.it.fs), before = {txt:e.it.txt, tw:e.it.tw, th:e.it.th}, after = {txt, tw:m.tw, th:m.th};
      setFields(e.it, after); pushHist({a:'upd', it:e.it, before, after});
    }
  } else if (txt) {
    const fs = fsFor(), m = measure(txt, fs);
    const it = {id:uid(), k:'text', c:S.color, fs, x:e.p[0], y:e.p[1], txt, tw:m.tw, th:m.th};
    items.push(it); send({t:'add', item:it, done:true}); pushHist({a:'add', its:[it]});
  }
  baseDirty = needDraw = true; ui();
}

// ---------------- input
function pt(e){
  const r = cv.getBoundingClientRect();
  return [rnd(Math.min(1,Math.max(0,(e.clientX-r.left)/r.width))), rnd(Math.min(1,Math.max(0,(e.clientY-r.top)/r.height)))];
}
function accept(e){
  if (e.pointerType==='touch' && !S.fingers) return false;
  if (e.pointerType==='mouse' && e.button!==0) return false;
  return pid===null;
}
function pushHist(h){ hist.push(h); if (hist.length>200) hist.shift(); redoS.length = 0; ui(); }

cv.addEventListener('pointerdown', e => {
  if (!accept(e)) return;
  e.preventDefault();
  if (editor) { commitEditor(); return; }
  pid = e.pointerId; try { cv.setPointerCapture(pid); } catch(_){}
  const p = pt(e), t = S.tool;
  if (STROKE[t]) {
    cur = {id:uid(), k:t, c:S.color, w:S.size*WMUL[t], pts:[p]};
    if (t==='laser') lasers.push(cur); else items.push(cur);
    send({t:'add', item:{id:cur.id, k:t, c:cur.c, w:cur.w, pts:[p]}});
    if (t!=='laser') baseDirty = true;
  } else if (SHAPE[t]) {
    cur = {id:uid(), k:t, c:S.color, w:S.size, a:p, b:p};
    items.push(cur); send({t:'add', item:{id:cur.id, k:t, c:cur.c, w:cur.w, a:p, b:p}}); baseDirty = true;
  } else if (t==='text') {
    downPt = p;
  } else if (t==='eraser') {
    erase = []; eraseAt(p);
  } else if (t==='select') {
    const hit = hitSelect(p);
    sel = hit ? hit.it : null;
    if (hit) {
      drag = {it:hit.it, mode:hit.mode, p0:p, o:geomOf(hit.it), b0:bboxOf(hit.it)};
      send({t:'grab', id:hit.it.id}); baseDirty = true;
    }
  }
  needDraw = true; ui();
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
    needDraw = true;
  } else if (cur && SHAPE[cur.k]) {
    cur.b = pt(e); commit(); pend = {op:{t:'upd', id:cur.id, b:cur.b}}; needDraw = true;
  } else if (erase) {
    for (const ev of evs) eraseAt(pt(ev));
  } else if (drag) {
    const p = pt(e), it = drag.it, o = drag.o, m = drag.mode, dx = p[0]-drag.p0[0], dy = p[1]-drag.p0[1];
    let g;
    if (m==='move') g = translate(o, it.k, dx, dy);
    else if (m==='a' || m==='b') { g = {a:o.a.slice(), b:o.b.slice()}; g[m] = [rnd(o[m][0]+dx), rnd(o[m][1]+dy)]; }
    else if (m[0]==='c') {
      const cs = cornersOf(o), i = +m[1], c = cs[i], opp = cs[(i+2)%4];
      g = {a: opp.slice(), b: [rnd(c[0]+dx), rnd(c[1]+dy)]};
    } else g = scaleG(o, it.k, drag.b0, dx);
    Object.assign(it, g);
    commit(); pend = {op:Object.assign({t:'upd', id:it.id}, g)}; needDraw = true;
  }
});
function endPointer(e){
  if (e.pointerId !== pid) return;
  pid = null;
  if (cur) {
    const it = cur; cur = null;
    if (it.k==='laser') { it._end = performance.now(); send({t:'end', id:it.id}); }
    else if (SHAPE[it.k] && Math.hypot((it.b[0]-it.a[0])*sw, (it.b[1]-it.a[1])*sh) < 4) {
      items.splice(items.indexOf(it), 1); send({t:'del', id:it.id});
    } else { send({t:'end', id:it.id}); pushHist({a:'add', its:[it]}); }
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
  } else if (drag) {
    const it = drag.it, o = drag.o; drag = null;
    send({t:'end', id:it.id});
    const after = geomOf(it);
    if (JSON.stringify(after) !== JSON.stringify(o)) pushHist({a:'upd', it, before:o, after});
  }
  baseDirty = needDraw = true; ui();
}
cv.addEventListener('pointerup', endPointer);
cv.addEventListener('pointercancel', endPointer);
document.addEventListener('touchmove', e => {
  if (!e.target.closest || !e.target.closest('#top,#side,#ed')) e.preventDefault();
}, {passive:false});
document.addEventListener('gesturestart', e => e.preventDefault());
document.addEventListener('contextmenu', e => { if (e.target!==ed) e.preventDefault(); });

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
      if (sel===it) sel = null; baseDirty = true;
    }
  }
}

// ---------------- edits, undo / redo / clear
function setFields(it, r){
  const g = clone(r); Object.assign(it, g);
  send(Object.assign({t:'upd', id:it.id}, g)); baseDirty = needDraw = true;
}
function applySize(it){
  if (it.k==='text') { const fs = fsFor(), m = measure(it.txt, fs); setFields(it, {fs, tw:m.tw, th:m.th}); }
  else if (STROKE[it.k]) setFields(it, {w: S.size*(WMUL[it.k]||1)});
  else if (SHAPE[it.k]) setFields(it, {w: S.size});
}
function readd(it){ items.push(it); send({t:'add', item:it, done:true}); }
function remove(it){ const i = items.indexOf(it); if (i>=0) { items.splice(i,1); send({t:'del', id:it.id}); } if (sel===it) sel=null; }
function deleteSel(){
  if (!sel) return;
  const it = sel, idx = items.indexOf(it);
  if (idx < 0) { sel = null; ui(); return; }
  remove(it); pushHist({a:'del', its:[{it, idx}]}); baseDirty = needDraw = true; ui();
}
function applyH(h, back){
  if (h.a==='add') (back ? h.its.forEach(remove) : h.its.forEach(readd));
  else if (h.a==='del' || h.a==='clear') {
    if (back) h.its.slice().sort((a,b)=>a.idx-b.idx).forEach(o => readd(o.it));
    else h.its.forEach(o => remove(o.it));
  } else if (h.a==='upd') setFields(h.it, back ? h.before : h.after);
  baseDirty = needDraw = true; ui();
}
function undo(){ if (editor) commitEditor(); const h = hist.pop(); if (h) { applyH(h, true); redoS.push(h); ui(); } }
function redo(){ const h = redoS.pop(); if (h) { applyH(h, false); hist.push(h); ui(); } }
function clearAll(){
  if (editor) commitEditor();
  if (!items.length) return;
  pushHist({a:'clear', its: items.map((it,idx)=>({it,idx}))});
  items.length = 0; sel = null; send({t:'clear'}); baseDirty = needDraw = true; ui();
}

// ---------------- image input
$('file').onchange = e => {
  const f = e.target.files[0]; e.target.value = ''; if (!f) return;
  const r = new FileReader();
  r.onload = () => {
    const im = new Image();
    im.onload = () => {
      const MAX = 1600, k = Math.min(1, MAX/Math.max(im.width, im.height));
      const c = document.createElement('canvas'); c.width = Math.round(im.width*k); c.height = Math.round(im.height*k);
      c.getContext('2d').drawImage(im, 0, 0, c.width, c.height);
      const png = /png|gif|webp|svg/i.test(f.type);
      const src = png ? c.toDataURL('image/png') : c.toDataURL('image/jpeg', 0.9);
      let w = 0.3, h = w*sw*c.height/(c.width*sh);
      if (h > 0.6) { w *= 0.6/h; h = 0.6; }
      const it = loadImg({id:uid(), k:'img', src, x:rnd((1-w)/2), y:rnd((1-h)/2), w:rnd(w), h:rnd(h), c:'#000'});
      items.push(it); send({t:'add', item:it, done:true}); pushHist({a:'add', its:[it]});
      setTool('select'); sel = it; baseDirty = needDraw = true; ui();
    };
    im.src = r.result;
  };
  r.readAsDataURL(f);
};

layout(); ui(); connect(); requestAnimationFrame(loop);
</script></body></html>
"""

if __name__ == "__main__":
    main()
