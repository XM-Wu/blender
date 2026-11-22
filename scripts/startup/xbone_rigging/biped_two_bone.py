# SPDX-License-Identifier: GPL-2.0-or-later
"""Shifter's analytic two-bone triangle expressed as native pose constraints and drivers."""
from mathutils import Matrix, Vector
import bpy
import math

from .limb_parameters import drive
from . import xbiped_limb


def build(rig, guide):
    arm = rig.data
    bpy.ops.object.mode_set(mode='EDIT')
    edit = arm.edit_bones
    mechanism = arm.collections["Mechanism"]

    def helper(name, frame, parent):
        b = edit.new("MCH.Triangle." + name)
        b.head = frame.translation
        b.tail = b.head + Vector((0, .01, 0))
        b.matrix = frame
        b.parent = parent
        b.use_deform = False
        for group in list(b.collections):
            group.unassign(b)
        mechanism.assign(b)
        return b

    limbs = []
    gliding_limbs = {}
    for tip in guide.data.bones:
        comp = tip.get("ik_limb")
        if not comp or "MCH.Reach." + tip.parent.parent.name not in edit:
            continue
        root, mid, end = (edit["MCH." + b.name] for b in (tip.parent.parent, tip.parent, tip))
        goal = "IK." + tip.name
        pole = "Pole." + tip.parent.name
        axis = (end.head - root.head).normalized()
        across = (edit[pole].head - root.head)
        across = (across - axis * across.dot(axis)).normalized()
        if not across.length:
            across = axis.orthogonal().normalized()
        basis = Matrix((across, axis, across.cross(axis))).transposed().to_4x4()
        basis.translation = root.head
        if comp.startswith("arm_"):
            upstream = tip.parent.parent
            original = Matrix([upstream["biped_frame"][i:i + 4] for i in range(0, 16, 4)])
            world = Matrix.Rotation(math.pi / 2, 4, 'X') if guide["biped_display_up"] == 'Y' else Matrix.Identity(4)
            world.translation = original.translation
            base = upstream.matrix_local @ original.inverted_safe() @ world
            helper(comp + ".component", base, root.parent)
        plane = helper(comp + ".plane", basis, None)
        roll = helper(comp + ".roll", basis, plane)
        middle = helper(comp + ".mid", basis, roll)
        lower_pivot = middle
        if guide.get("xbiped") and comp in {"leg_L0", "leg_R0", "arm_L0", "arm_R0"}:
            lower_pivot = helper(comp + "." + xbiped_limb.limb(comp).lower, basis, roll)
            gliding_limbs[comp] = xbiped_limb.create_fk(edit, helper, comp, tip, across)
        terminal = helper(comp + ".tip", basis, roll)
        f = basis.copy()
        f.translation += basis.to_3x3().col[2]
        normal = helper(comp + ".normal", f, roll)
        lower_normal = helper(comp + ".lowerNormal", f, lower_pivot)
        frames = []
        for source, finish, parent in ((root, mid, plane), (mid, end, lower_pivot)):
            y = (finish.head - source.head).normalized()
            z = basis.to_3x3().col[2].normalized()
            frame = Matrix((y.cross(z), y, z)).transposed().to_4x4()
            frame.translation = source.head
            alignment = helper(source.name + ".axis", frame, parent)
            if parent == lower_pivot:
                # The middle marker has its origin at the root in rest space.
                alignment.translate(root.head - alignment.head)
            result = helper(source.name + ".result", alignment.matrix.copy(), alignment)
            # Attach in the original joint orientation, with coincident pivots.
            mat = source.matrix.copy()
            mat.translation = alignment.head
            result.matrix = mat
            frames.append((alignment.name, result.name, source.name,
                           lower_normal.name if parent == lower_pivot else normal.name))
        length = (root.head - mid.head).length + (mid.head - end.head).length
        limbs.append((comp, goal, pole, plane.name, roll.name, middle.name, terminal.name, normal.name,
                      frames, root.name, mid.name, end.name, "MCH.Reach." + tip.parent.parent.name, length))
    bpy.ops.object.mode_set(mode='POSE')
    pose = rig.pose.bones

    def constraint(owner, kind, target):
        c = pose[owner].constraints.new(kind)
        c.target, c.subtarget = rig, target
        if kind.startswith("COPY_"):
            c.owner_space = c.target_space = 'POSE'
        return c
    for comp, goal, pole, plane, roll, mid, tip, normal, frames, root, lower, end, origin_name, length in limbs:
        origin = pose[origin_name]
        target = pose[goal]
        target["ik_match_frames"] = [entry[1] for entry in frames]
        eff = target.get("foot_effector", goal)
        constraint(plane, "COPY_LOCATION", origin_name)
        reach = constraint(plane, "DAMPED_TRACK", eff)
        reach.track_axis = 'TRACK_Y'
        orient = constraint(plane, "LOCKED_TRACK", pole)
        orient.lock_axis, orient.track_axis = 'LOCK_Y', 'TRACK_X'
        pose[roll].rotation_mode = 'XYZ'
        sign = 1 if comp.endswith("R0") else -1
        drive(pose[roll], "rotation_euler", f"radians(v)*{sign}", {"v": (target, "roll")}, 1)

        def calc(name, expr, **variables):
            origin[name] = 0.
            drive(origin, '["' + name + '"]', expr, variables)
            return origin, name
        k = origin, "length"
        share = origin, "upper_share"
        a = calc("triangle_a", "max(0.000001,k*u)", k=k, u=share)
        b = calc("triangle_b", "max(0.000001,k*(1-u))", k=k, u=share)
        # Shifter measures the root-to-goal distance in world space and
        # scales both rest lengths by the root transform's X scale.
        dr = origin.driver_add('["distance"]').driver
        v = dr.variables.new()
        v.name = "d"
        v.type = 'LOC_DIFF'
        for t, n in zip(v.targets, (origin_name, eff)):
            t.id = rig
            t.bone_target = n
            t.transform_space = 'WORLD_SPACE'
        scale_ref = "Display." + comp + \
            "_root_ctl" if comp.startswith("leg_") else "MCH.Triangle." + comp + ".component"

        def scale_variable(driver, alias, bone):
            v = driver.variables.new()
            v.name = alias
            v.type = 'TRANSFORMS'
            t = v.targets[0]
            t.id = rig
            t.bone_target = bone
            t.transform_type = 'SCALE_X'
            t.transform_space = 'WORLD_SPACE'
        scale_variable(dr, "globalScale", scale_ref)
        v = dr.variables.new()
        v.name = "size"
        v.targets[0].id = rig
        v.targets[0].data_path = target.path_from_id() + '["ikscale"]'
        dr.expression = f"d/max(0.000001,abs(globalScale)*size*{length:.12g})"
        origin["world_factor"] = 1.
        dr = origin.driver_add('["world_factor"]').driver
        scale_variable(dr, "rootScale", scale_ref)
        scale_variable(dr, "objectScale", "")
        dr.expression = "abs(rootScale)/max(0.000001,abs(objectScale))"
        a = calc("scaled_a", "a*g", a=a, g=(origin, "world_factor"))
        b = calc("scaled_b", "b*g", b=b, g=(origin, "world_factor"))
        d = calc("triangle_d", "distance*scale*g", distance=(origin, "distance"),
                 scale=(target, "ikscale"), g=(origin, "world_factor"))
        if comp in gliding_limbs:
            reach, along = xbiped_limb.solve(rig, target, comp, gliding_limbs[comp], calc, a, b, d, length)
        else:
            reach = calc("triangle_reach", "d if abs(a-b)+0.000001<d<a+b else a+b", a=a, b=b, d=d)
            along = calc("triangle_along", "(a*a+d*d-b*b)/(2*d) if abs(a-b)+0.000001<d<a+b else a", a=a, b=b, d=d)
        drive(pose[mid], "location", f"along*{length:.12g}", {"along": along}, 1)
        drive(pose[mid], "location", f"sqrt(max(0,a*a-along*along))*{length:.12g}*(-1 if rev>0.5 else 1)",
              {"a": a, "along": along, "rev": (target, "reverse")}, 0)
        drive(pose[tip], "location", f"reach*{length:.12g}", {"reach": reach}, 1)
        for (alignment, result, source, normal_target), end_target in zip(frames, (mid, tip)):
            aim = constraint(alignment, "DAMPED_TRACK", end_target)
            aim.track_axis = 'TRACK_Y'
            norm = constraint(alignment, "LOCKED_TRACK", normal_target)
            norm.lock_axis, norm.track_axis = 'LOCK_Y', 'TRACK_Z'
            constraint(source, "COPY_TRANSFORMS", result)
        constraint(end, "COPY_LOCATION", tip)
