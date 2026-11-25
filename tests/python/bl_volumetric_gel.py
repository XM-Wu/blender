# SPDX-License-Identifier: GPL-2.0-or-later
"""Volumetric gel constitutive law, collisions and embedded bake lifetime."""

import math
from pathlib import Path
import tempfile

import bpy
import numpy as np

from _xblender_gel import Gel, contact
import xblender_gel as gel_ui


def material_test():
    points = np.array(((0., 0., 0.), (1., 0., 0.), (0., 1., 0.), (0., 0., 1.)))
    tets = np.array(((0, 1, 2, 3),))
    shear_forces = []
    volume_forces = []
    for bulk in (3000., 30000.):
        state = Gel(points, tets, [False] * 4, 1000., 500., bulk, 1., .5)
        np.testing.assert_allclose(state.elastic_forces(), 0, atol=1e-12)
        angle = .62
        rotation = np.array([[math.cos(angle), -math.sin(angle), 0.],
                             [math.sin(angle), math.cos(angle), 0.], [0., 0., 1.]])
        state.x[:] = points @ rotation + np.array((2., -7., 3.))
        np.testing.assert_allclose(state.elastic_forces(), 0, atol=1e-10)
        state.x[:] = points
        state.x[:, 0] += .2 * points[:, 1]
        shear_forces.append(state.elastic_forces())
        state.x[:] = .9 * points
        force = state.elastic_forces()
        np.testing.assert_allclose(force.sum(axis=0), 0, atol=1e-10)
        assert (force * (state.x - state.x.mean(axis=0))).sum() > 0
        volume_forces.append(np.linalg.norm(force))
    np.testing.assert_allclose(*shear_forces)
    assert np.linalg.norm(shear_forces[0]) > 0 and volume_forces[1] > 5 * volume_forces[0]
    # Both point-on-axis and sphere (coincident endpoints) contacts need finite normals.
    for end in (np.zeros(3), np.ones(3)):
        _, normals, _ = contact(np.zeros((1, 3)), np.zeros(3), end)
        np.testing.assert_allclose(np.linalg.norm(normals, axis=1), 1.)


def triangle_contact_test():
    from _xblender_gel import triangle_contacts

    tri = np.array([[[0., 0., 0.], [1., 0., 0.], [0., 1., 0.]]])
    for start, end, expected in (
            ((.2, .3, -1), (.2, .3, 1), 0.),
            ((.3, .3, .2), (.3, .3, .2), .2),
            ((-1, .5, .1), (1, .5, .1), .1),
            ((1.2, 0, 0), (2, 0, 0), .2)):
        start, end = np.array(start), np.array(end)
        weights, along, delta, normal = triangle_contacts(tri, start, end)
        np.testing.assert_allclose(np.linalg.norm(delta[0]), expected, atol=1e-9)
        np.testing.assert_allclose(weights.sum(axis=1), 1.)
        assert weights.min() >= 0 and 0 <= along[0] <= 1
        np.testing.assert_allclose(normal[0], [0, 0, 1])


def coordinates(obj):
    bpy.context.view_layer.update()
    me = obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).data
    result = np.empty(len(me.vertices) * 3, np.float32)
    me.vertices.foreach_get("co", result)
    return result.reshape(-1, 3)


def bake_test():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 4, 23
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3, radius=.5, location=(0, 0, .51))
    obj = bpy.context.object
    name = obj.name
    obj.scale = (1.1, .9, 1.)
    rest = coordinates(obj)
    op = bpy.ops.object.volumetric_gel_edit
    assert op(action='ADD') == {'FINISHED'}
    cfg = obj.volumetric_gel
    cfg.settle_frames = 3
    cfg.resolution = '2'
    col = bpy.data.objects.new("Press", None)
    scene.collection.objects.link(col)
    for f, z in ((1, 1.5), (12, .66), (16, .66), (23, 1.5)):
        col.location.z = z
        col.keyframe_insert(data_path="location", frame=f)
    cfg.collider, cfg.radius = col, .19
    cfg.start, cfg.end = (-.6, 0, 0), (.6, 0, 0)
    scene.frame_set(9, subframe=.25)
    assert op(action='BAKE') == {'FINISHED'}
    assert scene.frame_current == 9 and scene.frame_subframe == .25
    scene.frame_set(1)
    np.testing.assert_allclose(rest, coordinates(obj), atol=1e-6)
    scene.frame_set(12)
    hit = coordinates(obj)
    world = gel_ui.transform(hit, obj.matrix_world)
    _, _, distance = contact(world, np.array((-.6, 0, .66)), np.array((.6, 0, .66)))
    assert distance.min() >= cfg.radius - 1e-5 and world[:, 2].min() >= -1e-6
    assert np.max(np.abs(hit - rest)) > .05
    scene.frame_set(13)
    following = coordinates(obj)
    scene.frame_set(12, subframe=.5)
    np.testing.assert_allclose(coordinates(obj), (hit + following) / 2, atol=2e-6)
    old = gel_ui.cache_modifier(obj).node_group
    cfg.pin_group = "Missing"
    try:
        op(action='BAKE')
    except RuntimeError:
        pass  # Blender raises for operator ERROR reports.
    assert gel_ui.cache_modifier(obj).node_group == old
    assert scene.frame_current == 12 and scene.frame_subframe == .5
    cfg.pin_group = ""
    assert op(action='BAKE') == {'FINISHED'}
    assert sum(bool(o.get(gel_ui.CACHE_TAG)) for o in bpy.data.objects) == 1
    with tempfile.TemporaryDirectory() as folder:
        scene.frame_set(23)
        file = str(Path(folder) / 'gel.blend')
        bpy.ops.wm.save_as_mainfile(filepath=file)
        bpy.ops.wm.open_mainfile(filepath=file)
        obj = bpy.data.objects[name]
        bpy.context.scene.frame_set(12)
        np.testing.assert_allclose(coordinates(obj), hit, atol=2e-6)
        assert op(action='CLEAR') == {'FINISHED'}
        np.testing.assert_allclose(coordinates(obj), rest, atol=1e-6)
        assert not any(o.get(gel_ui.CACHE_TAG) for o in bpy.data.objects)
        assert op(action='REMOVE') == {'FINISHED'} and not obj.volumetric_gel.enabled


if __name__ == "__main__":
    material_test()
    triangle_contact_test()
    bake_test()
    print("PASS volumetric gel material, contact, interpolation, rebake and reload")
