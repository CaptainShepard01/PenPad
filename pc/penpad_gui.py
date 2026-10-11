"""
PenPad for Windows: a small window around penpad_server.Server.

Run:    uv run --no-project penpad_gui.py      (or dist\\PenPad.exe, see build_exe.bat)

- pick the screen (or a region of one) the phone maps to, also while drawing
- Ctrl+Alt+Shift+M switches to the next screen from anywhere
- pen button click / double click / long press (while hovering) and button + touch are programmable
- keeps the USB tunnel (adb reverse) up, so re-plugging the phone just works
- settings are saved in %APPDATA%\\PenPad\\settings.json
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import messagebox, ttk

import penpad_server as core  # also makes this process per-monitor DPI aware: all px are physical

APP_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "PenPad")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
LOG_FILE = os.path.join(APP_DIR, "penpad.log")
DEFAULTS = {
    "target": 0,         # monitor index, or "region"
    "region": None,      # [left, top, right, bottom] in physical px
    "keep_aspect": core.KEEP_ASPECT,
    "tap_deadzone_mm": core.TAP_DEADZONE_MM,
    "barrel": core.BARREL,
    "scroll_step_px": core.SCROLL_STEP_PX,
    "button_click": core.BUTTON_CLICK,
    "button_double": core.BUTTON_DOUBLE,
    "button_long": core.BUTTON_LONG,
    "auto_tunnel": True,
    "hotkey": True,
}
BARREL_LABELS = {"right": "Right drag", "middle": "Middle drag (pan)", "left+shift": "Left drag + Shift",
                 "left+ctrl": "Left drag + Ctrl", "left+alt": "Left drag + Alt",
                 "scroll": "Scroll (drag up/down)", "scroll+ctrl": "Ctrl + scroll (zoom)",
                 "panzoom": "Pan or zoom", "panzoom+ctrl": "Pan or Ctrl + zoom"}
SCROLL_SPEEDS = {"Slow": 100, "Normal": 60, "Fast": 30}  # phone px dragged per wheel step
ACTION_LABELS = {"none": "Nothing", "right_click": "Right click", "middle_click": "Middle click",
                 "next_screen": "Next screen", "key": "Keyboard shortcut"}
HOTKEY_TEXT = "Ctrl+Alt+Shift+M"
ACCENT = "#4FC3F7"


def resource(name):
    """Path of a file shipped next to this script, or inside the PyInstaller exe."""
    return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), name)


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def load_settings():
    """Saved settings; anything missing, of the wrong type or out of range falls back to its default,
    so a damaged or hand-edited settings.json can't stop PenPad from starting."""
    cfg = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    r = cfg["region"]
    if not (isinstance(r, list) and len(r) == 4 and all(map(is_int, r)) and r[0] < r[2] and r[1] < r[3]):
        cfg["region"] = None
    t = cfg["target"]
    if not ((is_int(t) and t >= 0) or (t == "region" and cfg["region"])):
        cfg["target"] = 0
    for key in ("keep_aspect", "auto_tunnel", "hotkey"):
        if not isinstance(cfg[key], bool):
            cfg[key] = DEFAULTS[key]
    dz = cfg["tap_deadzone_mm"]  # the old "tap_deadzone_px" (monitor px, far too small) is ignored
    if not (isinstance(dz, (int, float)) and not isinstance(dz, bool) and 0 <= dz <= 10):
        cfg["tap_deadzone_mm"] = DEFAULTS["tap_deadzone_mm"]
    if cfg["barrel"] not in BARREL_LABELS:
        cfg["barrel"] = DEFAULTS["barrel"]
    if cfg["scroll_step_px"] not in SCROLL_SPEEDS.values():
        cfg["scroll_step_px"] = DEFAULTS["scroll_step_px"]
    for key in ("button_click", "button_double", "button_long"):
        action = cfg[key]
        if not isinstance(action, str) or (action not in ACTION_LABELS and not action.startswith("key:")):
            cfg[key] = DEFAULTS[key]
    return cfg


def save_settings(cfg):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"  # write then swap, so a crash mid-write can't leave half a file
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except OSError:
        pass


def log(line):
    """Appends to %APPDATA%\\PenPad\\penpad.log (the exe has no console). Kept under ~1 MB."""
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 1_000_000:
            os.replace(LOG_FILE, LOG_FILE + ".old")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {line}\n")
    except OSError:
        pass


