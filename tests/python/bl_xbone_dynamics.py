# SPDX-License-Identifier: GPL-2.0-or-later
"""Run in an XBone build. Pass -- --demo to open the interactive sample."""

import math
import sys
import tempfile
import time
import traceback
from pathlib import Path

import bpy
from bpy.app.translations import pgettext_data as data_
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "startup"))
import xbone_dynamics as dynamics

if not hasattr(bpy.types.Object, "xbone_dynamics"):
    dynamics.register()


def mesh_object(name, verts, faces, color):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    obj.color = (*color, 1)
    for p in mesh.polygons:
        p.use_smooth = True
    return obj


def make_demo():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.object.xbone_add(enter_editmode=True)
    rig = bpy.context.edit_object
    rig.name = data_("XBone Ponytail Preview")
    rig.show_in_front = True
    edits = rig.data.edit_bones
    edits.active.name = "Head"
    pivot = edits.active.head.copy()
    points = [Vector(p) + pivot for p in [(0.26, 0, 0.18), (0.49, 0, 0.20), (0.73, 0, 0.12),
                                            (0.95, 0, -0.02), (1.15, 0, -0.16), (1.34, 0, -0.25)]]
    names = [f"Hair.{i:02d}" for i in range(len(points))]

    def joint(parent, name, point):
        bpy.ops.armature.select_all(action='DESELECT')
        edits.active = edits[parent]
        edits.active.select = edits.active.select_head = edits.active.select_tail = True
        bpy.context.scene.cursor.location = point
        bpy.ops.armature.xbone_joint_add()
        edits.active.name = name

    for i, p in enumerate(points):
        joint(names[i-1] if i else "Head", names[i], p)
    shoulder = pivot + Vector((0.48, 0, -0.45))
    for name, y in (("Shoulder.L", -0.29), ("Shoulder.R", 0.29)):
        joint("Head", name, shoulder + Vector((0, y, 0)))
    bpy.ops.object.mode_set(mode='OBJECT')

    verts, faces = [], []
    for i, p in enumerate(points):
        radius = 0.09 * (1 - i / len(points)) + 0.012
        for k in range(12):
            angle = math.tau * k / 12
            verts.append(p + radius * Vector((0, math.cos(angle), math.sin(angle))))
            if i:
                a, b = (i-1)*12+k, (i-1)*12+(k+1)%12
                faces.append((a, b, b+12, a+12))
    faces.extend((tuple(reversed(range(12))), tuple(range(len(verts)-12, len(verts)))))
    hair = mesh_object(data_("Ponytail Skin"), verts, faces, (0.58, 0.2, 0.055))
    hair.parent = rig
    for i, name in enumerate(names):
        hair.vertex_groups.new(name=name).add(list(range(i*12, i*12+12)), 1, 'REPLACE')
    hair.modifiers.new("XBone", 'ARMATURE').object = rig
    for label, center, scale in ((data_("Head"), pivot, (.31, .27, .34)),
                                 (data_("Neck"), pivot + Vector((0.03, 0, -0.29)), (.15, .16, .22)),
                                 (data_("Upper Body"), pivot + Vector((0.12, 0, -0.55)), (.45, .40, .24)),
                                 (data_("Shoulders"), shoulder, (.16, .43, .16))):
        bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16, location=center)
        obj = bpy.context.object
        obj.name, obj.scale, obj.color = label, scale, (0.21, 0.42, 0.49, 1)
        obj.parent = rig
        group = obj.vertex_groups.new(name="Head")
        group.add(list(range(len(obj.data.vertices))), 1, 'REPLACE')
        obj.modifiers.new("XBone", 'ARMATURE').object = rig
        for poly in obj.data.polygons:
            poly.use_smooth = True

    bpy.ops.object.select_all(action='DESELECT')
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    cfg = rig.xbone_dynamics
    cfg.enabled = True
    c = cfg.chains.add()
    c.root, c.tip = names[0], names[-1]
    collider = cfg.colliders.add()
    collider.shape, collider.bone, collider.end = 'CAPSULE', "Shoulder.L", "Shoulder.R"
    collider.radius = 0.16
    bpy.context.scene.cursor.location = pivot
    for area in bpy.context.screen.areas:
        if area.type == 'PROPERTIES':
            area.spaces.active.context = 'PHYSICS'
        if area.type == 'VIEW_3D':
            space = area.spaces.active
            space.shading.color_type = 'OBJECT'
            space.overlay.show_floor = space.overlay.show_cursor = False
            space.overlay.show_extras = False
            space.overlay.show_bones = True
            space.region_3d.view_location = pivot + Vector((0.47, 0, -0.12))
            space.region_3d.view_rotation = Vector((0, 1, -0.14)).to_track_quat('-Z', 'Y')
            space.region_3d.view_distance = 3.4
    return rig, hair


