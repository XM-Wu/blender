# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

import bpy


def select_edit(arm, names):
    for bone in arm.edit_bones:
        bone.select = bone.select_head = bone.select_tail = bone.name in names


def test_armature():
    mesh = bpy.context.active_object
    assert mesh.skin_layers.strength == 1.0
    mesh.skin_layers.strength = 0.25
    bpy.ops.object.armature_add(enter_editmode=True)
    rig = bpy.context.edit_object
    arm = rig.data
    root = arm.edit_bones[0]
    root.name = "Arm_002_L"
    # Create the second branch before the grandchild. DFS must still visit the grandchild first.
    for name, parent, x in (("Bridge", root.name, 1), ("Branch", root.name, 2),
                            ("Arm_001_L", "Bridge", 3), ("Untouched", None, 4)):
        bone = arm.edit_bones.new(name)
        bone.head, bone.tail = (x, 0, 0), (x, 1, 0)
        if parent:
            bone.parent = arm.edit_bones[parent]
    bpy.ops.object.mode_set(mode='OBJECT')
    mesh.modifiers.new("Deform", 'ARMATURE').object = rig
    group = mesh.vertex_groups.new(name="Branch")
    group.add([0], 0.75, 'REPLACE')
    group.skin_channel = 187
    constraint = mesh.constraints.new('COPY_ROTATION')
    constraint.target, constraint.subtarget = rig, "Arm_001_L"
    rig.pose.bones["Arm_001_L"].keyframe_insert(data_path="location", frame=1)
    bpy.ops.object.mode_set(mode='EDIT')
    select_edit(arm, {"Arm_002_L", "Arm_001_L", "Branch"})
    assert bpy.ops.armature.batch_rename_hierarchy(base_name="Arm", side='LEFT') == {'FINISHED'}
    assert arm.edit_bones["Arm_001_L"].parent is None
    assert arm.edit_bones["Arm_002_L"].parent.name == 'Bridge'
    assert arm.edit_bones["Arm_003_L"].parent.name == 'Arm_001_L'
    assert {"Bridge", "Untouched"} <= set(arm.edit_bones.keys())
    assert constraint.subtarget == 'Arm_002_L'
    assert mesh.vertex_groups['Arm_003_L'].weight(0) == 0.75
    assert mesh.vertex_groups['Arm_003_L'].skin_channel == 187
    assert mesh.skin_layers.strength == 0.25

    bpy.ops.object.mode_set(mode='POSE')
    for bone in rig.pose.bones:
        bone.select = bone.name.startswith("Arm_")
    assert bpy.ops.armature.batch_rename_hierarchy(base_name="Limb", side='NONE') == {'FINISHED'}
    assert constraint.subtarget == "Limb_002"
    assert mesh.vertex_groups["Limb_003"].weight(0) == 0.75
    action = rig.animation_data.action
    channelbag = action.layers[0].strips[0].channelbag(rig.animation_data.action_slot)
    assert all(fc.data_path == 'pose.bones["Limb_002"].location' for fc in channelbag.fcurves)

    # A name owned outside the selection must fail before changing any bone or skin binding.
    rig.data.bones["Untouched"].name = "Blocked_003"
    mesh.vertex_groups.new(name="Cloth_001")
    names = set(arm.bones.keys())
    for base in ("Cloth", "Blocked", "骨" * 30, "   "):
        try:
            bpy.ops.armature.batch_rename_hierarchy(base_name=base)
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"Expected rejection for {base!r}")
        assert set(arm.bones.keys()) == names and constraint.subtarget == "Limb_002"
    bpy.ops.object.mode_set(mode='OBJECT')


def test_xbone():
    bpy.ops.object.xbone_add(enter_editmode=True)
    arm = bpy.context.edit_object.data
    arm.edit_bones.active.name = "Root"
    for x in (1, 2, 3):
        bpy.context.scene.cursor.location = (x, x, 0)
        bpy.ops.armature.xbone_joint_add()
    select_edit(arm, set(arm.edit_bones.keys()) - {"Root"})
    assert bpy.ops.armature.batch_rename_hierarchy(base_name="Finger", side='RIGHT') == {'FINISHED'}
    assert bpy.ops.armature.symmetrize() == {'FINISHED'}
    for i in (1, 2, 3):
        right = arm.edit_bones[f"Finger_{i:03d}_R"]
        left = arm.edit_bones[bpy.utils.flip_name(right.name)]
        assert left.name == f"Finger_{i:03d}_L"
        assert abs(left.head.x + right.head.x) < 1e-5
        assert left.parent.name == ('Root' if i == 1 else f"Finger_{i-1:03d}_L")
    # Mirror editing includes implied counterparts in the context. Rename only explicit picks.
    arm.use_mirror_x = True
    select_edit(arm, {"Finger_001_R"})
    assert bpy.ops.armature.batch_rename_hierarchy(base_name="Only", side='RIGHT') == {'FINISHED'}
    assert "Only_001_R" in arm.edit_bones and "Finger_001_L" in arm.edit_bones


def test_multiple_armatures():
    bpy.ops.object.mode_set(mode='OBJECT')
    rigs = []
    for name in ("First", "Second"):
        bpy.ops.object.armature_add()
        rig = bpy.context.object
        rig.data.name = name
        rigs.append(rig)
    for rig in rigs:
        rig.select_set(True)
    bpy.context.view_layer.objects.active = rigs[1]
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.armature.select_all(action='SELECT')
    assert bpy.ops.armature.batch_rename_hierarchy(base_name="Joint") == {'FINISHED'}
    assert "Joint_001" in rigs[1].data.edit_bones and "Joint_002" in rigs[0].data.edit_bones


if __name__ == "__main__":
    test_armature()
    test_xbone()
    test_multiple_armatures()
    print("PASS brush strength, DFS, atomic conflicts, pose and skin references, XBone side suffixes and mirror")
