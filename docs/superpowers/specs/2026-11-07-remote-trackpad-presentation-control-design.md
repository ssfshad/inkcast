# Design Spec: Remote Trackpad & Presentation Controller for InkCast

**Date:** 2026-11-07  
**Status:** Approved  
**Author:** Pair Programming (User & Antigravity)  

---

## 1. Executive Summary & Goals

InkCast enables wireless screen annotation and digital whiteboard drawing from tablets/smartphones to a Windows host PC. During presentations and classroom teaching, presenters often step away from their desk and need to interact with slides, click hyperlinks, navigate desktop applications, or scroll documents without returning to the PC mouse or carrying a separate presenter clicker.

This specification introduces a **Remote Trackpad & Presentation Controller** mode directly into InkCast. It allows tablets and smartphones to serve as a low-latency remote mousepad and presentation remote while maintaining strict security via host-configured PIN authentication.

### Key Goals:
1. **Hybrid Mouse Control:** Support both *Relative Trackpad* (laptop touchpad gesture style) and *Direct Touch* (absolute screen coordinate mapping).
2. **Dedicated Presentation Controls:** Integrated `Prev` and `Next` buttons for seamless slide progression (PowerPoint, Google Slides, PDF, Canva).
3. **Landscape Optimization:** A semi-transparent floating Heads-Up Display (HUD) with a one-tap minimize pill (`[ 🖱️ ]`) ensuring zero occlusion of 16:9 fullscreen presentation content.
4. **Zero-Dependency Native Input:** Use Windows `ctypes.windll.user32.SendInput` directly on the host to avoid third-party Python dependencies and ensure instantaneous response.
5. **Robust PIN Security & Master Killswitch:** Host-defined static PIN in the Windows System Tray menu with a host-side enable/disable toggle. Only authenticated sessions can control the host cursor, while screen drawing and viewing remain open and unrestricted.

---

## 2. Architecture & Subsystems

```mermaid
graph TD
    subgraph Host PC (Windows)
        Tray[System Tray Menu\n- Set Remote PIN\n- Allow Remote Mouse toggle] --> Settings[settings.json\n%LOCALAPPDATA%/InkCast]
        Settings --> ServerCore[Python Server / aiohttp]
        ServerCore --> AuthManager[PIN Auth & Rate Limiter]
        ServerCore --> InputInjector[ctypes.SendInput\nNative Win32 Mouse Events]
        InputInjector --> WindowsOS[Windows Desktop Cursor & Windows Input Queue]
        ServerCore --> SlideTrigger[trigger_slide prev/next]
    end

    subgraph Client (Tablet / Smartphone Web App)
        Toolbar[Side Toolbar: Trackpad Button] --> PinModal[PIN Entry Dialog]
        PinModal --> AuthToken[localStorage Token]
        AuthToken --> WsClient[WebSocket Client /ws]
        TouchSurface[Touch Gesture Engine\n60Hz throttled] --> WsClient
        FloatingHUD[Floating Landscape HUD\n- Prev/Next\n- L/R Click\n- Scroll Slider\n- Mode Toggle\n- Minimize Pill] --> WsClient
    end

    WsClient <== "WebSocket (LAN or Cloudflare Tunnel)" ==> ServerCore
```

---

## 3. Subsystem Specifications

### 3.1. Host Windows Input Subsystem (`ctypes.SendInput`)

All mouse interactions on the host PC will be executed via Windows `SendInput` using Python's built-in `ctypes` and `ctypes.wintypes`.

#### Win32 Structures:
```python
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
        ("type", wintypes.DWORD), # INPUT_MOUSE = 0
        ("u", INPUT_UNION),
    ]
```

#### Event Handling:
1. **Relative Movement:**
   - Flag: `MOUSEEVENTF_MOVE = 0x0001`
   - Relative offsets `(dx, dy)` calculated with an adaptive acceleration curve ($dx_{scaled} = dx \cdot (1 + |dx| \cdot 0.05)$) for precision on slow swipes and fast traversal on quick swipes.
2. **Absolute / Direct Touch:**
   - Flags: `MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE = 0x8001`
   - Normalized coordinates `(x, y)` in range $[0.0, 1.0]$ mapped to $[0, 65535]$.
3. **Buttons & Clicks:**
   - Left Down/Up: `MOUSEEVENTF_LEFTDOWN (0x0002)`, `MOUSEEVENTF_LEFTUP (0x0004)`
   - Right Down/Up: `MOUSEEVENTF_RIGHTDOWN (0x0008)`, `MOUSEEVENTF_RIGHTUP (0x0010)`
4. **Vertical Scroll:**
   - Flag: `MOUSEEVENTF_WHEEL = 0x0800`
   - `mouseData = int(delta_y)` where one standard notch is `120` (or dynamic fractions).
5. **Slide Flipping:**
   - Directly reuses InkCast's existing `trigger_slide("next")` and `trigger_slide("prev")` executor routines (sending `Left`/`Right` and `PgUp`/`PgDn`).

---

### 3.2. Security & PIN Authentication Subsystem

#### Host Configuration & Persistence:
* Configuration stored in `%LOCALAPPDATA%\InkCast\settings.json`:
  ```json
  {
    "remote_mouse_enabled": true,
    "mouse_pin": "1234"
  }
  ```
