# SPDX-License-Identifier: GPL-2.0-or-later
"""Check hand channels against the evaluated skin skeleton and the animator's FK channels."""
import tempfile
from pathlib import Path

import bpy
from mathutils import Vector


def update(rig):
    rig.update_tag()
    bpy.context.view_layer.update()


def tips(rig, side):
    points = []
    for i, name in enumerate(("thumb", "index", "middle", "ring", "pinky")):
        digit = f"thumb_{side}0" if name == "thumb" else f"finger_{side}{i-1}"
        skin = rig.pose.bones[f"{name}_03_{side.lower()}"]
        tip = rig.data.bones[digit + "_2_loc"].head_local
        points.append(skin.matrix @ skin.bone.matrix_local.inverted() @ tip)
    return points


def same(actual, expected):
    for a, b in zip(actual, expected, strict=True):
        assert (a - b).length < .00015, (a, b)


bpy.ops.wm.read_factory_settings(use_empty=True)
assert bpy.ops.xbone.biped_guide(template='xbiped') == {'FINISHED'}
assert bpy.ops.xbone.build_rig() == {'FINISHED'}
rig = bpy.context.object
name = rig.name
left, right = [rig.pose.bones[rig["xbiped_hand_" + s]] for s in "LR"]
assert left.custom_shape and right.custom_shape and not left.bone.use_deform
opened = tips(rig, 'L')
other = tips(rig, 'R')
for key in ("spread", "cup"):
    left[key] = 1.
    update(rig)
    moved = tips(rig, 'L')
    if key == "spread":
        assert (moved[1] - moved[-1]).length > (opened[1] - opened[-1]).length + .015
    else:
        same(moved[:2], opened[:2])
        assert (moved[-1] - opened[-1]).length > .01
    left[key] = 0.
    update(rig)
for value in (.5, 1.):
    left["grip"] = value
    update(rig)
    same(tips(rig, 'R'), other)
    moved = tips(rig, 'L')
    assert all((a - b).length > .025 for a, b in zip(moved, opened))
    if value == .5:
        half = moved
    else:
        assert all((a - b).length > .015 for a, b in zip(moved, half))
closed = tips(rig, 'L')
right["grip"] = 1.
update(rig)
same(tips(rig, 'R'), [Vector((-p.x, p.y, p.z)) for p in closed])
left["index_curl"] = -1.
update(rig)
pointing = tips(rig, 'L')
same(pointing, [closed[0], opened[1], *closed[2:]])

# Weight Paint's reset also clears the procedural gestures on both hands.
bpy.ops.object.mode_set(mode='OBJECT')
bpy.ops.mesh.primitive_cube_add()
mesh = bpy.context.object
mesh.modifiers.new("Skin", 'ARMATURE').object = rig
bpy.ops.object.mode_set(mode='WEIGHT_PAINT')
left["spread"] = -1.
right["cup"] = .7
assert bpy.ops.paint.skin_rest_pose() == {'FINISHED'}
update(rig)
same(tips(rig, 'L'), opened)
same(tips(rig, 'R'), other)
bpy.ops.object.mode_set(mode='OBJECT')
mesh.select_set(False)
rig.select_set(True)
bpy.context.view_layer.objects.active = rig
bpy.ops.object.mode_set(mode='POSE')
left["grip"] = right["grip"] = 1.
left["index_curl"] = -1.
update(rig)

fk = rig.pose.bones["FK.finger_L0_0_loc"]
fk.rotation_mode = 'XYZ'
fk.rotation_euler.z = .23
fk.keyframe_insert(data_path="rotation_euler", frame=1)
update(rig)
saved = tips(rig, 'L')
assert (saved[1] - pointing[1]).length > .001
left.keyframe_insert(data_path='["grip"]', frame=1)
left["grip"] = 0.
left.keyframe_insert(data_path='["grip"]', frame=9)
bpy.context.scene.frame_set(9)
same(tips(rig, 'L')[2:], opened[2:])
bpy.context.scene.frame_set(1)
same(tips(rig, 'L'), saved)
action = rig.animation_data.action
assert bpy.ops.xbone.edit_source_guide() == {'FINISHED'}
assert bpy.ops.xbone.build_rig() == {'FINISHED'} and bpy.context.object == rig
assert rig.animation_data.action == action
update(rig)
same(tips(rig, 'L'), saved)
with tempfile.TemporaryDirectory() as directory:
    path = str(Path(directory) / "grip.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path)
    bpy.ops.wm.open_mainfile(filepath=path)
    rig = bpy.data.objects[name]
    same(tips(rig, 'L'), saved)
    same(tips(rig, 'R'), [Vector((-p.x, p.y, p.z)) for p in closed])
    assert rig.pose.bones["HandGrip.L"]["index_curl"] == -1.
    assert abs(rig.pose.bones["FK.finger_L0_0_loc"].rotation_euler.z - .23) < .0001
    assert all(curve.driver.is_valid for curve in rig.animation_data.drivers)
print("XBiped grip, individual curl, palm controls, FK animation, rebuild and reload passed")
