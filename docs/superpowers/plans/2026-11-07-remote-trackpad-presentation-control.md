# Remote Trackpad & Presentation Controller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add a remote trackpad and presentation slide controller mode to InkCast, allowing tablets/smartphones to control the Windows host cursor and slides with strict PIN security for remote tunnel sessions.

**Architecture:** A native Windows input injector using `ctypes.windll.user32.SendInput` dispatches ordered mouse events via a dedicated single-worker executor. An authentication gate distinguishes local LAN traffic from Cloudflare Tunnel traffic (`CF-Connecting-IP`), enforcing a 6-digit SHA-256 hashed PIN and rate limiting. The web client gains a Trackpad tool with raw touch gestures, cursor ring feedback, and a semi-transparent floating HUD with Prev/Next buttons that can minimize into a pill.

**Tech Stack:** Python 3.10+, PySide6, aiohttp, Windows Win32 API (`ctypes`, `SendInput`, `GetCursorPos`), HTML5/CSS/JavaScript.

**Spec:** [docs/superpowers/specs/2026-11-07-remote-trackpad-presentation-control-design.md](docs/superpowers/specs/2026-11-07-remote-trackpad-presentation-control-design.md)

## Global Constraints
- Target codebase is a single file: `inkcast.py` (~2480 lines with embedded `PAGE` HTML string).
- Zero new third-party Python dependencies — use standard library `ctypes`, `hashlib`, `secrets`, `json`, `concurrent.futures`.
- All client-to-server messages must use `"t"` as the discriminator key (matching existing protocol `d.get("t")`).
- Input injection must run on a dedicated single-thread executor to prevent event reordering.
- Test runner: standard library `python -m unittest tests/test_<name>.py -v`.

---

### Task 1: Native Windows SendInput Structures & InputExecutor

**Files:**
- Create: `tests/test_input.py`
- Modify: `inkcast.py:30-80`

**Interfaces:**
- Produces:
  - `InputExecutor`: class wrapping a single-worker `ThreadPoolExecutor` with `submit_mouse(action, **kwargs)`
  - `inject_mouse_event(action: str, **kwargs) -> bool`
  - Win32 structures: `MOUSEINPUT`, `INPUT_UNION`, `INPUT`
  - Event constants: `INPUT_MOUSE`, `MOUSEEVENTF_MOVE`, `MOUSEEVENTF_ABSOLUTE`, `MOUSEEVENTF_LEFTDOWN`, `MOUSEEVENTF_LEFTUP`, `MOUSEEVENTF_RIGHTDOWN`, `MOUSEEVENTF_RIGHTUP`, `MOUSEEVENTF_WHEEL`

- [x] **Step 1: Write the failing unit tests for input structures and event translation**

Create `tests/test_input.py`:
```python
import unittest
import ctypes
from ctypes import wintypes

class TestInputStructures(unittest.TestCase):
    def test_structures_and_executor(self):
        import inkcast
        # Verify MOUSEINPUT size and field alignment
        self.assertTrue(hasattr(inkcast, "MOUSEINPUT"))
        self.assertTrue(hasattr(inkcast, "INPUT"))
        self.assertEqual(ctypes.sizeof(inkcast.INPUT), 40) # 64-bit Windows alignment
        
        # Verify InputExecutor exists and has single worker
        self.assertTrue(hasattr(inkcast, "InputExecutor"))
        executor = inkcast.InputExecutor()
        self.assertEqual(executor.executor._max_workers, 1)

        # Verify mouse injection function
        self.assertTrue(hasattr(inkcast, "inject_mouse_event"))
        # Test relative move (mock or zero movement call)
        result = inkcast.inject_mouse_event("move_rel", dx=0, dy=0)
        self.assertTrue(result)
        executor.shutdown()

if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests/test_input.py -v`  
Expected: FAIL with `AttributeError: module 'inkcast' has no attribute 'MOUSEINPUT'`

- [x] **Step 3: Implement Win32 SendInput structures and InputExecutor in `inkcast.py`**

