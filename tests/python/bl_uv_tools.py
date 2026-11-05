# SPDX-FileCopyrightText: 2026 Blender Authors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Run with blender --background --factory-startup --python tests/python/bl_uv_tools.py."""
import math
import unittest

from bl_operators.uv_tools import normalized_ratios, distortion_color


class TestUVAreaDistortion(unittest.TestCase):
    def test_uniform_density_is_neutral(self):
        ratios, bins = normalized_ratios([(2, 8), (3, 12), (5, 20)])
        self.assertEqual(ratios, [1, 1, 1])
        self.assertAlmostEqual(sum(bins), 1)

    def test_independent_global_scales_do_not_change_distortion(self):
        areas = [(1, .2), (2, 3), (5, 2)]
        ratios, bins = normalized_ratios(areas)
        scaled, scaled_bins = normalized_ratios([(a * 17, b * 9) for a, b in areas])
        for a, b in zip(ratios, scaled):
            self.assertAlmostEqual(a, b)
        for a, b in zip(bins, scaled_bins):
            self.assertAlmostEqual(a, b)

    def test_subdivision_does_not_change_histogram(self):
        _, coarse = normalized_ratios([(1, .5), (1, 1.5)])
        _, subdivided = normalized_ratios([(.25, .125)] * 4 + [(1, 1.5)])
        self.assertEqual(coarse, subdivided)

    def test_degenerate_geometry_and_collapsed_uvs(self):
        ratios, bins = normalized_ratios([(0, 100), (1, 0), (1, 2)])
        self.assertEqual(ratios, [None, 0, 2])
        self.assertEqual(bins[0], .5)
        self.assertEqual(bins[-1], .5)
        self.assertEqual(normalized_ratios([(1, 0)])[0], [])
        self.assertEqual(normalized_ratios([])[0], [])

    def test_colors_and_overflow(self):
        red, blue = distortion_color(0), distortion_color(2)
        self.assertGreater(red[0], red[2])
        self.assertGreater(blue[2], blue[0])
        self.assertEqual(blue, distortion_color(100))
        self.assertTrue(all(math.isfinite(v) for v in red + blue))


