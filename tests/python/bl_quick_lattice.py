# SPDX-FileCopyrightText: 2026 Blender Authors
# SPDX-License-Identifier: GPL-2.0-or-later
import unittest
import bpy
import bmesh
from mathutils import Vector


def cube(location):
    bpy.ops.mesh.primitive_cube_add(location=location)
    return bpy.context.object


def evaluated_world(obj):
    bpy.context.view_layer.update()
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    return [evaluated.matrix_world @ vertex.co for vertex in evaluated.data.vertices]


class QuickLatticeTests(unittest.TestCase):
    def setUp(self):
        if bpy.context.object and bpy.context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)

    def targets(self):
        a, b = cube((-3, 0, 0)), cube((4, 2, 1))
        a.select_set(True)
        b.select_set(True)
        return a, b

    def translate_controls(self):
        self.assertEqual(bpy.context.mode, 'EDIT_LATTICE')
        bpy.ops.transform.translate(value=(.5, 0, 0))
        bpy.context.view_layer.update()

    def test_multi_object_bounds_and_deformation(self):
        a, b = self.targets()
        b.rotation_euler.z = .6
        b.scale = (2, .5, 1.5)
        bpy.context.view_layer.update()
        original = {obj: evaluated_world(obj) for obj in (a, b)}
        points = [p for obj in (a, b) for p in original[obj]]
        self.assertEqual(bpy.ops.object.quick_lattice(), {'FINISHED'})
        cage = a.modifiers[-1].object
        self.assertIs(cage, b.modifiers[-1].object)
        self.assertEqual(len(cage.modifiers), 0)
        self.assertEqual(len(cage.data.points), 8)
        self.assertFalse(any(o.type == 'EMPTY' for o in bpy.data.objects))
        bounds = [cage.matrix_world @ point.co_deform for point in cage.data.points]
        for axis in range(3):
            self.assertAlmostEqual(min(p[axis] for p in bounds), min(p[axis] for p in points), places=5)
            self.assertAlmostEqual(max(p[axis] for p in bounds), max(p[axis] for p in points), places=5)
        for obj in (a, b):
            self.assertLess(max((p - q).length for p, q in zip(original[obj], evaluated_world(obj))), 1e-5)
        self.translate_controls()
        for obj in (a, b):
            self.assertLess(max((p + Vector((.5, 0, 0)) - q).length for p,
                            q in zip(original[obj], evaluated_world(obj))), 1e-4)

    def test_edit_selection_isolation_all_modes(self):
        for mode in ('VERT', 'EDGE', 'FACE'):
            self.setUp()
            a, b = self.targets()
            original = {obj: evaluated_world(obj) for obj in (a, b)}
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_mode(type=mode)
            bpy.ops.mesh.select_all(action='DESELECT')
            expected = {}
            for obj in (a, b):
                bm = bmesh.from_edit_mesh(obj.data)
                if mode == 'VERT':
                    bm.verts.ensure_lookup_table()
                    bm.verts[0].select_set(True)
                elif mode == 'EDGE':
                    bm.edges.ensure_lookup_table()
                    bm.edges[0].select_set(True)
                else:
                    bm.faces.ensure_lookup_table()
                    bm.faces[0].select_set(True)
                bm.select_flush_mode()
                bm.verts.index_update()
                expected[obj] = {v.index for v in bm.verts if v.select}
                bmesh.update_edit_mesh(obj.data)
            self.assertEqual(bpy.ops.object.quick_lattice(), {'FINISHED'})
            for obj in (a, b):
                group = obj.vertex_groups[obj.modifiers[0].vertex_group]
                actual = {v.index for v in obj.data.vertices if any(
                    g.group == group.index and g.weight == 1 for g in v.groups)}
                self.assertEqual(actual, expected[obj])
            self.translate_controls()
            for obj in (a, b):
                result = evaluated_world(obj)
                for i, (p, q) in enumerate(zip(original[obj], result)):
                    wanted = p + Vector((.5, 0, 0)) if i in expected[obj] else p
                    self.assertLess((wanted - q).length, 1e-4, (mode, i))

    def test_non_default_resolution_keeps_bounds_and_rest_shape(self):
        obj = cube((2, 3, 4))
        original = evaluated_world(obj)
        self.assertEqual(bpy.ops.object.quick_lattice(resolution=(3, 4, 2)), {'FINISHED'})
        cage = obj.modifiers[-1].object
        self.assertEqual(len(cage.data.points), 24)
        self.assertEqual(len(cage.modifiers), 0)
        corners = [cage.matrix_world @ p.co_deform for p in cage.data.points]
        for axis in range(3):
            self.assertAlmostEqual(min(p[axis] for p in corners), min(p[axis] for p in original))
            self.assertAlmostEqual(max(p[axis] for p in corners), max(p[axis] for p in original))
        self.assertLess(max((p - q).length for p, q in zip(original, evaluated_world(obj))), 1e-5)
        self.translate_controls()
        self.assertLess(max((p + Vector((.5, 0, 0)) - q).length for p, q in zip(original, evaluated_world(obj))), 1e-4)

    def test_empty_edit_selection_is_noop(self):
        cube((0, 0, 0))
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='DESELECT')
        count = len(bpy.data.objects)
        self.assertEqual(bpy.ops.object.quick_lattice(), {'CANCELLED'})
        self.assertEqual(len(bpy.data.objects), count)
        self.assertEqual(bpy.context.mode, 'EDIT_MESH')

    def test_shared_mesh_selection_does_not_change_unselected_instance(self):
        obj = cube((0, 0, 0))
        other = bpy.data.objects.new('Linked', obj.data)
        bpy.context.scene.collection.objects.link(other)
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        self.assertEqual(bpy.ops.object.quick_lattice(), {'FINISHED'})
        self.assertIsNot(obj.data, other.data)
        self.assertEqual(len(other.modifiers), 0)
        self.assertFalse(any(v.groups for v in other.data.vertices))


