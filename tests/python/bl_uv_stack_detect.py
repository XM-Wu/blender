# SPDX-License-Identifier: GPL-2.0-or-later
"""Run with Blender --background --factory-startup --python-exit-code 1 --python this_file."""
import os
import sys
import unittest

import bmesh
import bpy

sys.path.insert(0, os.path.dirname(__file__))
from bl_uv_texel_pack import make_islands, island_uvs


def spec(x, shape=(0.2, 0.2), **kwargs):
    return dict(size=(1, 1), uv=(x, 0.1), uv_size=shape, **kwargs)


def ids(ob, ranges):
    bm = bmesh.from_edit_mesh(ob.data)
    bm.faces.ensure_lookup_table()
    layer = bm.faces.layers.int.get('uvpack_stack')
    return [bm.faces[r.start][layer] if layer else 0 for r in ranges]


def points(uvs):
    return sorted((round(u, 5), round(v, 5)) for face in uvs for u, v in face)


class StackDetectTest(unittest.TestCase):
    def test_multiple_sets_and_exclusions(self):
        ob, ranges = make_islands([
            spec(0), spec(1, rotate=0.7), spec(2, (0.4, 0.2)),
            spec(3, (0.8, 0.4), rotate=1.2), spec(4, (0.6, 0.2)),
            spec(5, attrs={'uvpack_stack': -1}),
            spec(6, attrs={'uvpack_stack': 18}),
            spec(7, attrs={'uvpack_lock': 1}), spec(8),
            spec(9, attrs={'uvpack_group': 9}),
        ])
        bm = bmesh.from_edit_mesh(ob.data)
        bm.faces.ensure_lookup_table()
        bm.faces[8].select_set(False)
        bmesh.update_edit_mesh(ob.data)
        before = island_uvs(ob, ranges)
        self.assertEqual(bpy.ops.uv_tools.stack_detect(), {'FINISHED'})
        groups = ids(ob, ranges)
        self.assertEqual(groups[0], groups[1])
        self.assertEqual(groups[2], groups[3])
        self.assertNotEqual(groups[0], groups[2])
        self.assertGreater(min(groups[:4]), 18)
        self.assertEqual(groups[4:], [0, -1, 18, 0, 0, 0])
        after = island_uvs(ob, ranges)
        self.assertEqual(points(after[0]), points(after[1]))
        self.assertEqual(points(after[2]), points(after[3]))
        self.assertEqual(before[4:], after[4:])
        self.assertEqual(bpy.ops.uv_tools.stack_detect(), {'CANCELLED'})
        self.assertEqual(ids(ob, ranges), groups)
        self.assertEqual(island_uvs(ob, ranges), after)

    def test_three_copies_and_scene_id(self):
        ob, ranges = make_islands([spec(0), spec(1), spec(2)])
        mesh = bpy.data.meshes.new('OtherStack')
        mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
        mesh.attributes.new('uvpack_stack', 'INT', 'FACE').data[0].value = 100
        other = bpy.data.objects.new('OtherStack', mesh)
        bpy.context.scene.collection.objects.link(other)
        self.assertEqual(bpy.ops.uv_tools.stack_detect(), {'FINISHED'})
        groups = ids(ob, ranges)
        self.assertGreater(groups[0], 100)
        self.assertEqual(len(set(groups)), 1)
        after = island_uvs(ob, ranges)
        self.assertTrue(all(points(uvs) == points(after[0]) for uvs in after))
        self.assertEqual(mesh.attributes['uvpack_stack'].data[0].value, 100)

    def test_multiple_edit_meshes(self):
        ob, ranges = make_islands([spec(0), spec(1, (0.4, 0.2))])
        bpy.ops.object.mode_set(mode='OBJECT')
        other = bpy.data.objects.new('Copies', ob.data.copy())
        bpy.context.scene.collection.objects.link(other)
        for loop in other.data.uv_layers.active.data:
            loop.uv.x += 3.0
        other.select_set(True)
        bpy.ops.object.mode_set(mode='EDIT')
        self.assertEqual(bpy.ops.uv_tools.stack_detect(), {'FINISHED'})
        self.assertEqual(ids(ob, ranges), ids(other, ranges))
        self.assertEqual(len(set(ids(ob, ranges))), 2)
        self.assertEqual([points(uvs) for uvs in island_uvs(ob, ranges)],
                         [points(uvs) for uvs in island_uvs(other, ranges)])

    def test_mirror_option(self):
        ob, ranges = make_islands([spec(0, taper=0.4),
                                  spec(1, taper=0.4, mirror=True, reverse=True)])
        settings = bpy.context.scene.uv_tools_texel_pack
        settings.stack_mirrored = False
        self.assertEqual(bpy.ops.uv_tools.stack_detect(), {'CANCELLED'})
        settings.stack_mirrored = True
        try:
            self.assertEqual(bpy.ops.uv_tools.stack_detect(), {'FINISHED'})
            self.assertEqual(len(set(ids(ob, ranges))), 1)
            after = island_uvs(ob, ranges)
            self.assertEqual(points(after[0]), points(after[1]))
        finally:
            settings.stack_mirrored = False


if __name__ == '__main__':
    sys.argv = [__file__]
    unittest.main()
