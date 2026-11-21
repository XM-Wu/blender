# SPDX-License-Identifier: GPL-2.0-or-later
"""Connect Biped component controls after the solver and skin hierarchy exist."""
import bpy
from mathutils import Matrix
from .affine import LocalFrames


def connect(rig, guide):
    arm = rig.data
    names = {b["biped_locator"]: b.name for b in guide.data.bones if "biped_locator" in b}
    mechanism = arm.collections["Mechanism"]
    bpy.ops.object.mode_set(mode='EDIT')
    edit = arm.edit_bones
    local_frames = LocalFrames(arm)

    def ctl(name, parent):
        bone = edit["Display." + name]
        bone.parent = parent
        bone.hide_select = False
        return bone

    world = ctl("world_ctl", None)
    for name in (names["global_C0"], "FK." + names["global_C0"]):
        edit[name].parent = world
    neck = names["neck_C0_neck"]
    edit["Display.neck_C0_ik_ctl"].hide_select = False
    for suffix in ("teethup", "teethlow"):
        tooth = edit["Display.mouth_C0_" + suffix + "_ctl"]
        tooth.hide_select = False
        edit["MCH.Bind.mouth_C0_" + suffix + "_jnt"].parent = tooth

    spaces = []
    bend_centers = []
    aimed_spaces = []
    position_locks = []
    scale_spaces = []

    def insert_space(bone, prop, references, host, rotation_only=False):
        def neutral(prefix):
            base = edit.new(prefix + bone.name)
            base.head, base.tail, base.roll = bone.head, bone.tail, bone.roll
            base.use_deform = False
            for group in list(base.collections):
                group.unassign(base)
            mechanism.assign(base)
            return base
        base = neutral("MCH.Ref.")
        if rotation_only:
            loc = neutral("MCH.Pin.")
            loc.parent = bone.parent
            position_locks.append((base.name, loc.name))
        bone.parent = base
        spaces.append((host, base.name, prop, references))

    for host in ("faceUI_C0", "spineUI_C0"):
        edit["Display." + host + "_ctl"].hide_select = False
    for label, references, host, prop, rot_only in (
            ("FK." + names["eyeslook_C0"], ["neck_C0_head", "local_C0", "body_C0", "spine_C0_eff"],
             "faceUI_C0", "control_ikref", False),
            ("Display.neck_C0_ik_ctl", ["spine_C0_eff", "body_C0", "local_C0", "global_C0"],
             "spineUI_C0", "neck_ikref", True),
            ("FK." + names["neck_C0_head"], ["neck_C0_neck", "spine_C0_eff", "body_C0", "local_C0", "global_C0"],
             "spineUI_C0", "neck_headref", True)):
        refs = [names[n] for n in references]
        if prop == "neck_headref":
            refs[0] = edit[label].parent.name
        refs = ["MCH.Spine.Chest" if ref == names["spine_C0_eff"] else ref for ref in refs]
        insert_space(edit[label], prop, refs, "Display." + host + "_ctl", rot_only)
    ik_parents = []
    wrists = []
    for side in ('L0', 'R0'):
        eye = edit[names["eye_" + side]]
        over = ctl("eye_" + side + "_Over_ctl", eye.parent)
        eye.parent = over
        local = ctl("eye_" + side + "_fk_ctl", eye)
        edit["MCH.Bind.eye_" + side + "_eyeOver_jnt"].parent = over
        edit["MCH.Bind.eye_" + side + "_eye_jnt"].parent = local

        root = names["leg_" + side]
        control = ctl("leg_" + side + "_root_ctl", edit[root].parent)
        for name in (root, "FK." + root, "MCH." + root, "MCH.Reach." + root):
            edit[name].parent = control

        for component, mid, end in (("arm", "elbow", "wrist"), ("leg", "knee", "ankle")):
            tip = names[f"{component}_{side}_{end}"]
            ik = edit["IK." + tip]
            offset = ctl(f"{component}_{side}_ikcns_ctl", ik.parent)
            ik.parent = offset
            ik_parents.append((ik.name, offset.name))
            if component == "arm":
                wrist = edit[f"Display.arm_{side}_ikRot_ctl"]
                wrist.hide_select = False
                base = edit.new("MCH.WristSpace." + tip)
                base.head, base.tail, base.roll = wrist.head, wrist.tail, wrist.roll
                wrist.parent = base
                result = edit.new("MCH.WristResult." + tip)
                result.head, result.tail, result.roll = edit[tip].head, edit[tip].tail, edit[tip].roll
                result.parent = wrist
                for helper in (base, result):
                    helper.use_deform = False
                    for group in list(helper.collections):
                        group.unassign(helper)
                    mechanism.assign(helper)
                refs = ["MCH." + names[f"arm_{side}_elbow"], ik.name]
                refs.extend(n for n in guide.data.bones[tip].get("ik_references", ()) if n in edit)
                spaces.append((ik.name, base.name, "rotation_references", refs))
                wrists.append((ik.name, tip, wrist.name, base.name, result.name))
            host = edit[f"Display.{component}UI_{side}_ctl"]
            host.hide_select = False
            host["limb_control"] = "IK." + tip
            sources = [n for n in guide.data.bones[tip].get("ik_references", ()) if n in edit]
            mid_control = edit[f"Display.{component}_{side}_mid_ctl"]
            root_name = names[component + "_" + side]
            mid_name = names[component + "_" + side + "_" + mid]

            def frame_ref(label, frame, parent):
                b = edit.new("MCH.Center." + label + "." + tip)
                b.head, b.tail, b.roll = frame.head, frame.tail, frame.roll
                b.parent = parent
                b.use_deform = False
                for coll in list(b.collections):
                    coll.unassign(b)
                mechanism.assign(b)
                return b
            first = frame_ref("A", edit["FK." + root_name], edit[root_name])
            second = frame_ref("B", edit["FK." + mid_name], edit[mid_name])
            local_frames.rotation("CenterDelta." + tip, second, first)
            half = frame_ref("half", first, first)
            center = frame_ref("result", mid_control, None)
            # Mirrored middle controls can have a different rest orientation.
            center.matrix = first.matrix.lerp(second.matrix, .5)
            center.translate(mid_control.head - center.head)
            identity = frame_ref("identity", first, None)
            identity.matrix = Matrix.Identity(4)
            pure = local_frames.rotation("CenterWorld." + tip, half, identity)
            unit_center = frame_ref("centerUnit", center, None)
            root_parent = edit[root_name].parent
            inherited = local_frames.rotation("CenterParent." + tip, unit_center, root_parent)
            bend_centers.append(("MCH.QR.CenterDelta." + tip, half.name, pure.name, unit_center.name,
                                 center.name, inherited.name, mid_name))
            insert_space(mid_control, "pin_references", [center.name, *sources], ik.name)
            if component == "arm":
                shoulder = edit["FK." + names["shoulder_" + side + "_tip"]]
                refs = [
                    shoulder.parent.name,
                    names["local_C0"],
                    names["body_C0"],
                    "MCH.Spine.Chest",
                    names["global_C0"]]
                insert_space(shoulder, "shoulder_rotRef", refs, host.name, True)
            for key, bone in (("ik_references", offset),
                              ("pole_references", edit["Pole." + names[f"{component}_{side}_{mid}"]])):
                refs = [n for n in guide.data.bones[tip].get(key, ()) if n in edit]
                if key == "pole_references":
                    root = edit[names[component + "_" + side]]
                    auto = edit.new("MCH.PoleAuto." + tip)
                    auto.head, auto.tail = root.head, edit[tip].head
                    auto.parent = root.parent.parent if component == "leg" else root.parent
                    auto.use_deform = False
                    for group in list(auto.collections):
                        group.unassign(auto)
                    mechanism.assign(auto)
                    aimed_spaces.append((auto.name, ik.name))
                    refs = [auto.name, *([ik.name] if component == "leg" else []), *refs]
                if len(refs) < 2:
                    continue
                base = edit.new("MCH.Space." + bone.name)
                base.head, base.tail, base.roll = bone.head, bone.tail, bone.roll
                base.use_deform = False
                for group in list(base.collections):
                    group.unassign(base)
                mechanism.assign(base)
                bone.parent = base
                spaces.append(("IK." + tip, base.name, key, refs))
                # Maya's parentConstraint changes translation and orientation.
                # Scale still comes from the original DAG parent.
                scale_ref = edit.new("MCH.SpaceScale." + bone.name)
                scale_ref.head, scale_ref.tail, scale_ref.roll = base.head, base.tail, base.roll
                if component == "leg":
                    scale_ref.parent = edit["Display.leg_" + side + "_root_ctl"] if key == "ik_references" else ik
                else:
                    scale_ref.parent = edit[names["arm_" + side]].parent
                scale_ref.use_deform = False
                for group in list(scale_ref.collections):
                    group.unassign(scale_ref)
                mechanism.assign(scale_ref)
                scale_spaces.append((base.name, scale_ref.name))
    bpy.ops.object.mode_set(mode='POSE')
    pose = rig.pose.bones
    local_frames.configure(rig)
    for relative, half, pure, unit_center, center, inherited, mid_name in bend_centers:
        for owner, kind, source in ((unit_center, 'COPY_ROTATION', pure), (unit_center, 'COPY_LOCATION', mid_name),
                                    (center, 'COPY_TRANSFORMS', inherited)):
            c = pose[owner].constraints.new(kind)
            c.target, c.subtarget = rig, source
            c.target_space = c.owner_space = 'POSE'
        pose[half].rotation_mode = 'XYZ'
        for i, axis in enumerate('XYZ'):
            dr = pose[half].driver_add("rotation_euler", i).driver
            v = dr.variables.new()
            v.name = "angle"
            v.type = 'TRANSFORMS'
            t = v.targets[0]
            t.id = rig
            t.bone_target = relative
            t.transform_space = 'LOCAL_SPACE'
            t.transform_type = 'ROT_' + axis
            t.rotation_mode = 'XYZ'
            dr.expression = "angle*0.5"
    pose[neck].constraints[0].target_space = pose[neck].constraints[0].owner_space = 'POSE'
    for name in [names["neck_C0_head"], *(names["shoulder_" + s + "_tip"] for s in ('L0', 'R0'))]:
        pose[name].constraints[0].target_space = pose[name].constraints[0].owner_space = 'POSE'
    for auto, goal in aimed_spaces:
        aim = pose[auto].constraints.new('DAMPED_TRACK')
        aim.target, aim.subtarget, aim.track_axis = rig, goal, 'TRACK_Y'
    for host in pose:
        if host.bone.get("limb_control"):
            host["limb_control"] = host.bone["limb_control"]
            host.lock_location = host.lock_rotation = host.lock_scale = (True, True, True)
    for control, base, key, refs in spaces:
        target = pose[control]
        target[key] = refs
        prop = key.replace("_references", "_space")
        target[prop] = 0
        target.id_properties_ui(prop).update(min=0, max=len(refs) - 1,
                                             description="Reference space: " + ", ".join(refs))
        for index, reference in enumerate(refs):
            con = pose[base].constraints.new('CHILD_OF')
            con.target, con.subtarget = rig, reference
            con.owner_space = con.target_space = 'POSE'
            con.inverse_matrix = arm.bones[reference].matrix_local.inverted_safe()
            driver = con.driver_add("influence").driver
            var = driver.variables.new()
            var.name = "space"
            var.targets[0].id = rig
            var.targets[0].data_path = target.path_from_id() + '["' + prop + '"]'
            driver.expression = f"space == {index}"
    for base, pin in position_locks:
        con = pose[base].constraints.new('COPY_LOCATION')
        con.target, con.subtarget = rig, pin
        con.target_space = con.owner_space = 'POSE'
    for base, reference in scale_spaces:
        con = pose[base].constraints.new('COPY_SCALE')
        con.target, con.subtarget = rig, reference
        con.target_space = con.owner_space = 'POSE'
    for control, offset in ik_parents:
        driver = pose[offset].driver_add("hide").driver
        driver.type = 'AVERAGE'
        var = driver.variables.new()
        var.targets[0].id = rig
        var.targets[0].data_path = pose[control].path_from_id("hide")
    for control, tip, wrist, base, result in wrists:
        target = pose[control]
        target["wrist_rotation"] = wrist
        target["wrist_result"] = result
        pb = pose[wrist]
        pb.lock_location = pb.lock_scale = (True, True, True)
        driver = pb.driver_add("hide").driver
        driver.type = 'AVERAGE'
        var = driver.variables.new()
        var.targets[0].id = rig
        var.targets[0].data_path = target.path_from_id("hide")
        loc = pose[base].constraints.new('COPY_LOCATION')
        loc.target, loc.subtarget = rig, target["solver_chain"][-1]
        loc.target_space = loc.owner_space = 'POSE'
