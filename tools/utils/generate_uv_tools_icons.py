# SPDX-License-Identifier: GPL-2.0-or-later
"""Bake the UV toolbar's existing procedural artwork outside the UI draw path.

Run with Python from any directory. Each resource contains a little-endian uint32
size followed by size*size RGBA float32 pixels, compressed with zlib. Keeping float
pixels preserves the previous previews exactly (including antialiasing/alpha).
"""
import math
import sys
import struct
import zlib
from array import array
from pathlib import Path

ICONS = ('cut','stitch','unwrap','pack','texel_pack','orient_h','orient_v',
         'straight','straight_h','straight_v','rect_h','rect_v',
         'rotate_cw','rotate_ccw','rotate_180',
         'fit_u','fit_both','fit_v','pfit_u','pfit_both','pfit_v',
         'align_tl','align_t','align_tr','align_l','align_c','align_r',
         'align_bl','align_b','align_br','align_u','align_v')


def rasterize(name):
    size, samples = 64, 4

    def line(x, y, a, b, r):
        dx, dy = b[0] - a[0], b[1] - a[1]
        t = max(0, min(1, ((x - a[0]) * dx + (y - a[1]) * dy) / (dx * dx + dy * dy)))
        return math.hypot(x - a[0] - t * dx, y - a[1] - t * dy) <= r
    for kind in (name,):
        pixels = []
        for iy in range(size):
            for ix in range(size):
                accum = [0.0] * 4
                for sy in range(samples):
                    for sx in range(samples):
                        x, y = (ix + (sx + .5) / samples) * 32 / size, (iy + (sy + .5) / samples) * 32 / size
                        c = (0, 0, 0, 0)
                        if kind == 'cut':
                            if line(x, y, (10, 11), (25, 28), 1.3) or line(x, y, (22, 11), (7, 28), 1.3):
                                c = (.85, .92, 1, 1)
                            if any(abs(math.hypot(x - cx, y - 8) - 4) < 1.5 for cx in (8, 24)):
                                c = (1, .35, .08, 1)
                            if math.hypot(x - 16, y - 18) < 2:
                                c = (.1, .8, .9, 1)
                        elif kind == 'stitch':
                            if line(x, y, (10, 4), (10, 28), .8) or line(x, y, (22, 4), (22, 28), .8):
                                c = (.12, .76, .94, 1)
                            for yy in (7, 13, 19, 25):
                                if line(x, y, (7, yy - 2), (25, yy + 2), 1.1):
                                    c = (1, .64, .15, 1)
                        elif kind == 'unwrap':
                            # Six panels of an unfolded cube; warm top panel, cyan sides.
                            for index, (ox, oy) in enumerate(((12, 3), (12, 10), (12, 17), (12, 24), (5, 17), (19, 17))):
                                if ox <= x <= ox + 7 and oy <= y <= oy + 7:
                                    c = (1, .58, .12, 1) if index == 3 else (.12, .7, .95, 1)
                                    if min(x-ox, ox+7-x, y-oy, oy+7-y) < .65:
                                        c = (.88, .95, 1, 1)
                        elif kind == 'pack':
                            # A tight atlas, distinct from the unfolded cube icon.
                            if 3 <= x <= 29 and 3 <= y <= 29 and min(x-3, 29-x, y-3, 29-y) < 1:
                                c = (.88, .95, 1, 1)
                            for ox, oy, w, h, color in (
                                    (6, 6, 8, 19, (.12, .7, .95, 1)),
                                    (16, 6, 10, 8, (1, .58, .12, 1)),
                                    (16, 16, 10, 9, (.3, .85, .48, 1))):
                                if ox <= x <= ox+w and oy <= y <= oy+h:
                                    c = color
                        elif kind == 'texel_pack':
                            # Islands snapped to a texel grid, with a stacked pair.
                            if 3 <= x <= 29 and 3 <= y <= 29 and (min(x-3, 29-x, y-3, 29-y) < 1 or (
                                    (x - 3) % 6.5 < .45 or (y - 3) % 6.5 < .45)):
                                c = (.45, .52, .6, 1)
                            for ox, oy, w, h, color in (
                                    (5, 5, 11, 11, (.12, .7, .95, 1)),
                                    (18, 5, 9, 17, (1, .58, .12, 1)),
                                    (7, 18, 8, 8, (.3, .85, .48, 1)),
                                    (5, 16, 8, 8, (.2, .62, .36, 1))):
                                if ox <= x <= ox+w and oy <= y <= oy+h:
                                    c = color
                                    if min(x-ox, ox+w-x, y-oy, oy+h-y) < .7:
                                        c = (.9, .96, 1, 1)
                        elif kind.startswith('rotate_'):
                            # A single continuous arc with a tangential arrowhead.
                            xx = 32-x if kind == 'rotate_ccw' else x
                            theta = math.atan2(y-16, xx-16) % (2*math.pi)
                            radius = math.hypot(xx-16, y-16)
                            half = kind == 'rotate_180'
                            on_arc = (0 <= theta <= math.pi) if half else (theta <= 5*math.pi/4 or theta >= 7*math.pi/4)
                            if abs(radius-10) < 1.2 and on_arc:
                                c = (.3,.85,1,1)
                            angle = 0 if half else -math.pi/4
                            tip = (16+10*math.cos(angle),16+10*math.sin(angle))
                            tangent = (math.sin(angle),-math.cos(angle))
                            normal = (-tangent[1],tangent[0])
                            wings = [(tip[0]-4*tangent[0]+sign*3*normal[0],
                                      tip[1]-4*tangent[1]+sign*3*normal[1]) for sign in (-1,1)]
                            if any(line(xx,y,tip,wing,1.2) for wing in wings):
                                c = (1,.65,.15,1)
                        elif kind.startswith(('fit_', 'pfit_', 'align_')):
                            if 4 <= x <= 28 and 4 <= y <= 28 and min(x-4,28-x,y-4,28-y)<.75:
                                c = (.15,.65,.7,1)
                            if kind.startswith('align_'):
                                anchor = kind[6:]
                                segments = {
                                    'tl': (((7,15),(7,25)),((7,25),(17,25))),
                                    't': (((6,25),(26,25)),),
                                    'tr': (((15,25),(25,25)),((25,25),(25,15))),
                                    'l': (((7,6),(7,26)),),
                                    'r': (((25,6),(25,26)),),
                                    'bl': (((7,17),(7,7)),((7,7),(17,7))),
                                    'b': (((6,7),(26,7)),),
                                    'br': (((15,7),(25,7)),((25,7),(25,17))),
                                    'u': (((16,5),(16,27)),),
                                    'v': (((5,16),(27,16)),),
                                }
                                if any(line(x,y,a,b,1.8) for a,b in segments.get(anchor, ())):
                                    c = (1,.65,.15,1)
                                if anchor == 'c' and (abs(math.hypot(x-16,y-16)-5)<1.5 or math.hypot(x-16,y-16)<1.5):
                                    c = (1,.65,.15,1)
                            else:
                                axis = kind.split('_')[1]
                                if axis in {'u','both'} and (line(x,y,(7,16),(25,16),1) or line(x,y,(7,16),(10,19),1) or line(x,y,(7,16),(10,13),1) or line(x,y,(25,16),(22,13),1) or line(x,y,(25,16),(22,19),1)):
                                    c = (1,.55,.15,1)
                                if axis in {'v','both'} and (line(x,y,(16,7),(16,25),1) or line(x,y,(16,7),(19,10),1) or line(x,y,(16,7),(13,10),1) or line(x,y,(16,25),(13,22),1) or line(x,y,(16,25),(19,22),1)):
                                    c = (1,.55,.15,1)
                                if kind.startswith('pfit_') and line(x,y,(7,7),(25,25),.6):
                                    c = (.8,.95,1,1)
                        elif kind.startswith(('orient_', 'rect_')):
                            xx, yy = (y, x) if kind.endswith('_v') else (x, y)
                            if 4 <= xx <= 28 and 10 <= yy <= 22:
                                c = (.10, .42, .58, 1)
                                if min(xx-4, 28-xx, yy-10, 22-yy) < 1:
                                    c = (.3, .85, 1, 1)
                                if kind.startswith('rect_') and (abs(yy-16)<.6 or min(abs(xx-12),abs(xx-20))<.6):
                                    c = (.9, .96, 1, 1)
                            if kind.startswith('orient_') and (
                                    line(xx, yy, (7, 5), (25, 5), .8) or
                                    line(xx, yy, (7, 5), (10, 8), .8) or
                                    line(xx, yy, (25, 5), (22, 8), .8)):
                                c = (1, .65, .15, 1)
                        elif kind.startswith('straight'):
                            xx, yy = (y, x) if kind.endswith('_v') else (x, y)
                            a, b = ((5, 7), (27, 25)) if kind == 'straight' else ((5, 16), (27, 16))
                            if line(xx, yy, a, b, 1):
                                c = (.9, .96, 1, 1)
                            for t in (0, .33, .67, 1):
                                if math.hypot(xx-(a[0]+t*(b[0]-a[0])), yy-(a[1]+t*(b[1]-a[1]))) < 2:
                                    c = (1, .65, .15, 1)
                        for j in range(4):
                            accum[j] += c[j] / (samples * samples)
                pixels.extend(accum)
    return pixels


def main():
    root = Path(__file__).resolve().parents[2]
    destination = root / 'scripts/startup/bl_operators/data/uv_tools_icons'
    destination.mkdir(parents=True, exist_ok=True)
    for name in ICONS:
        pixels = array('f', rasterize(name))
        if sys.byteorder != 'little':
            pixels.byteswap()
        data = zlib.compress(struct.pack('<I', 64) + pixels.tobytes(), level=9)
        (destination / (name + '.rgba.zlib')).write_bytes(data)
    print('Baked', len(ICONS), 'UV icons')


if __name__ == '__main__':
    main()
