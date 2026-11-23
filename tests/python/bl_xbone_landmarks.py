# SPDX-License-Identifier: GPL-2.0-or-later
"""Place XBiped landmarks freely and build the ordinary editable rig."""
import tempfile
from pathlib import Path

import bpy
from mathutils import Vector
from xbone_rigging import smart_guide as smart


def near(a, b):
    assert (a - b).length < .0001, (a[:], b[:])


names = {"pelvis": "spine_C0", "neck": "neck_C0", "head": "neck_C0_head", "crown": "neck_C0_eff"}
for side in 'LR':
    for key, name in (("shoulder", "arm_@0"), ("elbow", "arm_@0_elbow"), ("wrist", "arm_@0_wrist"),
                      ("hand", "finger_@1_2_loc"), ("hip", "leg_@0"), ("knee", "leg_@0_knee"),
                      ("ankle", "leg_@0_ankle"), ("toe", "foot_@0_2_loc")):
        names[key + '.' + side] = name.replace('@', side)

c = bpy.context
ops = bpy.ops.xbone
bpy.ops.wm.read_factory_settings(use_empty=True)
# Every untouched marker must match the ordinary XBiped template, even in an empty scene.
cursor = Vector((14, 5, -9))
c.scene.cursor.location = cursor
assert not c.selected_objects and ops.start_landmarks(symmetric=False) == {'FINISHED'}
root = c.scene.xbone_marker_session
ref, *_ = smart.guides.template_points(Path(smart.__file__).with_name("biped.sgt"), 0.1, 'Y')
neutral = {label: pos for label, pos, *_ in ref.values()}
for key, _ in smart.steps(root):
    assert ops.place_landmarks() == {'FINISHED'}
    assert c.selected_objects == [c.object] and c.object["xbone_landmark"] == key
    near(c.object.matrix_world.translation, neutral[names[key]])
assert ops.fit_landmarks() == {'FINISHED'}
for name in names.values():
    near(c.object.matrix_world @ c.object.data.bones[name].head_local, neutral[name])
near(c.scene.cursor.location, cursor)

bpy.ops.mesh.primitive_cube_add(size=2, location=(3, -4, 5))
body = c.object
body.scale = (.25, .9, 1.1)
c.view_layer.update()
original = body.matrix_world.copy()
assert ops.start_landmarks(facing='POS_X') == {'FINISHED'}
c.view_layer.update()
root = c.scene.xbone_marker_session
assert len(smart.steps(root)) == 12
near(root.location, Vector((0, 0, 0)))
root.location = (3, -4, 3.9)
c.view_layer.update()
points = dict(pelvis=(.04, .03, 1.05), neck=(.04, .01, 1.65), head=(.04, -.005, 1.82), crown=(.04, 0, 2.12),
              shoulder=(.26, 0, 1.60), elbow=(.52, .04, 1.53), wrist=(.79, 0, 1.49), hand=(.98, -.015, 1.46),
              hip=(.14, 0, 1.01), knee=(.16, -.05, .54), ankle=(.16, 0, .12), toe=(.18, -.21, 0))
expected = {}
for i, (key, _) in enumerate(smart.steps(root)):
    xyz = Vector(points[key.split('.')[0]])
    expected[key] = root.matrix_world @ xyz
    assert ops.place_landmarks() == {'FINISHED'}
    assert c.object["xbone_landmark"] == key
    near(c.object.location, neutral[names[key]])
    c.object.location = xyz
    if i == 2:
        assert ops.marker_back() == {'FINISHED'}
        assert c.object["xbone_landmark"] == "neck"
        assert ops.place_landmarks() == {'FINISHED'}
        near(c.object.location, xyz)
    if key.endswith('.L'):
        right = xyz.copy()
        right.x = 2 * points['pelvis'][0] - right.x
        expected[key[:-1] + 'R'] = root.matrix_world @ right
with tempfile.TemporaryDirectory() as tmp:
    path = str(Path(tmp) / 'markers.blend')
    bpy.ops.wm.save_as_mainfile(filepath=path)
    bpy.ops.wm.open_mainfile(filepath=path)
    root = c.scene.xbone_marker_session
    body = bpy.data.objects["Cube"]
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
    assert ops.place_landmarks() == {'FINISHED'}
    c.object.location = v
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
print("PASS: template defaults, selection-independent placement, free movement, resumable landmarks, symmetry, fitted guide, XBiped and in-place rebuild")