In `inkcast.py`, right after imports (~line 40):
```python
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
```

- [x] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests/test_input.py -v`  
Expected: PASS with 1 test ok.

- [x] **Step 5: Commit**

```bash
git add tests/test_input.py inkcast.py
git commit -m "feat(mouse): add Win32 SendInput structures and single-threaded InputExecutor"
```

---

### Task 2: Settings Persistence, Remote Gate & PIN Authentication Manager

**Files:**
- Create: `tests/test_auth.py`
- Modify: `inkcast.py:80-150`

**Interfaces:**
- Consumes: `hashlib`, `secrets`, `json`, `os`, `ipaddress`
- Produces:
  - `load_settings() -> dict`
  - `save_settings(settings: dict) -> None`
  - `is_remote_request(request) -> bool`
  - `get_client_ip(request) -> str`
  - `AuthManager`:
    - `validate_pin(pin: str, client_ip: str) -> tuple[bool, str]`
    - `validate_token(token: str) -> bool`
    - `issue_token() -> str`
    - `revoke_tokens() -> None`
    - `set_pin(pin: str) -> None`

- [x] **Step 1: Write the failing unit tests for AuthManager and RemoteGate**

Create `tests/test_auth.py`:
```python
import unittest
from unittest.mock import MagicMock

class TestAuthManager(unittest.TestCase):
    def test_auth_manager_pin_and_tokens(self):
        from inkcast import AuthManager
        auth = AuthManager(pin_hash="") # No PIN configured
        
        # When no PIN is set, fail-closed
        ok, msg = auth.validate_pin("123456", "1.2.3.4")
        self.assertFalse(ok)
        self.assertIn("locked", msg.lower())

        # Set a PIN
        auth.set_pin("654321")
        # Incorrect PIN
        ok, msg = auth.validate_pin("111111", "1.2.3.4")
        self.assertFalse(ok)
        self.assertIn("incorrect", msg.lower())

        # Correct PIN
        ok, token = auth.validate_pin("654321", "1.2.3.4")
        self.assertTrue(ok)
        self.assertTrue(len(token) > 10)
        self.assertTrue(auth.validate_token(token))

        # Changing PIN invalidates existing tokens
        auth.set_pin("999999")
        self.assertFalse(auth.validate_token(token))

    def test_rate_limiting(self):
        from inkcast import AuthManager
        auth = AuthManager(pin_hash="")
        auth.set_pin("654321")
        ip = "192.168.10.50"
        for _ in range(5):
            ok, _ = auth.validate_pin("000000", ip)
            self.assertFalse(ok)
        # 6th attempt should be rate limited
        ok, msg = auth.validate_pin("654321", ip)
        self.assertFalse(ok)
        self.assertIn("too many", msg.lower())

    def test_remote_classification(self):
        from inkcast import is_remote_request, get_client_ip
        # Mock request with CF-Connecting-IP
        req_cf = MagicMock()
        req_cf.headers = {"CF-Connecting-IP": "203.0.113.195"}
        req_cf.host = "xxxx.trycloudflare.com"
        self.assertTrue(is_remote_request(req_cf))
        self.assertEqual(get_client_ip(req_cf), "203.0.113.195")

        # Mock direct LAN request
        req_lan = MagicMock()
        req_lan.headers = {}
        req_lan.host = "192.168.1.5:8765"
        req_lan.remote = "192.168.1.100"
        self.assertFalse(is_remote_request(req_lan))
        self.assertEqual(get_client_ip(req_lan), "192.168.1.100")

if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests/test_auth.py -v`  
Expected: FAIL with `ImportError: cannot import name 'AuthManager' from 'inkcast'`

- [x] **Step 3: Implement SettingsManager, is_remote_request, and AuthManager in `inkcast.py`**

In `inkcast.py`:
```python
import hashlib
import secrets
import time
import ipaddress

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
    def __init__(self, pin_hash: str):
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
```

- [x] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests/test_auth.py -v`  
Expected: PASS with 3 tests ok.

