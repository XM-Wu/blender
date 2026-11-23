# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Check monitor persistence across real GUI processes using an isolated user config."""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def screen_rectangles():
    user32 = ctypes.windll.user32
    # The Blender process uses physical pixels. Match it on mixed-DPI desktops.
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    screens = []
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                  ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    @callback
    def collect(handle, dc, rect, data):
        r = rect.contents
        screens.append((r.left, r.top, r.right, r.bottom))
        return True

    assert user32.EnumDisplayMonitors(None, None, collect, 0)
    return screens


# Run after the window manager has received move/resize events.
PROBE = """
import bpy, ctypes, json, os
from pathlib import Path

def check_window():
    win = bpy.context.window_manager.windows[0]
    desktop_height = ctypes.windll.user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
    center = (win.x + win.width // 2, desktop_height - win.y - win.height // 2)
    Path(os.environ["TEST_WINDOW_CENTER"]).write_text(json.dumps(center))
    if os.environ.get("TEST_SAVE_BLEND"):
        bpy.ops.wm.save_as_mainfile(filepath=os.environ["TEST_SAVE_BLEND"], copy=True)
    bpy.ops.wm.quit_blender()

bpy.app.timers.register(check_window, first_interval=0.7)
"""


def main(executable):
    screens = screen_rectangles()
    if len(screens) < 2:
        print("Requires two attached displays")
        return 77
    primary = next(rect for rect in screens if rect[:2] == (0, 0))
    secondary = next(rect for rect in screens if rect != primary)

    desktop_height = ctypes.windll.user32.GetSystemMetrics(79)

    def geometry(rect):
        return ["--window-geometry", str(rect[0] + 30), str(desktop_height - rect[1] - 510), "640", "480"]

    with tempfile.TemporaryDirectory() as temp:
        folder = Path(temp)
        blend = folder / "primary.blend"

        def launch(expected, args, save=False):
            result = folder / "center.json"
            result.unlink(missing_ok=True)
            env = os.environ.copy()
            env.update(BLENDER_USER_CONFIG=temp, TEMP=temp, TMP=temp, TEST_WINDOW_CENTER=str(result),
                       TEST_SAVE_BLEND=str(blend) if save else "")
            subprocess.run([executable, "--factory-startup", *args, "--python-expr", PROBE],
                           env=env, cwd=temp, timeout=60, check=True)
            x, y = json.loads(result.read_text())
            assert expected[0] <= x < expected[2] and expected[1] <= y < expected[3], (expected, x, y)

        launch(primary, geometry(primary), save=True)
        launch(secondary, [*geometry(secondary), "--window-maximized"])
        launch(secondary, [])
        launch(secondary, [str(blend)])
        launch(primary, geometry(primary))
        # Simulate reconnecting with a different monitor topology.
        (folder / "recents.toml").write_text('["main.window"]\nmonitor = "disconnected-display"\n')
        launch(primary, [])

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
