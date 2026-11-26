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


def native_kernel_test():
    from _xblender_gel_native import live_kernels

    rest = np.array(((0., 0., 0.), (1., 0., 0.), (0., 1., 0.), (0., 0., 1.), (0., 0., -1.)))
    tets = np.array(((0, 1, 2, 3), (0, 1, 2, 4)))
    faces = np.array(((0, 1, 3), (1, 2, 3), (2, 0, 3), (0, 1, 4), (1, 2, 4), (2, 0, 4)))

    def state():
        return Gel(rest, tets, [True, False, False, False, False], 1000., 1500., 28000., 1., .4, faces)

    reference, fast = state(), state()
    fast.kernels = live_kernels(fast)
    if fast.kernels is None:
        return  # The pure Python fallback is supported in script-only installations.
    # Include opposite rest windings, a pinned node, an inverted element,
    # interior face hits, the sphere limit, plane friction and changed mass.
    rng = np.random.default_rng(14)
    for z in (.08, -.03, .94):
        for ends in (None, np.array(((.23, .21, -.15), (.28, .35, .27))),
                     np.array(((.38, .19, .04), (.38, .19, .04)))):
            reference.x[:] = rest + rng.normal(0., .014, rest.shape)
            reference.x[3, 2] = z
            reference.v[:] = rng.normal(0., .3, rest.shape)
            fast.x[:], fast.v[:] = reference.x, reference.v
            velocities = None if ends is None else rng.normal(0., .25, (2, 3))
            incline = (np.array((-.6, 0., .8)), -.3)
            for gel in (reference, fast):
                gel.set_material(940., 2500., 36000., 1.5, .48)
            np.testing.assert_allclose(reference.elastic_forces(), fast.elastic_forces(), atol=1e-9)
            np.testing.assert_allclose(reference._volume_ratios(), fast._volume_ratios(), atol=1e-12)
            if ends is not None:
                np.testing.assert_allclose(reference._contact_depth(ends, .19), fast._contact_depth(ends, .19))
            for gel in (reference, fast):
                gel._limit_compression(.15, -.08, incline, ends, .19)
            np.testing.assert_allclose(reference.x, fast.x, atol=1e-10)
            np.testing.assert_allclose(reference.v, fast.v, atol=1e-10)
            for gel in (reference, fast):
                gel._project_contacts(-.08, incline, ends, velocities, .19)
            np.testing.assert_allclose(reference.x, fast.x, atol=1e-9)
            np.testing.assert_allclose(reference.v, fast.v, atol=1e-9)


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


def balloon_skin_test():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3, radius=.65, location=(0, 0, .68))
    obj = bpy.context.object
    bpy.ops.object.volumetric_gel_edit(action='ADD')
    bpy.ops.object.volumetric_gel_preset(material='BALLOON')
    cfg = obj.volumetric_gel
    sim = gel_ui.Simulation(bpy.context, obj, cfg)
    # Preserve even a nonuniform affine deformation in world coordinates.
    affine = sim.world @ np.array(((1.1, .2, 0.), (0., .8, .1), (0., 0., 1.2))) + (1., 2., 3.)
    np.testing.assert_allclose(sim.relax_skin(affine, cfg.surface_smoothing), affine, atol=1e-10)
    folded = affine.copy()
    folded[::3, 2] += .04
    filtered = sim.relax_skin(folded, cfg.surface_smoothing)
    assert np.linalg.norm(filtered - affine) < np.linalg.norm(folded - affine)
    from _xblender_gel_native import live_kernels
    sim.gel.kernels = live_kernels(sim.gel)
    np.testing.assert_allclose(sim.relax_skin(folded, cfg.surface_smoothing), filtered, atol=1e-10)
    bpy.ops.object.volumetric_gel_preset(material='SOFT')
    assert cfg.surface_smoothing == 0.