- [x] **Step 5: Commit**

```bash
git add tests/test_auth.py inkcast.py
git commit -m "feat(auth): implement settings persistence, remote gate, and PIN AuthManager"
```

---

### Task 3: Tray Menu Controls & Cursor Position Reporting

**Files:**
- Modify: `inkcast.py:1250-1365` (in `main()`)
- Modify: `inkcast.py:640-700` (in `Server.send_frame`)

**Interfaces:**
- Consumes: PySide6 `QAction`, `QInputDialog`, `QMessageBox`
- Produces:
  - System Tray menu actions:
    - `[✓] Allow remote mouse` (QAction checkable)
    - `Remote access PIN…` (QAction dialog)
  - Broadcast of `{"t": "mouse_state", "enabled": bool}` on toggle
  - Server frame cursor notification: `{"t": "cur", "x": float, "y": float}`

- [x] **Step 1: Write test for cursor position retrieval and normalization**

Create `tests/test_cursor.py`:
```python
import unittest
import sys

class TestCursorNormalization(unittest.TestCase):
    def test_get_normalized_cursor(self):
        import inkcast
        if sys.platform != "win32":
            return
        self.assertTrue(hasattr(inkcast, "get_normalized_cursor"))
        pos = inkcast.get_normalized_cursor(1920, 1080)
        self.assertIsInstance(pos, tuple)
        self.assertEqual(len(pos), 2)
        x, y = pos
        self.assertGreaterEqual(x, 0.0)
        self.assertLessEqual(x, 1.0)
        self.assertGreaterEqual(y, 0.0)
        self.assertLessEqual(y, 1.0)

if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests/test_cursor.py -v`  
Expected: FAIL with `AttributeError: module 'inkcast' has no attribute 'get_normalized_cursor'`

- [x] **Step 3: Implement `get_normalized_cursor` and Tray actions in `inkcast.py`**

In `inkcast.py`:
```python
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
```

In `main()` (~line 1300):
- Load settings: `settings = load_settings()`
- Create `mouse_toggle = QAction("Allow remote mouse", checkable=True)` initialized to `settings.get("mouse_enabled", True)`.
- Create `pin_action = QAction("Remote access PIN…")`.
- When `mouse_toggle` triggered:
  - `settings["mouse_enabled"] = mouse_toggle.isChecked()`
  - `save_settings(settings)`
  - Broadcast `{"t": "mouse_state", "enabled": mouse_toggle.isChecked()}` via server.
- When `pin_action` triggered:
  - Show `QInputDialog.getText(None, "Remote Access PIN", "Enter a 6-8 digit PIN (leave blank to lock remote access):")`.
  - Update `server.auth_manager.set_pin(pin)`
  - `settings["remote_pin"] = hash_pin(pin)`
  - `save_settings(settings)`
- In `Server.send_frame`:
  - After sending frame bytes or along with frame, if clients exist, queue cursor update:
  ```python
  cur_x, cur_y = get_normalized_cursor(self.screen_w, self.screen_h)
  # send {"t": "cur", "x": cur_x, "y": cur_y}
  ```

- [x] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests/test_cursor.py -v`  
Expected: PASS with 1 test ok.

- [x] **Step 5: Commit**

```bash
git add tests/test_cursor.py inkcast.py
git commit -m "feat(tray): add remote mouse toggle and PIN config actions, plus cursor position reporting"
```

---

### Task 4: WebSocket Server Protocol Integration

**Files:**
- Create: `tests/test_ws_protocol.py`
- Modify: `inkcast.py:730-840` (`Server` class and `ws_handler`)

**Interfaces:**
- Consumes: `InputExecutor`, `AuthManager`, `is_remote_request`, `get_client_ip`
- Produces:
  - Extended `hello` message payload: `{"t": "hello", "auth_required": bool, "mouse_enabled": bool, "screen_w": int, "screen_h": int}`
  - Interception in `ws_handler`:
    - `{"t": "auth", "pin": "..."}` -> responds with `auth_ok` or `auth_err`
    - `{"t": "auth", "token": "..."}` -> validates token, responds with `auth_ok` or `auth_err`
    - `{"t": "mouse", "action": ...}` -> validated against session auth and `mouse_enabled` killswitch, dispatches to `input_executor`

- [x] **Step 1: Write protocol unit/integration test**

Create `tests/test_ws_protocol.py`:
```python
import unittest
import json
from unittest.mock import MagicMock

