# PenPad

Use an Android phone's stylus as a pen tablet for a Windows PC. There is no screen mirroring: you draw on the black phone screen and watch the monitor, like a Wacom Intuos. The pen moves the Windows cursor as a mouse, which keeps it predictable in DaVinci Resolve.

```
penpad/
├── android/
│   ├── AndroidManifest.xml
│   ├── build.bat                  builds the APK, no Gradle or Android Studio
│   ├── res/                       app icon (vector, no image files)
│   └── src/com/example/penpad/MainActivity.java
└── pc/
    ├── penpad_gui.py              Windows app: screen picker, settings, status
    ├── penpad_server.py           receives pen events, moves the mouse (also usable on its own)
    ├── build_exe.bat              builds dist\PenPad.exe
    ├── penpad.ico                 Windows icon
    └── make_icon.py               redraws penpad.ico
```

Requirements: Windows 10 or 11, [uv](https://docs.astral.sh/uv/) (runs Python), an Android 11+ phone with an **active** stylus (S Pen, USI pen and similar). Passive rubber-tip styluses register as fingers and are ignored.

**Just want to use it?** The prebuilt files are in this repo: [`pc/dist/PenPad.exe`](pc/dist/PenPad.exe) and [`android/build/PenPad.apk`](android/build/PenPad.apk). You don't need the SDK, Python or uv; follow [Share PenPad](#share-penpad). The setup below is only for building PenPad yourself.

---

## One-time setup (PC)

### 1. Install a JDK and uv

```
winget install Microsoft.OpenJDK.17
winget install astral-sh.uv
```

Open a new terminal afterwards and check that `java -version` and `uv --version` work. uv downloads a Python by itself the first time it needs one.

### 2. Install the Android command-line tools

1. Go to <https://developer.android.com/studio>, scroll to **Command line tools only**, and download the Windows zip.
2. Create the folder `C:\Android\cmdline-tools\latest`. The zip contains `cmdline-tools\bin`, `cmdline-tools\lib` and a few files; move all of them into `latest`, so this file exists:
   ```
   C:\Android\cmdline-tools\latest\bin\sdkmanager.bat
   ```
   The `latest` folder is required. sdkmanager refuses to run from any other layout.
3. Set the SDK location and PATH. Press Win, search for **Edit environment variables for your account**, and under **User variables**:
   - **New**: name `ANDROID_HOME`, value `C:\Android`
   - **Path → Edit → New**: add `C:\Android\cmdline-tools\latest\bin` and `C:\Android\platform-tools`. The second folder appears in step 3.
4. Close and reopen your terminal. Terminals only read environment variables when they start. **In VS Code, quit every window and start it again.** A new terminal tab or *Reload Window* still has the old environment.

   Check it with `echo $env:ANDROID_HOME` (PowerShell) or `echo %ANDROID_HOME%` (cmd).

### 3. Install the SDK packages

See which versions exist, then accept the licenses:

```
sdkmanager --list | findstr "build-tools; platforms;android-"
sdkmanager --licenses
```

From the list, pick the newest **stable** entry of each kind:

- **Platform** (the Android version the app is compiled against): the highest `platforms;android-…`. Skip entries ending in `-beta…` or `-ext…` and the `canary` ones.
- **Build tools** (compiler and packaging tools): the highest `build-tools;…`. Skip entries ending in `-rc…`.

Then install them. As of October 2026 that is:

```
sdkmanager "platform-tools" "platforms;android-37.2" "build-tools;37.0.0"
```

You don't need to tell `build.bat` which versions you picked. It uses the highest stable platform and build tools it finds in `C:\Android`.

## One-time setup (phone)

1. **Settings → About phone → Software information → Build number**: tap it 7 times to enable Developer options.
2. **Settings → Developer options → USB debugging**: turn it on.
3. Connect the phone by USB and run `adb devices`. Accept the "Allow USB debugging?" prompt on the phone and tick "Always allow".
4. Samsung: turn off **Air command** (Settings → Advanced features → S Pen), otherwise it captures the pen button.

---

## Build and install

From the project folder:

```
cd android
.\build.bat install
```

This builds `build\PenPad.apk`, installs it, and sets up the USB tunnel (`adb reverse`). The first run also creates `debug.keystore`. Keep that file: Android refuses updates signed with a different key. If you lose it, uninstall PenPad from the phone first.

`.\build.bat` with no argument builds only. The `.\` is required in PowerShell (the default VS Code terminal) and harmless in cmd.

## Use it

1. On the PC, start the Windows app: `pc\dist\PenPad.exe` (see [Build the Windows app](#build-the-windows-app)), or from the project folder `uv run --no-project pc\penpad_gui.py`.
2. On the phone: open **PenPad**. The window turns to **Phone connected**.
3. Hover the pen to move the cursor, touch to click or drag. The pen button does more; see [Pen button](#pen-button).

The app keeps the USB tunnel (`adb reverse`) up by itself, so re-plugging the phone just works; the line under the status says what's missing if it doesn't.

### Choosing the screen

- **Screen 0, 1, 2…**: the phone maps to that whole monitor. Switching works while you draw.
- **Identify screens** shows each screen's number on it for a moment.
- **Select region…** dims all screens; drag a rectangle, e.g. around the Resolve viewer. Mapping a smaller area gives much finer control for masks. The region is remembered and appears as an extra choice.
- **Ctrl+Alt+Shift+M** (anywhere, even while Resolve has focus) switches to the next screen/region and shows its number on it.
- **Refresh** re-reads the monitors after you plug one in or change the layout.

### Pen settings

| Setting | What it does |
|---|---|
| Keep aspect ratio | Keeps circles round. The unused strips at the edges of the phone clamp to the screen edge. |
| Tap dead zone (mm) | After the pen touches down, the cursor stays still until the pen has moved this far **on the phone**, so a wobbly tap is still a clean click. This keeps curve power-window point clicks from turning into tangent drags. Default 1.5 mm; raise it if taps still turn into small drags, lower it if the start of drags feels sticky. `0` turns it off. It's measured on the phone, so it feels the same whichever screen or region you map to. |

### Pen button

Pressing the side button while the pen **hovers** (not touching) can do something different for a click, a double click and a long press (held about 0.6 s):

| Gesture | Default | Choices |
|---|---|---|
| Click | Right click | Nothing, Right click, Middle click, Next screen, Keyboard shortcut |
| Double click | Keyboard shortcut `ctrl+z` (undo) | same |
| Long press | Next screen | same |

Clicks happen where the cursor is. For **Keyboard shortcut**, type the keys in the field next to it, joined with `+`: `ctrl+z`, `ctrl+shift+s`, `space`, `f5`, `alt+[`. The field turns red if a key name isn't understood. Key names: `ctrl`, `shift`, `alt`, `win`, letters, digits, `f1`–`f24`, `space`, `enter`, `tab`, `esc`, `backspace`, `delete`, `insert`, `home`, `end`, `pageup`, `pagedown`, arrow keys as `left`/`right`/`up`/`down`, and `; = , - . / [ ] \ ' `` ` ``.

When a double click is set, a single click waits about 0.3 s to make sure no second click follows. Set Double click to **Nothing** if you want single clicks to be instant.

**Held while touching** decides what the pen does when you touch down with the button held:

- **Right drag** (default) or **Middle drag** (pans the viewer in Resolve).
- **Left drag + Shift / Ctrl / Alt**: a normal drag with that key held.
- **Scroll (drag up/down)**: works like a mouse wheel. Drag up to scroll up, drag down to scroll down. The cursor stays where you touched, so zooming centers on that point.
- **Ctrl + scroll (zoom)**: the same with Ctrl held, which zooms in many apps. Use whichever of the two zooms in the app you're in.
- **Pan or zoom** / **Pan or Ctrl + zoom**: the first few millimetres of each drag decide what it does until you lift the pen:
  - **start sideways**: grab and move the image (a middle-button drag); after that it follows the pen in any direction.
  - how it decides: once the pen is 2.5 mm from where it touched down (on the phone), PenPad compares the sideways and up/down distance so far. More sideways means pan; more up/down (or exactly diagonal) means zoom. Wobble smaller than 2.5 mm decides nothing.
  - **start up or down**: zoom, like the scroll modes above (with Ctrl held in the Ctrl variant).

  Lift and touch again to switch between moving and zooming.

**Scroll / zoom speed** (shown for the scroll and zoom modes) sets how far you drag per wheel step: Slow, Normal or Fast.

Taps and double taps with the pen tip always stay left clicks and double clicks. There's no long-press-with-the-tip action on purpose: holding a point still before dragging it is normal in Resolve and would trigger it by accident.

The Galaxy S Pen has one button and no eraser end; the settings above are all it can do.

Settings are saved in `%APPDATA%\PenPad\settings.json`. If that file gets damaged, PenPad starts with defaults for whatever it can't read. Connections and errors are logged to `%APPDATA%\PenPad\penpad.log`.

### When something goes wrong mid-stroke

PenPad never leaves a mouse button or key (Shift, Ctrl, …) held down. It releases them, ends the drag, and carries on when:

- the cable is pulled, the phone app is closed, or it goes to the background
- the phone app freezes: after 2 s without a sign of life, the PC drops the connection, and the app reconnects by itself once it responds again
- the screen is switched mid-drag: the drag ends instead of continuing on the other screen
- the PenPad window is closed
- a "pen lifted" message gets lost: the next hover or touch ends the old drag first

A monitor that's turned off or unplugged is handled too: the phone maps to the primary screen until the chosen one is back.

What PenPad can't undo is being killed from Task Manager mid-drag. If a button seems stuck afterwards, click the real mouse once.

### Without the window

`penpad_server.py` still runs on its own in a terminal, with the defaults at the top of the file (`TARGET`, `KEEP_ASPECT`, `TAP_DEADZONE_MM`, `BARREL`, `SCROLL_STEP_PX`, `PANZOOM_DECIDE_MM`, `BUTTON_CLICK`, `BUTTON_DOUBLE`, `BUTTON_LONG`). "Next screen" there cycles through the monitors:

```
uv run --no-project pc\penpad_server.py --target 2
uv run --no-project pc\penpad_server.py --target 2560,-605,4480,1555
```

`--target` takes a monitor number (they're printed at start) or a rectangle `LEFT,TOP,RIGHT,BOTTOM` in physical pixels. It doesn't switch screens while running or set up the tunnel: after re-plugging USB, run `adb reverse tcp:47300 tcp:47300`. Only one of the window and the terminal server can run at a time.

## Build the Windows app

```
cd pc
.\build_exe.bat
```

This produces `pc\dist\PenPad.exe`, a single file that runs without Python or uv; copy it anywhere. uv fetches PyInstaller for the build and doesn't install it. Rebuild after changing `penpad_gui.py` or `penpad_server.py`.

`adb` from your platform-tools is packed into the exe too, so it also works on PCs without the Android SDK. On such a PC it's copied to `%LOCALAPPDATA%\PenPad\adb` on first start. If the PC has its own adb (Android SDK, or `adb` on PATH), PenPad uses that one instead, because two different adb versions keep restarting each other.

Some antivirus programs flag new PyInstaller exes they haven't seen before. If yours does, allow it or use `uv run --no-project pc\penpad_gui.py` instead.

To change the icon, edit `make_icon.py` (and `android\res\drawable\ic_launcher_foreground.xml`, the same design for the phone), then run `uv run --no-project --with pillow make_icon.py` in `pc`.

## Share PenPad

Send two files: `pc\dist\PenPad.exe` and `android\build\PenPad.apk`. The other person needs no SDK, Python or uv, only:

1. An Android 11+ phone with an active stylus (see Requirements).
2. On the phone: open the APK and allow installing apps from that source when asked. Play Protect may warn about an unknown app; choose to install anyway.
3. USB debugging, as in [One-time setup (phone)](#one-time-setup-phone), steps 1, 2 and 4. Instead of `adb devices` in step 3, plug the phone in with PenPad.exe running and accept the prompt on the phone.
4. Then [Use it](#use-it) as usual.

Updates you send later install over the old app, as long as you build them with the same `debug.keystore`.

## Wi-Fi instead of USB

Higher latency and occasional jitter, but no cable:

1. In `MainActivity.java`, set `HOST` to the PC's LAN IP (`ipconfig`), then run `.\build.bat install`.
2. In `penpad_server.py`, change `"127.0.0.1"` to `"0.0.0.0"` on the `HOST, PORT = ...` line. This applies to the window too; rebuild `PenPad.exe` if you use it.
3. Allow PenPad (or Python) through Windows Firewall for **private** networks when it asks.
4. Turn off "Set up the USB tunnel automatically" in the window; it's not needed.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `ANDROID_HOME is not set` | Open a **new** terminal after setting it. In VS Code, quit all windows and restart; *Reload Window* is not enough. |
| `sdkmanager` / `adb` "is not recognized" | The PATH entries from step 2 are missing, or the terminal was opened before you added them. |
| `build.bat` "is not recognized" (PowerShell) | Type `.\build.bat`. PowerShell doesn't run scripts from the current folder without `.\`. |
| sdkmanager: "Could not determine SDK root" | The folder layout is wrong; it must be `cmdline-tools\latest\bin`. |
| "Python was not found" during build | uv isn't installed or isn't on PATH, so `build.bat` fell back to the `python` command, which here is only a Microsoft Store shortcut. Install uv (step 1), then restart the terminal (or VS Code). |
| `uv` "is not recognized" | Install it (step 1), then open a new terminal. In VS Code, quit all windows and restart. |
| `INSTALL_FAILED_UPDATE_INCOMPATIBLE` | It was signed with a different key. Uninstall PenPad from the phone and install again. |
| Window stays on "Waiting for the phone…" | Read the line under it. "No phone on USB": check the cable and USB debugging. "Accept the USB debugging prompt": look at the phone. "adb not found": you're running `penpad_gui.py` without platform-tools; install them (step 3) or use `PenPad.exe`. Then open PenPad on the phone. |
| Terminal server never prints `connected` | Run `adb reverse --list`. If it's empty, run `adb reverse tcp:47300 tcp:47300`. |
| Connected, but the pen does nothing | The stylus is passive (reports as a finger), or another screen is selected than the one you're watching. Use **Identify screens**. |
| Cursor lands in the wrong place | Check the selected screen or region. For `--target` / `TARGET`, use physical pixels (unaffected by Windows display scaling); **Select region…** in the window does this for you. |
| "Cannot listen on port 47300" / "cannot listen on 127.0.0.1:47300" | PenPad is already running: in another terminal, or as the window (check the taskbar). Close that one first. |
| Ctrl+Alt+Shift+M does nothing | The window says so if another program already uses it. Otherwise make sure the checkbox is on and PenPad is running. |
| Works everywhere except Resolve | Resolve is running as administrator. Run the server as administrator too, because Windows blocks input from lower-privileged processes. |
| Pen button does nothing | Samsung Air command is still on, or the pen has no button. Also check the [Pen button](#pen-button) settings; "Nothing" is a choice. |
| Keyboard shortcut does nothing in Resolve | Resolve is running as administrator, see above. Or the field is red: a key name isn't understood. |
| Single pen-button click feels slow | It waits ~0.3 s for a possible double click. Set Double click to **Nothing** to make it instant. |
| Phone keeps connecting and disconnecting every few seconds | The phone app is older than the PC side. Run `.\build.bat install` in `android` so both match. |
| Window shows "PenPad stopped working" (red) | Restart PenPad. The reason is in `%APPDATA%\PenPad\penpad.log`; please keep that file for a bug report. |
| PenPad.exe doesn't open at all | Look at `%APPDATA%\PenPad\penpad.log`. If something is wrong with the settings, deleting `settings.json` there resets them. |

## Adding pressure later

The phone already sends pressure, tilt and orientation; the server currently ignores them. Pressure support means adding a pen backend to the server, using Windows' synthetic pointer API (`CreateSyntheticPointerDevice` / `InjectSyntheticPointerInput`), as an alternative to `send_mouse()`. The phone app doesn't change.

## License

[MIT](LICENSE). The bundled `adb` comes from the Android SDK platform-tools and is under the Apache License 2.0; its `NOTICE.txt` is included in `PenPad.exe`.