def verify_chain_stability(cfg):
    # Same reach, motion, material and timestep. Only the subdivision count changes.
    gravity = Vector((0, 0, -9.81))
    for segments in (6, 12, 24, 48):
        rest = [Vector((1.2 * i / segments, 0, 0)) for i in range(segments + 1)]
        chain = dynamics.ChainState([])
        chain.reset(rest)
        for _ in range(360):
            chain.advance(rest, cfg, gravity, [])
        largest_turn = 0.
        lag = 0.
        for frame in range(240):
            t = (frame + 1) * dynamics.STEP
            offset = Vector((0, .25 * math.sin(math.tau * t) if frame < 120 else 0, 0))
            pose = [p + offset for p in rest]
            before = chain.positions[-1] - chain.positions[-2]
            chain.advance(pose, cfg, gravity, [])
            after = chain.positions[-1] - chain.positions[-2]
            if frame < 120:
                lag = max(lag, abs(chain.positions[-1].y - offset.y))
            if frame >= 180:
                largest_turn = max(largest_turn, math.atan2(before.cross(after).length, before.dot(after)))
        assert lag > .01, ("no delayed motion", segments)
        assert math.degrees(largest_turn) < 3, (segments, math.degrees(largest_turn))
        assert max(abs((b - a).length - 1.2 / segments)
                   for a, b in zip(chain.positions, chain.positions[1:])) < 1e-5
    # A curved input with uneven spacing must remain at rest with no external force.
    pose = [Vector(p) for p in ((0, 0, 0), (.2, 0, 0), (.31, .09, 0), (.31, .09, .08))]
    chain.reset(pose)
    for _ in range(60):
        chain.advance(pose, cfg, Vector((0, 0, 0)), [])
    assert max((a-b).length for a, b in zip(pose, chain.positions)) < 2e-5


def verify(rig, hair):
    scene = bpy.context.scene
    frame = scene.frame_current
    layer = bpy.context.view_layer
    tip = rig.xbone_dynamics.chains[0].tip

    def sample():
        layer.update()
        out = rig.evaluated_get(layer.depsgraph)
        skinned = hair.evaluated_get(layer.depsgraph)
        return out.pose.bones[tip].head.copy(), skinned.data.vertices[-1].co.copy()

    verify_chain_stability(rig.xbone_dynamics.chains[0])
    initial = sample()
    assert bpy.ops.xbone.dynamics_start() == {'FINISHED'}
    preview = dynamics._session
    for _ in range(90):
        preview.tick(1)
    moved = sample()
    chain = preview.chains[0]
    assert (initial[0] - moved[0]).length > 0.1, (initial, moved)
    assert (chain.positions[-1] - moved[0]).length < 0.002, (chain.positions[-1], moved[0])
    assert (initial[1] - moved[1]).length > 0.1
    c = rig.xbone_dynamics.colliders[0]
    for i, (a, b) in enumerate(zip(chain.positions, chain.positions[1:])):
        _, p, q = dynamics.closest_pair(a, b, rig.pose.bones[c.bone].head, rig.pose.bones[c.end].head)
        assert (p - q).length >= c.radius + rig.xbone_dynamics.chains[0].radius - 0.003
        src = rig.data.bones[chain.names[i+1]].head_local - rig.data.bones[chain.names[i]].head_local
        assert abs((b-a).length - src.length) < 0.0001
    rig.location.x += 0.22
    preview.tick(1)
    assert abs(chain.positions[0].x - rig.pose.bones[chain.names[0]].head.x - 0.22) < 0.001
    assert bpy.ops.xbone.dynamics_stop() == {'FINISHED'}
    assert scene.frame_current == frame
    assert all((a-b).length < 1e-5 for a, b in zip(initial, sample()))
    assert rig.animation_data is None
    assert not any(o.get(dynamics.TARGET_TAG) for o in bpy.data.objects)
    assert not bpy.app.timers.is_registered(dynamics.advance_timer)
    # Isolate an interior contact with both endpoints outside the sphere.
    cfg = rig.xbone_dynamics.chains[0]
    cfg.root_angle = math.pi
    ab = [Vector((0, 0, 0)), Vector((1, 0, 0))]
    center = Vector((0.5, 0.04, 0))
    probe = dynamics.ChainState(["A", "B"])
    probe.reset(ab)
    probe.advance(ab, cfg, Vector((0, 0, 0)), [(center, center, 0.12)])
    t, p, q = dynamics.closest_pair(*probe.positions, center, center)
    assert 0.1 < t < 0.9 and (p-q).length >= 0.12 + cfg.radius - 0.001
    cfg.root_angle = math.radians(8)
    for _ in range(80):
        probe.advance(ab, cfg, Vector((0, 0, -90)), [])
    assert (probe.positions[1]-ab[0]).angle(ab[1]) <= cfg.root_angle + 0.001
    # Run the production timer callback with a fixed elapsed interval.
    assert bpy.ops.xbone.dynamics_start() == {'FINISHED'}
    preview = dynamics._session
    preview.clock = time.monotonic() - 3.1 * dynamics.STEP
    assert dynamics.advance_timer() > 0
    assert (sample()[0] - initial[0]).length > 0.001
    layer.objects.active = hair
    assert bpy.ops.xbone.dynamics_stop() == {'FINISHED'}
    assert not dynamics.XBD_GGT_stop.poll(bpy.context)
    layer.objects.active = rig
    rig.pose.bones["Head"].rotation_mode = 'XYZ'
    rig.pose.bones["Head"].rotation_euler.y = 0.3
    rig.pose.bones["Head"].keyframe_insert(data_path="rotation_euler")
    posed = sample()
    assert bpy.ops.xbone.dynamics_start() == {'FINISHED'}
    for _ in range(6):
        dynamics._session.tick(1)
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "dynamics.blend")
        rig_name = rig.name
        bpy.ops.wm.save_as_mainfile(filepath=path)
        assert dynamics._session is None
        assert all((a-b).length < 1e-5 for a, b in zip(posed, sample()))
        bpy.ops.wm.open_mainfile(filepath=path)
        restored = bpy.data.objects[rig_name]
        assert restored.xbone_dynamics.enabled and len(restored.xbone_dynamics.chains) == 1
        assert not any(o.get(dynamics.TARGET_TAG) for o in bpy.data.objects)
        assert not any(b.constraints for b in restored.pose.bones)
        assert restored.animation_data.action is not None and not restored.animation_data.drivers
    verify_parameter_undo(restored)
    print("PASS: XBone paused preview, skinned tip, segment lengths, capsule clearance, anchor and stop")


