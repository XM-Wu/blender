# SPDX-License-Identifier: GPL-2.0-or-later
"""Connect Biped component controls after the solver and skin hierarchy exist."""
import bpy


def connect(rig, guide):
    arm = rig.data
    names = {b["biped_locator"]: b.name for b in guide.data.bones if "biped_locator" in b}
    mechanism = arm.collections["Mechanism"]
    bpy.ops.object.mode_set(mode='EDIT')
    edit = arm.edit_bones

    def ctl(name, parent):
        bone = edit["Display." + name]
        bone.parent = parent
        bone.hide_select = False
        return bone

    world = ctl("world_ctl", None)
    for name in (names["global_C0"], "FK." + names["global_C0"]):
        edit[name].parent = world
    neck = names["neck_C0_neck"]
    neck_ik = ctl("neck_C0_ik_ctl", edit[names["neck_C0"]])
    edit["FK." + neck].parent = neck_ik
    for suffix in ("teethup", "teethlow"):
        tooth = edit["Display.mouth_C0_" + suffix + "_ctl"]
        tooth.hide_select = False
        edit["MCH.Bind.mouth_C0_" + suffix + "_jnt"].parent = tooth

    spaces = []
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
            host = edit[f"Display.{component}UI_{side}_ctl"]
            host.hide_select = False
            host["limb_control"] = "IK." + tip
            for key, bone in (("ik_references", edit["IK." + tip]),
                              ("pole_references", edit["Pole." + names[f"{component}_{side}_{mid}"]])):
                refs = [n for n in guide.data.bones[tip].get(key, ()) if n in edit]
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
    bpy.ops.object.mode_set(mode='POSE')
    pose = rig.pose.bones
    pose[neck].constraints[0].target_space = pose[neck].constraints[0].owner_space = 'POSE'
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