def live_test():
    import time

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.frame_end = 15
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3, radius=.5, location=(0., 0., .58))
    obj = bpy.context.object
    assert bpy.ops.object.volumetric_gel_edit(action='ADD') == {'FINISHED'}
    cfg = obj.volumetric_gel
    cfg.resolution = '2'
    cfg.grab_radius = .27
    for key, (_, parameters) in gel_ui.MATERIALS.items():
        assert bpy.ops.object.volumetric_gel_preset(material=key) == {'FINISHED'}
        np.testing.assert_allclose([getattr(cfg, field) for field in gel_ui.MATERIAL_FIELDS], parameters)
    # Keep the bake/grab fixture independent of the preset menu order.
    assert bpy.ops.object.volumetric_gel_preset(material='GRIP') == {'FINISHED'}
    assert bpy.ops.object.volumetric_gel_edit(action='BAKE') == {'FINISHED'}
    scene.frame_set(3, subframe=.25)
    baked = coordinates(obj)
    assert bpy.ops.object.volumetric_gel_live(running=True) == {'FINISHED'}
    session = gel_ui._live
    for _ in range(25):
        session.step(bpy.context, 1 / 60)
    live = coordinates(obj)
    assert np.max(np.abs(live - baked)) > .01
    assert scene.frame_current == 3 and scene.frame_subframe == .25
    # An existing baked modifier cannot override live geometry when scrubbing.
    scene.frame_set(13)
    np.testing.assert_allclose(coordinates(obj), live, atol=1e-6)
    assert bpy.ops.object.volumetric_gel_preset(material='FIRM') == {'FINISHED'}
    session.clock.last_time = time.monotonic() - .1
    assert gel_ui.live_tick() > 0 and session.sim.gel.shear == 18000
    surface = session.sim.gel.surface
    hit = session.sim.gel.x[surface[np.argmax(session.sim.gel.x[surface, 2])]].copy()
    assert session.grab_at(hit / session.sim.meters)
    ids = session.grip[0]
    before = session.sim.gel.x[ids].copy()
    session.pointer += np.array((.16, 0., .25))
    for _ in range(16):
        session.step(bpy.context, 1 / 60)
    assert (session.sim.gel.x[ids] - before)[:, 2].mean() > .035
    held = session.sim.gel.x[ids].copy()
    cfg.use_grab = False
    for _ in range(16):
        session.step(bpy.context, 1 / 60)
    assert session.grip is None and np.max(np.abs(session.sim.gel.x[ids] - held)) > .01
    assert bpy.ops.object.volumetric_gel_live(running=False) == {'FINISHED'}
    scene.frame_set(3, subframe=.25)
    np.testing.assert_allclose(coordinates(obj), baked, atol=1e-6)
    assert not bpy.app.timers.is_registered(gel_ui.live_tick)

    bpy.ops.object.volumetric_gel_live(running=True)
    bpy.ops.ed.undo_push(message="Before gel material change")
    bpy.ops.object.volumetric_gel_preset(material='SOFT')
    bpy.ops.ed.undo_push(message="After gel material change")
    assert bpy.ops.ed.undo() == {'FINISHED'}
    assert gel_ui._live and gel_ui._live.obj.volumetric_gel.shear == 18000
    assert bpy.ops.ed.redo() == {'FINISHED'}
    assert gel_ui._live and gel_ui._live.obj.volumetric_gel.shear == 4400
    obj = gel_ui._live.obj
    assert sum(1 for o in bpy.data.objects if o.get(gel_ui.LIVE_TAG)) == 1
    bpy.ops.object.volumetric_gel_live(running=False)

    # Preview data is transient even if saving while the timer is active.
    with tempfile.TemporaryDirectory() as folder:
        name = obj.name
        assert bpy.ops.object.volumetric_gel_live(running=True) == {'FINISHED'}
        path = str(Path(folder) / "live-save.blend")
        bpy.ops.wm.save_as_mainfile(filepath=path)
        bpy.ops.wm.open_mainfile(filepath=path)
        assert gel_ui._live is None and not any(g.get(gel_ui.LIVE_TAG) for g in bpy.data.node_groups)
        assert not any(o.get(gel_ui.LIVE_TAG) for o in bpy.data.objects)
        np.testing.assert_allclose(coordinates(bpy.data.objects[name]), baked, atol=1e-6)


