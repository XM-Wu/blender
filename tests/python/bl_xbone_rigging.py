# SPDX-License-Identifier: GPL-2.0-or-later

import json
import math
import tempfile
from pathlib import Path

import bpy
import xbone_rigging
from mathutils import Matrix, Quaternion, Vector


def check_maya_poses(rig):
    # Evaluated jnt_org transforms from Maya 2027, mGear a7dd129.
    cases = json.loads(Path(__file__).with_name("xbone_biped_poses.json").read_text())
    basis = Quaternion((1, 0, 0), math.pi/2)
    for case in cases:
        ctrl = rig.pose.bones[case["control"]]
        saved = ctrl.matrix_basis.copy()
        m = ctrl.matrix.copy()
        axis = Vector((1, 0, 0) if case["axis"] == "X" else (0, 0, 1))
        axis = basis @ Quaternion(case["rotation"]) @ axis
        delta = Quaternion(axis, math.radians(case["angle"]))
        ctrl.matrix = Matrix.LocRotScale(m.translation, delta @ m.to_quaternion(), m.to_scale())
        bpy.context.view_layer.update()
        for name, frame in case["joints"].items():
            m = rig.pose.bones[name].matrix
            assert (m.translation - basis @ Vector(frame[:3])*0.1).length < 0.0001, (case["control"], name)
            rot = basis @ Quaternion(frame[3:])
            assert abs(rot.dot(m.to_quaternion())) > math.cos(math.radians(0.15)/2), (case["control"], name)
        ctrl.matrix_basis = saved
        bpy.context.view_layer.update()


def check_combined_poses(rig):
    """Maya 2027 compound poses independent of the single-channel audit."""
    cases = json.loads(Path(__file__).with_name("xbone_biped_combinations.json").read_text())
    mapping = json.loads(Path(xbone_rigging.__file__).with_name("biped_display.json").read_text())["controls"]
    basis = Matrix.Rotation(math.pi/2, 4, 'X')
    def converted(values):
        matrix = Matrix([values[i:i+4] for i in range(0, 16, 4)])
        matrix.translation *= 0.1
        return basis @ matrix @ basis.transposed()
    for case in cases:
        initial = {b.name: rig.pose.bones[b.name].matrix.copy() for b in rig.data.bones if b.use_deform}
        saved = []
        for name, key, value, *delta in case["inputs"]:
            record = mapping[name]
            pb = rig.pose.bones["Display."+name if record["role"] == "display" else record["role"]+"."+record["anchor"]]
            if delta:
                saved.append((pb, None, pb.matrix_basis.copy()))
                pose = converted(delta[0]) @ pb.matrix
                pose.translation = pb.matrix.translation + (basis.to_3x3() @ Vector(delta[1]))*0.1
                pb.matrix = pose
            else:
                owner = pb
                if pb.get("limb_control"):
                    owner = rig.pose.bones[pb["limb_control"]]
                    key = key.split("_", 1)[1]
                    if key == "blend":
                        owner, key = rig, owner["blend_property"]
                    elif key == "ikRotRef":
                        key = "rotation_space"
                saved.append((owner, key, owner[key]))
                owner[key] = type(owner[key])(value)
                rig.update_tag()
            bpy.context.view_layer.update()
        for name, rest in initial.items():
            expected = converted(case["changes"][name]) @ rest if name in case["changes"] else rest
            actual = rig.pose.bones[name].matrix
            assert (expected.translation-actual.translation).length < 0.001, (case["pose"], name)
            assert abs(expected.to_quaternion().dot(actual.to_quaternion())) > math.cos(math.radians(.5)/2), (case["pose"], name)
            assert max(abs(a-b) for a,b in zip(expected.to_scale(),actual.to_scale())) < .01, (case["pose"], name)
        for owner, key, value in reversed(saved):
            if key is None:
                owner.matrix_basis = value
            else:
                owner[key] = value
                rig.update_tag()
            bpy.context.view_layer.update()