def verify_parameter_undo(rig):
    uid = rig.session_uid
    layer = bpy.context.view_layer
    rig_name = rig.name
    initial_gravity = rig.xbone_dynamics.gravity
    initial_damping = rig.xbone_dynamics.chains[0].damping
    bpy.ops.ed.undo_push(message="Before tuning")
    assert bpy.ops.xbone.dynamics_start() == {'FINISHED'}
    rig.xbone_dynamics.gravity = 3.5
    bpy.ops.ed.undo_push(message="Change gravity")
    rig.xbone_dynamics.chains[0].damping = 13
    bpy.ops.ed.undo_push(message="Change damping")
    # Picking a mesh while testing should not change which armature resumes.
    layer.objects.active = next(o for o in layer.objects if o.type == 'MESH')
    for op, gravity, damping in ((bpy.ops.ed.undo, 3.5, initial_damping),
                                 (bpy.ops.ed.undo, initial_gravity, initial_damping),
                                 (bpy.ops.ed.redo, 3.5, initial_damping),
                                 (bpy.ops.ed.redo, 3.5, 13)):
        for _ in range(4):
            dynamics._session.tick(1)
        tip = dynamics._session.chains[0].positions[-1].copy()
        assert op() == {'FINISHED'}
        preview = dynamics._session
        assert preview and preview.rig.session_uid == uid
        assert preview.rig.xbone_dynamics.gravity == gravity
        assert preview.rig.xbone_dynamics.chains[0].damping == damping
        assert (preview.chains[0].positions[-1] - tip).length < 1e-5
        assert bpy.app.timers.is_registered(dynamics.advance_timer)
        moving = preview.chains[0].names[:-1]
        assert len(preview.rig.animation_data.drivers) == len(moving)
        assert sum(bool(o.get(dynamics.TARGET_TAG)) for o in bpy.data.objects) == len(moving) + 1
        assert all(len(preview.rig.pose.bones[n].constraints) == 1 for n in moving)
    assert bpy.ops.xbone.dynamics_stop() == {'FINISHED'}
    bpy.ops.ed.undo()
    assert dynamics._session is None
    assert not bpy.app.timers.is_registered(dynamics.advance_timer)
    assert not bpy.data.objects[rig_name].animation_data.drivers
    assert not any(o.get(dynamics.TARGET_TAG) for o in bpy.data.objects)
    print("PASS: live undo/redo of preview parameters, particle continuity and stopped-session cleanup")


