# SPDX-License-Identifier: GPL-2.0-or-later
"""Bezier pen gestures in the fixed view where the path is drawn."""

from math import ceil, sqrt
from mathutils import Vector


class PenPath:
    def __init__(self):
        self.nodes = []
        self.dragging = False
        self.hover = None
        self.base = None

    def add(self, point, radius):
        self.nodes.append((Vector(point), Vector((0, 0)), radius))
        self.dragging = True
        self.hover = None

    def tangent(self, point, radius):
        anchor, _, _ = self.nodes[-1]
        self.nodes[-1] = anchor, Vector(point) - anchor, radius

    def samples(self, index):
        a, ha, ra = self.nodes[index - 1]
        b, hb, rb = self.nodes[index]
        c, d = a + ha, b - hb
        # Bound the chord approximation error to roughly a quarter screen pixel.
        curvature = max((a - 2*c + d).length, (c - 2*d + b).length)
        steps = max(1, ceil(sqrt(3 * curvature)))
        yield tuple(a), ra
        for i in range(1, steps + 1):
            t = i / steps
            u = 1 - t
            p = u**3*a + 3*u*u*t*c + 3*u*t*t*d + t**3*b
            yield tuple(p), u*ra + t*rb

    def draw(self):
        import gpu
        from gpu_extras.batch import batch_for_shader
        if not self.nodes:
            return
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        shader.bind()
        def draw(kind, points, color):
            shader.uniform_float("color", color)
            batch_for_shader(shader, kind, {'pos': points}).draw(shader)

        a, handle, _ = self.nodes[-1]
        if self.hover is not None and not self.dragging:
            draw('LINES', (a, self.hover), (.55, .64, .74, 1))
        handles = []
        anchors = []
        for p, h, _ in self.nodes:
            if h.length > 2:
                handles.extend((p-h, p+h))
            x, y = p
            anchors.extend(((x-3,y),(x,y-3),(x+3,y),(x-3,y),(x+3,y),(x,y+3)))
        if handles:
            draw('LINES', handles, (.35, .7, 1, 1))
        draw('TRIS', anchors, (1, .65, .18, 1))