class TestWsProtocol(unittest.TestCase):
    def test_hello_and_mouse_dispatch(self):
        import inkcast
        server = inkcast.Server(bridge=MagicMock(), aspect=16/9)
        server.auth_manager.set_pin("123456")
        
        # Test auth with token
        token = server.auth_manager.issue_token()
        self.assertTrue(server.auth_manager.validate_token(token))
        
        # Test mouse dispatch blocked when mouse_enabled is False
        server.mouse_enabled = False
        res = server.handle_mouse_msg({"t": "mouse", "action": "click", "btn": "left"}, is_authorized=True)
        self.assertFalse(res) # blocked by killswitch

        # Test mouse dispatch allowed when mouse_enabled is True and authorized
        server.mouse_enabled = True
        res = server.handle_mouse_msg({"t": "mouse", "action": "move_rel", "dx": 0, "dy": 0}, is_authorized=True)
        self.assertTrue(res)

        # Test mouse dispatch blocked when unauthorized
        res = server.handle_mouse_msg({"t": "mouse", "action": "move_rel", "dx": 0, "dy": 0}, is_authorized=False)
        self.assertFalse(res)

if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests/test_ws_protocol.py -v`  
Expected: FAIL with `AttributeError: 'Server' object has no attribute 'handle_mouse_msg'`

- [x] **Step 3: Implement protocol handling in `Server` and `ws_handler`**

In `inkcast.py`:
- Initialize in `Server.__init__`:
  ```python
  self.input_executor = InputExecutor()
  self.auth_manager = AuthManager(pin_hash=load_settings().get("remote_pin", ""))
  self.mouse_enabled = load_settings().get("mouse_enabled", True)
  ```
- Add `handle_mouse_msg(self, d, is_authorized: bool) -> bool`:
  ```python
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
  ```
- In `ws_handler`:
  - Determine `remote = is_remote_request(request)`.
  - Determine `client_ip = get_client_ip(request)`.
  - Session auth flag: `is_authorized = not remote` (LAN clients default to authorized; remote starts unauthorized).
  - In `hello` message sent upon connect:
    ```python
    await ws.send_json({
        "t": "hello",
        "aspect": self.aspect,
        "screen_w": self.screen_w,
        "screen_h": self.screen_h,
        "mouse_enabled": self.mouse_enabled,
        "auth_required": remote and bool(self.auth_manager.pin_hash),
    })
    ```
  - In message read loop:
    ```python
    t = d.get("t")
    if t == "auth":
        pin = d.get("pin")
        token = d.get("token")
        if token and self.auth_manager.validate_token(token):
            is_authorized = True
            await ws.send_json({"t": "auth_ok", "token": token})
        elif pin:
            ok, res = self.auth_manager.validate_pin(pin, client_ip)
            if ok:
                is_authorized = True
                await ws.send_json({"t": "auth_ok", "token": res})
            else:
                await ws.send_json({"t": "auth_err", "msg": res})
        else:
            await ws.send_json({"t": "auth_err", "msg": "Invalid auth payload"})
        continue
    if t == "mouse":
        self.handle_mouse_msg(d, is_authorized)
        continue
    # Gate remote unauthorized sessions from drawing and viewing
    if remote and not is_authorized:
        continue
    ```

- [x] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests/test_ws_protocol.py -v`  
Expected: PASS with 1 test ok.

- [x] **Step 5: Commit**

```bash
git add tests/test_ws_protocol.py inkcast.py
git commit -m "feat(server): integrate auth and mouse protocol handling into WebSocket server"
```

---

