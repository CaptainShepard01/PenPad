"""
PenPad server: turns stylus events from the Android PenPad app into Windows mouse input.

Setup (USB):  adb reverse tcp:47300 tcp:47300     # once per USB connection
Run:          uv run --no-project penpad_server.py [--target N | --target LEFT,TOP,RIGHT,BOTTOM]
GUI:          penpad_gui.py wraps the Server class below.
No dependencies. Windows 10+.

Mouse injection (not Windows Ink) on purpose: Resolve sees a plain mouse, so there are no
press-and-hold / flick gestures and no pen-mode quirks. Pressure/tilt are already in the
protocol; a pen backend can replace send_mouse() later without touching the phone app.
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import math
import socket
import struct
import sys
import threading
import time
import traceback

# ---- config -----------------------------------------------------------------
HOST, PORT = "127.0.0.1", 47300  # use "0.0.0.0" for Wi-Fi (phone then connects to the PC's LAN IP)
TARGET = 0               # monitor index (0 = primary), or (left, top, right, bottom) in physical px,
                         # e.g. just the Resolve viewer for finer control when drawing masks
KEEP_ASPECT = True       # circles stay circles; unused pad edges clamp to the target edge
TAP_DEADZONE_MM = 1.0    # cursor stays put after pen-down until the pen moves this far on the phone,
                         # so a wobbly tap stays a click (0 = off). Stops curve-window point clicks
                         # turning into tangent-handle drags. Physical distance: same feel on any screen.
BARREL = "right"         # pen button held while touching: "right", "middle" (drag),
                         # a left drag with a key held: "left+shift", "left+ctrl", "left+alt",
                         # mouse-wheel scrolling by dragging up/down: "scroll", "scroll+ctrl" (zoom),
                         # or "panzoom", "panzoom+ctrl": the first movement decides; sideways =
                         # pan (middle drag, then free in any direction), up/down = scroll / zoom
SCROLL_STEP_PX = 60      # phone px dragged per wheel step while scrolling (smaller = faster)
PANZOOM_DECIDE_MM = 2.5  # how far the pen moves on the phone before "panzoom" picks pan or zoom
DEFAULT_DPI = 450        # phone screen density if the app doesn't report it
# Pen button pressed while hovering (not touching). Actions: "none", "right_click", "middle_click",
# "next_screen", or "key:<shortcut>" such as "key:ctrl+z" (see parse_shortcut).
BUTTON_CLICK = "right_click"
BUTTON_DOUBLE = "key:ctrl+z"
BUTTON_LONG = "next_screen"
DOUBLE_CLICK_S = 0.3     # a second press within this time makes a double click
LONG_PRESS_S = 0.6       # held this long makes a long press

# ---- protocol (must match the Android app) ----------------------------------
PKT = struct.Struct("<BBxxfffff")  # type, buttons, x, y, pressure, tilt, orientation = 24 bytes
HELLO, HOVER, DOWN, MOVE, UP, LEAVE, PING = range(7)  # HELLO carries pad width/height in x/y
BTN_BARREL = 0x01
PHONE_TIMEOUT_S = 2.0    # the app pings every 0.25 s; silence this long = phone frozen or link dead

# ---- Win32 ------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2: physical pixels

MOVE_F, ABSOLUTE, VIRTUALDESK = 0x0001, 0x8000, 0x4000
BUTTONS = {  # name -> (down flag, up flag)
    "left": (0x0002, 0x0004),
    "right": (0x0008, 0x0010),
    "middle": (0x0020, 0x0040),
}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


MonitorEnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC,
                                     ctypes.POINTER(wt.RECT), wt.LPARAM)
user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.POINTER(MONITORINFO)]
user32.EnumDisplayMonitors.argtypes = [wt.HDC, ctypes.POINTER(wt.RECT), MonitorEnumProc, wt.LPARAM]

def virtual_desktop():
    """(left, top, width, height) of all monitors together, in physical px. Read on every use:
    the layout changes when a monitor is plugged in or turned off."""
    return tuple(user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))


def monitors():
    """Monitor rects in physical px, primary first."""
    found = []

    def cb(hmon, _hdc, _rect, _lparam):
        mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        r = mi.rcMonitor
        found.append(((r.left, r.top, r.right, r.bottom), bool(mi.dwFlags & 1)))
        return True

    user32.EnumDisplayMonitors(None, None, MonitorEnumProc(cb), 0)
    found.sort(key=lambda m: not m[1])
    return [rect for rect, _ in found]


def send_raw(flags, dx=0, dy=0, data=0):
    inp = INPUT(type=0)  # INPUT_MOUSE
    inp.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=data & 0xFFFFFFFF, dwFlags=flags, time=0, dwExtraInfo=0)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def send_wheel(steps):
    """Mouse-wheel steps at the cursor: positive = away from the user (scroll up / zoom in)."""
    send_raw(0x0800, data=steps * 120)  # MOUSEEVENTF_WHEEL, WHEEL_DELTA per step


def send_mouse(px, py, button_flag=0):
    """Absolute move to physical pixel (px, py), optionally with a button down/up in the same event."""
    vx, vy, vw, vh = virtual_desktop()
    dx = round((px - vx) * 65535 / max(vw - 1, 1))
    dy = round((py - vy) * 65535 / max(vh - 1, 1))
    send_raw(MOVE_F | ABSOLUTE | VIRTUALDESK | button_flag, dx, dy)


# ---- keyboard ---------------------------------------------------------------
KEY_NAMES = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
    "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}
EXTENDED_KEYS = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x5B}


def parse_shortcut(text):
    """'ctrl+shift+s' -> [VK_CONTROL, VK_SHIFT, VK_S]; None if any part is unknown.

    Parts: ctrl, shift, alt, win, a-z, 0-9, f1-f24, space, enter, tab, esc, backspace, delete,
    insert, home, end, pageup, pagedown, left/up/right/down, and ; = , - . / ` [ \\ ] '
    """
    vks = []
    for part in text.lower().replace(" ", "").split("+"):
        if len(part) == 1 and part.isalnum():
            vks.append(ord(part.upper()))
        elif part in KEY_NAMES:
            vks.append(KEY_NAMES[part])
        elif part[:1] == "f" and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            vks.append(0x6F + int(part[1:]))
        else:
            return None
    return vks or None


def send_key(vk, up=False):
    flags = (0x0001 if vk in EXTENDED_KEYS else 0) | (0x0002 if up else 0)  # EXTENDEDKEY, KEYUP
    inp = INPUT(type=1)  # INPUT_KEYBOARD
    inp.ki = KEYBDINPUT(wVk=vk, wScan=user32.MapVirtualKeyW(vk, 0), dwFlags=flags, time=0, dwExtraInfo=0)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def press_shortcut(vks):
    for vk in vks:
        send_key(vk)
    for vk in reversed(vks):
        send_key(vk, up=True)


def valid_action(action):
    if action in ("none", "right_click", "middle_click", "next_screen"):
        return True
    return action.startswith("key:") and parse_shortcut(action[4:]) is not None


class ButtonGestures:
    """Pen button presses while hovering -> "click", "double" or "long" (times in seconds)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.down_at = None    # press time while the button is held
        self.second = False    # this press came soon after a click: releasing it is a double click
        self.long_done = False
        self.click_at = None   # a click waiting to see whether a second press follows

    def press(self, now):
        self.second = self.click_at is not None
        self.click_at = None
        self.down_at, self.long_done = now, False

    def release(self, now, wait_for_double):
        down_at, self.down_at = self.down_at, None
        if down_at is None or self.long_done:
            return None
        if self.second:
            return "double"
        if wait_for_double:
            self.click_at = now
            return None
        return "click"

    def tick(self, now):
        if self.down_at is not None and not self.long_done and now - self.down_at >= LONG_PRESS_S:
            self.long_done = True
            return "long"
        if self.click_at is not None and now - self.click_at >= DOUBLE_CLICK_S:
            self.click_at = None
            return "click"
        return None

    def flush(self):
        """Pen touched down: a click still waiting for its double happens now; everything else is dropped."""
        pending = "click" if self.click_at is not None else None
        self.reset()
        return pending


def packets(conn, stop):
    """Yields decoded packets, and None whenever nothing arrived for a moment (for timers)."""
    buf = b""
    while not stop.is_set():
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            yield None  # also lets Ctrl+C / stop() through on Windows
            continue
        if not chunk:
            return
        buf += chunk
        n = len(buf) // PKT.size * PKT.size
        for off in range(0, n, PKT.size):
            yield PKT.unpack_from(buf, off)
        buf = buf[n:]


class Server:
    """Accepts the phone and drives the mouse, one phone at a time.

    rect, keep_aspect, tap_deadzone_mm, barrel and button_* may be changed from another thread
    while it runs (the GUI does this); the next pen event uses the new values.
    on_next_screen is called (on the server thread) for the "next_screen" action.
    """

    def __init__(self, rect, keep_aspect=KEEP_ASPECT, tap_deadzone_mm=TAP_DEADZONE_MM,
                 barrel=BARREL, scroll_step_px=SCROLL_STEP_PX, button_click=BUTTON_CLICK,
                 button_double=BUTTON_DOUBLE, button_long=BUTTON_LONG, on_next_screen=None,
                 host=HOST, port=PORT, log=print):
        self.rect = rect  # (left, top, right, bottom) in physical px
        self.keep_aspect = keep_aspect
        self.tap_deadzone_mm = tap_deadzone_mm
        self.barrel = barrel
        self.scroll_step_px = scroll_step_px
        self.button_click, self.button_double, self.button_long = button_click, button_double, button_long
        self.on_next_screen = on_next_screen
        self.host, self.port, self.log = host, port, log
        self.peer = None      # phone address while connected
        self.pad_size = None  # phone drawing area (w, h) in px, from HELLO
        self.pad_dpi = DEFAULT_DPI  # phone screen density, from HELLO
        self._stop = threading.Event()
        self._srv = socket.create_server((host, port))  # OSError if the port is taken
        self._srv.settimeout(0.5)

    def stop(self):
        self._stop.set()

    def serve_forever(self):
        """Runs until stop(). Nothing a phone sends, and no error in handling it, ends this loop:
        the connection is dropped (held buttons/keys released) and the next one is accepted."""
        with self._srv:
            self.log(f"listening on {self.host}:{self.port}")
            while not self._stop.is_set():
                try:
                    conn, addr = self._srv.accept()
                except socket.timeout:
                    continue
                except OSError as e:  # transient accept failure: don't die, try again
                    self.log(f"accept failed: {e}")
                    self._stop.wait(0.5)
                    continue
                self.log(f"connected {addr}")
                self.peer = addr
                try:
                    with conn:
                        conn.settimeout(0.05)  # how often timers (gestures, phone timeout) are checked
                        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        self._session(conn)
                except OSError as e:
                    self.log(f"connection error: {e}")
                except Exception:  # a bug must not take the server down with it
                    self.log("error while handling the phone:\n" + traceback.format_exc())
                finally:
                    self.peer = self.pad_size = None
                    self.log("disconnected")

    def map(self, x, y):
        """Normalized pad coords (0..1) -> physical screen px inside the target rect."""
        left, top, right, bottom = self.rect
        tw, th = max(right - left, 1), max(bottom - top, 1)
        if self.keep_aspect and self.pad_size:
            pad_aspect, ta = self.pad_size[0] / self.pad_size[1], tw / th
            if pad_aspect > ta:   # pad wider than target: use a centered vertical slice
                f = ta / pad_aspect
                x = (x - (1 - f) / 2) / f
            else:                 # pad taller than target: use a centered horizontal slice
                f = pad_aspect / ta
                y = (y - (1 - f) / 2) / f
        x = min(max(x, 0.0), 1.0)
        y = min(max(y, 0.0), 1.0)
        return round(left + x * (tw - 1)), round(top + y * (th - 1))

    def run_action(self, action):
        if action in ("right_click", "middle_click"):
            down, up = BUTTONS[action.split("_")[0]]
            send_raw(down)  # at the current cursor position
            send_raw(up)
        elif action == "next_screen":
            if self.on_next_screen:
                self.on_next_screen()
        elif action.startswith("key:"):
            vks = parse_shortcut(action[4:])
            if vks:
                press_shortcut(vks)

    def _gesture(self, gesture):
        if gesture:
            action = {"click": self.button_click, "double": self.button_double,
                      "long": self.button_long}[gesture]
            self.log(f"pen button {gesture}: {action}")
            self.run_action(action)

    def _session(self, conn):
        held_up = 0     # UP flag of the mouse button currently held down
        held_keys = []  # keys held down during this touch ("left+shift", "scroll+ctrl" etc.)
        scroll_y = None  # while scrolling: pad y (phone px) where the next wheel step is measured from
        deciding = None  # "panzoom" touch that hasn't picked pan or zoom yet: (pad x, pad y, px, py, keys)
        anchor = None   # while still inside the tap dead zone: pen-down (screen x, y, pad x, pad y)
        gestures = ButtonGestures()

        def mm(v):  # millimetres on the phone -> phone px
            return v * self.pad_dpi / 25.4
        button_was = False

        def release(px=None, py=None):
            nonlocal held_up, held_keys, scroll_y, deciding
            if held_up:
                if px is None:
                    send_raw(held_up)
                else:
                    send_mouse(px, py, held_up)
            for vk in reversed(held_keys):
                send_key(vk, up=True)
            held_up, held_keys, scroll_y, deciding = 0, [], None, None

        def touch_active():
            return bool(held_up or held_keys or scroll_y is not None or deciding)

        last_rx = time.monotonic()
        touch_rect = None  # target rect when the current touch began
        try:
            for pkt in packets(conn, self._stop):
                now = time.monotonic()
                if pkt is None:  # quiet moment: only timers
                    if now - last_rx > PHONE_TIMEOUT_S:
                        self.log("phone stopped responding")
                        return  # finally releases; the app reconnects when it's back
                    self._gesture(gestures.tick(now))
                    continue
                last_rx = now
                # pressure / tilt / orientation are unused by the mouse backend (reserved for a pen backend)
                typ, btn, x, y, _pressure, _tilt, _orient = pkt
                if typ == HELLO:  # x, y = pad size in px; the pressure slot = screen dpi (0 from old apps)
                    ok = math.isfinite(x) and math.isfinite(y) and x > 0 and y > 0
                    self.pad_size = (x, y) if ok else None
                    dpi = _pressure
                    self.pad_dpi = dpi if math.isfinite(dpi) and 100 <= dpi <= 1000 else DEFAULT_DPI
                    continue
                if typ not in (HOVER, DOWN, MOVE, UP, LEAVE) or not (math.isfinite(x) and math.isfinite(y)):
                    continue  # PING, unknown (newer app) or garbage
                if touch_active() and (typ == HOVER or self.rect != touch_rect):
                    # Hovering means the pen is up, so an UP got lost; or the screen was switched
                    # mid-drag. Either way end the touch here instead of dragging on.
                    release()
                    anchor = None
                button = bool(btn & BTN_BARREL)
                px, py = self.map(x, y)
                pad_x, pad_y = (x * self.pad_size[0], y * self.pad_size[1]) if self.pad_size else (x * 1000, y * 1000)
                if typ == MOVE and deciding:
                    sx, sy, spx, spy, keys = deciding
                    if math.hypot(pad_x - sx, pad_y - sy) >= mm(PANZOOM_DECIDE_MM):
                        deciding = None
                        if abs(pad_x - sx) > abs(pad_y - sy):  # sideways: grab at the touch point and pan
                            down_flag, held_up = BUTTONS["middle"]
                            send_mouse(spx, spy, down_flag)
                        else:                                  # up/down: zoom, counting the distance so far
                            held_keys = (parse_shortcut(keys) or []) if keys else []
                            for vk in held_keys:
                                send_key(vk)
                            scroll_y = sy
                if typ == MOVE and deciding:
                    pass  # still deciding: the cursor stays at the touch point
                elif typ == MOVE and scroll_y is not None:
                    steps = int((scroll_y - pad_y) / max(self.scroll_step_px, 1))  # dragging up = positive
                    if steps:
                        send_wheel(steps)
                        scroll_y -= steps * max(self.scroll_step_px, 1)
                elif typ in (HOVER, MOVE):
                    if anchor and math.hypot(pad_x - anchor[2], pad_y - anchor[3]) < mm(self.tap_deadzone_mm):
                        pass  # wobble while pressing: the cursor stays where the pen came down
                    else:
                        anchor = None
                        send_mouse(px, py)
                    if typ == HOVER and button != button_was:
                        if button:
                            gestures.press(now)
                        else:
                            self._gesture(gestures.release(now, wait_for_double=self.button_double != "none"))
                elif typ == DOWN:
                    self._gesture(gestures.flush())
                    if touch_active():  # missed an UP somewhere
                        release(px, py)
                    touch_rect = self.rect
                    name, _, keys = (self.barrel if button else "left").partition("+")
                    if name == "panzoom":  # keys (ctrl) only once it turns out to be a zoom
                        send_mouse(px, py)
                        deciding = (pad_x, pad_y, px, py, keys)
                    else:
                        held_keys = (parse_shortcut(keys) or []) if keys else []
                        for vk in held_keys:
                            send_key(vk)
                        if name == "scroll":  # no button press; the wheel acts around the touch point
                            send_mouse(px, py)
                            scroll_y = pad_y
                        else:
                            down_flag, held_up = BUTTONS.get(name, BUTTONS["right"])
                            send_mouse(px, py, down_flag)
                            anchor = (px, py, pad_x, pad_y) if self.tap_deadzone_mm > 0 else None
                elif typ in (UP, LEAVE):
                    if touch_active():
                        release(*(anchor[:2] if anchor else (px, py)))
                    anchor = None
                    if typ == LEAVE:
                        gestures.reset()
                button_was = button
                self._gesture(gestures.tick(now))
        finally:
            if held_up or held_keys:  # never leave a button or key stuck on disconnect
                release()


def parse_target(s):
    """'2' -> monitor index 2; '0,0,1920,1080' -> rectangle in physical px."""
    parts = [p.strip() for p in s.split(",")]
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a monitor number or LEFT,TOP,RIGHT,BOTTOM: {s!r}")
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 4 and nums[0] < nums[2] and nums[1] < nums[3]:
        return tuple(nums)
    raise argparse.ArgumentTypeError(f"rectangle must be LEFT,TOP,RIGHT,BOTTOM with left<right, top<bottom: {s!r}")


def main():
    ap = argparse.ArgumentParser(description="PenPad server: stylus on the phone -> Windows mouse.")
    ap.add_argument("--target", type=parse_target, default=TARGET,
                    help="monitor number (0 = primary, see the list printed at start) or "
                         "LEFT,TOP,RIGHT,BOTTOM in physical px. Default: TARGET in this file")
    # argparse reads "--target -1920,0,0,1080" as a missing value followed by an option;
    # glue the value on so negative rectangles work with a space too.
    argv = sys.argv[1:]
    for i in range(len(argv) - 1):
        if argv[i] == "--target":
            argv[i:i + 2] = [f"--target={argv[i + 1]}"]
            break
    target = ap.parse_args(argv).target

    mons = monitors()
    for i, (left, top, right, bottom) in enumerate(mons):
        print(f"monitor {i}: {(left, top, right, bottom)}  {right - left}x{bottom - top}"
              + ("  (primary)" if i == 0 else ""))
    if isinstance(target, int) and not 0 <= target < len(mons):
        ap.error(f"--target {target}: there are only monitors 0..{len(mons) - 1}")
    rect = target if isinstance(target, tuple) else mons[target]
    print(f"target: {rect}")

    current = target if isinstance(target, int) else -1

    def next_screen():  # "next_screen" pen button action: cycle through the monitors
        nonlocal current
        current = (current + 1) % len(mons)
        server.rect = mons[current]
        print(f"target: monitor {current} {server.rect}")

    try:
        server = Server(rect, on_next_screen=next_screen)
    except OSError as e:
        sys.exit(f"cannot listen on {HOST}:{PORT} ({e.strerror}). Is PenPad already running?")
    server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