class TestIncrementalCache(unittest.TestCase):
    def test_local_uv_edit_matches_full_rebuild(self):
        import bpy
        import numpy as np
        from bl_operators.uv_tools_cache import EditCache, CHUNK_SIZE
        if bpy.context.object and bpy.context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete(use_global=False)
        bpy.ops.mesh.primitive_grid_add(x_subdivisions=48, y_subdivisions=48)
        bpy.ops.object.mode_set(mode='EDIT')
        try:
            cache = EditCache(bpy.context)
            count = (len(cache.triangles) + CHUNK_SIZE - 1) // CHUNK_SIZE
            for name in cache.batches:
                cache.batches[name] = {i: object() for i in range(count)}
            _, _, layer, verts, loops, _ = cache.sources[0]
            loop = loops[len(loops) // 2]
            original = loop[layer].uv.copy()
            loop[layer].uv.x += .01
            self.assertTrue(cache.update())
            self.assertLess(cache.changed_triangles, 10)
            self.assertGreaterEqual(len(cache.batches['uvs']), count - 2)
            reference = EditCache(bpy.context)
            np.testing.assert_allclose(cache.uv_area, reference.uv_area)
            np.testing.assert_allclose(cache.bins, reference.bins)
            self.assertAlmostEqual(cache.normalization, reference.normalization)
            loop[layer].uv = original
            cache.update()
            np.testing.assert_allclose(cache.bins, EditCache(bpy.context).bins)
            verts[0].co.z += .1
            cache.update()
            np.testing.assert_allclose(cache.area, EditCache(bpy.context).area)
            marker = object()
            cache.batches['uvs'][0] = marker
            self.assertFalse(cache.update())
            self.assertIs(cache.batches['uvs'][0], marker)
        finally:
            bpy.ops.object.mode_set(mode='OBJECT')


class TestShapeDistortion(unittest.TestCase):
    def test_similarity_scale_shear_and_degenerate_uv(self):
        import numpy as np
        from bl_operators.uv_tools_cache import triangle_shape_ratios
        world = np.asarray([[(0,0,0),(1,0,0),(0,1,0)]] * 5)
        uv = np.asarray([
            [(0,0),(1,0),(0,1)],
            [(10,20),(10,25),(5,20)],
            [(0,0),(2,0),(0,.5)],
            [(0,0),(1,0),(1,1)],
            [(0,0),(0,0),(0,0)],
        ])
        ratios = triangle_shape_ratios(world, uv)
        np.testing.assert_allclose(ratios[:3], [1,1,4])
        self.assertGreater(ratios[3], 2)
        self.assertGreaterEqual(ratios[4], 1e5)

    def test_incremental_shape_change_with_unchanged_area(self):
        import bpy
        import numpy as np
        from bl_operators.uv_tools_cache import EditCache
        if bpy.context.object and bpy.context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete(use_global=False)
        bpy.ops.mesh.primitive_plane_add()
        bpy.ops.object.mode_set(mode='EDIT')
        cache = EditCache(bpy.context)
        before = cache.density.copy()
        cache.batches['positions'][0] = object()
        _, _, layer, _, loops, _ = cache.sources[0]
        for loop in loops:
            loop[layer].uv.x *= 2
            loop[layer].uv.y *= .5
        self.assertTrue(cache.update())
        np.testing.assert_allclose(before,cache.density)
        np.testing.assert_allclose(cache.shape,4)
        self.assertNotIn(0,cache.batches['positions'])
        np.testing.assert_allclose(cache.shape,EditCache(bpy.context).shape)
        self.assertAlmostEqual(sum(cache.shape_bins),1)
        bpy.ops.object.mode_set(mode='OBJECT')


class TestIslandAreaDistortion(unittest.TestCase):
    def test_rizom_measured_values_and_polygon_aggregation(self):
        import numpy as np
        from bl_operators.uv_tools_cache import island_area_values
        # Two equal-area quads, each represented by two triangles; UV areas 1 and 2.5.
        areas = np.array([.5, .5, .5, .5])
        uv = np.array([.5, .5, 1, 1.5])
        faces = np.array([0, 0, 1, 1])
        _, _, values = island_area_values(areas, uv, faces, np.array([0, 0]))
        np.testing.assert_allclose(values, [math.sqrt(4/7)]*2+[math.sqrt(10/7)]*2)
        _, _, scaled = island_area_values(areas, uv*9, faces, np.array([0, 0]))
        np.testing.assert_allclose(scaled, values)
        _, _, split = island_area_values(areas, uv, faces, np.array([0, 1]))
        np.testing.assert_allclose(split, 1)

    def test_uv_connectivity_updates_during_transform(self):
        import bpy
        import numpy as np
        from bl_operators.uv_tools_cache import EditCache
        if bpy.context.object and bpy.context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete(use_global=False)
        bpy.ops.mesh.primitive_grid_add(x_subdivisions=2, y_subdivisions=1)
        bpy.ops.object.mode_set(mode='EDIT')
        cache = EditCache(bpy.context)
        self.assertEqual(len(set(cache.face_islands)), 1)
        _, bm, layer, _, _, _ = cache.sources[0]
        bm.faces.ensure_lookup_table()
        for loop in bm.faces[1].loops:
            loop[layer].uv *= 2
            loop[layer].uv.x += 5
        cache.update()
        self.assertEqual(len(set(cache.face_islands)), 2)
        np.testing.assert_allclose(cache.distortion, 1, atol=1e-6)
        np.testing.assert_allclose(cache.distortion, EditCache(bpy.context).distortion)
        bpy.ops.object.mode_set(mode='OBJECT')


class TestExclusiveUVSelectionModes(unittest.TestCase):
    def test_sync_and_unsynced_modes_exit_island(self):
        import bpy
        if bpy.context.object and bpy.context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.mesh.primitive_cube_add()
        bpy.ops.object.mode_set(mode='EDIT')
        screen = bpy.data.workspaces['UV Editing'].screens[0]
        area = next(a for a in screen.areas if a.type == 'IMAGE_EDITOR')
        with bpy.context.temp_override(window=bpy.context.window, screen=screen, area=area):
            settings = bpy.context.tool_settings
            for sync in (False, True):
                settings.use_uv_select_sync = sync
                for mode in ('ISLAND', 'ISLAND', 'VERTEX', 'ISLAND', 'EDGE', 'ISLAND', 'FACE'):
                    self.assertEqual(bpy.ops.uv_tools.select_mode(type=mode), {'FINISHED'})
                    self.assertEqual(settings.use_uv_select_island, mode == 'ISLAND')
                    target = 'FACE' if mode == 'ISLAND' else mode
                    self.assertEqual(settings.uv_select_mode, target)
                    if sync:
                        self.assertEqual(tuple(settings.mesh_select_mode),
                                         tuple(target == item for item in ('VERTEX', 'EDGE', 'FACE')))


if __name__ == '__main__':
    unittest.main(argv=[__file__])
