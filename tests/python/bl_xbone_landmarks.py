# SPDX-License-Identifier: GPL-2.0-or-later
"""Fit XBiped to an offset, rotated mesh and build the ordinary editable rig."""
import tempfile
from pathlib import Path

import bpy
from mathutils import Vector
from xbone_rigging import smart_guide as smart


def near(a, b):
    assert (a - b).length < .0001, (a[:], b[:])


c = bpy.context
ops = bpy.ops.xbone
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=2, location=(3, -4, 5))
body = c.object
body.scale = (.25, .9, 1.1)
c.view_layer.update()
original = body.matrix_world.copy()
assert ops.start_landmarks(facing='POS_X') == {'FINISHED'}
c.view_layer.update()
root = c.scene.xbone_marker_session
assert len(smart.steps(root)) == 12 and smart.sources(c, root) == [body]
near(smart.surface_center(c, root, Vector((10, -4, 5)), Vector((-1, 0, 0))), Vector((3, -4, 5)))
assert smart.surface_center(c, root, Vector((10, 10, 5)), Vector((-1, 0, 0))) is None
points = dict(pelvis=(.04, .03, 1.05), neck=(.04, .01, 1.65), head=(.04, -.005, 1.82), crown=(.04, 0, 2.12),
              shoulder=(.26, 0, 1.60), elbow=(.52, .04, 1.53), wrist=(.79, 0, 1.49), hand=(.98, -.015, 1.46),
              hip=(.14, 0, 1.01), knee=(.16, -.05, .54), ankle=(.16, 0, .12), toe=(.18, -.21, 0))
expected = {}
for i, (key, _) in enumerate(smart.steps(root)):
    xyz = Vector(points[key.split('.')[0]])
    expected[key] = root.matrix_world @ xyz
    c.scene.cursor.location = expected[key]
    assert ops.marker_cursor() == {'FINISHED'}
    if i == 2:
        assert ops.marker_back() == {'FINISHED'}
        assert ops.marker_cursor() == {'FINISHED'}
    if key.endswith('.L'):
        right = xyz.copy()
        right.x = 2 * points['pelvis'][0] - right.x
        expected[key[:-1] + 'R'] = root.matrix_world @ right
with tempfile.TemporaryDirectory() as tmp:
    path = str(Path(tmp) / 'markers.blend')
    bpy.ops.wm.save_as_mainfile(filepath=path)
    bpy.ops.wm.open_mainfile(filepath=path)
    root = c.scene.xbone_marker_session
    body = smart.sources(c, root)[0]
    assert smart.current_step(root) == 12 and len(smart.markers(root)) == 12
    assert len([o for o in root.children if o.get("xbone_mirrored_landmark")]) == 8
    # Invalid joint centers leave the placement session available to correct.
    m = smart.markers(root)
    pos = m["wrist.L"].location.copy()
    m["wrist.L"].location = m["elbow.L"].location
    try:
        ops.fit_landmarks()
    except RuntimeError:
        pass
    assert c.scene.xbone_marker_session == root
    m["wrist.L"].location = pos
    assert ops.fit_landmarks() == {'FINISHED'}
guide = c.object
c.view_layer.update()
assert guide.get("xbiped") and not c.scene.xbone_marker_session and not body.modifiers
assert body.matrix_world == original
names = {"pelvis": "spine_C0", "neck": "neck_C0", "head": "neck_C0_head", "crown": "neck_C0_eff"}
for side in 'LR':
    for key, name in (("shoulder", "arm_@0"), ("elbow", "arm_@0_elbow"), ("wrist", "arm_@0_wrist"),
                      ("hand", "finger_@1_2_loc"), ("hip", "leg_@0"), ("knee", "leg_@0_knee"),
                      ("ankle", "leg_@0_ankle"), ("toe", "foot_@0_2_loc")):
        names[key + '.' + side] = name.replace('@', side)
for key, name in names.items():
    near(guide.matrix_world @ guide.data.bones[name].head_local, expected[key])
assert ops.build_rig() == {'FINISHED'}
rig = c.object
c.view_layer.update()
assert sum(b.use_deform for b in rig.data.bones) == 106
assert not any(not f.driver.is_valid for f in rig.animation_data.drivers)
for side in 'LR':
    ankle = rig.pose.bones[names['ankle.' + side]]
    near(rig.matrix_world @ ankle.head, expected['ankle.' + side])
    wrist = rig.pose.bones[names['wrist.' + side]]
    near(wrist.head, rig.pose.bones["FK." + wrist.name].head)
    goal = rig.pose.bones["IK." + wrist.name]
    assert ops.match_limb(control=goal.name, mode='IK') == {'FINISHED'}
    mat = goal.matrix.copy()
    mat.translation = rig.matrix_world.inverted() @ expected['wrist.' + side]
    goal.matrix = mat
    c.view_layer.update()
    near(rig.matrix_world @ wrist.head, expected['wrist.' + side])
assert ops.edit_source_guide() == {'FINISHED'}
assert ops.build_rig() == {'FINISHED'} and c.object == rig
bpy.ops.object.mode_set(mode='OBJECT')
for ob in c.selected_objects:
    ob.select_set(False)
body.select_set(True)
c.view_layer.objects.active = body
assert ops.start_landmarks(symmetric=False) == {'FINISHED'}
root = c.scene.xbone_marker_session
c.view_layer.update()
assert len(smart.steps(root)) == 20
for key, _ in smart.steps(root):
    v = Vector(points[key.split('.')[0]])
    if key.endswith('.R'):
        v.x = -v.x
        v.y += .1
    c.scene.cursor.location = root.matrix_world @ v
    assert ops.marker_cursor() == {'FINISHED'}
right_wrist = root.matrix_world @ smart.markers(root)["wrist.R"].location
assert ops.fit_landmarks() == {'FINISHED'}
c.view_layer.update()
near(c.object.matrix_world @ c.object.data.bones["arm_R0_wrist"].head_local, right_wrist)
for ob in c.selected_objects:
    ob.select_set(False)
body.select_set(True)
c.view_layer.objects.active = body
assert ops.start_landmarks() == {'FINISHED'}
assert ops.cancel_landmarks() == {'FINISHED'} and not c.scene.xbone_marker_session
print("PASS: mesh depth, resumable landmarks, symmetry, fitted guide, XBiped and in-place rebuild")
