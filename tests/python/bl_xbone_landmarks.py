# SPDX-License-Identifier: GPL-2.0-or-later
"""Place XBiped landmarks freely and build the ordinary editable rig."""
import tempfile
from pathlib import Path
from unittest.mock import Mock

import bpy
from mathutils import Vector
from xbone_rigging import smart_guide as smart


def near(a, b):
    assert (a - b).length < .0001, (a[:], b[:])


def check_current(root, index):
    order = smart.steps(root)
    key, label = order[index]
    assert c.selected_objects == [c.object] and c.object["xbone_landmark"] == key
    panel = Mock()
    smart.RIGGING_PT_landmarks.draw(panel, c)
    panel.layout.label.assert_called_once_with(text=f"{index + 1}/{len(order)}  {label}", icon='EMPTY_AXIS')


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
for i, (key, _) in enumerate(smart.steps(root)):
    if i:
        assert ops.place_landmarks() == {'FINISHED'}
    check_current(root, i)
    near(c.object.matrix_world.translation, neutral[names[key]])
assert ops.place_landmarks() == {'CANCELLED'}
assert ops.fit_landmarks() == {'FINISHED'}
for name in names.values():
    near(c.object.matrix_world @ c.object.data.bones[name].head_local, neutral[name])
near(c.scene.cursor.location, cursor)
old_name = c.object.name
other_layer = c.scene.view_layers.new("Other Layer")
c.object.hide_set(True, view_layer=other_layer)

bpy.ops.mesh.primitive_cube_add(size=2, location=(3, -4, 5))
body = c.object
body.scale = (.25, .9, 1.1)
c.view_layer.update()
original = body.matrix_world.copy()
assert ops.start_landmarks(facing='POS_X') == {'FINISHED'}
c.view_layer.update()
root = c.scene.xbone_marker_session
assert len(smart.steps(root)) == 12
assert bpy.data.objects[old_name].hide_get() and body.visible_get()
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
    if i:
        assert ops.place_landmarks() == {'FINISHED'}
    check_current(root, i)
    near(c.object.location, neutral[names[key]])
    c.object.location = xyz
    if i == 2:
        assert ops.marker_back() == {'FINISHED'}
        check_current(root, i - 1)
        assert ops.place_landmarks() == {'FINISHED'}
        check_current(root, i)
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
    check_current(root, 11)
    assert bpy.data.objects[old_name].hide_get() and len(smart.markers(root)) == 12
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
    assert bpy.data.objects[old_name].hide_get()
    m["wrist.L"].location = pos
    assert ops.fit_landmarks() == {'FINISHED'}
guide = c.object
c.view_layer.update()
assert guide.get("xbiped") and not c.scene.xbone_marker_session and not body.modifiers
assert guide.visible_get() and bpy.data.objects[old_name].visible_get()
assert bpy.data.objects[old_name].hide_get(view_layer=c.scene.view_layers["Other Layer"])
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
for i, (key, _) in enumerate(smart.steps(root)):
    v = Vector(points[key.split('.')[0]])
    if key.endswith('.R'):
        v.x = -v.x
        v.y += .1
    if i:
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
assert bpy.data.objects[old_name].hide_get()
assert ops.cancel_landmarks() == {'FINISHED'} and not c.scene.xbone_marker_session
assert bpy.data.objects[old_name].visible_get()
assert bpy.data.objects[old_name].hide_get(view_layer=c.scene.view_layers["Other Layer"])
# An older file saved on the initial Pelvis prompt has a session but no marker.
assert ops.start_landmarks() == {'FINISHED'}
root = c.scene.xbone_marker_session
bpy.data.objects.remove(c.object, do_unlink=True)
root["step"] = 0
for layer in c.scene.view_layers:
    for obj in layer["xbone_landmark_hidden_guides"].values():
        obj.hide_set(False, view_layer=layer)
    del layer["xbone_landmark_hidden_guides"]
with tempfile.TemporaryDirectory() as tmp:
    path = str(Path(tmp) / 'legacy.blend')
    bpy.ops.wm.save_as_mainfile(filepath=path)
    bpy.ops.wm.open_mainfile(filepath=path)
    root = c.scene.xbone_marker_session
    check_current(root, 0)
    assert bpy.data.objects[old_name].hide_get()
    assert ops.place_landmarks() == {'FINISHED'}
    check_current(root, 1)
    assert ops.cancel_landmarks() == {'FINISHED'}
    assert bpy.data.objects[old_name].visible_get()
def finish_steps(root):
    while smart.next_step(root, smart.current_step(root, c.object)) is not None:
        assert ops.place_landmarks() == {'FINISHED'}
        if root.get("isolate_marker"):
            assert c.object.visible_get()
            assert all(not ob.visible_get() for ob in root.children if ob != c.object)


