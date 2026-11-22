# SPDX-License-Identifier: GPL-2.0-or-later
"""Arc-length sampled Bézier spans for Shifter's roundness control."""
import bpy
import json
import math
from pathlib import Path
from mathutils import Matrix, Vector

from .biped_skeleton import limb_samples, read_frame
from .limb_parameters import drive
from .affine import LocalFrames


def build(rig, guide, bind_plan):
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
    roll_frames = []
    relative_frames = LocalFrames(arm)
    volume_outputs = []
    mid_reads = []
    root_aims = []
    glide_offsets = []
    with Path(__file__).with_name("biped_pivots.json").open() as stream:
        references = json.load(stream)
    basis = Matrix.Rotation(math.pi / 2, 3, 'X') if guide["biped_display_up"] == 'Y' else Matrix.Identity(3)

    def aligned(name, parent, frame):
        bone = edit[helper(name, parent, frame.translation, frame.to_3x3().col[1].normalized())]
        bone.matrix = frame
        return bone
    for tip in guide.data.bones:
        comp = tip.get("ik_limb")
        if not comp or not tip.parent or "Display." + comp + "_mid_ctl" not in edit:
            continue
        root = tip.parent.parent
        anchors = [edit[root.name], edit["Display." + comp + "_mid_ctl"], edit[tip.name]]
        root_rest = Matrix([root["biped_frame"][k:k + 4] for k in range(0, 16, 4)])
        reference_frame = root.matrix_local @ root_rest.inverted_safe(
        ) @ read_frame(references[comp + "_root"], unit, basis)
        volume_parent = edit.get(
            "Display." +
            comp +
            "_root_ctl") or aligned(
            comp +
            ".rootSpace",
            anchors[0].parent,
            reference_frame)
        component_root = aligned(
            comp + ".rootScale",
            volume_parent.parent if comp.startswith("leg") else anchors[0].parent,
            reference_frame)
        axes = [(b.head - a.head).normalized() for a, b in zip(anchors, anchors[1:])]
        anchors[1].hide_select = False
        upstream = anchors[0].parent.parent
        if comp.startswith("arm"):
            shoulder = next(b for b in guide.data.bones if b.get("biped_locator") == "shoulder_" + comp.split("_")[1])
            upstream = edit["FK." + shoulder.name]
        aim = edit[helper(comp + ".rootAim", upstream, anchors[0].head, axes[0])]
        leg = comp.startswith("leg")
        aim_target = tip.parent.name
        gliding = leg and guide.get("xbiped")
        if gliding:
            joint = edit[aim_target]
            femur = aligned(comp + ".femoralPivot", joint, joint.matrix.copy())
            delta = "MCH.Triangle." + comp + ".glideDelta"
            glide_offsets.append((femur.name, delta))
            aim_target = femur.name
        root_aims.append((aim.name, root.name, aim_target))
        parents = []
        rotations = []
        # rollSplineKine uses the parent frames for its interpolated up vector,
        # but the child rotations for the Bézier tangents and the local X roll.
        for i, guide_joint in enumerate((root, tip.parent, tip)):
            original = Matrix([guide_joint["biped_frame"][k:k + 4] for k in range(0, 16, 4)])
            correction = guide_joint.matrix_local @ original.inverted_safe()
            f = correction @ read_frame(references[comp + f"_tws{i}_loc"], unit, basis)
            p = (aim if i == 0 else
                 (edit["MCH.Center.result." + tip.name] if leg else anchors[1]) if i == 1 else
                 (edit["MCH.End." + comp + ".lower"] if leg else anchors[2]))
            par = aligned(comp + f".rollParent{i}", p, f)
            parents.append(par.name)
            f = correction @ read_frame(references[comp + f"_tws{i}_rot"], unit, basis)
            rot = aligned(comp + f".rollInput{i}", anchors[i] if i else par, f)
            rotations.append(rot.name)
            if leg and i in (1, 2):
                probe = aligned(comp + f".rollChannel{i}", par, par.matrix.copy())
                mid_reads.append((probe.name, rot.name))
        samples = limb_samples(root.get("div0", 2), root.get("div1", 2), root.get("supportJoints", True))
        for half in (0, 1):
            start, end = anchors[half:half + 2]
            end_rotation = edit[rotations[half + 1]]
            if half == 0 and gliding:
                end = aligned(comp + ".femoralEnd", end, end.matrix.copy())
                end_rotation = aligned(comp + ".femoralTangent", end_rotation, end_rotation.matrix.copy())
                glide_offsets.extend(((end.name, delta), (end_rotation.name, delta)))
            direction_sign = -1 if comp.endswith("R0") else 1
            start_dir, end_dir = (edit[n].matrix.to_3x3().col[0].normalized() *
                                  direction_sign for n in rotations[half:half + 2])
            epsilon = unit * 0.0025
            a, b = start.head.copy(), end.head.copy()
            ha, hb = a + start_dir * epsilon, b - end_dir * epsilon
            h0 = helper(comp + str(half) + ".a", edit[rotations[half]], ha, start_dir)
            h1 = helper(comp + str(half) + ".b", end_rotation, hb, end_dir)
            probes = []
            for i, u in enumerate(samples):
                if (u >= .5) != bool(half):
                    continue
                f = u * 2 - half
                joint = comp + "_" + str(i) + "_jnt"
                track = 'TRACK_X' if edit[joint].matrix.to_3x3().col[0].dot(b - a) > 0 else 'TRACK_NEGATIVE_X'
                loc = helper(joint + ".sample", None, Vector((0, 0, 0)), Vector((0, 1, 0)))
                tangent = helper(joint + ".tangent", None, Vector((0, 0, 0)), Vector((0, 1, 0)))
                probes.append((joint, loc, tangent, f, i in (0, len(samples) - 1) or u in (.49, .5, .51)))
                rest = edit[joint].matrix.copy()
                x = (start_dir if f == 0 else (b - a).normalized()) * direction_sign
                q = edit[parents[half]].matrix.to_quaternion().slerp(edit[parents[half + 1]].matrix.to_quaternion(), f)
                y = q @ Vector((0, 1, 0))
                z = x.cross(y).normalized()
                y = z.cross(x).normalized()
                frame = Matrix((x, y, z)).transposed().to_4x4()
                frame.translation = rest.translation
                orient = aligned(joint + ".axes", None, frame)
                rolled = aligned(joint + ".roll", orient, frame)
                result = aligned(joint + ".output", rolled, rest)
                parent_frame = relative_frames.rotation(joint, result, volume_parent)
                skin = aligned(joint + ".skin", parent_frame, parent_frame.matrix.copy())
                volume_outputs.append(
                    (joint,
                     skin.name,
                     component_root.name,
                     comp,
                     root.name,
                     anchors[1].name,
                        tip.name))
                up = aligned(joint + ".up", None, q.to_matrix().to_4x4())
                up.head = rest.translation
                up.tail = up.head + (q @ Vector((0, 1, 0))) * unit
                upf = up.matrix.copy()
                upf.translation += q @ Vector((0, unit, 0))
                up_point = aligned(joint + ".upPoint", up, upf)
                roll_frames.append((orient.name, rolled.name, up.name, up_point.name,
                                    parents[half], parents[half + 1], half, f, comp, tip.name, loc, tangent, track))
            spans.append((tip.name, half, (a, ha, hb, b), (start.name, h0, h1, end.name),
                         probes, 45 if leg and root.get("supportJoints", True) else 40))
    # Refresh the authored bind frames on the final edit pass. Converting the
    # deep finger hierarchies to edit bones repeatedly accumulates float error.
    for name, (_, rest, *_) in bind_plan.items():
        edit[name].matrix = rest
    bpy.ops.object.mode_set(mode='POSE')
    pose = rig.pose.bones
    relative_frames.configure(rig)

    def constraint(owner, kind, target):
        c = pose[owner].constraints.new(kind)
        c.target, c.subtarget = rig, target
        c.target_space = c.owner_space = 'POSE'
        return c
    for owner, delta in glide_offsets:
        constraint(owner, 'COPY_LOCATION', delta).use_offset = True
    for probe, rot in mid_reads:
        constraint(probe, 'COPY_ROTATION', rot)
    for aim, root, mid in root_aims:
        constraint(aim, 'COPY_LOCATION', root)
        c = constraint(aim, 'DAMPED_TRACK', mid)
        c.track_axis = 'TRACK_Y'
    for tip, half, points, hooks, probes, divisions in spans:
        ctl = pose["IK." + tip]
        if not half:
            ctl["volume"] = 1.0
            ctl.id_properties_ui("volume").update(min=0, max=1, default=1.0, description="Squash/stretch volume")
            if pose[tip].bone.get("ik_limb", "").startswith("arm"):
                ctl["aproll"] = 0.0
                ctl.id_properties_ui("aproll").update(min=-360, max=360, default=0.0, description="Armpit roll")
            ctl["roundness"] = 0.0
            ctl.id_properties_ui("roundness").update(min=0.0, soft_max=2.0, default=0.0)
        from . import roll_curve
        path = roll_curve.create(rig, "RollSpline." + tip + "." + str(half), points, hooks,
                                 [(j, f, high) for j, _, _, f, high in probes], divisions, unit)
        handle = pose[hooks[1 if half else 2]]
        drive(handle, "location", f"r*{unit * 2.5 * (1 if half else -1):.12g}", {"r": (ctl, "roundness")}, 1)
        for joint, loc, tangent, _, _ in probes:
            for name, suffix in ((loc, "point"), (tangent, "tangent")):
                sample = pose[name].constraints.new('COPY_LOCATION')
                sample.target, sample.subtarget = path, joint + "." + suffix
    for orient, rolled, up, up_point, p0, p1, half, f, comp, tip, loc, tangent, track in roll_frames:
        ctl = pose["IK." + tip]
        for n in (orient, up):
            constraint(n, 'COPY_LOCATION', loc)
        constraint(up, 'COPY_ROTATION', p0)
        c = constraint(up, 'COPY_ROTATION', p1)
        c.influence = f
        c = constraint(orient, 'DAMPED_TRACK', tangent)
        c.track_axis = track
        c = constraint(orient, 'LOCKED_TRACK', up_point)
        c.lock_axis, c.track_axis = 'LOCK_X', 'TRACK_Y'
        pb = pose[rolled]
        pb.rotation_mode = 'XYZ'
        if comp.startswith("leg"):
            dr = pb.driver_add("rotation_euler", 0).driver
            terms = []
            for node, weight in ((half, 1 - f), (half + 1, f)):
                if node == 0 or weight == 0:
                    continue
                v = dr.variables.new()
                v.name = "r" + str(node)
                v.type = 'TRANSFORMS'
                t = v.targets[0]
                t.id = rig
                t.bone_target = "MCH.Curve." + comp + ".rollChannel" + str(node)
                t.transform_space = 'LOCAL_SPACE'
                t.transform_type = 'ROT_X'
                t.rotation_mode = 'XYZ'
                terms.append(f"{v.name}*{weight:.12g}")
            v = dr.variables.new()
            v.name = "magnitude"
            v.type = 'LOC_DIFF'
            for t, n in zip(v.targets, (loc, tangent)):
                t.id = rig
                t.bone_target = n
                t.transform_space = 'WORLD_SPACE'
            v = dr.variables.new()
            v.name = "objectScale"
            v.type = 'TRANSFORMS'
            t = v.targets[0]
            t.id = rig
            t.transform_space = 'WORLD_SPACE'
            t.transform_type = 'SCALE_X'
            a = "(" + ("+".join(terms) or "0") + ")*0.5"
            dr.expression = f"2*atan2(sin({a})*magnitude/max(0.000001,objectScale*{unit:.12g}),cos({a}))"
            if half and guide.get("xbiped"):
                reach = pose["MCH.Reach." + guide.data.bones[tip].parent.parent.name]
                for alias, source, key in (("flex", reach, "knee_coupling"), ("inward", ctl, "knee_internal_rotation")):
                    var = dr.variables.new()
                    var.name = alias
                    var.targets[0].id = rig
                    var.targets[0].data_path = source.path_from_id() + '["' + key + '"]'
                # Positive inward rotation is mirrored about the character's
                # center, independently of each exported joint's X convention.
                sign = (1 if track == 'TRACK_X' else -1) * (1 if comp.endswith("L0") else -1)
                dr.expression += f"+{sign}*radians(inward)*flex"
        elif half == 0:
            drive(pb, "rotation_euler", f"radians(a)*{1 - f:.12g}", {"a": (ctl, "aproll")}, 0)
    volumes = set()
    for name, skin, root_scale, comp, root, mid, tip in volume_outputs:
        ctl = pose["IK." + tip]
        owner = pose[root_scale]
        if comp not in volumes:
            volumes.add(comp)
            owner["ratio"] = 1.0
            dr = owner.driver_add('["ratio"]').driver
            for alias, ends in (("a", (root, mid)), ("b", (mid, tip))):
                v = dr.variables.new()
                v.name = alias
                v.type = 'LOC_DIFF'
                for t, n in zip(v.targets, ends):
                    t.id = rig
                    t.bone_target = n
                    t.transform_space = 'WORLD_SPACE'
            v = dr.variables.new()
            v.name = 'g'
            v.type = 'TRANSFORMS'
            t = v.targets[0]
            t.id = rig
            t.bone_target = root_scale
            t.transform_type = 'SCALE_X'
            t.transform_space = 'WORLD_SPACE'
            length = sum((arm.bones[a].head_local - arm.bones[b].head_local).length for a,
                         b in ((root, mid), (mid, tip)))
            if comp.startswith("leg"):
                v = dr.variables.new()
                v.name = "hip"
                v.targets[0].id = rig
                v.targets[0].data_path = pose["Display." + comp + "_root_ctl"].path_from_id("scale") + "[0]"
            dr.expression = f"(a+b)/max(.000001,{length:.12g}*abs(g)" + ("*hip)" if comp.startswith("leg") else ")")
        upper = guide.data.bones[root]
        samples = limb_samples(upper.get("div0", 2), upper.get("div1", 2), upper.get("supportJoints", True))
        i = int(name.split("_")[2])

        def profile(key, sign):
            values = upper.get(key)
            if not values:
                return sign * 0.5 * math.sin(math.pi * i / (len(samples) - 1))**2
            x = i * (len(values) - 1) / (len(samples) - 1)
            lo = int(x)
            return values[lo] + (x - lo) * (values[min(lo + 1, len(values) - 1)] - values[lo])
        expr = f"max(.0001,1+v*({profile('sq_profile', 1):.10g}*min(1,max(0,2*(1-r)))+{profile('st_profile', -1):.10g}*min(1,max(0,r-1))))"
        for i in (1, 2):
            drive(pose[skin], "scale", expr, {"v": (ctl, "volume"), "r": (owner, "ratio")}, i)
        constraint(name, 'COPY_TRANSFORMS', skin)