def check_bind_hierarchy(rig):
    reference = json.loads(Path(xbone_rigging.__file__).with_name("biped_bind.json").read_text())
    deform = {b.name: b for b in rig.data.bones if b.use_deform}
    assert len(deform) == 106 and deform.keys() == reference.keys()
    assert set(rig.data.collections["Skeleton"].bones.keys()) == deform.keys()
    basis = Quaternion((1, 0, 0), math.pi/2)
    for name, (parent, frame) in reference.items():
        bone = deform[name]
        assert (bone.parent.name if bone.parent else None) == parent, name
        assert (bone.head_local - (basis @ Vector(frame[:3])) * 0.1).length < 0.00001, name
        assert abs(bone.matrix_local.to_quaternion().dot(basis @ Quaternion(frame[3:]))) > 0.99999, name
        assert (rig.pose.bones[name].head - bone.head_local).length < 0.001, name
    for component, mid, tip, controller in (
            ("arm_L0", "elbow", "wrist", "FK."), ("arm_R0", "elbow", "wrist", "FK."),
            ("leg_L0", "knee", "ankle", "IK."), ("leg_R0", "knee", "ankle", "IK.")):
        end = component + "_" + tip
        ctl = rig.pose.bones[controller + end]
        original = ctl.matrix_basis.copy()
        axis = (rig.pose.bones[end].head - rig.pose.bones[component+"_"+mid].head).normalized()
        matrix = ctl.matrix.copy()
        rotation = Quaternion(axis, math.pi/4) @ matrix.to_quaternion()
        ctl.matrix = Matrix.LocRotScale(matrix.translation, rotation, matrix.to_scale())
        bpy.context.view_layer.update()
        for i, t in ((5, 0.02), (6, 1/3), (7, 2/3), (8, 0.98)):
            name = f"{component}_{i}_jnt"
            rest = deform[name].matrix_local.to_quaternion()
            delta = rig.pose.bones[name].matrix.to_quaternion() @ rest.inverted()
            assert abs(delta.dot(Quaternion(axis, t*math.pi/4))) > 0.9999, (name, delta[:])
        ctl.matrix_basis = original
        bpy.context.view_layer.update()

    ctl = rig.pose.bones["IK.leg_L0_ankle"]
    root, knee, ankle = (rig.pose.bones[n] for n in ctl["joints"])
    saved = ctl.matrix_basis.copy()
    length = (knee.head-root.head).length + (ankle.head-knee.head).length
    direction = (ankle.head-root.head).normalized()
    m = ctl.matrix.copy()
    m.translation = root.head + direction * length * 1.25
    ctl.matrix = m
    bpy.context.view_layer.update()
    assert (ankle.head-ctl.head).length < 0.003, (ankle.head[:], ctl.head[:])
    ctl["maxstretch"] = 1.0
    rig.update_tag()
    bpy.context.view_layer.update()
    assert abs((ankle.head-root.head).length-length) < 0.003
    ctl["maxstretch"] = 1.5
    ctl.matrix_basis = saved
    rig.update_tag()
    bpy.context.view_layer.update()
    bent = ctl.matrix.copy()
    bent.translation = root.head.lerp(ankle.head, 0.65)
    ctl.matrix = bent
    bpy.context.view_layer.update()
    segment = rig.pose.bones["leg_L0_2_jnt"]
    before = segment.head.copy()
    ankle_position = ankle.head.copy()
    ctl["roundness"] = 0.4
    rig.update_tag()
    bpy.context.view_layer.update()
    moved = (segment.head - before).length
    assert 0.001 < moved < length*0.25, moved
    assert (ankle_position-ankle.head).length < 0.00001
    assert (rig.pose.bones["leg_L0_4_jnt"].head-knee.head).length < 0.001
    ctl["roundness"] = 0.0
    ctl.matrix_basis = saved
    rig.update_tag()
    bpy.context.view_layer.update()

    # The reverse pivots travel with the goal. Skin toes stay attached to
    # the solved ankle, including when the goal lies beyond maximum stretch.
    for side in ("L0", "R0"):
        goal = rig.pose.bones["IK.leg_" + side + "_ankle"]
        hip, mid, ankle = (rig.pose.bones[n] for n in goal["joints"])
        toes = [rig.pose.bones[f"foot_{side}_{i}_jnt"] for i in range(2)]
        offsets = [b.head - ankle.head for b in toes]
        initial, limit = goal.matrix_basis.copy(), goal["maxstretch"]
        rest_length = (mid.head-hip.head).length + (ankle.head-mid.head).length
        for maximum in (1.0, 1.5):
            goal["maxstretch"] = maximum
            rig.update_tag()
            for axis in ((0, 0, -1), (1 if side == "L0" else -1, 0, -0.5)):
                m = goal.matrix.copy()
                m.translation = hip.head + Vector(axis).normalized() * rest_length * 2.2
                goal.matrix = m
                bpy.context.view_layer.update()
                assert (ankle.head-goal.head).length > rest_length * 0.65
                for b, offset in zip(toes, offsets):
                    assert (b.head-ankle.head-offset).length < 0.001, b.name
        goal["maxstretch"], goal.matrix_basis = limit, initial
        rig.update_tag()
        bpy.context.view_layer.update()

    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory)/"biped.fbx")
        assert bpy.ops.export_scene.fbx(filepath=path, use_selection=True, use_armature_deform_only=True,
                                        add_leaf_bones=False, bake_anim=False) == {'FINISHED'}
        bpy.ops.object.mode_set(mode='OBJECT')
        assert bpy.ops.wm.xbone_fbx_import(filepath=path) == {'FINISHED'}
        other, = [o for o in bpy.context.selected_objects if o.type == 'ARMATURE' and o != rig]
        assert set(other.data.bones.keys()) == deform.keys()
        for bone in other.data.bones:
            original = deform[bone.name]
            assert (bone.parent.name if bone.parent else None) == reference[bone.name][0]
            assert (bone.head_local-original.head_local).length < 0.001
        bpy.data.objects.remove(other, do_unlink=True)
        rig.select_set(True)
        bpy.context.view_layer.objects.active = rig
        bpy.ops.object.mode_set(mode='POSE')


