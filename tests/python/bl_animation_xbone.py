# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Exercise the same operators used by the Add and Armature menus."""

import math
import sys
import tempfile
from pathlib import Path

import bpy
from mathutils import Matrix, Vector


POSITIONS = [(2, 1, 2), (0, 2, 3), (2, 1, 3)]
WEIGHTS = [1, 0, 0.25]


def near(actual, expected):
    assert (Vector(actual) - Vector(expected)).length < 0.0001, (actual, expected)


def make_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.object.armature_add()
    legacy = bpy.context.object
    legacy.name = "Legacy"
    assert not legacy.data.is_xbone
    assert legacy.data.bones[0].xbone_joint is None
    near(legacy.data.bones[0].head_local, (0, 0, 0))
    near(legacy.data.bones[0].tail_local, (0, 0, 1))

    bpy.ops.object.xbone_add(location=(3, 4, 1), enter_editmode=True)
    rig = bpy.context.edit_object
    rig.name = "XBone"
    bpy.context.view_layer.update()
    edits = rig.data.edit_bones
    root = edits.active
    root.name = "Root"

    def child(parent, target, name):
        bpy.ops.armature.select_all(action='DESELECT')
        edits.active = parent
        parent.select = parent.select_head = parent.select_tail = True
        bpy.context.scene.cursor.location = rig.matrix_world @ Vector(target)
        assert bpy.ops.armature.xbone_joint_add() == {'FINISHED'}
        joint = edits.active
        joint.name = name
        near(joint.joint_position, target)
        return joint

    trunk = child(root, (0, 0, 2), "Trunk")
    # Exercise the E macro as well as the cursor placement operator.
    assert bpy.ops.armature.extrude_move(
        TRANSFORM_OT_translate={"value": (0, 2, 0), "orient_type": 'GLOBAL'}
    ) == {'FINISHED'}
    edits.active.name = "Tip"
    near(edits.active.joint_position, (0, 2, 2))
    child(trunk, (2, 0, 2), "Branch")
    near(trunk.matrix.to_3x3().col[0], (0, 1, 0))
    near(root.matrix.to_3x3().col[0], (0, 0, 1))
    branch = edits["Branch"]
    frame = branch.joint_orientation.copy()
    branch.joint_position = (3, 0, 2)
    near(branch.joint_orientation, frame)
    branch.joint_position = (2, 0, 2)
    bpy.ops.object.mode_set(mode='POSE')
    for name in ("Branch", "Trunk"):
        joint = rig.pose.bones[name]
        joint.rotation_mode = 'XYZ'
        for frame, angle in ((1, 0), (11, math.pi / 2)):
            joint.rotation_euler.x = angle
            joint.keyframe_insert(data_path="rotation_euler", index=0, frame=frame)
    bpy.ops.object.mode_set(mode='OBJECT')

    mesh = bpy.data.meshes.new("Weighted triangle")
    mesh.from_pydata(POSITIONS, [], [(0, 1, 2)])
    mesh.update()
    obj = bpy.data.objects.new("Skin", mesh)
    bpy.context.collection.objects.link(obj)
    obj.matrix_world = rig.matrix_world.copy()
    branch = obj.vertex_groups.new(name="Branch")
    trunk = obj.vertex_groups.new(name="Trunk")
    for index, weight in enumerate(WEIGHTS):
        branch.add([index], weight, 'REPLACE')
        trunk.add([index], 1 - weight, 'REPLACE')
    modifier = obj.modifiers.new("XBone", 'ARMATURE')
    modifier.object = rig
    modifier.use_bone_envelopes = False


def verify():
    rig = bpy.data.objects["XBone"]
    bones = rig.data.bones
    assert rig.data.is_xbone and len(bones) == 4
    assert {b.name for b in bones["Trunk"].children} == {"Tip", "Branch"}
    assert bpy.data.objects["Legacy"].data.bones[0].xbone_joint is None
    for bone in bones:
        joint = bone.xbone_joint
        rest = Matrix.LocRotScale(joint.position, joint.orientation, None)
        if bone.parent:
            rest = bone.parent.matrix_local @ rest
        for got, want in zip(rest, bone.matrix_local):
            near(got, want)
    # Check independent ownership of the optional joint data when duplicating an armature.
    duplicate = rig.data.copy()
    assert duplicate.bones["Tip"].xbone_joint.as_pointer() != bones["Tip"].xbone_joint.as_pointer()
    bpy.data.armatures.remove(duplicate)

    obj = bpy.data.objects["Skin"]
    for frame, angle in ((1, 0), (6, math.pi / 4), (11, math.pi / 2)):
        bpy.context.scene.frame_set(frame)
        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        parent_rot = Matrix.Rotation(angle, 3, 'Y')
        leaf_rot = Matrix.Rotation(angle, 3, 'X')
        pivot = Vector((0, 0, 2))
        leaf = Vector((2, 0, 2))
        for vertex, position, weight in zip(evaluated.data.vertices, POSITIONS, WEIGHTS):
            position = Vector(position)
            branch_position = leaf + leaf_rot @ (position - leaf)
            mixed = position.lerp(branch_position, weight)
            near(vertex.co, pivot + parent_rot @ (mixed - pivot))


if __name__ == "__main__":
    if bpy.data.filepath:
        verify()
    else:
        make_scene()
        verify()
        bpy.context.scene.frame_set(1)
        bpy.context.scene.frame_end = 11
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "XBone.blend"
            if "--" in sys.argv:
                path = Path(sys.argv[sys.argv.index("--") + 1])
            bpy.ops.wm.save_as_mainfile(filepath=str(path))
            bpy.ops.wm.open_mainfile(filepath=str(path))
            verify()
    print("XBone chain, branching, FK animation, skinning and blend round-trip passed")
