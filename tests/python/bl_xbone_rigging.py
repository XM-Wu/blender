# SPDX-License-Identifier: GPL-2.0-or-later

import json
import tempfile
from pathlib import Path

import bpy
import xbone_rigging
from mathutils import Matrix, Vector


def check_biped_display(rig):
    source = json.loads(Path(xbone_rigging.__file__).with_name("biped_display.json").read_text())
    assert source["source"].endswith("Maya 2027")
    assert len(source["controls"]) == 123 and len(source["joints"]) == 106
    offsets = {}
    for name, record in source["controls"].items():
        role, joint = record["role"], record["anchor"]
        owner = rig.pose.bones["Display." + name if role == "display" else role + "." + joint]
        mesh = owner.custom_shape.data
        start = offsets.get(owner.name, 0)
        for path in record["lines"]:
            for x, y, z in path:
                actual = owner.bone.matrix_local @ mesh.vertices[start].co
                assert (actual - Vector((x,-z,y))*0.1).length < 0.00001, (name, start)
                start += 1
        offsets[owner.name] = start
    for name, count in offsets.items():
        assert count == len(rig.pose.bones[name].custom_shape.data.vertices)
    for joint in source["joints"]:
        assert not rig.data.bones["Display." + joint].use_deform
    pole = rig.pose.bones["Pole.leg_L0_knee"]
    old = pole.location.copy()
    pole.location.x += 0.13
    bpy.context.view_layer.update()
    line = rig.pose.bones["Display.leg_L0.pole"]
    assert (line.head - pole.head).length < 0.0001
    assert (line.tail - rig.pose.bones["leg_L0_knee"].head).length < 0.0001
    pole.location = old
    bpy.context.view_layer.update()


def check_biped_controls(rig):
    """Exercise every selectable widget in both endpoint modes via its pose transform."""
    pose = rig.pose.bones
    layer = bpy.context.view_layer
    switches = {p["blend_property"]: rig[p["blend_property"]] for p in pose if "blend_property" in p}
    widgets = {p.name for p in pose if p.custom_shape and not p.bone.hide_select}
    extra = rig.data.collections["Additional FK"]
    assert not extra.is_visible
    extra.is_visible = True
    visited = set()
    assert not any("sizeRef" in n or "UI_" in n for n in widgets)
    for mode in (0.0, 1.0):
        for key in switches:
            rig[key] = mode
        rig.update_tag()
        layer.update()
        for target in (p for p in pose if "blend_property" in p):
            assert target.hide == (mode == 0)
            assert pose[target["pole_control"]].hide == target.hide
            assert all(pose[n].hide != target.hide for n in target["fk_controls"])
        for p in pose:
            if p.name not in widgets or p.hide:
                continue
            assert any(c.is_visible_effectively for c in p.bone.collections), p.name
            visited.add(p.name)
            name = p.name.split(".", 1)[1]
            translation = False
            if p.name.startswith("IK."):
                assert mode == 1
                check_ik_drag(rig, name)
                continue
            if p.name.startswith("Pole."):
                assert mode == 1
                # Bend the limb before checking pole motion away from a straight rest pose.
                target, = [t for t in pose if t.get("pole_control") == p.name]
                original_target = target.matrix_basis.copy()
                m = target.matrix.copy()
                m.translation = m.translation.lerp(pose[target["joints"][0]].head, 0.2)
                target.matrix = m
                layer.update()
                translation = True
                affected = [name]
            elif name.startswith("eye") and (name.endswith("_look") or name == "eyeslook_C0"):
                affected = ["eye_L0", "eye_R0"] if name == "eyeslook_C0" else [name.removesuffix("_look")]
                translation = True
            elif pose[name].bone.use_deform:
                affected = [name]
            elif name.startswith("thumbRoll_"):
                affected = [name.replace("thumbRoll_", "thumb_")]
            else:
                assert name in {"global_C0", "local_C0", "body_C0"}, name
                affected = ["spine_C0", "neck_C0"]

            original = p.matrix_basis.copy()
            before = [pose[j].matrix.copy() for j in affected]
            delta = Matrix.Translation((0.12, 0.09, 0.06)) if translation else Matrix.Rotation(0.3, 4, 'Z')
            p.matrix_basis = original @ delta
            layer.update()
            for j, prev in zip(affected, before):
                after = pose[j].matrix
                assert max(abs(a-b) for x, y in zip(prev, after) for a, b in zip(x, y)) > 0.0001, (p.name, j)
                if translation and j.startswith("eye_"):
                    direction = (pose["FK." + j + "_look"].head - pose[j].head).normalized()
                    assert after.to_3x3().col[1].normalized().dot(direction) > 0.999, (p.name, j)
                elif not translation and name == j:
                    assert abs(after.to_quaternion().dot(p.matrix.to_quaternion())) > 0.999, p.name
            p.matrix_basis = original
            if p.name.startswith("Pole."):
                target.matrix_basis = original_target
            layer.update()
    assert visited == widgets, widgets - visited
    extra.is_visible = False
    for key, default in switches.items():
        rig[key] = default
    rig.update_tag()
    layer.update()
    print(f"Biped: {len(visited)} distinct controllers exercised in FK and IK modes")


