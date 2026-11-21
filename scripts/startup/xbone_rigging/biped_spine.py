# SPDX-License-Identifier: GPL-2.0-or-later
"""Shifter spine and neck master/slave paths and hierarchical FK frames."""
import math
import json
from pathlib import Path
from .biped_skeleton import read_frame
from .affine import LocalFrames
import bpy
from mathutils import Matrix, Vector
from .limb_parameters import drive


def build(rig, guide, output):
    names = {b["biped_locator"]: b.name for b in guide.data.bones if "biped_locator" in b}
    host = rig.pose.bones["Display.spineUI_C0_ctl"]
    for part, endpoint in (("spine", "eff"), ("neck", "neck")):
        spec = (("tan0", 1, 0, 10), ("tan1", 1, 0, 10), ("volume", 1, 0, 1),
                ("maxstretch", 1.5, 1, 10), ("maxsquash", .5, 0, 1), ("softness", 0, 0, 1))
        if part == "spine":
            spec += (("position", 0, 0, 1), ("lock_ori0", 1, 0, 1), ("lock_ori1", 1, 0, 1),
                     ("sideBend", .5, 0, 2), ("frontBend", .5, 0, 2))
        else:
            spec += (("lock_ori", 1, 0, 1),)
        for key, initial, lo, hi in spec:
            key = part + "_" + key
            host[key] = float(initial)
            host.id_properties_ui(key).update(min=lo, max=hi, default=float(initial))
        _component(rig, guide, names, part, endpoint, output)