* **Default PIN:** `1234` on first run.
* **System Tray Menu Additions:**
  * `[✓] Allow Remote Mouse` (QAction checkbox) — Instant master killswitch. If unchecked, all mouse commands from any client are dropped immediately.
  * `Set Remote Mouse PIN...` (QAction) — Opens a simple modal dialog with a numeric text field to view or change the PIN.

#### Authentication Protocol:
1. When a WebSocket client connects, its state has `is_mouse_authorized = False`.
2. Client sends token: `{"type": "mouse_auth", "token": "..."}` or PIN: `{"type": "mouse_auth", "pin": "..."}`.
3. Server checks matching PIN/Token against host memory and `settings.json`.
4. **Rate Limiting:** If 5 consecutive failed PIN attempts occur from an IP address within 60 seconds, further attempts from that IP are blocked for 60 seconds.
5. On successful auth:
   - Server creates a cryptographically secure random session token (`secrets.token_hex(16)`).
   - Marks WebSocket session as `is_mouse_authorized = True`.
   - Sends response `{"type": "mouse_auth_res", "ok": True, "token": "<token>"}`.
6. Client stores `<token>` in `localStorage['inkcast_mouse_token']`.
7. If host changes the PIN or unchecks `Allow Remote Mouse`, all active tokens are immediately invalidated.

---

### 3.3. WebSocket Message Protocol

| Action | Payload Sent by Client | Host Processing |
|---|---|---|
| **Auth with PIN** | `{"type": "mouse_auth", "pin": "1234"}` | Validates PIN, returns token |
| **Auth with Token** | `{"type": "mouse_auth", "token": "<hash>"}` | Validates existing session |
| **Move Relative** | `{"type": "mouse", "action": "move_rel", "dx": -10, "dy": 5}` | Injects `MOUSEEVENTF_MOVE` |
| **Move Absolute** | `{"type": "mouse", "action": "move_abs", "x": 0.45, "y": 0.32}` | Injects `MOUSEEVENTF_MOVE \| MOUSEEVENTF_ABSOLUTE` |
| **Click** | `{"type": "mouse", "action": "click", "btn": "left" \| "right"}` | Injects DOWN then UP |
| **Down / Up** | `{"type": "mouse", "action": "down" \| "up", "btn": "left"}` | Used for tap-and-hold drag & drop |
| **Scroll** | `{"type": "mouse", "action": "scroll", "dy": -120}` | Injects `MOUSEEVENTF_WHEEL` |
| **Slide Flip** | `{"type": "slide", "dir": "next" \| "prev"}` | Calls `trigger_slide(dir)` |

---

### 3.4. Web Client UI & Landscape Floating HUD

#### 1. Toolbar Addition:
* New button in `TOOLS`: `['trackpad', 'Trackpad']` with a mouse/touchpad SVG icon in the side toolbar.

#### 2. Floating Landscape HUD:
When `trackpad` mode is active:
* **Background Canvas:** Live screen remains visible at full resolution and aspect ratio.
* **Touch Interaction Surface:** The entire viewport acts as the touchpad.
  * Drag finger: moves mouse.
  * Single tap: left click.
  * Two-finger tap: right click.
  * Two-finger drag: scroll up/down.
* **Floating Control Bar:**
  * Positioned near the bottom center with `backdrop-filter: blur(12px)`, background `rgba(20, 21, 23, 0.85)`, and rounded pill borders.
  * **Controls Included:**
    1. `[ ◀ Prev ]` & `[ Next ▶ ]` presentation buttons.
    2. Mode switch toggle: `[ 🖱️ Relative / 🎯 Direct ]`.
    3. `[ L-Click ]` & `[ R-Click ]` physical buttons.
    4. `[ ↕ Scroll ]` quick swipe strip.
    5. Minimize button `[ ▾ ]`: Collapses bar into a small floating pill `[ 🖱️ ]` at the corner.
    6. Exit button `[ ✕ ]`: Exits trackpad mode and returns to last selected pen tool.
* **Throttling:** Pointer movements are capped to 60fps via `requestAnimationFrame` to avoid WebSocket saturation.

---

## 4. Verification & Testing Plan

1. **Host-Side Verification:**
   - Verify `settings.json` creation and persistence in `%LOCALAPPDATA%\InkCast`.
   - Test tray menu actions: toggle master switch, change PIN modal dialog.
   - Verify `SendInput` execution on Windows 11 (movement, click, right-click, wheel scroll).
2. **Security & PIN Verification:**
   - Attempt mouse actions without PIN: confirm server rejects with `Unauthorized`.
   - Submit wrong PIN 5 times: confirm rate-limiting lockout triggers.
   - Submit correct PIN: confirm token issued and stored in `localStorage`.
   - Change PIN on host: confirm previous tokens are rejected.
3. **Client UI Verification:**
   - Portrait and Landscape layout on mobile and tablet browsers (iOS Safari, Android Chrome).
   - Confirm HUD minimize/expand works without covering presentation content.
   - Confirm `Prev` / `Next` triggers slide transition in PowerPoint / PDF viewer.
   - Verify smooth 60fps cursor responsiveness over local Wi-Fi and Cloudflare Tunnel.
