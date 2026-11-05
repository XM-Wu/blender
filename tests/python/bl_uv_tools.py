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


if __name__ == '__main__':
    unittest.main(argv=[__file__])