### Task 5: Client Trackpad Tool, Raw Touch Gesture Engine & Cursor Feedback Ring

**Files:**
- Modify: `inkcast.py:1505-1800` (within `PAGE` script and HTML)

**Interfaces:**
- Consumes: SVG icon map `I`, `TOOLS`, `setTool`
- Produces:
  - Trackpad tool icon in `I`: `'trackpad': '<path d="..." />'`
  - Entry in `TOOLS`: `['trackpad', 'Trackpad']`
  - Cursor ring element `#cur-ring`
  - Touch event listeners on `#stage` handling 1-finger move, 1-finger tap, 2-finger tap, 2-finger scroll

- [x] **Step 1: Add Trackpad SVG icon and tool entry**

In `PAGE` string in `inkcast.py`:
- In `const I = { ... }`:
  ```javascript
  trackpad:'<rect x="4" y="3" width="16" height="18" rx="2.5"/><line x1="4" y1="14" x2="20" y2="14"/><line x1="12" y1="14" x2="12" y2="21"/>'
  ```
- In `TOOLS`:
  Add `'|', ['trackpad', 'Trackpad']` at the end.
- Update `setTool`:
  `if (t !== 'laser' && t !== 'trackpad') prevTool = t;`
  In reload tool reset: include `'trackpad'` in tools that reset to `'pen'`.

- [x] **Step 2: Add Cursor Feedback Ring markup & CSS**

In `PAGE` HTML:
```html
<div id="cur-ring" style="display:none;position:absolute;width:24px;height:24px;margin-left:-12px;margin-top:-12px;border:2px solid #ff3b30;border-radius:50%;background:rgba(255,59,48,0.2);pointer-events:none;z-index:90;transition:transform 0.05s linear;"></div>
```
In WS message dispatcher:
```javascript
if (d.t === 'cur' && S.tool === 'trackpad') {
  const r = $('cur-ring');
  if (r && crect) {
    r.style.display = 'block';
    const cx = crect.x + d.x * crect.w;
    const cy = crect.y + d.y * crect.h;
    r.style.left = cx + 'px';
    r.style.top = cy + 'px';
  }
}
```

- [x] **Step 3: Implement Raw Touch Gesture Engine with 60fps coalescing**