def aggressive_grab_test():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=4, radius=.89, location=(0., 0., .935))
    obj = bpy.context.object
    bpy.ops.object.volumetric_gel_edit(action='ADD')
    bpy.ops.object.volumetric_gel_preset(material='EXTRA_SOFT')
    cfg = obj.volumetric_gel
    cfg.floor = .038
    cfg.grab_radius = .22
    assert bpy.ops.object.volumetric_gel_live(running=True) == {'FINISHED'}
    try:
        session = gel_ui._live
        gel = session.sim.gel
        for _ in range(48):
            session.step(bpy.context, 1 / 60)
        rest = gel.x.copy()
        surface = gel.surface
        picked = surface[np.argmax(gel.x[surface] @ np.array((.4, -.22, 1.)))]
        hit = gel.x[picked].copy()
        assert session.grab_at(hit / session.sim.meters)
        largest_move = 0.
        for target in ((3., 0., .4), (-3.5, 0., 1.1), (0., .3, -3.), (1., -.2, 3.)):
            session.pointer = hit + target
            for _ in range(18):
                session.step(bpy.context, 3 / 60)  # A busy viewport combines up to three ticks.
                assert gel._volume_ratios().min() > 0.
                assert gel.x[:, 2].min() >= cfg.floor - 1e-6
                largest_move = max(largest_move, np.linalg.norm(gel.x - rest, axis=1).max())
        assert largest_move > .25 and scene.frame_current == 1
        held = gel.x.copy()
        session.grip = None
        for _ in range(30):
            session.step(bpy.context, 1 / 60)
        assert np.linalg.norm(gel.x - held, axis=1).max() > .03
        assert gel_ui._live is session and gel._volume_ratios().min() > 0.
    finally:
        gel_ui.stop_live()


def fast_collider_test():
    """Drive the collider itself, including a reversal while the gel is loaded."""
    for preset, tick in (('SOFT', 1 / 60), ('EXTRA_SOFT', 3 / 60)):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        scene = bpy.context.scene
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3, radius=.89, location=(0., 0., .935))
        obj = bpy.context.object
        rod = bpy.data.objects.new("Fast striker", None)
        scene.collection.objects.link(rod)
        rod.location = (-1.65, -.04, .75)
        rod.rotation_euler.y = .15
        bpy.context.view_layer.update()
        bpy.ops.object.volumetric_gel_edit(action='ADD')
        bpy.ops.object.volumetric_gel_preset(material=preset)
        cfg = obj.volumetric_gel
        cfg.floor, cfg.collider, cfg.radius = .038, rod, .14
        cfg.start, cfg.end = (0, 0, -.52), (0, 0, .52)
        assert bpy.ops.object.volumetric_gel_live(running=True) == {'FINISHED'}
        try:
            session = gel_ui._live
            gel = session.sim.gel
            for _ in range(35):
                session.step(bpy.context, tick)
            settled = gel.x.copy()
            max_motion = 0.
            for x, tilt in ((1.6, .15), (-1.6, -.9), (1.6, .9), (-.15, .15), (0., .15), (6., .15)):
                rod.location.x = x
                rod.rotation_euler.y = tilt
                bpy.context.view_layer.update()
                for _ in range(90 if x == 6. else 9):
                    session.step(bpy.context, tick)
                    assert gel._volume_ratios().min() > 0 and gel.x[:, 2].min() >= cfg.floor - 1e-6
                    max_motion = max(max_motion, np.linalg.norm(gel.x - settled, axis=1).max())
            requested = gel_ui.input_samples(bpy.context, obj, cfg)[1]
            np.testing.assert_allclose(session.sim.previous.mean(axis=0), requested.mean(axis=0), atol=1e-6)
            assert max_motion > .15 and gel_ui._live is session and scene.frame_current == 1
        finally:
            gel_ui.stop_live()


