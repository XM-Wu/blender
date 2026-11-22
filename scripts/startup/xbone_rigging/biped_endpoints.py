# SPDX-License-Identifier: GPL-2.0-or-later
"""Separate Shifter effector, twist and toe frames, including nonuniform scale."""
import json
import math
from pathlib import Path
import bpy
from mathutils import Matrix, Vector
from .biped_skeleton import read_frame
from .affine import LocalFrames
from .limb_parameters import rotation_input


def build(rig, guide):
    arm = rig.data
    with Path(__file__).with_name("biped_pivots.json").open() as f:
        data = json.load(f)
    basis = Matrix.Rotation(math.pi / 2, 3, 'X') if guide["biped_display_up"] == 'Y' else Matrix.Identity(3)
    bpy.ops.object.mode_set(mode='EDIT')
    edit = arm.edit_bones
    nodes = []
    local = LocalFrames(arm)
    arm_spaces = []
    fk_frames = []
    blend_chains = []
    wrist_blends = []
    lower_axes = []

    def helper(label, frame, parent=None):
        b = edit.new("MCH.End." + label)
        b.head = frame.translation
        b.tail = b.head + Vector((0, .01, 0))
        b.matrix = frame
        b.parent = parent
        b.use_deform = False
        for coll in list(b.collections):
            coll.unassign(b)
        arm.collections["Mechanism"].assign(b)
        return b
    for guide_tip in guide.data.bones:
        comp = guide_tip.get("ik_limb", "")
        if comp not in {"arm_L0", "arm_R0", "leg_L0", "leg_R0"}:
            continue
        leg = comp.startswith("leg")
        tip = guide_tip.name
        ik = edit["IK." + tip]
        original = Matrix([guide_tip["biped_frame"][i:i + 4] for i in range(0, 16, 4)])
        corrected = guide_tip.matrix_local @ original.inverted_safe()
        def frame(s): return corrected @ read_frame(data[comp + "_" + s], guide["biped_display_scale"], basis)
        root_joint = edit[guide_tip.parent.parent.name]
        parent = root_joint.parent
        component_root = helper(comp + ".component", frame("root"), parent)
        eff = frame("eff_loc")
        goal = edit["MCH.foot_" + comp.split("_")[1] + ".effector"] if leg else edit["Display." + comp + "_ikRot_ctl"]
        unit_frame = helper(comp + ".poseSpace", Matrix.Identity(4))
        ikref = helper(comp + ".rotation", eff, goal)
        ik_rotation = local.rotation(comp + ".IK", ikref, component_root, scale=leg)
        fk_ref = helper(comp + ".fkInput", eff, edit["FK." + tip])
        fk_eff = local.rotation(comp + ".FK", fk_ref, component_root, scale=True)
        fk_frame = helper(comp + ".fkFrame", eff)
        fk_frames.append((fk_frame.name, fk_eff.name))
        fk_result = helper(comp + ".fkJoint", edit[tip].matrix.copy(), fk_frame)
        global_eff = helper(comp + ".solved", eff)
        # Preserve the shear produced when an oriented child inherits scale.
        # Converting the scale channels in compatibility-bone axes loses this.
        result = helper(comp + ".joint", edit[tip].matrix.copy(), global_eff)
        # Shifter blends each joint in its own chain's parent space. Reading
        # FK in pose space under a partially blended parent double-counts
        # that parent's motion at intermediate blend values.
        parent = root_joint.parent
        fk_sources = []
        for b in (guide_tip.parent.parent, guide_tip.parent, guide_tip):
            source = helper(comp + ".fkLocal." + b.name, edit[b.name].matrix.copy(), parent)
            parent = source
            fk_sources.append(source.name)
        ik_end = helper(comp + ".ikLocalEnd", edit[tip].matrix.copy(), edit["MCH." + guide_tip.parent.name])
        blend_chains.append((tip, fk_sources, ik_end.name, result.name))
        if leg:
            scale_ref = ik_rotation.name
        else:
            scale_ref = local.rotation(comp + ".sourceScale", ik, component_root, scale=True).name
            ik_rotation = helper(comp + ".scaleLayers", ik_rotation.matrix.copy(), ik_rotation)
            npo = helper(comp + ".ikRotNpo", frame("ikRot_npo"))
            orient = helper(comp + ".forearmAxes", frame("ikRot_npo"), edit[guide_tip.parent.name])
            ik_goal = helper(comp + ".ikGoalRef", edit[tip].matrix.copy(), ik)
            basic_ik = helper(comp + ".basicIK", edit[tip].matrix.copy(), edit["MCH." + guide_tip.parent.name])
            basic = helper(comp + ".basicEnd", edit[tip].matrix.copy(), edit[guide_tip.parent.name])
            wrist_blends.append((tip, fk_sources[-1], ik_goal.name, basic_ik.name,
                                basic.name, global_eff.name, result.name))
            arm_goal = helper(comp + ".ikScale", frame("ik_ref"), ik)
            scaled = local.rotation(comp + ".ikScale", arm_goal, unit_frame, scale=True)
            unit = helper(comp + ".ikRotUnit", frame("ikRot_npo"))
            ref = helper(comp + ".ikRotOffset", frame("ikRot_ctl"), unit)
            # Maya parentConstraint extracts the parent-inverse rotation
            # separately. Decomposing inverse(parent) * target instead
            # produces a different rotation under nonuniform scale.
            maya_world = helper(comp + ".worldAxes", basis.to_4x4())
            local.rotation(comp + ".inverseParent", maya_world, npo)
            relative = helper(comp + ".localWrist", Matrix.Identity(4))
            solved = helper(comp + ".ikRotOffsetResult", npo.matrix.copy(), npo)
            actual = helper(comp + ".ikRotAuto", frame("ikRot_ctl"))
            arm_spaces.append((tip, npo.name, orient.name, scaled.name, unit.name, solved.name, actual.name,
                               ref.name, relative.name, "MCH.QR." + comp + ".inverseParent", maya_world.name))
        leg_data = None
        if leg:
            # The backward pivots feed the IK goal; the FK chain originates at
            # the solved ankle twist transform and cancels backward rotations.
            input_eff = helper(comp + ".effector", eff, edit[tip])
            loc_frame = frame("tws2_loc")
            rot_frame = frame("tws2_rot")
            fk_lower = edit["FK." + guide_tip.parent.name]
            f = fk_lower.matrix.copy()
            fk_axes = helper(comp + ".fkAxes", f)
            axes = helper(comp + ".lowerAxes", f)
            ik_axes = helper(comp + ".ikAxes", f, edit["MCH." + guide_tip.parent.name])
            f.translation += f.to_3x3().col[1]
            up = helper(comp + ".fkUp", f, fk_lower)
            lower_axes.append((tip, fk_lower.name, fk_axes.name, up.name, ik_axes.name, axes.name,
                               'TRACK_NEGATIVE_X' if comp.endswith('R0') else 'TRACK_X'))
            lower_ref = helper(comp + ".lowerRef", loc_frame, axes)
            parent = edit["Display." + comp + "_root_ctl"]
            lower = local.rotation(comp + ".localLower", lower_ref, parent)
            scaled_loc = helper(comp + ".lowerScale", lower.matrix.copy(), lower)
            loc = helper(comp + ".lower", loc_frame)
            local.rotation(comp + ".localEffScale", input_eff, parent, scale=True)
            unit = helper(comp + ".unscaled", loc_frame)
            aim = helper(comp + ".footAxes", rot_frame, input_eff)
            read = local.rotation(comp + ".ankleOrient", aim, loc)
            rotated = helper(comp + ".twist", rot_frame)
            edit["MCH.Bind." + comp + "_end_jnt"].parent = rotated
            foot = "foot_" + comp.split("_")[1]
            space0 = edit["MCH." + foot + ".toeSpace0"]
            space1 = edit["MCH." + foot + ".toeSpace1"]
            space0.parent = rotated
            fk0 = next(b for b in guide.data.bones if b.get("biped_locator") == foot + "_0_loc").name
            fk1 = next(b for b in guide.data.bones if b.get("biped_locator") == foot + "_1_loc").name
            space1.parent = edit["FK." + fk0]
            edit["FK." + fk1].parent = space1
            leg_data = (
                input_eff.name,
                loc.name,
                scaled_loc.name,
                "MCH.Scale." +
                comp +
                ".localEffScale",
                unit.name,
                aim.name,
                read.name,
                rotated.name,
                foot)
        nodes.append((tip, ik_rotation.name, global_eff.name, result.name, scale_ref, leg_data, fk_result.name))
    bpy.ops.object.mode_set(mode='POSE')
    pose = rig.pose.bones

    def con(n, kind, ref):
        c = pose[n].constraints.new(kind)
        c.target, c.subtarget = rig, ref
        c.owner_space = c.target_space = 'POSE'
        return c
    local.configure(rig)
    for tip, lower, fk_axes, up, ik_axes, axes, track in lower_axes:
        con(fk_axes, 'COPY_LOCATION', lower)
        c = con(fk_axes, 'DAMPED_TRACK', "FK." + tip)
        c.track_axis = track
        c = con(fk_axes, 'LOCKED_TRACK', up)
        c.lock_axis, c.track_axis = 'LOCK_X', 'TRACK_Y'
        con(axes, 'COPY_TRANSFORMS', fk_axes)
        c = con(axes, 'COPY_TRANSFORMS', ik_axes)
        dr = c.driver_add("influence").driver
        dr.type = 'AVERAGE'
        v = dr.variables.new()
        v.targets[0].id = rig
        v.targets[0].data_path = '["' + pose["IK." + tip]["blend_property"] + '"]'
    for frame, source in fk_frames:
        con(frame, 'COPY_TRANSFORMS', source)
    for tip, npo, orient, scale, unit, solved, actual, ref, relative, inverse, maya_world in arm_spaces:
        con(npo, 'COPY_LOCATION', pose["IK." + tip]["solver_chain"][-1])
        con(npo, 'COPY_ROTATION', orient)
        con(npo, 'COPY_SCALE', scale)
        con(unit, 'COPY_ROTATION', orient)
        con(unit, 'COPY_LOCATION', pose["IK." + tip]["solver_chain"][-1])
        c = con(relative, 'COPY_ROTATION', ref)
        c.target_space = 'CUSTOM'
        c.space_object, c.space_subtarget = rig, maya_world
        c = con(relative, 'COPY_ROTATION', inverse)
        c.mix_mode = 'BEFORE'
        c = con(solved, 'COPY_ROTATION', relative)
        c.owner_space = 'LOCAL'
        con(actual, 'COPY_TRANSFORMS', solved)
        auto = next(c for c in pose["MCH.WristSpace." + tip].constraints if c.type == 'CHILD_OF')
        auto.subtarget = actual
        auto.inverse_matrix = arm.bones[actual].matrix_local.inverted_safe()
    for tip, rot, eff, result, scale, leg, fk_result in nodes:
        ctl = pose["IK." + tip]
        if not leg:
            c = con(rot, 'COPY_SCALE', scale)
            c.target_space = c.owner_space = 'LOCAL'
        con(eff, 'COPY_TRANSFORMS', rot)
        con(eff, 'COPY_LOCATION', ctl["solver_chain"][-1])
        for c in list(pose[tip].constraints)[1:]:
            c.driver_remove("influence")
            pose[tip].constraints.remove(c)
        pose[tip].constraints[0].subtarget = fk_result
        pose[tip].constraints[0].owner_space = pose[tip].constraints[0].target_space = 'POSE'
        c = con(tip, 'COPY_TRANSFORMS', result)
        dr = c.driver_add("influence").driver
        dr.type = 'AVERAGE'
        v = dr.variables.new()
        v.targets[0].id = rig
        v.targets[0].data_path = '["' + ctl["blend_property"] + '"]'
        if not leg:
            continue
        input_eff, loc, scaled_loc, scale_ref, unit, aim, read, rotated, foot = leg
        con(scaled_loc, 'COPY_LOCATION', input_eff)
        c = con(scaled_loc, 'COPY_SCALE', scale_ref)
        c.target_space = c.owner_space = 'LOCAL'
        con(loc, 'COPY_TRANSFORMS', scaled_loc)
        con(unit, 'COPY_LOCATION', loc)
        con(unit, 'COPY_ROTATION', loc)
        con(rotated, 'COPY_TRANSFORMS', read)
        for i in range(2):
            owner = pose["MCH." + foot + ".toeSpace" + str(i)]
            for c in list(owner.constraints):
                c.driver_remove("influence")
                owner.constraints.remove(c)
            owner.rotation_mode = 'ZYX'
            backward = pose["Display." + foot + f"_bk{2 - i}_ctl"]
            backward.rotation_mode = 'XYZ'
            base = pose["MCH." + foot + f".bk{2 - i}"]
            for axis in range(3):
                dr = owner.driver_add("rotation_euler", axis).driver
                for alias, p in (('bk', backward), ('base', base)):
                    rotation_input(dr, alias, p, "XYZ"[axis])
                v = dr.variables.new()
                v.name = "blend"
                v.targets[0].id = rig
                v.targets[0].data_path = '["' + ctl["blend_property"] + '"]'
                dr.expression = "-(bk+base)*blend"
    for tip, sources, ik_end, result in blend_chains:
        for name, source in zip(pose["IK." + tip]["joints"], sources):
            follow = pose[name].constraints[0]
            con(source, 'COPY_TRANSFORMS', follow.subtarget)
            follow.subtarget = source
            follow.owner_space = follow.target_space = 'LOCAL'
        con(ik_end, 'COPY_TRANSFORMS', result)
        blend = pose[tip].constraints[-1]
        blend.subtarget = ik_end
        blend.owner_space = blend.target_space = 'LOCAL'
    for tip, fk, ik_goal, basic_ik, basic, eff, result in wrist_blends:
        ctl = pose["IK." + tip]
        con(basic_ik, 'COPY_TRANSFORMS', ik_goal)
        con(basic_ik, 'COPY_LOCATION', ctl["solver_chain"][-1])
        c = con(basic, 'COPY_TRANSFORMS', fk)
        c.target_space = c.owner_space = 'LOCAL'
        c = con(basic, 'COPY_TRANSFORMS', basic_ik)
        c.target_space = c.owner_space = 'LOCAL'
        dr = c.driver_add("influence").driver
        dr.type = 'AVERAGE'
        v = dr.variables.new()
        v.targets[0].id = rig
        v.targets[0].data_path = '["' + ctl["blend_property"] + '"]'
        for owner in (eff, "MCH.WristSpace." + tip):
            for c in pose[owner].constraints:
                if c.type == 'COPY_LOCATION':
                    c.subtarget = basic
        first, last = pose[tip].constraints[0], pose[tip].constraints[-1]
        first.subtarget, last.subtarget = basic, result
        first.owner_space = first.target_space = last.owner_space = last.target_space = 'POSE'
