# SPDX-License-Identifier: GPL-2.0-or-later

import json
import tempfile
from pathlib import Path

import bpy
import xbone_rigging
from mathutils import Vector


def check_pose(rig, elbow, wrist):
    pose = rig.pose.bones
    tip = pose[wrist]
    fk = pose["FK." + elbow]
    fk.rotation_mode = 'XYZ'
    fk.rotation_euler.z += 0.35
    bpy.context.view_layer.update()
    assert (tip.head - rig.data.bones[wrist].head_local).length > 0.01
    key = "IK " + tip.bone["ik_limb"]
    rig[key] = 1.0
    rig.update_tag()
    control = pose["IK." + wrist]
    dest = control.matrix.copy()
    root = tip.parent.parent.head
    dest.translation = root.lerp(control.head, 0.74) + Vector((0, 0.03, 0))
    control.matrix = dest
    bpy.context.view_layer.update()
    assert (tip.head - control.head).length < 0.005, (wrist, tip.head[:], control.head[:])
    pole = pose["Pole." + elbow]
    old = pose[elbow].head.copy()
    pole.location.x += 0.25
    pole.location.z += 0.15
    bpy.context.view_layer.update()
    assert (pose[elbow].head - old).length > 0.001
    assert (tip.head - control.head).length < 0.005
    rig[key] = 0.0
    rig.update_tag()
    bpy.context.view_layer.update()
    assert (tip.head - pose["FK." + wrist].head).length < 0.001
    for mode in ('IK', 'FK'):
        before = [b.head.copy() for b in (tip.parent.parent, tip.parent, tip)]
        orientations = [b.matrix.to_quaternion() for b in (tip.parent.parent, tip.parent, tip)]
        assert bpy.ops.xbone.match_limb(control=control.name, mode=mode) == {'FINISHED'}
        for old, current in zip(before, (tip.parent.parent, tip.parent, tip)):
            assert (old - current.head).length < 0.005, (mode, current.name, old[:], current.head[:])
        for rotation, current in zip(orientations, (tip.parent.parent, tip.parent, tip)):
            assert abs(rotation.dot(current.matrix.to_quaternion())) > 0.999, (mode, current.name)
    assert all(not c.use_deform for c in rig.data.bones if c.name.startswith(("FK.", "MCH.", "IK.", "Pole.")))


bpy.ops.wm.read_factory_settings(use_empty=True)
path = Path(xbone_rigging.__file__).with_name("biped.sgt")
assert bpy.ops.xbone.import_guide(filepath=str(path)) == {'FINISHED'}
guide = bpy.context.object
assert guide.data.is_xbone
data = json.loads(path.read_text())
assert len(guide.data.bones) == sum(len(c["pos"]) for c in data["components_dict"].values())
assert guide.data.bones["shoulder_L0"].parent.name == "spine_C0_eff"
assert bpy.ops.xbone.build_rig() == {'FINISHED'}
rig = bpy.context.object
assert sum(key.startswith("IK ") for key in rig.keys()) == 4
check_pose(rig, "arm_L0_elbow", "arm_L0_wrist")
check_pose(rig, "leg_R0_knee", "leg_R0_ankle")
bpy.ops.object.mode_set(mode='OBJECT')
assert bpy.ops.xbone.biped_guide(template='game_biped') == {'FINISHED'}
assert bpy.ops.xbone.build_rig() == {'FINISHED'}
assert bpy.context.object.pose.bones["FK.arm_L0"].custom_shape.type == 'CURVE'
check_pose(bpy.context.object, "arm_R0_elbow", "arm_R0_wrist")
bpy.ops.object.mode_set(mode='OBJECT')
assert bpy.ops.xbone.new_guide() == {'FINISHED'}
assert bpy.ops.xbone.add_component(kind='ARM') == {'FINISHED'}
assert bpy.ops.xbone.build_rig() == {'FINISHED'}
rig = bpy.context.object
check_pose(rig, "Forearm.L", "Hand.L")
bpy.context.scene.tool_settings.use_keyframe_insert_auto = True
for frame, mode in ((1, 'FK'), (12, 'IK')):
    bpy.context.scene.frame_set(frame)
    assert bpy.ops.xbone.match_limb(control="IK.Hand.L", mode=mode) == {'FINISHED'}
assert rig.animation_data.action
bpy.context.scene.frame_set(1)
assert rig["IK Hand.L"] == 0
bpy.context.scene.frame_set(12)
assert rig["IK Hand.L"] == 1
bpy.context.scene.tool_settings.use_keyframe_insert_auto = False
name = rig.name
with tempfile.TemporaryDirectory() as directory:
    blend = str(Path(directory) / "rig.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    bpy.ops.wm.open_mainfile(filepath=blend)
    rig = bpy.data.objects[name]
    assert rig.pose.bones["IK.Hand.L"].custom_shape
    check_pose(rig, "Forearm.L", "Hand.L")
    bpy.ops.object.mode_set(mode='OBJECT')
    expected = {bone.name: (bone.parent.name if bone.parent else None, bone.matrix_local.copy())
                for bone in rig.data.bones if bone.use_deform}
    fbx = str(Path(directory) / "deform.fbx")
    rig.data.pose_position = 'REST'
    bpy.context.view_layer.update()
    assert bpy.ops.export_scene.fbx(filepath=fbx, use_selection=True, use_armature_deform_only=True,
                                    add_leaf_bones=False, bake_anim=False) == {'FINISHED'}
    bpy.ops.wm.read_factory_settings(use_empty=True)
    assert bpy.ops.wm.xbone_fbx_import(filepath=fbx) == {'FINISHED'}
    imported, = (o for o in bpy.context.scene.objects if o.type == 'ARMATURE')
    assert set(imported.data.bones.keys()) == set(expected)
    for bone in imported.data.bones:
        parent, matrix = expected[bone.name]
        assert (bone.parent.name if bone.parent else None) == parent
        assert max(abs(x-y) for row, ref in zip(bone.matrix_local, matrix)
                   for x, y in zip(row, ref)) < 0.0001, (bone.name, bone.matrix_local, matrix)
    bpy.context.view_layer.objects.active = imported
    assert bpy.ops.xbone.copy_guide() == {'FINISHED'}
    guide = bpy.context.object
    bpy.ops.object.mode_set(mode='EDIT')
    guide.data.edit_bones.active = guide.data.edit_bones["Hand.L"]
    assert bpy.ops.xbone.tag_limb() == {'FINISHED'}
    assert bpy.ops.xbone.build_rig() == {'FINISHED'}
    check_pose(bpy.context.object, "Forearm.L", "Hand.L")
    assert bpy.ops.xbone.edit_source_guide() == {'FINISHED'}
    assert bpy.context.edit_object == guide
print("Shifter templates, custom and imported guides, FK/IK, matching, reload and FBX passed")
