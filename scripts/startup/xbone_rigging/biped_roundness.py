# SPDX-License-Identifier: GPL-2.0-or-later
"""Arc-length sampled Bézier spans for Shifter's roundness control."""
import bpy
from mathutils import Vector

from .biped_skeleton import limb_samples
from .limb_parameters import drive


def build(rig, guide):
    arm = rig.data
    unit = guide["biped_display_scale"]
    mechanics = arm.collections["Mechanism"]
    bpy.ops.object.mode_set(mode='EDIT')
    edit = arm.edit_bones

    def helper(name, parent, point, direction):
        b = edit.new("MCH.Curve." + name)
        b.head = point
        b.tail = point + direction * unit
        b.parent = parent
        b.use_deform = False
        for group in list(b.collections):
            group.unassign(b)
        mechanics.assign(b)
        return b.name

    spans = []
    for tip in guide.data.bones:
        comp = tip.get("ik_limb")
        if not comp or not tip.parent or "Display." + comp + "_mid_ctl" not in edit:
            continue
        root = tip.parent.parent
        anchors = [edit[root.name], edit["Display." + comp + "_mid_ctl"], edit[tip.name]]
        axes = [(b.head - a.head).normalized() for a, b in zip(anchors, anchors[1:])]
        directions = [axes[0], (axes[0] + axes[1]).normalized(), axes[1]]
        samples = limb_samples(root.get("div0", 2), root.get("div1", 2), root.get("supportJoints", True))
        for half in (0, 1):
            start, end = anchors[half:half + 2]
            start_dir, end_dir = directions[half:half + 2]
            epsilon = unit * 0.0025
            a, b = start.head.copy(), end.head.copy()
            ha, hb = a + start_dir * epsilon, b - end_dir * epsilon
            h0 = helper(comp + str(half) + ".a", start, ha, start_dir)
            h1 = helper(comp + str(half) + ".b", end, hb, end_dir)
            probes = []
            for i, u in enumerate(samples):
                if (u >= .5) != bool(half):
                    continue
                f = u * 2 - half
                joint = comp + "_" + str(i) + "_jnt"
                track = 'TRACK_X' if edit[joint].matrix.to_3x3().col[0].dot(b - a) > 0 else 'TRACK_NEGATIVE_X'
                loc = helper(joint + ".sample", None, Vector((0, 0, 0)), Vector((0, 1, 0)))
                tangent = helper(joint + ".tangent", None, Vector((0, 0, 0)), Vector((0, 1, 0)))
                probes.append((joint, loc, tangent, f, track))
            spans.append((tip.name, half, (a, ha, hb, b), (start.name, h0, h1, end.name), probes))
    bpy.ops.object.mode_set(mode='POSE')
    pose = rig.pose.bones
    for tip, half, points, hooks, probes in spans:
        ctl = pose["IK." + tip]
        if not half:
            ctl["roundness"] = 0.0
            ctl.id_properties_ui("roundness").update(min=0.0, soft_max=2.0, default=0.0)
        curve = bpy.data.curves.new("RollSpline." + tip + "." + str(half), 'CURVE')
        curve.dimensions = '3D'
        curve.use_path = True
        curve.resolution_u = 44 if tip.startswith("leg") else 39
        spline = curve.splines.new('BEZIER')
        spline.bezier_points.add(1)
        p0, p1 = spline.bezier_points
        for p in (p0, p1):
            p.handle_left_type = p.handle_right_type = 'FREE'
        a, ha, hb, b = points
        p0.co, p0.handle_left, p0.handle_right = a, a, ha
        p1.co, p1.handle_left, p1.handle_right = b, hb, b
        path = bpy.data.objects.new(curve.name, curve)
        rig.users_collection[0].objects.link(path)
        path.parent = rig
        path.hide_render = path.hide_select = True
        path.hide_viewport = True
        for joint, indices in zip(hooks, ((0, 1), (2,), (3,), (4, 5))):
            mod = path.modifiers.new(joint, 'HOOK')
            mod.object, mod.subtarget = rig, joint
            mod.vertex_indices_set(indices)
            mod.matrix_inverse = arm.bones[joint].matrix_local.inverted_safe()
        handle = pose[hooks[1 if half else 2]]
        drive(handle, "location", f"r*{unit * 2.5 * (1 if half else -1):.12g}", {"r": (ctl, "roundness")}, 1)
        for joint, loc, tangent, f, track in probes:
            for name, u in ((loc, f), (tangent, min(1.0, f + 0.0005))):
                follow = pose[name].constraints.new('FOLLOW_PATH')
                follow.target = path
                follow.use_fixed_location = True
                follow.offset_factor = u
            position = pose[joint].constraints.new('COPY_LOCATION')
            position.target, position.subtarget = rig, loc
            position.owner_space = position.target_space = 'POSE'
            aim = pose[joint].constraints.new('DAMPED_TRACK')
            aim.target, aim.subtarget, aim.track_axis = rig, tangent, track
            for con in (position, aim):
                drive(con, "influence", "min(1,1000*r)", {"r": (ctl, "roundness")})