def check_biped_display(rig):
    source = json.loads(Path(xbone_rigging.__file__).with_name("biped_display.json").read_text())
    assert source["source"].endswith("Maya 2027")
    assert len(source["controls"]) == 125
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
    display = rig.data.collections["Biped Display"]
    expected = {"Display." + n for n, record in source["controls"].items() if record["role"] == "display"}
    expected.update("Display." + component + ".pole" for component in source["poles"])
    assert set(display.bones.keys()) == expected
    pole = rig.pose.bones["Pole.leg_L0_knee"]
    old = pole.location.copy()
    pole.location.x += 0.13
    bpy.context.view_layer.update()
    line = rig.pose.bones["Display.leg_L0.pole"]
    assert (line.head - pole.head).length < 0.0001
    assert (line.tail - rig.pose.bones["leg_L0_knee"].head).length < 0.0001
    pole.location = old
    bpy.context.view_layer.update()


def check_rebuild(rig, guide):
    mesh = bpy.data.meshes.new("Rebuild skin")
    mesh.from_pydata([(0.2, 0, 1)], [], [])
    skin = bpy.data.objects.new(mesh.name, mesh)
    bpy.context.collection.objects.link(skin)
    skin.parent = rig
    skin.modifiers.new("Skin", 'ARMATURE').object = rig
    skin.vertex_groups.new(name="arm_L0_1_jnt").add([0], 0.75, 'REPLACE')
    skin.vertex_groups.new(name="spine_C0_0_jnt").add([0], 0.25, 'REPLACE')
    rig.pose.bones["FK.body_C0"].keyframe_insert(data_path="location", frame=1)
    anim = rig.animation_data
    action, slot = anim.action, anim.action_slot
    track = anim.nla_tracks.new()
    track.name = "User track"
    track.strips.new("User strip", 20, action)
    rig.pose.bones["IK.leg_L0_ankle"]["maxstretch"] = 1.25
    constraint = rig.pose.bones["FK.body_C0"].constraints.new('LIMIT_LOCATION')
    constraint.name = "Animator limit"
    original = rig.as_pointer()
    for divisions in (3, 3):
        assert bpy.ops.xbone.edit_source_guide() == {'FINISHED'}
        guide.data.edit_bones["arm_L0"]["div0"] = divisions
        assert bpy.ops.xbone.build_rig() == {'FINISHED'}
        assert bpy.context.object.as_pointer() == original
        assert rig.animation_data.action == action and rig.animation_data.action_slot == slot
        assert rig.animation_data.nla_tracks[track.name].strips[0].action == action
        assert rig.pose.bones["IK.leg_L0_ankle"]["maxstretch"] == 1.25
        assert "Animator limit" in rig.pose.bones["FK.body_C0"].constraints
        assert skin.parent == rig and skin.modifiers["Skin"].object == rig
        assert abs(skin.vertex_groups["arm_L0_1_jnt"].weight(0) - 0.5) < 1e-5
        assert abs(skin.vertex_groups["arm_L0_2_jnt"].weight(0) - 0.25) < 1e-5
        assert abs(skin.vertex_groups["spine_C0_0_jnt"].weight(0) - 0.25) < 1e-5
        assert len({d for d in rig.children if d.type == 'CURVE'}) == 4
        assert len({d for d in rig.children if d.get("xbone_solver_mesh")}) == 8
        assert all(d.driver.is_valid for d in rig.animation_data.drivers)
        assert all(mod.object == rig for child in rig.children if child.type == 'CURVE' for mod in child.modifiers)
        for child in rig.children:
            if not (child.get("xbone_solver_mesh") or child.get("xbone_solver_curve")):
                continue
            for mod in child.modifiers:
                if mod.type == 'HOOK':
                    rest = rig.data.bones[mod.subtarget].matrix_local
                    assert mod.object == rig
                    assert max(abs(a-b) for row, ref in zip(mod.matrix_inverse, rest.inverted())
                               for a,b in zip(row,ref)) < 0.0001, (child.name, mod.subtarget)
        check_ik_drag(rig, "leg_L0_ankle", "foot_L0")


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
    assert not any("sizeRef" in n for n in widgets)
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
            if p.get("limb_control"):
                assert p["limb_control"] in pose and all(p.lock_location)
                continue
            if p.name in {"Display.faceUI_C0_ctl","Display.spineUI_C0_ctl"}:
                assert any(k.startswith(("mouth_", "neck_")) for k in p.keys())
                continue
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
            elif p.name.startswith("Display.") and name.endswith("_mid_ctl"):
                translation = True
                affected = [name.removesuffix("_mid_ctl") + "_4_jnt"]
            elif name.endswith(("_ikRot_ctl", "_ikcns_ctl")):
                translation = name.endswith("_ikcns_ctl")
                affected = [name.rsplit("_",1)[0].rsplit("_",1)[0]+"_end_jnt"]
            elif p.name.startswith("Display.foot_"):
                affected = ["leg_"+name.split("_")[1]+"_end_jnt"]
            elif p.name.startswith("Display.spine_C0_"):
                translation = "fk" not in name
                affected = ["spine_C0_3_jnt"]
            elif p.name.startswith("Display."):
                if name.startswith("eye_"):
                    affected = [name.replace("_Over_ctl", "_eyeOver_jnt").replace("_fk_ctl", "_eye_jnt")]
                elif name.startswith("mouth_"):
                    affected = [name.replace("_ctl", "_jnt")]
                elif name.startswith("neck_"):
                    translation = True
                    affected = ["neck_C0_head_jnt"]
                elif name.startswith("leg_"):
                    translation = True
                    affected = [name.replace("_root_ctl", "_0_jnt")]
                else:
                    assert name == "world_ctl"
                    affected = ["spine_C0_0_jnt"]
            elif name.startswith("meta_") and name.count("_") == 1:
                affected = [name+"_3_jnt"]
            elif name == "mouth_C0_jaw":
                translation = True
                affected = ["mouth_C0_jaw_jnt"]
            elif rig["rigging_guide"].data.bones[name].use_deform:
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
                    expected = p.matrix @ p.bone.matrix_local.inverted_safe() @ pose[j].bone.matrix_local
                    assert abs(after.to_quaternion().dot(expected.to_quaternion())) > 0.999, p.name
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
    orientation_before = pose[ankle].matrix.to_quaternion()
    control.matrix = dest @ Matrix.Rotation(0.3, 4, 'X')
    bpy.context.view_layer.update()
    if "wrist_rotation" in control:
        assert abs(pose[ankle].matrix.to_quaternion().dot(orientation_before)) > 0.99999
        wrist = pose[control["wrist_rotation"]]
        saved = wrist.matrix_basis.copy()
        wrist.matrix_basis = saved @ Matrix.Rotation(.3,4,'X')
        bpy.context.view_layer.update()
        assert abs(pose[ankle].matrix.to_quaternion().dot(orientation_before)) < .995
        wrist.matrix_basis = saved
    else:
        expected = control.matrix @ control.bone.matrix_local.inverted_safe() @ pose[ankle].bone.matrix_local
        assert abs(pose[ankle].matrix.to_quaternion().dot(expected.to_quaternion())) > 0.999, ankle
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
assert guide["show_shifter_guide"] and guide["shifter_guide_paths"]
assert len(guide["shifter_guide_aux"]) == 210
for side, target in (("L", "R"), ("R", "L")):
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.armature.select_all(action='DESELECT')
    edit = guide.data.edit_bones
    a, b = (edit["arm_" + s + "0_wrist"] for s in (side, target))
    old_a, old_b = a.matrix.copy(), b.matrix.copy()
    a.translate(Vector((0.04, 0.12, -0.02)))
    a.roll += 0.38
    a.select = True
    assert bpy.ops.xbone.symmetrize_guide(side=side, selected_only=True) == {'FINISHED'}
    reflection = Matrix.Diagonal((-1, 1, 1, 1))
    expected = reflection @ a.matrix @ reflection
    assert max(abs(x-y) for row, ref in zip(b.matrix, expected) for x,y in zip(row, ref)) < 1e-5
    a.matrix, b.matrix = old_a, old_b
    bpy.ops.object.mode_set(mode='OBJECT')
