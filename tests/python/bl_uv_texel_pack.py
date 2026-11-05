# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Tests for the Texel Pack UV packer (`uv.texel_pack`).

blender --background --factory-startup --python-exit-code 1 --python tests/python/bl_uv_texel_pack.py
"""

import math
import unittest

import bmesh
import bpy

RES = 256


def make_islands(specs):
    """
    Create a mesh with one disconnected grid per spec and enter edit mode.

    spec: dict with `size` (3D width, height), `cells` (nx, ny), `uv` (origin), `uv_size`,
    optional `rotate` (radians), `mirror` (bool), `reverse` (reverse face winding, with `mirror`
    this is a mirror-symmetric mesh half, unwrapped counter-clockwise like its source),
    `attrs` {name: value}.
    Returns the object and the face index ranges of every island.
    """
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob)

    verts, faces, face_uvs, ranges = [], [], [], []
    offset_x = 0.0
    for spec in specs:
        sx, sy = spec['size']
        nx, ny = spec.get('cells', (1, 1))
        u0, v0 = spec['uv']
        uw, uh = spec.get('uv_size', (0.2, 0.2))
        angle = spec.get('rotate', 0.0)
        mirror = spec.get('mirror', False)
        base = len(verts)
        for j in range(ny + 1):
            for i in range(nx + 1):
                verts.append((offset_x + sx * i / nx, sy * j / ny, 0.0))
        start = len(faces)
        for j in range(ny):
            for i in range(nx):
                a = base + j * (nx + 1) + i
                face = [a, a + 1, a + nx + 2, a + nx + 1]
                uvs = []
                for di, dj in ((0, 0), (1, 0), (1, 1), (0, 1)):
                    y = uh * (j + dj) / ny
                    # Optional taper makes the shape asymmetric (no mirror or 180° symmetry).
                    x = uw * (i + di) / nx * (1.0 + spec.get('taper', 0.0) * y / uh)
                    if mirror:
                        x = -x
                    c, s = math.cos(angle), math.sin(angle)
                    uvs.append((u0 + c * x - s * y, v0 + s * x + c * y))
                if spec.get('reverse', False):
                    face.reverse()
                    uvs.reverse()
                faces.append(tuple(face))
                face_uvs.append(uvs)
        ranges.append(range(start, len(faces)))
        offset_x += sx + 1.0

    mesh = bpy.data.meshes.new("TexelPack")
    mesh.from_pydata(verts, [], faces)
    uv_layer = mesh.uv_layers.new()
    for face, uvs in zip(mesh.polygons, face_uvs):
        for loop_index, uv in zip(face.loop_indices, uvs):
            uv_layer.data[loop_index].uv = uv
    for index, spec in enumerate(specs):
        for name, value in spec.get('attrs', {}).items():
            kind = 'FLOAT' if isinstance(value, float) else 'INT'
            attr = mesh.attributes.get(name) or mesh.attributes.new(name, kind, 'FACE')
            for f in ranges[index]:
                attr.data[f].value = value

    ob = bpy.data.objects.new("TexelPack", mesh)
    bpy.context.scene.collection.objects.link(ob)
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.context.scene.tool_settings.use_uv_select_sync = True
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    return ob, ranges


def island_uvs(ob, ranges):
    bm = bmesh.from_edit_mesh(ob.data)
    bm.faces.ensure_lookup_table()
    layer = bm.loops.layers.uv.active
    result = []
    for r in ranges:
        result.append([[tuple(loop[layer].uv) for loop in bm.faces[f].loops] for f in r])
    return result


def all_uvs(ob):
    bm = bmesh.from_edit_mesh(ob.data)
    layer = bm.loops.layers.uv.active
    return [tuple(loop[layer].uv) for f in bm.faces for loop in f.loops]


def set_face_selection(ob, faces):
    bm = bmesh.from_edit_mesh(ob.data)
    bm.faces.ensure_lookup_table()
    # Deselect first: deselecting a neighbor afterwards would also drop shared vertices.
    for f in bm.faces:
        f.select_set(False)
    for f in bm.faces:
        if f.index in faces:
            f.select_set(True)
    bm.select_flush_mode()
    bmesh.update_edit_mesh(ob.data)


def pack(**kwargs):
    options = dict(resolution_x=RES, resolution_y=RES, precision='EXACT', margin=4.0, edge_padding=2.0)
    options.update(kwargs)
    return bpy.ops.uv.texel_pack('EXEC_DEFAULT', **options)


def points(island):
    return [(u * RES, v * RES) for face in island for (u, v) in face]


def hull(pts):
    pts = sorted(set(pts))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def point_segment(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def convex_overlap(a, b):
    for poly in (a, b):
        for i in range(len(poly)):
            p, q = poly[i], poly[(i + 1) % len(poly)]
            nx, ny = q[1] - p[1], p[0] - q[0]
            pa = [nx * x + ny * y for x, y in a]
            pb = [nx * x + ny * y for x, y in b]
            if max(pa) <= min(pb) or max(pb) <= min(pa):
                return False
    return True


def hull_distance(a, b):
    """Distance between convex polygons in pixels, zero when overlapping."""
    if convex_overlap(a, b):
        return 0.0
    d = math.inf
    for poly, other in ((a, b), (b, a)):
        for p in poly:
            for i in range(len(other)):
                d = min(d, point_segment(p, other[i], other[(i + 1) % len(other)]))
    return d


def area_px(island):
    total = 0.0
    for face in island:
        pts = [(u * RES, v * RES) for u, v in face]
        for i in range(len(pts)):
            x0, y0 = pts[i]
            x1, y1 = pts[(i + 1) % len(pts)]
            total += x0 * y1 - x1 * y0
    return total * 0.5


def first_edge_angle(island):
    (u0, v0), (u1, v1) = island[0][0], island[0][1]
    return math.atan2((v1 - v0), (u1 - u0))


class TexelPackTest(unittest.TestCase):

    def assert_layout(self, islands, margin=4.0, edge=2.0, tile=(0, 0)):
        hulls = [hull(points(island)) for island in islands]
        for island in islands:
            for face in island:
                for u, v in face:
                    x, y = (u - tile[0]) * RES, (v - tile[1]) * RES
                    self.assertGreaterEqual(min(x, y, RES - x, RES - y), edge - 1e-2, (u, v))
        for i in range(len(hulls)):
            for j in range(i + 1, len(hulls)):
                self.assertGreaterEqual(hull_distance(hulls[i], hulls[j]), margin - 1e-2, (i, j))

    def test_tolerance_shrinks_around_keep_scale_island(self):
        # Only one 150 px island fits at full size, the Keep Scale island must keep it.
        specs = [
            {'size': (1, 1), 'uv': (0.1, 0.1), 'uv_size': (150 / RES, 150 / RES),
             'attrs': {'uvpack_scale': 1}},
            {'size': (1, 1), 'uv': (0.2, 0.2), 'uv_size': (0.2, 0.2)},
        ]
        ob, ranges = make_islands(specs)
        self.assertEqual(pack(density_mode='TARGET', target_density=150, density_tolerance=0.4,
                              max_tiles=1, rotation='NONE', use_stacking=False), {'FINISHED'})
        result = island_uvs(ob, ranges)
        self.assert_layout(result)
        self.assertAlmostEqual(math.sqrt(abs(area_px(result[0]))), 150, delta=0.05)
        self.assertAlmostEqual(math.sqrt(abs(area_px(result[1]))), 90, delta=0.05)

    def test_basic_uniform_density(self):
        specs = [
            {'size': (1, 1), 'uv': (0.1, 0.1)},
            {'size': (2, 1), 'cells': (2, 1), 'uv': (0.15, 0.1), 'uv_size': (0.1, 0.3)},
            {'size': (0.5, 0.5), 'uv': (0.2, 0.2), 'uv_size': (0.4, 0.4)},
            {'size': (1, 3), 'cells': (1, 3), 'uv': (0.3, 0.1)},
            {'size': (0.7, 0.4), 'uv': (0.1, 0.3)},
        ]
        ob, ranges = make_islands(specs)
        self.assertEqual(pack(rotation='CARDINAL'), {'FINISHED'})
        islands = island_uvs(ob, ranges)
        self.assert_layout(islands)
        densities = [math.sqrt(abs(area_px(isl)) / (s['size'][0] * s['size'][1]))
                     for isl, s in zip(islands, specs)]
        for d in densities:
            self.assertAlmostEqual(d / densities[0], 1.0, delta=0.01)
        # Filling: the largest island cannot be tiny.
        self.assertGreater(sum(abs(area_px(isl)) for isl in islands), RES * RES * 0.2)

    def test_deterministic(self):
        specs = [{'size': (1 + i * 0.3, 1), 'uv': (0.1 * i, 0.1)} for i in range(6)]
        ob, ranges = make_islands(specs)
        before = all_uvs(ob)
        pack(rotation='FREE')
        first = all_uvs(ob)
        bm = bmesh.from_edit_mesh(ob.data)
        layer = bm.loops.layers.uv.active
        it = iter(before)
        for f in bm.faces:
            for loop in f.loops:
                loop[layer].uv = next(it)
        bmesh.update_edit_mesh(ob.data)
        pack(rotation='FREE')
        self.assertEqual(first, all_uvs(ob))

    def test_rotation_and_flip_constraints(self):
        specs = [
            {'size': (1, 0.3), 'uv': (0.1, 0.1), 'rotate': 0.3, 'attrs': {'uvpack_rotation': 1}},
            {'size': (0.4, 1), 'uv': (0.5, 0.5), 'attrs': {'uvpack_flip': 1}},
            {'size': (0.8, 0.8), 'uv': (0.3, 0.3)},
        ]
        ob, ranges = make_islands(specs)
        before = island_uvs(ob, ranges)
        pack(rotation='FREE', allow_flip=True)
        after = island_uvs(ob, ranges)
        self.assert_layout(after)
        self.assertAlmostEqual(first_edge_angle(before[0]), first_edge_angle(after[0]), delta=1e-4)
        self.assertGreater(area_px(before[1]) * area_px(after[1]), 0.0)

    def test_keep_scale_and_pixel_snap(self):
        # Islands on the pixel grid, keeping their scale, stay on the pixel grid.
        px = 1.0 / RES
        specs = [
            {'size': (1, 1), 'uv': (10 * px, 10 * px), 'uv_size': (40 * px, 40 * px),
             'attrs': {'uvpack_scale': 1}},
            {'size': (2, 1), 'uv': (20 * px, 20 * px), 'uv_size': (64 * px, 30 * px),
             'attrs': {'uvpack_scale': 1}},
        ]
        ob, ranges = make_islands(specs)
        before = island_uvs(ob, ranges)
        pack(rotation='CARDINAL', pixel_snap=True)
        after = island_uvs(ob, ranges)
        self.assert_layout(after)
        for b, a in zip(before, after):
            self.assertAlmostEqual(abs(area_px(b)), abs(area_px(a)), delta=1e-2)
        for island in after:
            for face in island:
                for u, v in face:
                    self.assertAlmostEqual(u * RES, round(u * RES), delta=1e-3)
                    self.assertAlmostEqual(v * RES, round(v * RES), delta=1e-3)

    def test_stacking(self):
        specs = [
            {'size': (1, 0.5), 'cells': (2, 1), 'uv': (0.1, 0.1), 'uv_size': (0.3, 0.15)},
            {'size': (1, 0.5), 'cells': (2, 1), 'uv': (0.6, 0.2), 'uv_size': (0.3, 0.15), 'rotate': math.pi / 2},
            {'size': (1, 0.5), 'cells': (2, 1), 'uv': (0.5, 0.6), 'uv_size': (0.3, 0.15), 'mirror': True},
            {'size': (1, 0.5), 'cells': (2, 1), 'uv': (0.2, 0.7), 'uv_size': (0.3, 0.15),
             'attrs': {'uvpack_stack': -1}},
            {'size': (0.6, 0.6), 'uv': (0.4, 0.4)},
        ]

        def same(a, b):
            pa = sorted((round(u, 4), round(v, 4)) for f in a for u, v in f)
            pb = sorted((round(u, 4), round(v, 4)) for f in b for u, v in f)
            return pa == pb

        ob, ranges = make_islands(specs)
        pack(use_stacking=True, stack_mirrored=False)
        islands = island_uvs(ob, ranges)
        self.assertTrue(same(islands[0], islands[1]))
        self.assertFalse(same(islands[0], islands[2]))
        self.assertFalse(same(islands[0], islands[3]))
        self.assert_layout([islands[i] for i in (0, 2, 3, 4)])

        ob, ranges = make_islands(specs)
        pack(use_stacking=True, stack_mirrored=True)
        islands = island_uvs(ob, ranges)
        self.assertTrue(same(islands[0], islands[2]))

        ob, ranges = make_islands(specs)
        pack(use_stacking=True, stack_offset=1)
        islands = island_uvs(ob, ranges)
        shifted = [[(u - 1.0, v) for u, v in f] for f in islands[1]]
        self.assertTrue(same(islands[0], shifted))

    def test_stack_mirror_symmetric_halves(self):
        # Two halves of a symmetric mesh: mirror images, both counter-clockwise in UV.
        half = {'size': (1, 0.6), 'cells': (3, 2), 'uv_size': (0.25, 0.18), 'taper': 0.5}
        specs = [dict(half, uv=(0.1, 0.1)),
                 dict(half, uv=(0.9, 0.5), mirror=True, reverse=True, rotate=0.4),
                 {'size': (0.5, 0.5), 'uv': (0.4, 0.6)}]

        def same(a, b):
            pa = sorted((round(u, 4), round(v, 4)) for f in a for u, v in f)
            pb = sorted((round(u, 4), round(v, 4)) for f in b for u, v in f)
            return pa == pb

        for stack_attr, mirrored, expected in ((0, False, False), (0, True, True), (1, False, True)):
            for spec in specs[:2]:
                spec['attrs'] = {'uvpack_stack': stack_attr}
            ob, ranges = make_islands(specs)
            pack(use_stacking=True, stack_mirrored=mirrored)
            islands = island_uvs(ob, ranges)
            self.assertEqual(same(islands[0], islands[1]), expected, (stack_attr, mirrored))
            if expected:
                # Mirror image: the copy's faces wind the other way.
                self.assertLess(area_px(islands[0]) * area_px(islands[1]), 0.0)

    def test_tile_per_group(self):
        specs = [
            {'size': (1, 1), 'uv': (0.1, 0.1), 'attrs': {'uvpack_group': 1}},
            {'size': (1, 1), 'uv': (0.2, 0.1), 'attrs': {'uvpack_group': 2}},
            {'size': (0.5, 1), 'uv': (0.3, 0.1), 'attrs': {'uvpack_group': 1}},
        ]
        ob, ranges = make_islands(specs)
        pack(group_layout='TILE_PER_GROUP', max_tiles=2, target='FIXED', udim_tile=1001)
        islands = island_uvs(ob, ranges)
        tiles = [{math.floor(u) for f in isl for u, v in f} for isl in islands]
        self.assertEqual(tiles[0], {0})
        self.assertEqual(tiles[2], {0})
        self.assertEqual(tiles[1], {1})
        self.assert_layout([islands[0], islands[2]])
        self.assert_layout([islands[1]], tile=(1, 0))

    def test_target_density_overflow(self):
        specs = [{'size': (1, 1), 'uv': (0.1, 0.1)} for _ in range(6)]
        for i, spec in enumerate(specs):
            spec['attrs'] = {'uvpack_stack': -1}
        ob, ranges = make_islands(specs)
        # 100 px per meter: three islands of 100 px fit per row of a 256 tile only twice.
        pack(density_mode='TARGET', target_density=100.0, max_tiles=3, target='FIXED', udim_tile=1001)
        islands = island_uvs(ob, ranges)
        for island in islands:
            self.assertAlmostEqual(abs(area_px(island)), 100.0 * 100.0, delta=1.0)
        by_tile = {}
        for island in islands:
            tile = math.floor(min(u for f in island for u, v in f))
            self.assertIn(tile, (0, 1, 2))
            by_tile.setdefault(tile, []).append(island)
        self.assertGreater(len(by_tile), 1)
        for tile, group in by_tile.items():
            self.assert_layout(group, tile=(tile, 0))

    def test_locked_islands(self):
        specs = [
            {'size': (1, 1), 'uv': (0.3, 0.3), 'uv_size': (0.25, 0.25), 'attrs': {'uvpack_lock': 1}},
            {'size': (1, 1), 'uv': (0.32, 0.32)},
            {'size': (2, 1), 'uv': (0.35, 0.3)},
        ]
        ob, ranges = make_islands(specs)
        before = island_uvs(ob, ranges)
        pack()
        after = island_uvs(ob, ranges)
        self.assertEqual(before[0], after[0])
        self.assert_layout(after)
        d0 = math.sqrt(abs(area_px(after[0])) / 1.0)
        d1 = math.sqrt(abs(area_px(after[1])) / 1.0)
        self.assertAlmostEqual(d1 / d0, 1.0, delta=0.01)

    def test_constraint_operators(self):
        specs = [{'size': (1, 1), 'uv': (0.1 * i, 0.1)} for i in range(3)]
        ob, ranges = make_islands(specs)
        set_face_selection(ob, set(ranges[1]))
        self.assertEqual(bpy.ops.uv.texel_pack_constraint_set(kind='GROUP', group=7), {'FINISHED'})
        bpy.ops.object.mode_set(mode='OBJECT')
        values = [a.value for a in ob.data.attributes['uvpack_group'].data]
        self.assertEqual(values, [0, 7, 0])
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.uv.texel_pack_constraint_select(kind='GROUP', group=7)
        bm = bmesh.from_edit_mesh(ob.data)
        self.assertEqual([f.select for f in bm.faces], [False, True, False])
        bpy.ops.uv.texel_pack_constraint_clear(kind='ALL')
        bpy.ops.object.mode_set(mode='OBJECT')
        values = [a.value for a in ob.data.attributes['uvpack_group'].data]
        self.assertEqual(values, [0, 0, 0])
        bpy.ops.object.mode_set(mode='EDIT')

    def test_constraint_operators_mesh_selection(self):
        # Outside the UV editor the mesh selection is used, also without UV sync selection,
        # where unselected faces aren't visible in the UV editor.
        specs = [{'size': (1, 1), 'uv': (0.1 * i, 0.1)} for i in range(3)]
        ob, ranges = make_islands(specs)
        bpy.context.scene.tool_settings.use_uv_select_sync = False
        set_face_selection(ob, set(ranges[2]))
        self.assertEqual(bpy.ops.uv.texel_pack_constraint_set(kind='ROTATION', rotation='NONE'), {'FINISHED'})
        set_face_selection(ob, set())
        bpy.ops.uv.texel_pack_constraint_select(kind='ROTATION', rotation='NONE')
        bm = bmesh.from_edit_mesh(ob.data)
        self.assertEqual([f.select for f in bm.faces], [False, False, True])
        bpy.ops.object.mode_set(mode='OBJECT')
        values = [a.value for a in ob.data.attributes['uvpack_rotation'].data]
        self.assertEqual(values, [0, 0, 1])
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.context.scene.tool_settings.use_uv_select_sync = True

    def test_sidebar_direct_edit(self):
        from bl_operators import uv_tools_texel_pack as sidebar
        specs = [{'size': (1, 1), 'cells': (2, 1), 'uv': (0.3 * i, 0.1)} for i in range(3)]
        ob, ranges = make_islands(specs)
        island = bpy.context.scene.uv_tools_island

        # Selecting one face edits the whole island.
        set_face_selection(ob, {ranges[1][0]})
        sidebar.invalidate()
        island.rotation = 'FREE'
        island.density = 2.0
        bpy.ops.object.mode_set(mode='OBJECT')
        self.assertEqual([a.value for a in ob.data.attributes['uvpack_rotation'].data], [0, 0, 3, 3, 0, 0])
        self.assertEqual([a.value for a in ob.data.attributes['uvpack_density'].data], [0, 0, 2, 2, 0, 0])
        bpy.ops.object.mode_set(mode='EDIT')
        sidebar.invalidate()
        self.assertEqual(island.rotation, 'FREE')
        self.assertAlmostEqual(island.density, 2.0)

        set_face_selection(ob, set(ranges[0]) | set(ranges[1]))
        sidebar.invalidate()
        self.assertTrue(sidebar.selected_value('ROTATION', 0)[1])

        # Groups: new ids from the selection, listed with their UV bounds.
        set_face_selection(ob, set(ranges[0]))
        sidebar.invalidate()
        self.assertEqual(bpy.ops.uv_tools.texel_pack_new_id(kind='GROUP'), {'FINISHED'})
        set_face_selection(ob, set(ranges[2]))
        sidebar.invalidate()
        self.assertEqual(bpy.ops.uv_tools.texel_pack_new_id(kind='GROUP'), {'FINISHED'})
        sidebar.invalidate()
        groups = sidebar.island_state(bpy.context)['lists']['GROUP']
        self.assertEqual(sorted(groups), [1, 2])
        self.assertEqual(groups[1]['faces'], 2)
        self.assertFalse(groups[1]['selected'])
        self.assertTrue(groups[2]['selected'])
        for value, expected in zip(groups[1]['bounds'], (0.0, 0.1, 0.2, 0.3)):
            self.assertAlmostEqual(value, expected, places=5)


if __name__ == '__main__':
    import sys
    sys.argv = [__file__] + (sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
    unittest.main()