def find_adb():
    """An installed adb first (two adb versions keep restarting each other's server), else the bundled one."""
    sdks = [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"), r"C:\Android",
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk")]
    for sdk in filter(None, sdks):
        adb = os.path.join(sdk, "platform-tools", "adb.exe")
        if os.path.isfile(adb):
            return adb
    return shutil.which("adb") or bundled_adb()


def bundled_adb():
    """adb shipped inside the exe (build_exe.bat), copied to %LOCALAPPDATA%\\PenPad\\adb first: the adb
    server it starts outlives this app, and from the exe's temp folder it would block that folder's cleanup."""
    src = resource("adb")
    if not os.path.isdir(src):
        return None
    dst = os.path.join(os.environ.get("LOCALAPPDATA") or APP_DIR, "PenPad", "adb")
    try:
        os.makedirs(dst, exist_ok=True)
        for name in os.listdir(src):
            s, d = os.path.join(src, name), os.path.join(dst, name)
            if not os.path.isfile(d) or os.path.getsize(d) != os.path.getsize(s):
                shutil.copy2(s, d)
    except OSError:  # e.g. an older copy is running as the adb server; keep using that one
        log("adb copy: " + traceback.format_exc())
    adb = os.path.join(dst, "adb.exe")
    return adb if os.path.isfile(adb) else None


class Tunnel(threading.Thread):
    """Re-runs `adb reverse` every few seconds while the phone isn't connected, so the app
    can reach the server again after the USB cable was re-plugged."""

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.adb = find_adb()
        self.status = "adb not found. Use PenPad.exe or install platform-tools (README step 3)." if not self.adb else ""
        self.wake = threading.Event()

    def run(self):
        while self.adb:
            try:
                if self.app.cfg["auto_tunnel"] and not (self.app.server and self.app.server.peer):
                    self.status = self._reverse()
            except Exception as e:  # keep trying; a single odd adb result must not end the tunnel
                self.status = f"adb error: {e}"
                log("tunnel: " + traceback.format_exc())
            self.wake.wait(3)
            self.wake.clear()

    def _reverse(self):
        port = f"tcp:{core.PORT}"
        try:
            r = subprocess.run([self.adb, "reverse", port, port], capture_output=True, text=True,
                               errors="replace", timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"adb failed: {e}"
        err = (r.stderr or r.stdout).strip().lower()
        if r.returncode == 0:
            return "USB tunnel ready. Open PenPad on the phone."
        if "unauthorized" in err:
            return "Phone found. Accept the USB debugging prompt on it."
        if "more than one" in err:
            return "Several Android devices connected. Unplug the others."
        if "no devices" in err or "not found" in err:
            return "No phone on USB (or USB debugging is off)."
        return f"adb: {err.splitlines()[-1] if err else 'error'}"


class Hotkey(threading.Thread):
    """System-wide hotkey via RegisterHotKey; each press is put on `presses`."""
    MODS = 0x0001 | 0x0002 | 0x0004 | 0x4000  # Alt | Ctrl | Shift | NoRepeat
    VK = ord("M")

    def __init__(self, presses):
        super().__init__(daemon=True)
        self.presses = presses
        self.registered = threading.Event()
        self.failed = False
        self.thread_id = None

    def run(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, 1, self.MODS, self.VK):
            self.failed = True  # another program already uses it
            self.registered.set()
            return
        self.registered.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                self.presses.put(1)
        user32.UnregisterHotKey(None, 1)

    def stop(self):
        """Unregisters the hotkey; returns once it's free to be registered again."""
        if self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)  # WM_QUIT
        self.join(1)


def popup(root, rect, text, color, ms=900):
    """Large label centered on rect for a moment (identify / switch feedback)."""
    left, top, right, bottom = rect
    size = max(120, min(320, (right - left) // 6, (bottom - top) // 3))
    w = tk.Toplevel(root)
    w.overrideredirect(True)
    w.attributes("-topmost", True)
    w.attributes("-alpha", 0.85)
    w.configure(bg="#11141c", highlightthickness=4, highlightbackground=color)
    tk.Label(w, text=text, fg=color, bg="#11141c", font=("Segoe UI", -size // 2, "bold")).pack(expand=True)
    width = max(size, len(text) * size // 3)
    cx, cy = (left + right) // 2, (top + bottom) // 2
    w.geometry(f"{width}x{size}+{cx - width // 2}+{cy - size // 2}")
    w.after(ms, w.destroy)


class RegionPicker:
    """Dims all screens; the user drags out the rectangle the phone should map to."""

    def __init__(self, root, monitors, on_done):
        self.on_done = on_done
        self.start = None
        self.done = False
        self.vx, self.vy, vw, vh = core.virtual_desktop()
        self.top = t = tk.Toplevel(root)
        # however the overlay goes away (Esc, click, closed by Windows), on_done runs exactly once
        t.bind("<Destroy>", lambda e: e.widget is t and self.finish(None))
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.attributes("-alpha", 0.45)
        t.geometry(f"{vw}x{vh}+{self.vx}+{self.vy}")
        self.c = c = tk.Canvas(t, bg="black", highlightthickness=0, cursor="crosshair")
        c.pack(fill="both", expand=True)
        for left, top, right, bottom in monitors:
            c.create_text((left + right) // 2 - self.vx, (top + bottom) // 2 - self.vy, fill="white",
                          font=("Segoe UI", -28), justify="center",
                          text="Drag to select the area the phone maps to\nEsc or a click to cancel")
        self.box = c.create_rectangle(0, 0, 0, 0, outline=ACCENT, width=3)
        c.bind("<ButtonPress-1>", self.press)
        c.bind("<B1-Motion>", self.drag)
        c.bind("<ButtonRelease-1>", self.release)
        t.bind("<Escape>", lambda e: self.finish(None))
        t.focus_force()

    def press(self, e):
        self.start = (e.x_root, e.y_root)

    def drag(self, e):
        if self.start:
            x0, y0 = self.start
            self.c.coords(self.box, x0 - self.vx, y0 - self.vy, e.x_root - self.vx, e.y_root - self.vy)

    def release(self, e):
        if not self.start:
            return
        (x0, y0), (x1, y1) = self.start, (e.x_root, e.y_root)
        rect = [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
        self.finish(rect if rect[2] - rect[0] >= 50 and rect[3] - rect[1] >= 50 else None)

    def finish(self, rect):
        if self.done:
            return
        self.done = True
        try:
            self.top.destroy()
        except tk.TclError:
            pass
        self.on_done(rect)


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_settings()
        self.monitors = core.monitors()
        self.last_monitor_check = 0.0

        # screen switches requested by the hotkey thread or the pen button (server thread)
        self.next_screen_requests = queue.SimpleQueue()
        self.server = core.Server(self.target_rect(), keep_aspect=self.cfg["keep_aspect"],
                                  tap_deadzone_mm=self.cfg["tap_deadzone_mm"], barrel=self.cfg["barrel"],
                                  scroll_step_px=self.cfg["scroll_step_px"],
                                  button_click=self.cfg["button_click"],
                                  button_double=self.cfg["button_double"],
                                  button_long=self.cfg["button_long"],
                                  on_next_screen=lambda: self.next_screen_requests.put(1), log=self.server_log)
        self.server_thread = threading.Thread(target=self.run_server, daemon=True)
        self.server_thread.start()
        self.quitting = False
        self.tunnel = Tunnel(self)
        self.tunnel.start()
        self.hotkey = None

        self.build_ui()
        self.apply_hotkey()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.poll()

    # ---- server ----------------------------------------------------------------
    def run_server(self):
        try:
            self.server.serve_forever()
        except Exception:
            log("server stopped:\n" + traceback.format_exc())

    @staticmethod
    def server_log(line):
        if not line.startswith("pen button"):  # those are routine; connections and errors are kept
            log(line)

    # ---- target ----------------------------------------------------------------
    def target_rect(self):
        """The chosen screen; the primary one while the chosen monitor is off or unplugged."""
        t = self.cfg["target"]
        if t == "region":
            return tuple(self.cfg["region"])
        return self.monitors[t] if t < len(self.monitors) else self.monitors[0]

    def target_choices(self):
        return list(range(len(self.monitors))) + (["region"] if self.cfg["region"] else [])

    def set_target(self, target, announce=False):
        self.cfg["target"] = target
        self.target_var.set(str(target))
        self.server.rect = self.target_rect()
        save_settings(self.cfg)
        if announce:
            popup(self.root, self.server.rect, "Region" if target == "region" else str(target), ACCENT)

    def next_target(self):
        choices = self.target_choices()
        i = choices.index(self.cfg["target"]) if self.cfg["target"] in choices else -1
        self.set_target(choices[(i + 1) % len(choices)], announce=True)

    def identify(self):
        for i, rect in enumerate(self.monitors):
            popup(self.root, rect, str(i), ACCENT if self.cfg["target"] == i else "white", ms=1500)
        if self.cfg["region"]:
            popup(self.root, self.cfg["region"], "Region",
                  ACCENT if self.cfg["target"] == "region" else "white", ms=1500)

    def pick_region(self):
        self.root.withdraw()

        def open_picker():
            try:
                RegionPicker(self.root, self.monitors, self.region_picked)
            except Exception:  # never leave the main window hidden
                log("region picker:\n" + traceback.format_exc())
                self.root.deiconify()

        self.root.after(150, open_picker)

    def region_picked(self, rect):
        self.root.deiconify()
        if rect:
            self.cfg["region"] = rect
            self.rebuild_targets()
            self.set_target("region")

    def refresh_monitors(self):
        """Re-reads the monitors (also called every 2 s from poll). The chosen screen number is kept,
        so a monitor that was off comes back as the target once it's on again."""
        mons = core.monitors()
        if mons and mons != self.monitors:
            self.monitors = mons
            self.rebuild_targets()
            self.server.rect = self.target_rect()  # the server ends a drag that's in progress

    # ---- UI --------------------------------------------------------------------
    def build_ui(self):
        r = self.root
        r.title("PenPad")
        r.resizable(False, False)
        pad = {"padx": 12, "pady": (10, 0)}

        status = ttk.Frame(r)
        status.pack(fill="x", **pad)
        self.dot = tk.Label(status, text="●", font=("Segoe UI", 14))
        self.dot.pack(side="left")
        self.status_text = ttk.Label(status, font=("Segoe UI", 10, "bold"))
        self.status_text.pack(side="left", padx=(6, 0))
        self.tunnel_text = ttk.Label(r, foreground="gray40")
        self.tunnel_text.pack(fill="x", padx=12)

        screen = ttk.LabelFrame(r, text="Screen the phone maps to", padding=(10, 6))
        screen.pack(fill="x", **pad)
        self.target_var = tk.StringVar(value=str(self.cfg["target"]))
        self.targets_frame = ttk.Frame(screen)
        self.targets_frame.pack(fill="x")
        self.rebuild_targets()
        buttons = ttk.Frame(screen)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Identify screens", command=self.identify).pack(side="left")
        ttk.Button(buttons, text="Select region…", command=self.pick_region).pack(side="left", padx=6)
        ttk.Button(buttons, text="Refresh", command=self.refresh_monitors).pack(side="left")
        self.hotkey_var = tk.BooleanVar(value=self.cfg["hotkey"])
        ttk.Checkbutton(screen, text=f"{HOTKEY_TEXT} switches to the next screen", variable=self.hotkey_var,
                        command=self.apply_hotkey).pack(anchor="w", pady=(8, 0))
        self.hotkey_note = ttk.Label(screen, foreground="#b00020")  # packed only when there's a problem

        opts = ttk.LabelFrame(r, text="Pen", padding=(10, 6))
        opts.pack(fill="x", **pad)
        self.aspect_var = tk.BooleanVar(value=self.cfg["keep_aspect"])
        ttk.Checkbutton(opts, text="Keep aspect ratio (circles stay round)", variable=self.aspect_var,
                        command=self.apply_options).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(opts, text="Tap dead zone (mm):").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.deadzone_var = tk.StringVar(value=f"{self.cfg['tap_deadzone_mm']:g}")
        ttk.Spinbox(opts, from_=0, to=5, increment=0.5, width=5, textvariable=self.deadzone_var
                    ).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        # fires for the arrows and for typing; half-typed or empty input keeps the last valid value
        self.deadzone_var.trace_add("write", lambda *_: self.apply_options())
        ttk.Label(opts, foreground="gray40", wraplength=400, justify="left",
                  text="How far the pen may wobble on the phone after touching down and still be "
                       "a click; the cursor stays put until then. Raise it if taps turn into small "
                       "drags (e.g. curve points getting tangent handles); 0 = off."
                  ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))

        btn = ttk.LabelFrame(r, text="Pen button", padding=(10, 6))
        btn.pack(fill="x", **pad)
        ttk.Label(btn, text="While hovering (pen not touching):", foreground="gray40"
                  ).grid(row=0, column=0, columnspan=3, sticky="w")
        for row, (key, label) in enumerate([("button_click", "Click"), ("button_double", "Double click"),
                                            ("button_long", "Long press")], start=1):
            self.action_row(btn, row, key, label)
        ttk.Label(btn, text="Held while touching:").grid(row=4, column=0, sticky="w", pady=(10, 0))
        self.barrel_var = tk.StringVar(value=BARREL_LABELS[self.cfg["barrel"]])
        barrel = ttk.Combobox(btn, textvariable=self.barrel_var, values=list(BARREL_LABELS.values()),
                              state="readonly", width=21)
        barrel.grid(row=4, column=1, sticky="w", padx=(8, 0), pady=(10, 0))
        barrel.bind("<<ComboboxSelected>>", lambda e: self.apply_options())
        # shown only for the scroll / zoom modes
        self.speed_label = ttk.Label(btn, text="Scroll / zoom speed:")
        self.speed_var = tk.StringVar(value=next(k for k, v in SCROLL_SPEEDS.items()
                                                 if v == self.cfg["scroll_step_px"]))
        self.speed_combo = ttk.Combobox(btn, textvariable=self.speed_var, values=list(SCROLL_SPEEDS),
                                        state="readonly", width=18)
        self.speed_combo.bind("<<ComboboxSelected>>", lambda e: self.apply_options())
        ttk.Label(btn, foreground="gray40", wraplength=400, justify="left",
                  text="Shortcuts look like ctrl+z, ctrl+shift+s, space or f5. "
                       "Tip taps and double taps always stay left clicks."
                  ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(6, 0))

        usb = ttk.LabelFrame(r, text="USB", padding=(10, 6))
        usb.pack(fill="x", padx=12, pady=(10, 12))
        self.tunnel_var = tk.BooleanVar(value=self.cfg["auto_tunnel"])
        ttk.Checkbutton(usb, text="Set up the USB tunnel automatically (adb reverse)",
                        variable=self.tunnel_var, command=self.apply_options).pack(anchor="w")
        self.show_scroll_speed()

    def action_row(self, parent, row, key, label):
        """One programmable pen-button gesture: action choice, plus a shortcut field for "Keyboard shortcut"."""
        action = self.cfg[key]
        kind = "key" if action.startswith("key:") else action
        kind_var = tk.StringVar(value=ACTION_LABELS[kind])
        keys_var = tk.StringVar(value=action[4:] if kind == "key" else "")
        ttk.Label(parent, text=f"{label}:").grid(row=row, column=0, sticky="w", pady=(6, 0))
        combo = ttk.Combobox(parent, textvariable=kind_var, values=list(ACTION_LABELS.values()),
                             state="readonly", width=18)
        combo.grid(row=row, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        entry = ttk.Entry(parent, textvariable=keys_var, width=14)

        def update(*_):
            kind = next(k for k, v in ACTION_LABELS.items() if v == kind_var.get())
            if kind == "key":
                entry.grid(row=row, column=2, sticky="w", padx=(6, 0), pady=(6, 0))
                text = keys_var.get().strip()
                entry.configure(foreground="" if core.parse_shortcut(text) else "#b00020")  # red = not understood
                action = "key:" + text
            else:
                entry.grid_remove()
                action = kind
            self.cfg[key] = action
            setattr(self.server, key, action)
            save_settings(self.cfg)

        combo.bind("<<ComboboxSelected>>", update)
        keys_var.trace_add("write", update)
        update()

    def rebuild_targets(self):
        for w in self.targets_frame.winfo_children():
            w.destroy()
        for i, (left, top, right, bottom) in enumerate(self.monitors):
            text = f"Screen {i}:  {right - left}×{bottom - top}" + ("  (primary)" if i == 0 else "")
            ttk.Radiobutton(self.targets_frame, text=text, value=str(i), variable=self.target_var,
                            command=lambda i=i: self.set_target(i)).pack(anchor="w")
        if self.cfg["region"]:
            left, top, right, bottom = self.cfg["region"]
            text = f"Region:  {right - left}×{bottom - top} at ({left}, {top})"
            ttk.Radiobutton(self.targets_frame, text=text, value="region", variable=self.target_var,
                            command=lambda: self.set_target("region")).pack(anchor="w")

    def apply_options(self):
        try:
            deadzone = float(self.deadzone_var.get().replace(",", "."))
            if not 0 <= deadzone <= 10:
                raise ValueError
        except (tk.TclError, ValueError):
            deadzone = self.cfg["tap_deadzone_mm"]
        barrel = next((k for k, v in BARREL_LABELS.items() if v == self.barrel_var.get()), "right")
        scroll_step = SCROLL_SPEEDS.get(self.speed_var.get(), core.SCROLL_STEP_PX)
        self.cfg.update(keep_aspect=self.aspect_var.get(), tap_deadzone_mm=deadzone, barrel=barrel,
                        scroll_step_px=scroll_step, auto_tunnel=self.tunnel_var.get())
        self.server.keep_aspect, self.server.tap_deadzone_mm, self.server.barrel = (
            self.cfg["keep_aspect"], deadzone, barrel)
        self.server.scroll_step_px = scroll_step
        self.show_scroll_speed()
        self.tunnel.wake.set()
        save_settings(self.cfg)

    def show_scroll_speed(self):
        if self.cfg["barrel"].startswith(("scroll", "panzoom")):
            self.speed_label.grid(row=5, column=0, sticky="w", pady=(6, 0))
            self.speed_combo.grid(row=5, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        else:
            self.speed_label.grid_remove()
            self.speed_combo.grid_remove()

    def apply_hotkey(self):
        self.cfg["hotkey"] = self.hotkey_var.get()
        save_settings(self.cfg)
        if self.hotkey:
            self.hotkey.stop()
            self.hotkey = None
        self.hotkey_note.pack_forget()
        if self.cfg["hotkey"]:
            self.hotkey = Hotkey(self.next_screen_requests)
            self.hotkey.start()
            self.hotkey.registered.wait(2)
            if self.hotkey.failed:
                self.hotkey_note.configure(text=f"{HOTKEY_TEXT} is already used by another program.")
                self.hotkey_note.pack(anchor="w")

    # ---- status loop -----------------------------------------------------------
    def poll(self):
        try:
            self.update_status()
        except Exception:  # log it, but keep polling: the hotkey and status depend on this loop
            log("status loop:\n" + traceback.format_exc())
        if not self.quitting:
            self.root.after(200, self.poll)

    def update_status(self):
        while not self.next_screen_requests.empty():
            self.next_screen_requests.get()
            self.next_target()
        if time.monotonic() - self.last_monitor_check > 2:
            self.last_monitor_check = time.monotonic()
            self.refresh_monitors()
        if not self.server_thread.is_alive():
            self.dot.configure(fg="#b00020")
            self.status_text.configure(text="PenPad stopped working")
            self.tunnel_text.configure(text=f"Please restart it. Details: {LOG_FILE}")
        elif self.server.peer:
            size = self.server.pad_size
            text = "Phone connected" + (f"  ({int(size[0])}×{int(size[1])} pad)" if size else "")
            self.dot.configure(fg="#2e9d4f")
            self.status_text.configure(text=text)
            self.tunnel_text.configure(text="Draw on the phone; the cursor follows on the selected screen.")
        else:
            self.dot.configure(fg="#c98a00")
            self.status_text.configure(text="Waiting for the phone…")
            self.tunnel_text.configure(text=self.tunnel.status if self.cfg["auto_tunnel"] else
                                       f"Run: adb reverse tcp:{core.PORT} tcp:{core.PORT}")

    def quit(self):
        self.quitting = True
        try:
            self.apply_options()
        except Exception:
            log("saving on quit:\n" + traceback.format_exc())
        if self.hotkey:
            self.hotkey.stop()
        self.server.stop()
        # let the server finish: it releases any button or key still held by a drag in progress
        self.server_thread.join(2)
        self.root.destroy()


def main():
    try:  # own taskbar entry and icon instead of python.exe's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PenPad.Server")
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    try:
        root.iconbitmap(default=resource("penpad.ico"))
    except tk.TclError:
        pass
    # errors in button handlers etc.: the exe has no console, so keep them in the log
    root.report_callback_exception = lambda *exc: log("UI:\n" + "".join(traceback.format_exception(*exc)))
    try:
        App(root)
    except OSError as e:
        root.withdraw()
        if getattr(e, "winerror", None) == 10048:  # WSAEADDRINUSE
            messagebox.showerror("PenPad", f"Cannot listen on port {core.PORT}.\n\n"
                                           "PenPad (or penpad_server.py) is probably already running.")
        else:
            log("start:\n" + traceback.format_exc())
            messagebox.showerror("PenPad", f"PenPad could not start: {e}\n\nDetails: {LOG_FILE}")
        return
    except Exception as e:
        log("start:\n" + traceback.format_exc())
        root.withdraw()
        messagebox.showerror("PenPad", f"PenPad could not start: {e}\n\nDetails: {LOG_FILE}")
        return
    root.mainloop()


if __name__ == "__main__":
    main()