class ApplyAllTests(unittest.TestCase):
    setUp = QuickLatticeTests.setUp
    targets = QuickLatticeTests.targets
    translate_controls = QuickLatticeTests.translate_controls

    def test_generated_groups_removed_but_shared_group_preserved(self):
        a, b = self.targets()
        a.vertex_groups.new(name='Artist Group')
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.object.quick_lattice()
        cage = bpy.context.object
        group_a = a.modifiers[0].vertex_group
        group_b = b.modifiers[0].vertex_group
        keep = b.modifiers.new('Other User', 'MASK')
        keep.vertex_group = group_b
        self.assertEqual(bpy.ops.object.lattice_apply_all(), {'FINISHED'})
        self.assertNotIn(group_a, a.vertex_groups)
        self.assertIn('Artist Group', a.vertex_groups)
        self.assertIn(group_b, b.vertex_groups)
        self.assertEqual(keep.vertex_group, group_b)

    def test_apply_multiple_references_preserves_other_modifiers(self):
        a, b = self.targets()
        bpy.ops.object.quick_lattice()
        cage = bpy.context.object
        self.translate_controls()
        bpy.ops.object.mode_set(mode='OBJECT')
        extra = a.modifiers.new('Second Lattice', 'LATTICE')
        extra.object = cage
        other_data = bpy.data.lattices.new('Other Cage')
        other = bpy.data.objects.new('Other Cage', other_data)
        bpy.context.scene.collection.objects.link(other)
        keep = b.modifiers.new('Keep Other Lattice', 'LATTICE')
        keep.object = other
        expected = {obj: evaluated_world(obj) for obj in (a, b)}
        cage_name = cage.name
        self.assertEqual(bpy.ops.object.lattice_apply_all(), {'FINISHED'})
        self.assertNotIn(cage_name, bpy.data.objects)
        self.assertIn(other.name, bpy.data.objects)
        self.assertEqual(len(a.modifiers), 0)
        self.assertEqual(b.modifiers[0].name, 'Keep Other Lattice')
        for obj in (a, b):
            self.assertLess(max((x - y).length for x, y in zip(expected[obj], evaluated_world(obj))), 1e-4)

    def test_apply_from_edit_lattice_includes_hidden_and_other_scene(self):
        a, b = self.targets()
        bpy.ops.object.quick_lattice()
        self.translate_controls()
        expected = {obj: evaluated_world(obj) for obj in (a, b)}
        scene = bpy.data.scenes.new('Other Scene')
        scene.collection.objects.link(b)
        for collection in list(b.users_collection):
            if collection != scene.collection:
                collection.objects.unlink(b)
        a.hide_set(True)
        self.assertEqual(bpy.ops.object.lattice_apply_all(), {'FINISHED'})
        self.assertEqual(len(a.modifiers), 0)
        self.assertEqual(len(b.modifiers), 0)
        self.assertTrue(a.hide_get())
        for obj in (a, b):
            actual = [obj.matrix_world @ v.co for v in obj.data.vertices]
            self.assertLess(max((x - y).length for x, y in zip(expected[obj], actual)), 1e-4)

    def test_shape_key_failure_keeps_every_target(self):
        a, b = self.targets()
        b.shape_key_add(name='Basis')
        bpy.ops.object.quick_lattice()
        cage = bpy.context.object
        try:
            result = bpy.ops.object.lattice_apply_all()
            self.assertEqual(result, {'CANCELLED'})
        except RuntimeError as error:
            self.assertIn('shape keys', str(error))
        self.assertIn(cage.name, bpy.data.objects)
        self.assertEqual(bpy.context.mode, 'EDIT_LATTICE')
        self.assertTrue(all(any(m.type == 'LATTICE' and m.object == cage for m in o.modifiers) for o in (a, b)))


if __name__ == '__main__':
    unittest.main(argv=[__file__])
