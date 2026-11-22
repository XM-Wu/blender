# SPDX-License-Identifier: GPL-2.0-or-later
"""Shifter foot_bk_01 backward pivots and two forward toe controls."""
import bpy
from .limb_parameters import rotation_input


def build(rig, guide):
    arm = rig.data
    names = {b["biped_locator"]: b.name for b in guide.data.bones if "biped_locator" in b}
    mech = arm.collections["Mechanism"]
    bpy.ops.object.mode_set(mode='EDIT')
    edit = arm.edit_bones

    def pivot(name, source, parent, position=None):
        bone = edit.new("MCH." + name)
        bone.head, bone.tail, bone.roll = source.head, source.tail, source.roll
        if position is not None:
            bone.translate(position - bone.head)
        bone.parent = parent
        bone.use_deform = False
        for group in list(bone.collections):
            group.unassign(bone)
        mech.assign(bone)
        return bone

    feet = []
    for side in ('L0', 'R0'):
        foot = "foot_" + side
        ankle = names["leg_" + side + "_ankle"]
        ik = edit["IK." + ankle]
        controls = {k: edit["Display." + foot + "_" + k + "_ctl"] for k in ("roll", "heel", "tip", "bk0", "bk1", "bk2")}
        roll = controls["roll"]
        roll.parent = ik
        parent = ik
        spaces = {}
        for key, locator in (("bankIn", "inpivot"), ("bankOut", "outpivot")):
            parent = pivot(foot + "." + key, roll, parent, edit[names[foot + "_" + locator]].head)
            spaces[key] = parent.name
        for key in ("heel", "tip", "bk0", "bk1", "bk2"):
            ctl = controls[key]
            base = pivot(foot + "." + key, ctl, parent)
            spaces[key] = base.name
            ctl.parent = base
            parent = ctl
        effector = pivot(foot + ".effector", edit[ankle], parent)
        for i in range(2):
            fk = edit["FK." + names[foot + f"_{i}_loc"]]
            space = pivot(foot + f".toeSpace{i}", fk, fk.parent)
            fk.parent = space
        for ctl in controls.values():
            ctl.hide_select = False
        feet.append((ankle, guide.data.bones[names[foot]].get("rollAngle", -20),
                     {k: v.name for k, v in controls.items()}, spaces, effector.name))
    bpy.ops.object.mode_set(mode='POSE')
    pose = rig.pose.bones
    for ankle, angle, controls, spaces, effector in feet:
        ik = pose["IK." + ankle]
        ik["foot_effector"] = effector
        roll = pose[controls["roll"]]
        roll.rotation_mode = 'XYZ'
        for key, name in controls.items():
            ctl = pose[name]
            ctl.lock_location = ctl.lock_scale = (True, True, True)
            if key == "roll":
                ctl.lock_rotation = (False, True, False)
            driver = ctl.driver_add("hide").driver
            driver.type = 'AVERAGE'
            var = driver.variables.new()
            var.targets[0].id = rig
            var.targets[0].data_path = ik.path_from_id("hide")
        for i in (0, 1):
            roll["angle_" + str(i)] = angle
            roll.id_properties_ui("angle_" + str(i)).update(min=-180, max=0, description="Toe roll threshold (degrees)")
        # The backward chain peels from the ball to the tip as negative roll increases.
        for key, axis, expr in (
                ("bankIn", 0, "max(bank,0)"), ("bankOut", 0, "min(bank,0)"), ("heel", 2, "max(roll,0)"),
                ("bk2", 2, "min(0,max(radians(a),roll))"),
                ("bk1", 2, "min(0,max(radians(b),roll-radians(a)))"),
                ("bk0", 2, "min(0,roll-radians(a+b))")):
            owner = pose[spaces[key]]
            owner.rotation_mode = 'XYZ'
            driver = owner.driver_add("rotation_euler", axis).driver
            for alias, path in (("bank", "rotation_euler[0]"), ("roll", "rotation_euler[2]"),
                                ("a", '["angle_0"]'), ("b", '["angle_1"]')):
                if alias in {"bank", "roll"}:
                    rotation_input(driver, alias, roll, 'X' if alias == "bank" else 'Z')
                    continue
                var = driver.variables.new()
                var.name = alias
                var.targets[0].id = rig
                var.targets[0].data_path = roll.path_from_id() + ("" if path.startswith("[") else ".") + path
            driver.expression = expr