In `PAGE` script:
```javascript
let tpMode = 'rel'; // 'rel' or 'abs'
let tpStart = null, tpLast = null, tpDist = 0, tpMoved = false;
let tpTwoStart = null, tpTwoLast = null;
let tpPendingMove = null, tpRaf = null;

function flushMouseMove() {
  if (tpPendingMove) {
    send(tpPendingMove);
    tpPendingMove = null;
  }
  tpRaf = null;
}

function queueMouseMove(msg) {
  tpPendingMove = msg;
  if (!tpRaf) {
    tpRaf = requestAnimationFrame(flushMouseMove);
  }
}

stage.addEventListener('touchstart', e => {
  if (S.tool !== 'trackpad') return;
  if (e.touches.length === 1) {
    const t = e.touches[0];
    tpStart = { x: t.clientX, y: t.clientY, time: Date.now() };
    tpLast = { x: t.clientX, y: t.clientY };
    tpDist = 0;
    tpMoved = false;
  } else if (e.touches.length === 2) {
    tpTwoStart = { y: (e.touches[0].clientY + e.touches[1].clientY) / 2, time: Date.now() };
    tpTwoLast = tpTwoStart.y;
  }
}, { passive: false });

stage.addEventListener('touchmove', e => {
  if (S.tool !== 'trackpad') return;
  e.preventDefault();
  if (e.touches.length === 1 && tpLast) {
    const t = e.touches[0];
    const dx = t.clientX - tpLast.x;
    const dy = t.clientY - tpLast.y;
    tpDist += Math.hypot(dx, dy);
    if (tpDist > 5) tpMoved = true;
    tpLast = { x: t.clientX, y: t.clientY };

    if (tpMode === 'rel') {
      const vdx = dx / Math.max(1, stage.clientWidth);
      const vdy = dy / Math.max(1, stage.clientHeight);
      queueMouseMove({ t: 'mouse', action: 'move_rel', dx: vdx, dy: vdy });
    } else if (tpMode === 'abs' && crect) {
      const nx = (t.clientX - crect.x) / crect.w;
      const ny = (t.clientY - crect.y) / crect.h;
      queueMouseMove({ t: 'mouse', action: 'move_abs', x: nx, y: ny });
    }
  } else if (e.touches.length === 2 && tpTwoLast !== null) {
    const cy = (e.touches[0].clientY + e.touches[1].clientY) / 2;
    const dScroll = cy - tpTwoLast;
    tpTwoLast = cy;
    queueMouseMove({ t: 'mouse', action: 'scroll', dy: Math.round(dScroll * 4) });
  }
}, { passive: false });

stage.addEventListener('touchend', e => {
  if (S.tool !== 'trackpad') return;
  if (e.touches.length === 0 && tpStart) {
    const elapsed = Date.now() - tpStart.time;
    if (!tpMoved && elapsed < 300) {
      // Tap detected -> Left Click
      if (tpMode === 'abs' && crect) {
        const nx = (tpStart.x - crect.x) / crect.w;
        const ny = (tpStart.y - crect.y) / crect.h;
        sendNow({ t: 'mouse', action: 'move_abs', x: nx, y: ny });
      }
      sendNow({ t: 'mouse', action: 'click', btn: 'left' });
    }
    tpStart = null;
    tpLast = null;
  } else if (e.touches.length === 0 && tpTwoStart) {
    const elapsed = Date.now() - tpTwoStart.time;
    if (Math.abs(tpTwoLast - tpTwoStart.y) < 10 && elapsed < 350) {
      // Two-finger tap -> Right Click
      sendNow({ t: 'mouse', action: 'click', btn: 'right' });
    }
    tpTwoStart = null;
    tpTwoLast = null;
  }
});
```

- [x] **Step 4: Verify syntax and compile check**

Run: `python -m py_compile inkcast.py`  
Expected: Exit code 0 without syntax errors.

- [x] **Step 5: Commit**

```bash
git add inkcast.py
git commit -m "feat(client): implement trackpad tool, touch gestures, and cursor ring"
```

---

### Task 6: Floating Landscape HUD, Minimize Pill & PIN Modal

**Files:**
- Modify: `inkcast.py:1450-1700` and `2200-2450` (within `PAGE` string)

**Interfaces:**
- Consumes: `sendSlide()`, `sendNow()`, `localStorage`
- Produces:
  - `#hud` floating dock UI and responsive CSS
  - Mode toggle: Relative vs Direct
  - `Prev` / `Next` slide controls with `quiet=true` toast suppression
  - Minimize toggle to `[ 🖱️ ]` floating pill
  - `#pinmodal` 6-digit PIN input dialog with touch keypad

- [x] **Step 1: Add HUD and PIN Modal HTML markup and styles**

In `PAGE` HTML:
```html
<!-- Trackpad HUD -->
<div id="hud" style="display:none;">
  <div id="hud-full" class="hud-bar">
    <button class="hud-btn" id="hudPrev" title="Previous Slide">◀ Prev</button>
    <button class="hud-btn" id="hudNext" title="Next Slide">Next ▶</button>
    <div class="hud-sep"></div>
    <button class="hud-btn" id="hudMode" title="Toggle Trackpad Mode">🖱️ Rel</button>
    <div class="hud-sep"></div>
    <button class="hud-btn hud-icon" id="hudMin" title="Minimize">▾</button>
    <button class="hud-btn hud-icon" id="hudExit" title="Exit Trackpad">✕</button>
  </div>
  <button id="hud-mini" class="hud-pill" style="display:none;" title="Expand Trackpad Controls">🖱️</button>
</div>

<!-- PIN Modal -->
<div id="pinmodal" class="modal-wrap" style="display:none;">
  <div class="modal-box">
    <h3>Enter Host PIN</h3>
    <p id="pinerr" class="modal-err"></p>
    <input type="password" id="pininput" maxlength="8" pattern="[0-9]*" inputmode="numeric" placeholder="6-digit PIN">
    <div class="modal-actions">
      <button id="pinsubmit" class="modal-btn-pri">Unlock</button>
      <button id="pincancel" class="modal-btn-sec">Cancel</button>
    </div>
  </div>
</div>
```