def _component(rig, guide, names, part, endpoint, output):
    arm = rig.data
    comp = part + "_C0"
    root, end = names[comp], names[comp + "_" + endpoint]
    spine = part == "spine"
    prefix = "MCH." + part.title() + "."
    display = "Display." + comp + "_"
    host_name = "Display.spineUI_C0_ctl"
    count = sum(n.startswith(comp + "_") and n.split("_")[2].isdigit() for n in output)
    bpy.ops.object.mode_set(mode='EDIT')
    edit = arm.edit_bones
    local_frames = LocalFrames(arm)
    origin = Matrix.Identity(4)

    def helper(label, frame=origin, parent=None):
        b = edit.new(prefix + label)
        b.head = frame.translation
        b.tail = b.head + Vector((0, .01, 0))
        b.matrix = frame
        b.parent = edit[parent] if parent else None
        b.use_deform = False
        for coll in list(b.collections):
            coll.unassign(b)
        arm.collections["Mechanism"].assign(b)
        return b.name
    mount = edit[root].parent.name
    with Path(__file__).with_name("biped_pivots.json").open() as stream:
        reference = json.load(stream)
    basis = Matrix.Rotation(math.pi / 2, 3, 'X') if guide["biped_display_up"] == 'Y' else Matrix.Identity(3)
    bone = guide.data.bones[root]
    correction = bone.matrix_local @ Matrix([bone["biped_frame"][k:k + 4] for k in range(0, 16, 4)]).inverted_safe()

    def component_frame(key):
        return correction @ read_frame(reference[comp + "_" + key], guide["biped_display_scale"], basis)
    base = helper("Root", component_frame("root"), mount)
    measure = helper("Length")
    if spine:
        start = "FK." + root
        finish = "FK." + end
        chest = edit[display + "spinePosition_ctl"]
        chest.parent = edit[base]
        chest.hide_select = False
        aim_frame = edit[start].matrix.copy()
        axis = (edit[finish].head - edit[start].head).normalized()
        rotation = aim_frame.to_quaternion()
        aim_frame = ((rotation @ Vector((0, 1, 0))).rotation_difference(axis) @ rotation).to_matrix().to_4x4()
        aim_frame.translation = edit[start].head
        aim = helper("AutoBend", aim_frame, base)
        auto = helper("AutoRotation", edit[finish].matrix.copy(), aim)
        edit[finish].parent = edit[auto]
        tan = [display + "tan0_ctl", display + "tan1_ctl"]
        center = display + "tan_ctl"
        edit[center].hide_select = False
    else:
        start = base
        twist_start = helper("TwistRoot", component_frame("intMRef"), base)
        finish = display + "ik_ctl"
        edit[finish].parent = edit[base]
        tan = [helper("Tan" + str(i), edit[names[comp + "_tan" + str(i)]].matrix.copy()) for i in range(2)]
        center = None
    sockets = []
    tangents = []
    for i, (name, parent) in enumerate(zip(tan, (start, finish))):
        # Local position in the parent's space, scaled by the endpoint distance.
        f = edit[parent].matrix.copy()
        f.translation = edit[name].head
        socket = helper("TangentSocket" + str(i), f, parent)
        tangents.append((socket, f.to_3x3().inverted() @ (f.translation - edit[parent].head)))
        offset = helper("TangentOffset" + str(i), edit[name].matrix.copy(), socket)
        edit[name].parent = edit[offset]
        if spine:
            edit[name].hide_select = False
        sockets.append(offset)
    if center:
        center_base = helper("CenterSocket", edit[center].matrix.copy())
        edit[center].parent = edit[center_base]
    points = [edit[n].head.copy() for n in (start, *tan, finish)]

    def bez(u):
        a, b, c, d = points
        return (1 - u)**3 * a + 3 * u * (1 - u)**2 * b + 3 * u * u * (1 - u) * c + u**3 * d
    rest_length = sum((bez(i / 64) - bez((i - 1) / 64)).length for i in range(1, 65))
    chord = (points[3] - points[0]).length
    # Rest orientations from the built joints supply the component's up axis.
    raw_samples = [helper("Arc" + str(i)) for i in range(17)]
    local_samples = [helper("LocalArc" + str(i)) for i in range(17)]
    slave_samples = [helper("SkinArc" + str(i)) for i in range(17)]
    slave_local = [helper("LocalSkinArc" + str(i)) for i in range(17)]
    cvs = []
    for i in range(10):
        sample = helper("CVSample" + str(i))
        outdir = helper("CVAxis" + str(i), origin, sample)
        point = helper("CVExtend" + str(i), origin, outdir)
        hook = helper("CV" + str(i), Matrix.Translation(bez(i / 9)))
        cvs.append((sample, outdir, point, hook))
    samples = []
    sizes = []
    if not spine:
        world = helper("WorldSpace", basis.to_4x4())
        root_scale = local_frames.rotation(comp + ".rootScale", edit[base], edit[world], scale=True).name
        inverse_scale = local_frames.rotation(comp + ".inverseScale", edit[world], edit[base], scale=True).name
        compensated_root = helper("RootCompensation", edit[base].matrix.copy(), base)
    prev = None
    for i in range(count):
        u = i / (count - 1)
        name = comp + "_" + str(i) + "_jnt"
        bind = edit["MCH.Bind." + name]
        frame = bind.matrix.copy()
        motion = helper("Frame." + str(i), frame)
        fkbase = helper("FKBase." + str(i), frame)
        if not spine and not i:
            local_frames.rotation(comp + ".firstFrame", edit[motion], edit[base])
        scaled = fkbase if spine else helper("Size." + str(i), frame, fkbase)
        ctl = scaled
        if spine and 0 < i < count - 1:
            ctl = display + "fk" + str(i - 1) + "_ctl"
            if ctl not in edit:
                ctl = helper("ExtraFK" + str(i), frame)
                edit[ctl].name = display + "fk" + str(i - 1) + "_ctl"
                ctl = display + "fk" + str(i - 1) + "_ctl"
                arm.collections["Mechanism"].unassign(edit[ctl])
                arm.collections["FK"].assign(edit[ctl])
        elif not spine and i < count - 1:
            ctl = ("FK." + root if i == 0 else "FK." + end if i == 1 else helper("ExtraFK" + str(i), frame))
        if ctl != scaled:
            edit[ctl].parent = edit[scaled]
            edit[ctl].hide_select = False
        result = helper("Motion." + str(i), frame, ctl)
        bind.parent = edit[result]
        # FK controls and skin inherit the same volume on the connected hooks.
        sample = helper("Point." + str(i))
        tangent = helper("Forward." + str(i))
        up = helper("UpFrame." + str(i), frame)
        upframe = frame.copy()
        # Shifter's default spine and neck both use Y as path front axis.
        upframe.translation += frame.to_3x3().col[2] * rest_length
        up_point = helper("Up." + str(i), upframe, up)
        samples.append((u, motion, fkbase, result, prev, sample, tangent, up, up_point, ctl))
        successor = result if spine else helper("Compensate." + str(i), frame, result)
        sizes.append((scaled, successor))
        prev = (motion, successor)
    attach = helper("Chest" if spine else "Head", edit[end].matrix.copy(), result)
    if spine:
        hips = helper("Pelvis", edit[root].matrix.copy(), samples[0][3])
        for label, n in names.items():
            if label.startswith(comp) or not edit[n].parent:
                continue
            parent = edit[n].parent.name
            if parent not in (end, root):
                continue
            hook = attach if parent == end else hips
            edit[n].parent = edit[hook]
            fk = edit.get("FK." + n)
            if fk and fk.parent and fk.parent.name == parent:
                fk.parent = edit[hook]
    else:
        edit["FK." + names[comp + "_head"]].parent = edit[attach]
    bpy.ops.object.mode_set(mode='POSE')
    pose = rig.pose.bones
    host = pose[host_name]
    local_frames.configure(rig)
    def attr(k): return host, part + "_" + k

    def constraint(owner, kind, target):
        c = pose[owner].constraints.new(kind)
        c.target, c.subtarget = rig, target
        c.owner_space = c.target_space = 'POSE'
        return c
    if spine:
        c = constraint(aim, 'DAMPED_TRACK', display + "spinePosition_ctl")
        c.track_axis = 'TRACK_Y'
        # Auto-bend rotation is an extra fraction of the aimed one-bone chain.
        pb = pose[auto]
        pb.rotation_mode = 'XYZ'
        twist = pb.driver_add("rotation_euler", 1).driver
        twist.type = 'AVERAGE'
        var = twist.variables.new()
        var.type = 'TRANSFORMS'
        t = var.targets[0]
        t.id = rig
        t.bone_target = display + "spinePosition_ctl"
        t.transform_space = 'LOCAL_SPACE'
        t.rotation_mode = 'XYZ'
        t.transform_type = 'ROT_Y'
        for i, prop in ((0, "frontBend"), (2, "sideBend")):
            dr = pb.driver_add("rotation_euler", i).driver
            v = dr.variables.new()
            v.name = "bend"
            v.type = 'TRANSFORMS'
            t = v.targets[0]
            t.id = rig
            t.bone_target = aim
            t.transform_space = 'LOCAL_SPACE'
            t.rotation_mode = 'XYZ'
            t.transform_type = ('ROT_X', 'ROT_Y', 'ROT_Z')[i]
            v = dr.variables.new()
            v.name = "factor"
            v.targets[0].id = rig
            v.targets[0].data_path = host.path_from_id() + '["spine_' + prop + '"]'
            dr.expression = "bend*factor"
        for i, (socket, _) in enumerate(tangents):
            for kind in ('COPY_LOCATION', 'COPY_ROTATION'):
                c = constraint(center_base, kind, socket)
                c.influence = 1 if not i else .5
            c = constraint(sockets[i], 'COPY_LOCATION', center)
            c.owner_space = c.target_space = 'LOCAL'

    def property_driver(key, expr, variables):
        p = pose[measure]
        p[key] = 1.
        drive(p, '["' + key + '"]', expr, variables)
        return p, key

    def distances(key, nodes, divisor, reference=None):
        p = pose[measure]
        p[key] = 1.
        dr = p.driver_add('["' + key + '"]').driver
        terms = []
        for i, (a, b) in enumerate(zip(nodes, nodes[1:])):
            v = dr.variables.new()
            v.name = "d" + str(i)
            v.type = 'LOC_DIFF'
            for t, n in zip(v.targets, (a, b)):
                t.id = rig
                t.bone_target = n
                t.transform_space = 'WORLD_SPACE'
            terms.append(v.name)
        v = dr.variables.new()
        v.name = "globalScale"
        v.type = 'TRANSFORMS'
        t = v.targets[0]
        t.id = rig
        t.bone_target = base if reference is None else reference
        t.transform_type = 'SCALE_X'
        t.transform_space = 'WORLD_SPACE'
        dr.expression = "(" + "+".join(terms) + f")/max(0.000001,{divisor:.12g}*abs(globalScale))"
        return p, key
    ratio = distances("chord", [start, finish], chord)
    for i, (socket, offset) in enumerate(tangents):
        for axis, value in enumerate(offset):
            if axis == 1 and abs(value) > 1e-9:
                drive(pose[socket], "location", f"{value:.12g}*(r*t-1)", {"r": ratio, "t": attr("tan" + str(i))}, axis)

    def path(label, coords, hooks, bezier):
        curve = bpy.data.curves.new(comp + "." + label, 'CURVE')
        curve.dimensions = '3D'
        curve.use_path = True
        curve.resolution_u = 48
        spline = curve.splines.new('BEZIER' if bezier else 'NURBS')
        if bezier:
            spline.bezier_points.add(1)
            a, b = spline.bezier_points
            for p in (a, b):
                p.handle_left_type = p.handle_right_type = 'FREE'
            a.co, a.handle_left, a.handle_right = coords[0], coords[0], coords[1]
            b.co, b.handle_left, b.handle_right = coords[3], coords[2], coords[3]
            indices = ((0, 1), (2,), (3,), (4, 5))
        else:
            spline.points.add(len(coords) - 1)
            for p, co in zip(spline.points, coords):
                p.co = (*co, 1)
            spline.order_u = 4
            spline.use_endpoint_u = True
            indices = [(i,) for i in range(len(coords))]
        obj = bpy.data.objects.new(curve.name, curve)
        rig.users_collection[0].objects.link(obj)
        obj.parent = rig
        obj.hide_render = obj.hide_select = obj.hide_viewport = True
        obj["xbone_solver_curve"] = True
        for n, idx in zip(hooks, indices):
            mod = obj.modifiers.new(n, 'HOOK')
            mod.object, mod.subtarget = rig, n
            mod.vertex_indices_set(idx)
            mod.matrix_inverse = arm.bones[n].matrix_local.inverted_safe()
        return obj

    def follow(n, path, u):
        c = pose[n].constraints.new('FOLLOW_PATH')
        c.target = path
        c.use_fixed_location = True
        c.offset_factor = u
        return c
    master = path("Master", points, [start, *tan, finish], True)
    for i, n in enumerate(raw_samples):
        follow(n, master, i / 16)

    def local_arc(label, samples, local_samples):
        for source, dst in zip(samples, local_samples):
            c = constraint(dst, 'COPY_LOCATION', source)
            c.target_space = 'CUSTOM'
            c.space_object, c.space_subtarget = rig, base
        return distances(label, local_samples, rest_length, "")
    arc = local_arc("arc", raw_samples, local_samples)
    size = property_driver(
        "length",
        "1+min(d-1,(stretch-1)*(1-exp(-(d-1)/max(0.000001,stretch*soft)) if soft else 1)) if d>1 else 1-min(1-d,(1-squash)*(1-exp(-(1-d)/max(0.000001,squash*soft)) if soft else 1))",
        {
            "d": arc,
            "stretch": attr("maxstretch"),
            "squash": attr("maxsquash"),
            "soft": attr("softness")})
    for i, (s, orient, ext, hook) in enumerate(cvs):
        variables = {"s": size, "d": arc}
        if spine:
            variables["p"] = attr("position")
        expr = f"{i / 9:.12g}*s/max(d,0.000001)" + ("+p*(1-s/max(d,0.000001))" if spine else "")
        u = property_driver("u" + str(i), expr, variables)
        c = follow(s, master, i / 9)
        drive(c, "offset_factor", "min(1,max(0,u))", {"u": u})
        # Extend past either clamped path end along its endpoint tangent.
        for edge, next_point, sign, expr in (
                (start, tan[0], 'TRACK_Y', "u<0"), (finish, tan[1], 'TRACK_NEGATIVE_Y', "u>1")):
            c = constraint(orient, 'DAMPED_TRACK', next_point)
            c.track_axis = sign
            drive(c, "influence", expr, {"u": u})
        drive(pose[ext], "location", f"{rest_length:.12g}*d*(min(u,0)+max(u-1,0))", {"u": u, "d": arc}, 1)
        constraint(hook, 'COPY_LOCATION', ext)
    slave = path("Slave", [bez(i / 9) for i in range(10)], [c[-1] for c in cvs], False)
    for i, n in enumerate(slave_samples):
        follow(n, slave, i / 16)
    volume_length = local_arc("volumeLength", slave_samples, slave_local)
    for i, (u, motion, fkbase, result, prev, s, t, up, up_point, ctl) in enumerate(samples):
        follow(s, slave, u)
        follow(t, slave, u + .0005 if u < 1 else u - .0005)
        constraint(motion, 'COPY_LOCATION', s)
        first = constraint(up, 'COPY_ROTATION', start if spine else twist_start)
        second = constraint(up, 'COPY_ROTATION', finish)
        second.influence = u
        constraint(up, 'COPY_LOCATION', s)
        c = constraint(motion, 'DAMPED_TRACK', t)
        c.track_axis = 'TRACK_Y' if u < 1 else 'TRACK_NEGATIVE_Y'
        c = constraint(motion, 'LOCKED_TRACK', up_point)
        c.lock_axis = 'LOCK_Y'
        c.track_axis = 'TRACK_Z'
        if i == count - 1 or (spine and not i):
            c = constraint(motion, 'COPY_ROTATION', finish if i else start)
            drive(c, "influence", "v", {"v": attr(("lock_ori1" if i else "lock_ori0") if spine else "lock_ori")})
        if spine:
            constraint(motion, 'COPY_SCALE', base)
        relative = constraint(fkbase, 'COPY_TRANSFORMS', motion if spine or prev else "MCH.QR." + comp + ".firstFrame")
        if prev:
            relative.target_space = 'CUSTOM'
            relative.space_object, relative.space_subtarget = rig, prev[0]
            c = constraint(fkbase, 'COPY_TRANSFORMS', prev[1])
            c.mix_mode = 'BEFORE_FULL'
        elif not spine:
            c = constraint(fkbase, 'COPY_TRANSFORMS', compensated_root)
            c.mix_mode = 'BEFORE_FULL'
        config = guide.data.bones[root]

        def coeff(k):
            values = config.get(k)
            if not values:
                return 0.
            f = u * (len(values) - 1)
            lo = int(f)
            return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (f - lo)
        squash, stretch = coeff("sq_profile"), coeff("st_profile")
        compression = 2 if spine else 1 / max(.0001, 1 - .1 * guide["biped_display_scale"] / rest_length)
        expr = f"1+v*({stretch:.10g}*min(1,max(0,d-1))+{squash:.10g}*min(1,max(0,{compression:.10g}*(1-d))))"
        for axis in range(3):
            if spine:
                if axis != 1:
                    drive(pose["MCH.Bind." + comp + f"_{i}_jnt"], "scale",
                          expr, {"v": attr("volume"), "d": volume_length}, axis)
                continue
            scaled, successor = sizes[i]
            pb = pose[scaled]
            if axis != 1:
                dr = drive(pb, "scale", "g*(" + expr + ")", {"v": attr("volume"), "d": volume_length}, axis)
            else:
                dr = pb.driver_add("scale", axis).driver
                dr.expression = "g"
            var = dr.variables.new()
            var.name = "g"
            var.targets[0].id = rig
            var.targets[0].data_path = pose[root_scale].path_from_id("scale") + f"[{axis}]"
            dr = pose[successor].driver_add("scale", axis).driver
            var = dr.variables.new()
            var.name = "g"
            var.targets[0].id = rig
            var.targets[0].data_path = pb.path_from_id("scale") + f"[{axis}]"
            dr.expression = "1/max(0.00001,g)"
            if not i:
                dr = pose[compensated_root].driver_add("scale", axis).driver
                dr.type = 'AVERAGE'
                var = dr.variables.new()
                var.targets[0].id = rig
                var.targets[0].data_path = pose[inverse_scale].path_from_id("scale") + f"[{axis}]"
        if ctl != sizes[i][0] and not pose[ctl].custom_shape:
            source = pose[display + "fk0_ctl"] if spine else pose["FK." + root]
            pose[ctl].custom_shape = source.custom_shape
            pose[ctl].use_custom_shape_bone_size = False
            pose[ctl].bone.color.palette = 'CUSTOM'
            pose[ctl].bone.color.custom.normal = source.bone.color.custom.normal