for symmetric in (True, False):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    assert ops.start_landmarks(symmetric=symmetric) == {'FINISHED'}
    root = c.scene.xbone_marker_session
    finish_steps(root)
    assert ops.landmark_details(detail='FINGERS') == {'FINISHED'}
    assert c.object["xbone_landmark"] == "thumb_0.L"
    assert ops.isolate_landmark() == {'FINISHED'}
    finish_steps(root)
    assert ops.marker_back() == {'FINISHED'} and c.object.visible_get()
    finish_steps(root)
    assert ops.landmark_details(detail='FACE') == {'FINISHED'}
    finish_steps(root)
    assert len(smart.steps(root)) == (38 if symmetric else 67)
    assert ops.isolate_landmark() == {'FINISHED'}
    assert all(ob.visible_get() for ob in root.children)
    expected = {}
    for key, marker in smart.markers(root).items():
        component, locator = smart.landmark_keys()[key]
        if component.startswith(("thumb_", "finger_", "eye_", "mouth_")):
            # Bend, spread and vary each side independently.
            j = smart.DIGIT_LOCS.index(locator) if locator in smart.DIGIT_LOCS else 0
            marker.location += Vector((.005, .004 * (j + 1), -.003 * j * j)) * (-2 if key.endswith(".R") else 1)
            expected[key] = marker.location.copy()
            if symmetric and key.endswith(".L"):
                other = expected[key].copy()
                other.x = 2 * smart.markers(root)["pelvis"].location.x - other.x
                expected[key[:-1] + 'R'] = other
    assert ops.fit_landmarks() == {'FINISHED'}
    guide = c.object
    for key, pos in expected.items():
        comp, loc = smart.landmark_keys()[key]
        bone = guide.data.bones[comp if loc == "root" else comp + "_" + loc]
        near(guide.matrix_world @ bone.head_local, pos)
    # Preserve a custom branch and settings while changing an individual digit and eye.
    guide.data.bones["arm_L0"]["div0"] = 4
    bpy.ops.object.mode_set(mode='EDIT')
    custom = guide.data.edit_bones.new("Accessory")
    custom.parent = guide.data.edit_bones["finger_L0_0_loc"]
    custom.head = custom.parent.head + Vector((0, .04, 0))
    custom.tail = custom.head + Vector((0, .03, 0))
    bpy.ops.object.mode_set(mode='OBJECT')
    assert ops.build_rig() == {'FINISHED'}
    rig = c.object
    initial_mode = rig.mode
    assert ops.edit_landmarks() == {'FINISHED'} and rig.hide_get()
    root = c.scene.xbone_marker_session
    assert len(smart.markers(root)) == (38 if symmetric else 67)
    m = smart.markers(root)
    near(root.matrix_world @ m["eye.L"].location, expected["eye.L"])
    m["eye.L"].location.y += .01
    assert ops.cancel_landmarks() == {'FINISHED'}
    assert c.object == rig and rig.mode == initial_mode and not rig.hide_get()
    assert ops.edit_landmarks() == {'FINISHED'}
    root = c.scene.xbone_marker_session
    m = smart.markers(root)
    changes = {"index_1.L": Vector((0, -.005, .008)), "eye.L": Vector((.002, -.01, .004))}
    for key, offset in changes.items():
        m[key].location += offset
        expected[key] = root.matrix_world @ m[key].location
    assert ops.fit_landmarks() == {'FINISHED'} and c.object == guide
    assert guide.data.bones["arm_L0"]["div0"] == 4 and "Accessory" in guide.data.bones
    for key, name in (("index_1.L", "finger_L0_0_loc"), ("eye.L", "eye_L0")):
        near(guide.matrix_world @ guide.data.bones[name].head_local, expected[key])
    assert ops.build_rig() == {'FINISHED'} and c.object == rig
    for key, name in (("index_1.L", "FK.finger_L0_0_loc"), ("eye.L", "eye_L0")):
        near(rig.matrix_world @ rig.data.bones[name].head_local, expected[key])

bpy.ops.wm.read_factory_settings(use_empty=True)
assert ops.biped_guide(template='xbiped') == {'FINISHED'}
guide = c.object
guide.location = (3, -1, 2)
guide.rotation_euler.z = .4
bpy.ops.object.mode_set(mode='EDIT')
guide.data.edit_bones["eye_L0"].translate(Vector((.012, .002, 0)))
assert ops.edit_landmarks() == {'FINISHED'} and c.mode == 'OBJECT'
root = c.scene.xbone_marker_session
assert not root["symmetric"]
assert ops.landmark_details(detail='FACE') == {'FINISHED'}
near(c.object.location, guide.data.bones["eye_L0"].head_local)
assert ops.isolate_landmark() == {'FINISHED'}
with tempfile.TemporaryDirectory() as tmp:
    path = str(Path(tmp) / "refit.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path)
    bpy.ops.wm.open_mainfile(filepath=path)
    root = c.scene.xbone_marker_session
    guide = root["editing_guide"]
    assert c.object["xbone_landmark"] == "eye.L" and c.object.visible_get()
    assert all(not ob.visible_get() for ob in root.children if ob != c.object)
    finish_steps(root)
    assert ops.landmark_details(detail='FINGERS') == {'FINISHED'}
    finish_steps(root)
    assert ops.fit_landmarks() == {'FINISHED'} and c.object == guide
    assert ops.edit_landmarks() == {'FINISHED'}
    root = c.scene.xbone_marker_session
    assert root["isolate_marker"] and len(smart.markers(root)) == 67
    assert ops.cancel_landmarks() == {'FINISHED'}

print("PASS: XBiped body, fingers, face, marker isolation, cancel, guide refit and in-place rebuild")
