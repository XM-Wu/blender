# SPDX-License-Identifier: GPL-2.0-or-later
"""Run in an XBone build. Pass -- --demo to open the interactive sample."""

import math
import sys
import tempfile
import time
import traceback
from pathlib import Path

import bpy
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
    rig.name = "XBone · 马尾预览"
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
    hair = mesh_object("马尾 · 蒙皮", verts, faces, (0.58, 0.2, 0.055))
    hair.parent = rig
    for i, name in enumerate(names):
        hair.vertex_groups.new(name=name).add(list(range(i*12, i*12+12)), 1, 'REPLACE')
    hair.modifiers.new("XBone", 'ARMATURE').object = rig
    for label, center, scale in (("头部", pivot, (.31, .27, .34)),
                                 ("颈部", pivot + Vector((0.03, 0, -0.29)), (.15, .16, .22)),
                                 ("上身", pivot + Vector((0.12, 0, -0.55)), (.45, .40, .24)),
                                 ("肩部", shoulder, (.16, .43, .16))):
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
            space.overlay.show_extras = space.overlay.show_bones = False
            space.region_3d.view_location = pivot + Vector((0.47, 0, -0.12))
            space.region_3d.view_rotation = Vector((0, 1, -0.14)).to_track_quat('-Z', 'Y')
            space.region_3d.view_distance = 3.4
    return rig, hair


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
        assert restored.animation_data.action is not None
    print("PASS: XBone paused preview, skinned tip, segment lengths, capsule clearance, anchor and stop")


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


if __name__ == "__main__":
    rig, hair = make_demo()
    if "--window-test" in sys.argv:
        window_test(rig)
    elif "--demo" not in sys.argv:
        verify(rig, hair)