def native_scene_timeline_test():
    from _xblender_mesh_collision import MeshContact

    # Sweep through the interior, miss the finite edge, and skip pinned points.
    vertices = np.array([[-1., -1., 0.], [1., -1., 0.], [0., 1., 0.]])
    collider = MeshContact(vertices, np.array([[0, 1, 2]], dtype=np.int32))
    collider.update(vertices, vertices + [0., 0., .12], .015)
    collider.culling, collider.friction = True, .5
    previous = np.array([[0., 0., .2], [3., 0., .2], [0., 0., .2]])
    proposed = previous - [0., 0., .4]
    mobile = np.array([True, True, False])
    results = []
    for project in (collider.project_numpy, collider.project):
        p, v = proposed.copy(), np.tile([.2, 0., -.4], (3, 1))
        project(previous, p, v, mobile, 0., 1., 1., .5)
        np.testing.assert_allclose(p[:, 2], [.135, -.2, -.2])
        np.testing.assert_allclose(v[:, 2], [.12, -.4, -.4])
        results.append((p, v))
    np.testing.assert_allclose(*results, atol=1e-10)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.frame_end = 50
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3, radius=.5, location=(0., 0., .57))
    obj = bpy.context.object
    bpy.ops.object.volumetric_gel_edit(action='ADD')
    cfg = obj.volumetric_gel
    cfg.resolution = '2'
    bpy.ops.mesh.primitive_plane_add(size=4, location=(0., 0., -.45))
    platform = bpy.context.object
    platform.modifiers.new("Collision", 'COLLISION')
    platform.collision.thickness_outer = .015
    # Animated geometry in local space also exercises BVH refitting.
    platform.shape_key_add(name="Basis")
    lift = platform.shape_key_add(name="Lift")
    for point in lift.data:
        point.co.z += .3
    for frame, value in ((1, 0.), (25, 0.), (40, 1.)):
        lift.value = value
        lift.keyframe_insert(data_path="value", frame=frame)
    bpy.context.view_layer.objects.active = obj
    scene.frame_set(1)
    cfg.simulation_mode = 'TIMELINE'
    session = gel_ui._live
    assert isinstance(session, gel_ui.TimelineGel)
    snapshots = {}
    for frame, sub in ((1, 0.), (24, 0.), (40, 0.), (12, .5), (24, 0.)):
        scene.frame_set(frame, subframe=sub)
        for _ in range(200):
            session.tick()
            if session.displayed == (frame, sub):
                break
        assert session.displayed == (scene.frame_current, scene.frame_subframe) == (frame, sub)
        points = coordinates(obj)
        if (frame, sub) in snapshots:
            np.testing.assert_allclose(points, snapshots[frame, sub], atol=1e-6)
        snapshots[frame, sub] = points.copy()
    for frame, height in ((24, -.45), (40, -.15)):
        np.testing.assert_allclose(snapshots[frame, 0.][:, 2].min() + obj.location.z, height + .015, atol=.009)
    assert bpy.ops.object.volumetric_gel_edit(action='BAKE') == {'FINISHED'}
    assert gel_ui._live is None
    name = obj.name
    with tempfile.TemporaryDirectory() as folder:
        path = str(Path(folder) / "collision.blend")
        bpy.ops.wm.save_as_mainfile(filepath=path)
        bpy.ops.wm.open_mainfile(filepath=path)
        obj, scene = bpy.data.objects[name], bpy.context.scene
        assert obj.volumetric_gel.simulation_mode == 'TIMELINE'
        for (frame, sub), positions in snapshots.items():
            scene.frame_set(frame, subframe=sub)
            np.testing.assert_allclose(coordinates(obj), positions, atol=2e-6)
        assert bpy.ops.object.volumetric_gel_edit(action='CLEAR') == {'FINISHED'}
        assert gel_ui.cache_modifier(obj) is None


if __name__ == "__main__":
    material_test()
    triangle_contact_test()
    native_kernel_test()
    native_scene_timeline_test()
    bake_test()
    inclined_plane_test()
    animated_pin_test()
    balloon_skin_test()
    live_test()
    aggressive_grab_test()
    fast_collider_test()
    print("PASS volumetric gel material, contact, bake, live clock, grab, presets and reload")