def check_ik_drag(rig, ankle, foot=None):
    pose = rig.pose.bones
    control = pose["IK." + ankle]
    knee = pose[ankle].parent
    old_knee = knee.head.copy()
    old_ankle = pose[ankle].head.copy()
    old_control = control.matrix_basis.copy()
    old_foot = pose[foot].head.copy() if foot else None
    dest = control.matrix.copy()
    dest.translation = old_ankle.lerp(knee.parent.head, 0.15) + Vector((0, -0.04, 0))
    control.matrix = dest
    bpy.context.view_layer.update()
    assert (pose[ankle].head - dest.translation).length < 0.005, ankle
    assert (knee.head - old_knee).length > 0.01
    if foot:
        assert ((pose[foot].head - old_foot) - (dest.translation - old_ankle)).length < 0.005
    control.matrix = dest @ Matrix.Rotation(0.3, 4, 'X')
    bpy.context.view_layer.update()
    assert abs(pose[ankle].matrix.to_quaternion().dot(control.matrix.to_quaternion())) > 0.999, ankle
    control.matrix_basis = old_control
    bpy.context.view_layer.update()


def check_pose(rig, elbow, wrist):
    pose = rig.pose.bones
    tip = pose[wrist]
    key = "IK " + tip.bone["ik_limb"]
    rig[key] = 0.0
    rig.update_tag()
    bpy.context.view_layer.update()
    fk = pose["FK." + elbow]
    fk.rotation_mode = 'XYZ'
    fk.rotation_euler.z += 0.35
    bpy.context.view_layer.update()
    assert (tip.head - rig.data.bones[wrist].head_local).length > 0.01
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
        assert bpy.context.active_pose_bone.name == (control.name if mode == 'IK' else "FK." + wrist)
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
check_biped_display(rig)
check_biped_controls(rig)
body = rig.pose.bones["FK.body_C0"]
feet = [rig.pose.bones["leg_" + side + "_ankle"] for side in ("L0", "R0")]
planted = [foot.head.copy() for foot in feet]
body.location.z -= 0.08
bpy.context.view_layer.update()
assert all((foot.head - old).length < 0.005 for foot, old in zip(feet, planted))
body.location.z += 0.08
bpy.context.view_layer.update()
for side in ("L0", "R0"):
    check_ik_drag(rig, "leg_" + side + "_ankle", "foot_" + side)
check_pose(rig, "arm_L0_elbow", "arm_L0_wrist")
check_pose(rig, "leg_R0_knee", "leg_R0_ankle")
bpy.ops.object.mode_set(mode='OBJECT')
assert bpy.ops.xbone.biped_guide(template='game_biped') == {'FINISHED'}
assert bpy.ops.xbone.build_rig() == {'FINISHED'}
assert bpy.context.object.pose.bones["FK.arm_L0"].custom_shape.type == 'CURVE'
for side in ("L0", "R0"):
    check_ik_drag(bpy.context.object, "leg_" + side + "_ankle", "foot_" + side)
check_pose(bpy.context.object, "arm_R0_elbow", "arm_R0_wrist")
bpy.ops.object.mode_set(mode='OBJECT')
assert bpy.ops.xbone.new_guide() == {'FINISHED'}
assert bpy.ops.xbone.add_component(kind='LEG') == {'FINISHED'}
assert bpy.ops.xbone.build_rig() == {'FINISHED'}
check_ik_drag(bpy.context.object, "Foot.L")
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
