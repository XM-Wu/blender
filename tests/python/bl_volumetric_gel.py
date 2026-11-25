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


def animated_pin_test():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.frame_end = 20
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3, radius=.4, location=(.1, -.2, .42))
    obj = bpy.context.object
    rest = coordinates(obj)
    grip = obj.vertex_groups.new(name="Grip")
    selected = np.flatnonzero(rest[:, 2] > .25)
    grip.add(selected.tolist(), 1., 'REPLACE')
    target = bpy.data.objects.new("Handle", None)
    scene.collection.objects.link(target)
    target.location = (.2, -.15, .8)
    target.rotation_euler.y = -.3
    target.keyframe_insert(data_path="location", frame=1)
    target.keyframe_insert(data_path="rotation_euler", frame=1)
    scene.frame_set(1)
    local = gel_ui.transform(gel_ui.transform(rest[selected], obj.matrix_world), target.matrix_world.inverted())
    target.location.x += .3
    target.location.z += .6
    target.rotation_euler.y += .45
    target.keyframe_insert(data_path="location", frame=18)
    target.keyframe_insert(data_path="rotation_euler", frame=18)

    op = bpy.ops.object.volumetric_gel_edit
    assert op(action='ADD') == {'FINISHED'}
    cfg = obj.volumetric_gel
    cfg.pin_group, cfg.pin_target = grip.name, target
    scene.frame_set(10, subframe=.25)
    assert op(action='BAKE') == {'FINISHED'}
    assert scene.frame_current == 10 and scene.frame_subframe == .25
    for frame in (1, 9, 18, 20):
        scene.frame_set(frame)
        world = gel_ui.transform(coordinates(obj), obj.matrix_world)
        expected = gel_ui.transform(local, target.matrix_world)
        np.testing.assert_allclose(world[selected], expected, atol=2e-6)
    # The free underside lifted off the ground and lagged behind the driven patch.
    base = gel_ui.transform(rest, obj.matrix_world)
    delta = world - base
    assert world[:, 2].min() > .15
    assert abs(delta[rest[:, 2] < -.2, 0].mean() - delta[selected, 0].mean()) > .02
    cache = gel_ui.cache_modifier(obj).node_group
    cfg.pin_group = obj.vertex_groups.new(name="Empty grip").name
    try:
        op(action='BAKE')
    except RuntimeError:
        pass
    assert gel_ui.cache_modifier(obj).node_group == cache


def inclined_plane_test():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.frame_end = 28
    ramp = bpy.data.objects.new("Ramp", None)
    scene.collection.objects.link(ramp)
    ramp.location.z = .3
    ramp.rotation_euler = (0., -math.pi / 4, .3)
    ramp.scale = (1.4, .9, .7)
    bpy.context.view_layer.update()
    normal = np.array(ramp.matrix_world.to_3x3().inverted().transposed().col[2].normalized())
    origin = np.array(ramp.location)
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3, radius=.4, location=origin + normal * .55)
    body = bpy.context.object
    op = bpy.ops.object.volumetric_gel_edit
    assert op(action='ADD') == {'FINISHED'}
    cfg = body.volumetric_gel
    cfg.floor, cfg.incline, cfg.friction = -3., ramp, .20
    assert op(action='BAKE') == {'FINISHED'}
    centroids = []
    contact_frames = 0
    for frame in range(1, scene.frame_end + 1):
        scene.frame_set(frame)
        world = gel_ui.transform(coordinates(body), body.matrix_world)
        distance = (world - origin) @ normal
        assert distance.min() >= -1e-6
        if np.count_nonzero(distance < .001) >= 3:
            contact_frames += 1
        centroids.append(world.mean(axis=0))
    down = np.array((0., 0., -1.))
    downhill = down - normal * (down @ normal)
    assert contact_frames >= 5 and (centroids[-1] - centroids[0]) @ downhill > .10


if __name__ == "__main__":
    material_test()
    triangle_contact_test()
    bake_test()
    inclined_plane_test()
    animated_pin_test()
    print("PASS volumetric gel material, contact, interpolation, rebake and reload")