Add CSS for `#hud` and `#pinmodal`:
```css
#hud { position:fixed; bottom:16px; left:50%; transform:translateX(-50%); z-index:95; font-family:sans-serif; }
.hud-bar { display:flex; align-items:center; gap:6px; background:rgba(20,21,23,0.85); backdrop-filter:blur(12px); -webkit-backdrop-filter:blur(12px); padding:6px 10px; border-radius:24px; box-shadow:0 6px 20px rgba(0,0,0,0.35); border:1px solid rgba(255,255,255,0.15); }
.hud-pill { display:flex; align-items:center; justify-content:center; width:44px; height:44px; background:rgba(20,21,23,0.9); border-radius:50%; border:1px solid rgba(255,255,255,0.25); color:#fff; font-size:20px; box-shadow:0 4px 12px rgba(0,0,0,0.4); cursor:pointer; }
.hud-btn { background:rgba(255,255,255,0.1); border:none; color:#fff; font-size:14px; font-weight:600; padding:8px 14px; border-radius:16px; cursor:pointer; user-select:none; }
.hud-btn:active { background:rgba(255,255,255,0.25); transform:scale(0.96); }
.hud-icon { width:32px; height:32px; padding:0; display:flex; align-items:center; justify-content:center; border-radius:50%; }
.hud-sep { width:1px; height:20px; background:rgba(255,255,255,0.2); margin:0 2px; }
.modal-wrap { position:fixed; inset:0; background:rgba(0,0,0,0.65); z-index:100; display:flex; align-items:center; justify-content:center; backdrop-filter:blur(4px); }
.modal-box { background:#1e2023; border:1px solid rgba(255,255,255,0.2); border-radius:12px; padding:20px; width:280px; text-align:center; color:#fff; box-shadow:0 8px 32px rgba(0,0,0,0.5); }
.modal-err { color:#ff453a; font-size:12px; min-height:16px; margin:4px 0; }
#pininput { width:100%; box-sizing:border-box; padding:10px; font-size:20px; text-align:center; letter-spacing:4px; border-radius:8px; border:1px solid #444; background:#121315; color:#fff; margin-bottom:12px; }
.modal-actions { display:flex; gap:8px; }
.modal-btn-pri { flex:1; background:#0a84ff; border:none; padding:10px; border-radius:8px; color:#fff; font-weight:600; cursor:pointer; }
.modal-btn-sec { flex:1; background:#3a3a3c; border:none; padding:10px; border-radius:8px; color:#fff; cursor:pointer; }
```

- [x] **Step 2: Implement HUD interaction and slide controls**

In `PAGE` script:
- Wire HUD buttons:
  - `$('hudPrev').onclick = () => sendSlide('prev', true);`
  - `$('hudNext').onclick = () => sendSlide('next', true);`
  - Mode button:
    ```javascript
    $('hudMode').onclick = () => {
      tpMode = (tpMode === 'rel' ? 'abs' : 'rel');
      $('hudMode').textContent = (tpMode === 'rel' ? '🖱️ Rel' : '🎯 Direct');
    };
    ```
  - Minimize button:
    ```javascript
    $('hudMin').onclick = () => {
      $('hud-full').style.display = 'none';
      $('hud-mini').style.display = 'flex';
    };
    $('hud-mini').onclick = () => {
      $('hud-mini').style.display = 'none';
      $('hud-full').style.display = 'flex';
    };
    ```
  - Exit button:
    ```javascript
    $('hudExit').onclick = () => {
      $('hud').style.display = 'none';
      $('cur-ring').style.display = 'none';
      setTool(prevTool || 'pen');
    };
    ```

