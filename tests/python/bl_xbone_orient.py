# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

import itertools
from pathlib import Path
import tempfile

import bpy
from mathutils import Euler, Matrix, Vector


def near(a, b):
    assert (Vector(a) - Vector(b)).length < 1e-4, (a, b)


def select(joint):
    bpy.ops.armature.select_all(action='DESELECT')
    joint.select = joint.select_head = joint.select_tail = True
    arm.edit_bones.active = joint


def reset_frames():
    for j in joints:
        j.matrix = Matrix.Translation(j.head.copy())


def orient():
    assert bpy.ops.armature.xbone_orient_joints() == {'FINISHED'}
    for j, pos in zip(joints, positions):
        near(j.head, pos)
        assert abs(j.matrix.to_3x3().determinant() - 1) < 1e-4


bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.object.xbone_add(enter_editmode=True)
rig = bpy.context.edit_object
arm = rig.data
positions = list(map(Vector, [(0, 0, 0), (2, 1, 0), (3, 1, 2), (4, 3, 1)]))
joints = [arm.edit_bones.active]
for pos in positions[1:]:
    bpy.context.scene.cursor.location = pos
    assert bpy.ops.armature.xbone_joint_add() == {'FINISHED'}
    joints.append(arm.edit_bones.active)
select(joints[0])
arm.xbone_orient_children = True

for primary, secondary in itertools.permutations("XYZ", 2):
    arm.xbone_primary_axis = primary
    arm.xbone_secondary_axis = secondary
    for sign, axis in itertools.product(("POS", "NEG"), "XYZ"):
        arm.xbone_world_up = f"{sign}_{axis}"
        reset_frames()
        orient()
        ref = Vector(tuple((1 if sign == "POS" else -1) if a == axis else 0 for a in "XYZ"))
        for parent, child in zip(joints, joints[1:]):
            direction = (child.head - parent.head).normalized()
            near(parent.matrix.to_3x3().col["XYZ".index(primary)], direction)
            projected = ref - direction * ref.dot(direction)
            if projected.length > 1e-5:
                near(parent.matrix.to_3x3().col["XYZ".index(secondary)], projected.normalized())

arm.xbone_primary_axis = 'X'
arm.xbone_secondary_axis = 'Y'
arm.xbone_world_up = 'POS_Z'
arm.xbone_auto_secondary = True
reset_frames()
orient()
# Maya 2025, Z-up scene, joint -e -oj xyz -sao zup -aos -ch -zso.
for joint, expected in zip(joints, [(-.4364358, .8728716, .2182179),
                                     (-.4364358, .8728716, .2182179),
                                     (-.7427814, .557086, .3713907),
                                     (-.7427814, .557086, .3713907)]):
    near(joint.matrix.to_3x3().col[1], expected)

# Maya's pole fallback for an exactly collinear aim and up direction.
arm.xbone_auto_secondary = False
arm.xbone_world_up = 'POS_Y'
joints[1].joint_position = (0, 1, 0)
reset_frames()
assert bpy.ops.armature.xbone_orient_joints() == {'FINISHED'}
near(joints[0].matrix.to_3x3().col[0], (0, 1, 0))
near(joints[0].matrix.to_3x3().col[1], (-1, 0, 0))
joints[1].joint_position = positions[1]
arm.xbone_world_up = 'POS_Z'
arm.xbone_auto_secondary = True

arm.xbone_secondary_axis = 'NONE'
reset_frames()
orient()
near(joints[1].matrix.to_3x3().col[1], (-.7807201, -.48795, .39036))
near(joints[-1].matrix.to_3x3().col[0], (1, 0, 0))

arm.xbone_secondary_axis = 'Y'
arm.xbone_primary_axis = 'Y'
assert arm.xbone_secondary_axis == 'X'
arm.xbone_secondary_axis = 'Y'
assert arm.xbone_primary_axis == 'X'

# Object rotation and repeated execution must not move any joint.
rig.rotation_euler = Euler((.3, -.4, .2))
bpy.context.view_layer.update()
arm.xbone_auto_secondary = False
orient()
world = rig.matrix_world.to_3x3()
direction = (world @ (joints[1].head - joints[0].head)).normalized()
z = Vector((0, 0, 1))
near(world @ joints[0].matrix.to_3x3().col[1], (z - direction * z.dot(direction)).normalized())
arm.xbone_orient_world = True
arm.xbone_orient_children = False
rest_child = joints[1].matrix.copy()
orient()
for a, b in zip(world @ joints[0].matrix.to_3x3(), Matrix.Identity(3)):
    near(a, b)
for a, b in zip(joints[1].matrix, rest_child):
    near(a, b)

# A new branch must preserve the direction to the first child.
arm.xbone_orient_world = False
arm.xbone_primary_axis = 'Z'
arm.xbone_secondary_axis = 'X'
arm.xbone_world_up = 'NEG_Y'
orient()
frame = joints[0].matrix.copy()
bpy.context.scene.cursor.location = rig.matrix_world @ Vector((0, 3, 0))
assert bpy.ops.armature.xbone_joint_add() == {'FINISHED'}
for a, b in zip(joints[0].matrix, frame):
    near(a, b)
near(arm.edit_bones.active.matrix.to_3x3().col[2], frame.to_3x3().col[2])

# Both interactive extrusion and cursor placement use the configured local aim axis.
leaf = arm.edit_bones.active
start = leaf.head.copy()
end = start + Vector((1, -2, 1))
bpy.context.scene.cursor.location = rig.matrix_world @ end
assert bpy.ops.armature.xbone_joint_add() == {'FINISHED'}
near(leaf.matrix.to_3x3().col[2], (end - start).normalized())
leaf = arm.edit_bones.active
start = leaf.head.copy()
assert bpy.ops.armature.extrude_move(
    TRANSFORM_OT_translate={"value": (1, 0, 1), "orient_type": 'GLOBAL'}
) == {'FINISHED'}
near(leaf.matrix.to_3x3().col[2], (arm.edit_bones.active.head - start).normalized())

bpy.ops.object.mode_set(mode='OBJECT')
with tempfile.TemporaryDirectory() as directory:
    path = str(Path(directory) / "orientation.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path)
    bpy.ops.wm.open_mainfile(filepath=path)
    arm = bpy.context.object.data
    assert (arm.xbone_primary_axis, arm.xbone_secondary_axis, arm.xbone_world_up) == ('Z', 'X', 'NEG_Y')
print("XBone joint orientation passed")