data = json.loads(path.read_text())
assert len(guide.data.bones) == sum(len(c["pos"]) for c in data["components_dict"].values())
assert guide.data.bones["shoulder_L0"].parent.name == "spine_C0_eff"
assert bpy.ops.xbone.build_rig() == {'FINISHED'}
rig = bpy.context.object
assert sum(key.startswith("IK ") for key in rig.keys()) == 4
check_biped_display(rig)
check_bind_hierarchy(rig)
check_maya_poses(rig)
check_combined_poses(rig)
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
check_rebuild(rig, guide)
bpy.ops.object.mode_set(mode='OBJECT')
with tempfile.TemporaryDirectory() as tmp:
    arm_settings = data["components_dict"]["arm_L0"]["param_values"]
    arm_settings.update(div0=1, div1=4, supportJoints=False)
    data["components_dict"]["spine_C0"]["param_values"]["division"] = 7
    modified = Path(tmp)/"divisions.sgt"
    modified.write_text(json.dumps(data))
    assert bpy.ops.xbone.import_guide(filepath=str(modified)) == {'FINISHED'}
    assert bpy.ops.xbone.build_rig() == {'FINISHED'}
    arm = bpy.context.object.data
    skin = {b.name for b in arm.bones if b.use_deform}
    assert len(skin) == 107 and "arm_L0_8_jnt" not in skin
    assert arm.bones["arm_L0_end_jnt"].parent.name == "arm_L0_7_jnt"
    assert arm.bones["neck_C0_0_jnt"].parent.name == "spine_C0_6_jnt"
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
    assert bpy.ops.xbone.toggle_guide_display() == {'FINISHED'}
    assert not guide["show_shifter_guide"]
    assert bpy.ops.xbone.toggle_guide_display() == {'FINISHED'}
    assert guide["show_shifter_guide"]
    bpy.ops.armature.select_all(action='DESELECT')
    edit = guide.data.edit_bones
    edit["Hand.L"].select = True
    assert bpy.ops.xbone.symmetrize_guide(side='L', selected_only=True) == {'FINISHED'}
    assert edit["Hand.R"].parent == edit["Forearm.R"]
    assert edit["Forearm.R"].parent.name == bpy.utils.flip_name(edit["Forearm.L"].parent.name)
    assert abs(edit["Hand.R"].head.x + edit["Hand.L"].head.x) < 1e-6
print("Shifter templates, custom and imported guides, FK/IK, matching, reload and FBX passed")
