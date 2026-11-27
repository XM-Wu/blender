# SPDX-License-Identifier: GPL-2.0-or-later
"""Run with --enable-event-simulate examples/soft_body/Slime-Live-Grab.blend --python this_file."""

import os
import sys
import time
import traceback
import zlib

import bpy
import gpu
import numpy as np
from bpy_extras.view3d_utils import location_3d_to_region_2d
from mathutils import Vector

import xblender_gel as gel


def viewport_test():
    window = bpy.context.window_manager.windows[0]
    scene = window.scene
    body = scene.objects["Slime"]
    rod = body.volumetric_gel.collider
    area = next(a for a in window.screen.areas if a.type == 'VIEW_3D')
    region = next(r for r in area.regions if r.type == 'WINDOW')
    view = area.spaces.active.region_3d
    depths = []
    sampling = False

    def draw():
        if not sampling or bpy.context.region != region:
            return
        now = time.monotonic()
        if depths and now - depths[-1][0] < .04:
            return
        # POST_VIEW is bound to the overlay framebuffer, which shares the
        # actual scene depth. Reading it here does not request another redraw.
        fb = gpu.state.active_framebuffer_get()
        x, y, width, height = gpu.state.viewport_get()
        crc = 0
        for row in (.38, .44, .50, .56, .62):
            buf = fb.read_depth(x + width // 3, y + int(height * row), width // 3, 1)
            crc = zlib.crc32(np.frombuffer(buf, dtype=np.float32).round(6), crc)
        depths.append((now, crc))

    def observe(label):
        nonlocal sampling
        depths.clear()
        sampling = True
        start = time.monotonic()
        matrix = view.view_matrix.copy()
        initial = gel._live.sim.gel.x.copy()
        yield .7
        sampling = False
        # Exclude draws caused directly by the preceding input event.
        images = {crc for t, crc in depths if t > start + .1}
        assert np.max(np.abs(gel._live.sim.gel.x - initial)) > .003, label
        assert len(images) >= 3, (label, "distinct depth buffers", len(images))
        assert view.view_matrix == matrix and scene.frame_current == 1
        print("PASS fixed viewport:", label, len(images), "different depth samples", flush=True)

    def actions():
        bpy.context.preferences.use_preferences_save = False
        bpy.context.preferences.system.viewport_aa = 'OFF'
        area.spaces.active.show_gizmo = False
        area.spaces.active.overlay.show_overlays = False
        view.view_perspective = 'ORTHO'
        view.view_rotation = Vector((0., 1., -.16)).to_track_quat('-Z', 'Y')
        view.view_location = (0., 0., .72)
        view.view_distance = 4.8
        window.view_layer.objects.active = body
        assert bpy.ops.object.volumetric_gel_live(running=True) == {'FINISHED'}
        yield .85
        xy = location_3d_to_region_2d(region, view, Vector((-.25, 0., .65)))
        x, y = round(region.x + xy.x), round(region.y + xy.y)
        window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=x, y=y)
        yield .1
        window.event_simulate(type='LEFTMOUSE', value='PRESS', x=x, y=y)
        yield .1
        assert gel._live.grip is not None
        x += region.width // 13
        y += region.height // 7
        window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=x, y=y)
        yield .04
        yield from observe("held grab")
        window.event_simulate(type='LEFTMOUSE', value='RELEASE', x=x, y=y)
        yield .04
        assert gel._live.grip is None
        yield from observe("released grab")
        rod.location.x = -.20
        window.view_layer.update()
        yield .04
        yield from observe("capsule contact")

    events = actions()
    handle = bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_VIEW')

    def tick():
        try:
            return next(events)
        except StopIteration:
            bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
            gel.stop_live()
            bpy.ops.wm.quit_blender()
        except Exception:
            traceback.print_exc()
            sys.stderr.flush()
            os._exit(1)  # Exceptions from app timers do not set Blender's exit code.
        return None

    bpy.app.timers.register(tick, first_interval=1.5)


if __name__ == "__main__":
    viewport_test()