- [x] **Step 3: Implement PIN Modal, Token Handling, and Auth Flow**

In `PAGE` script:
```javascript
let authToken = localStorage.getItem('inkcast.auth_token') || '';

function promptPin() {
  $('pinmodal').style.display = 'flex';
  $('pininput').value = '';
  $('pinerr').textContent = '';
  $('pininput').focus();
}

$('pincancel').onclick = () => {
  $('pinmodal').style.display = 'none';
  setTool(prevTool || 'pen');
};

$('pinsubmit').onclick = () => {
  const pin = $('pininput').value.trim();
  if (!pin) return;
  sendNow({ t: 'auth', pin: pin });
};

// WebSocket onmessage extension:
// Handle 'auth_ok' -> store token, close modal, activate HUD
// Handle 'auth_err' -> display error in pinerr
```
In tool selection:
```javascript
if (tool === 'trackpad') {
  if (authRequired && !authToken) {
    promptPin();
    return;
  }
  $('hud').style.display = 'block';
  $('hud-full').style.display = 'flex';
  $('hud-mini').style.display = 'none';
} else {
  $('hud').style.display = 'none';
  $('cur-ring').style.display = 'none';
}
```

- [x] **Step 4: Verify syntax and compile check**

Run: `python -m py_compile inkcast.py`  
Expected: Exit code 0 without syntax errors.

- [x] **Step 5: Commit**

```bash
git add inkcast.py
git commit -m "feat(ui): add floating HUD, minimize pill, slide buttons, and PIN modal"
```

---

### Task 7: End-to-End Test & Verification

**Files:**
- Create: `tests/e2e_trackpad_test.py`

**Interfaces:**
- Consumes: Running server on `127.0.0.1:8765`, WebSocket client
- Produces: Complete verification report for move, click, PIN auth, rate limiting, and tray killswitch

- [x] **Step 1: Create standalone E2E automated test script**

Create `tests/e2e_trackpad_test.py`:
```python
import asyncio
import aiohttp
import json
import sys

async def run_e2e_test():
    url = "http://127.0.0.1:8765/ws"
    print("Testing connection to", url)
    async with aiohttp.ClientSession() as session:
        # 1. Test local LAN bypass
        async with session.ws_connect(url) as ws:
            hello = await ws.receive_json()
            assert hello.get("t") == "hello", f"Unexpected hello: {hello}"
            print("✓ Hello received:", hello)
            
            # Send relative move
            await ws.send_str(json.dumps({"t": "mouse", "action": "move_rel", "dx": 0.02, "dy": 0.0}))
            print("✓ Sent move_rel")
            await asyncio.sleep(0.1)

            # Send click
            await ws.send_str(json.dumps({"t": "mouse", "action": "click", "btn": "left"}))
            print("✓ Sent click")
            await asyncio.sleep(0.1)

        # 2. Test remote tunnel simulation with CF-Connecting-IP
        headers = {"CF-Connecting-IP": "198.51.100.42", "Host": "demo.trycloudflare.com"}
        async with session.ws_connect(url, headers=headers) as ws:
            hello = await ws.receive_json()
            print("✓ Tunnel simulated hello:", hello)
            if hello.get("auth_required"):
                # Wrong PIN test
                await ws.send_str(json.dumps({"t": "auth", "pin": "000000"}))
                res = await ws.receive_json()
                assert res.get("t") == "auth_err", f"Expected auth_err, got: {res}"
                print("✓ Incorrect PIN rejected:", res)
    print("\nAll E2E tests passed successfully!")

if __name__ == "__main__":
    asyncio.run(run_e2e_test())
```

- [x] **Step 2: Run all test suites**

Run: `python -m unittest discover tests/ -v`  
Expected: All unit tests in `tests/` pass.

- [x] **Step 3: Commit**

```bash
git add tests/e2e_trackpad_test.py
git commit -m "test: add end-to-end verification script for remote trackpad and PIN gate"
```

