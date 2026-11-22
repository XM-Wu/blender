# SPDX-License-Identifier: GPL-2.0-or-later
"""Manual XBiped joints must attach to the skin skeleton and display their FK axes."""
import math
from pathlib import Path
import tempfile

import bpy
from mathutils import Vector


def near(a, b):
    assert (a - b).length < 0.00012, (tuple(a), tuple(b))


def add_at(guide, parent, name, location):
    edit = guide.data.edit_bones
    bpy.ops.armature.select_all(action='DESELECT')
    edit.active = edit[parent]
    edit.active.select = True
    bpy.context.scene.cursor.location = guide.matrix_world @ location
    assert bpy.ops.armature.xbone_joint_add() == {'FINISHED'}
    edit.active.name = name


def check_chain(rig):
    pose = rig.pose.bones
    for name, parent, direction in (
            ("Branch.L", "shoulder_L0_shoulder_jnt", points[1] - points[0]),
            ("Segment.L", "Branch.L", points[2] - points[1]),
            ("Tip.L", "Segment.L", points[2] - points[1]),
            ("Antenna.C", "neck_C0_head_jnt", None),
            ("Cuff.L", "arm_L0_end_jnt", None)):
        joint, fk = pose[name], pose["FK." + name]
        assert joint.bone.use_deform and joint.parent.name == fk.parent.name == parent
        near(joint.head, fk.head)
        link = pose["Display.Link." + name]
        assert not link.bone.use_deform and link.bone.hide_select
        assert link.bone.collections.get("Additional FK")
        near(link.head, pose[parent].head)
        near(link.matrix @ Vector((0, link.custom_shape_scale_xyz.y, 0)), joint.head)
        near(link.bone.matrix_local @ Vector((0, link.custom_shape_scale_xyz.y, 0)), joint.bone.head_local)
        if direction is not None:
            normal = fk.bone.matrix_local.to_quaternion() @ (fk.custom_shape_rotation_euler.to_quaternion() @
                                                              Vector((0, 1, 0)))
            assert abs(normal.dot(direction.normalized())) > .9999, name


bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.xbone.biped_guide(template='xbiped')
guide = bpy.context.object
bpy.ops.object.mode_set(mode='EDIT')
edit = guide.data.edit_bones
base = edit["shoulder_L0"].head.copy()
points = [base + Vector(offset) for offset in ((.12, .06, .09), (.23, .1, .18), (.3, .21, .22))]
for name, parent, location in zip(("Branch.L", "Segment.L", "Tip.L"),
                                  ("shoulder_L0", "Branch.L", "Segment.L"), points):
    add_at(guide, parent, name, location)
# Rigging-panel additions use a different rest-axis convention than native extrusion.
edit.active = edit["neck_C0_head"]
assert bpy.ops.xbone.add_component(kind='FK', side='C') == {'FINISHED'}
edit.active.name = "Antenna.C"
edit["arm_L0_wrist"].name = "Wrist.L"
add_at(guide, "Wrist.L", "Cuff.L", edit["Wrist.L"].head + Vector((.04, .06, .03)))
assert bpy.ops.xbone.build_rig() == {'FINISHED'}
rig = bpy.context.object
rig.data.collections["Additional FK"].is_visible = True
check_chain(rig)
parent = rig.pose.bones["shoulder_L0_shoulder_jnt"]
start = parent.matrix.inverted() @ rig.pose.bones["Branch.L"].head
control = rig.pose.bones["FK.shoulder_L0"]
control.rotation_mode = 'XYZ'
control.rotation_euler.z = math.radians(25)
bpy.context.view_layer.update()
near(rig.pose.bones["Branch.L"].head, parent.matrix @ start)
assert (rig.pose.bones["Branch.L"].head - points[0]).length > .005
check_chain(rig)
before = rig.pose.bones["Segment.L"].head.copy()
fk = rig.pose.bones["FK.Branch.L"]
fk.rotation_mode = 'XYZ'
fk.rotation_euler.z = .45
bpy.context.view_layer.update()
assert (rig.pose.bones["Segment.L"].head - before).length > .005
check_chain(rig)
assert bpy.ops.xbone.edit_source_guide() == {'FINISHED'}
assert bpy.ops.xbone.build_rig() == {'FINISHED'}
assert bpy.context.object == rig
check_chain(rig)
with tempfile.TemporaryDirectory() as directory:
    path = str(Path(directory) / "custom-branch.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path)
    bpy.ops.wm.open_mainfile(filepath=path)
    check_chain(bpy.context.object)
    rig = bpy.context.object
    bpy.ops.object.mode_set(mode='OBJECT')
    rig.data.pose_position = 'REST'
    fbx = str(Path(directory) / "custom-branch.fbx")
    assert bpy.ops.export_scene.fbx(filepath=fbx, use_selection=True, add_leaf_bones=False,
                                    use_armature_deform_only=True, bake_anim=False) == {'FINISHED'}
    bpy.ops.wm.read_factory_settings(use_empty=True)
    assert bpy.ops.wm.xbone_fbx_import(filepath=fbx) == {'FINISHED'}
    arm, = (obj.data for obj in bpy.context.scene.objects if obj.type == 'ARMATURE')
    assert arm.bones["Branch.L"].parent.name == "shoulder_L0_shoulder_jnt"
    assert arm.bones["Segment.L"].parent.name == "Branch.L"
    assert arm.bones["Tip.L"].parent.name == "Segment.L"
    assert arm.bones["Antenna.C"].parent.name == "neck_C0_head_jnt"
    assert arm.bones["Cuff.L"].parent.name == "arm_L0_end_jnt"
    assert all(not b.name.startswith(("Display.", "MCH.", "FK.")) for b in arm.bones)
print("Custom shoulder, head and wrist attachments, live FK links and ring orientations passed")