def window_test(rig):
    """Exercise the real clock and the drawn gizmo using Blender APIs."""
    tip = rig.xbone_dynamics.chains[0].tip
    initial = rig.pose.bones[tip].head.copy()
    frame = bpy.context.scene.frame_current
    drawn = []
    original_draw = dynamics.XBD_GT_stop.draw

    def checked_draw(button, context):
        original_draw(button, context)
        x, y, w, h, _ = dynamics.stop_bounds(context)
        drawn.append(button.test_select(context, (x+w/2, y+h/2)) == 0)

    dynamics.XBD_GT_stop.draw = checked_draw
    start = time.monotonic()

    def check():
        try:
            if dynamics._session is None:
                assert bpy.ops.xbone.dynamics_start() == {'FINISHED'}
                return 0.1
            if (not drawn or (rig.pose.bones[tip].head-initial).length < 0.05) and time.monotonic()-start < 12:
                return 0.1
            assert drawn and all(drawn), "Stop gizmo did not draw with a valid hit target"
            assert (rig.pose.bones[tip].head-initial).length >= 0.05
            assert bpy.context.scene.frame_current == frame
            assert bpy.ops.xbone.dynamics_stop() == {'FINISHED'}
            assert not dynamics.XBD_GGT_stop.poll(bpy.context)
            assert (rig.pose.bones[tip].head-initial).length < 0.001
            print("PASS: live timer, paused timeline, viewport stop gizmo and pose recovery", flush=True)
        except Exception:
            traceback.print_exc()
        bpy.ops.wm.quit_blender()
        return None

    bpy.app.timers.register(check, first_interval=0.25)


def move_gizmo_test():
    """Run in the interactive sample with --enable-event-simulate -- --gizmo-test."""
    from bpy_extras.view3d_utils import location_3d_to_region_2d
    win = bpy.context.window_manager.windows[0]
    area = next(a for a in win.screen.areas if a.type == 'VIEW_3D')
    region = next(r for r in area.regions if r.type == 'WINDOW')
    space = area.spaces.active
    rig = win.view_layer.objects.active
    assert rig.xbone_dynamics.enabled

    def event(kind, value, x, y):
        win.event_simulate(type=kind, value=value, x=x, y=y)

    def drag():
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.wm.tool_set_by_id(name='builtin.move')
        space.show_gizmo = space.show_gizmo_object_translate = True
        view = space.region_3d
        view.view_perspective = 'ORTHO'
        view.view_rotation = Vector((0, 1, -.22)).to_track_quat('-Z', 'Y')
        view.view_location = rig.matrix_world.translation
        view.view_distance = 4
        yield
        for simulate in (False, True):
            if simulate:
                assert bpy.ops.xbone.dynamics_start() == {'FINISHED'}
                yield
            origin = rig.location.copy()
            xy = location_3d_to_region_2d(region, view, rig.matrix_world.translation)
            x = round(region.x + xy.x + bpy.context.preferences.view.gizmo_size * .7 *
                      bpy.context.preferences.system.ui_scale)
            y = round(region.y + xy.y)
            for kind, value, dx in (('MOUSEMOVE', 'NOTHING', 0), ('LEFTMOUSE', 'PRESS', 0),
                                    ('MOUSEMOVE', 'NOTHING', 15), ('MOUSEMOVE', 'NOTHING', 70)):
                event(kind, value, x + dx, y)
                yield
            delta = rig.location - origin
            assert delta.x > .01 and abs(delta.y) + abs(delta.z) < .0001, (simulate, delta)
            if simulate:
                assert (delta - baseline).length < .001, (delta, baseline)
            else:
                baseline = delta.copy()
            event('LEFTMOUSE', 'RELEASE', x + 70, y)
            yield
        assert bpy.ops.xbone.dynamics_stop() == {'FINISHED'}
        print("PASS: move gizmo drags match with and without the live preview", flush=True)

    events = drag()
    def step():
        try:
            next(events)
            return .2
        except StopIteration:
            pass
        except Exception:
            traceback.print_exc()
        bpy.ops.wm.quit_blender()
        return None
    bpy.app.timers.register(step, first_interval=.6)


if __name__ == "__main__":
    if "--gizmo-test" in sys.argv:
        move_gizmo_test()
    else:
        rig, hair = make_demo()
        if "--window-test" in sys.argv:
            window_test(rig)
        elif "--demo" not in sys.argv:
            verify(rig, hair)
