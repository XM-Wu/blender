# SPDX-License-Identifier: GPL-2.0-or-later
"""Wall-clock scheduling and viewport publication shared by live physics previews."""

import time

import bpy


class PreviewClock:
    def __init__(self, step, max_steps):
        self.step = step
        self.max_steps = max_steps
        self.last_time = time.monotonic()
        self.remainder = 0.

    def poll(self):
        now = time.monotonic()
        elapsed = max(0., now - self.last_time)
        self.last_time = now
        self.remainder += min(elapsed, self.max_steps * self.step)
        steps = int(self.remainder / self.step)
        self.remainder -= steps * self.step
        return elapsed, steps, max(.001, self.step - self.remainder)


def publish_sample(private_id, view_layer, *, redraw=True):
    # Only tag the private simulation input. Editor-wide transform/geometry
    # notifiers invalidate gizmo picking between mouse hover and press.
    private_id.update_tag()
    view_layer.update()
    if redraw and not bpy.app.background:
        # Evaluation updates geometry but may leave the last viewport image on
        # screen until the next input event. Queue only the 3D drawing regions.
        for window in bpy.context.window_manager.windows:
            if window.scene == view_layer.id_data:
                for area in window.screen.areas:
                    if area.type == 'VIEW_3D':
                        for region in area.regions:
                            if region.type == 'WINDOW':
                                region.tag_redraw()


def redraw_preview_controls():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type in {'VIEW_3D', 'PROPERTIES'}:
                area.tag_redraw()
