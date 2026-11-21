# SPDX-License-Identifier: GPL-2.0-or-later
"""Biped spine guide tangents, sampled spline frames and hierarchical FK offsets."""
from mathutils import Matrix, Vector
import bpy


def build(rig, guide, output):
    arm = rig.data
    guide_names = {b["biped_locator"]: b.name for b in guide.data.bones if "biped_locator" in b}
    root = guide_names["spine_C0"]
    eff = guide_names["spine_C0_eff"]
    mech = arm.collections["Mechanism"]
    fk_group = arm.collections["FK"]
    bpy.ops.object.mode_set(mode='EDIT')
    edit = arm.edit_bones

    def helper(name, frame, parent=None):
        b = edit.new("MCH.Spine." + name)
        b.head = frame.translation
        b.tail = b.head + Vector((0, .01, 0))
        b.matrix = frame
        b.parent = parent
        b.use_deform = False
        for group in list(b.collections):
            group.unassign(b)
        mech.assign(b)
        return b

    prefix = "Display.spine_C0_"
    chest = edit[prefix + "spinePosition_ctl"]
    chest.parent = edit[eff].parent
    chest.hide_select = False
    edit["FK." + eff].parent = chest
    # The effector now has an extra parent in the control hierarchy.
    center = edit[prefix + "tan_ctl"]
    center_name = center.name
    center.parent = edit[root]
    center.hide_select = False
    controls = [root, prefix + "tan0_ctl", prefix + "tan1_ctl", eff]
    center_offsets = []
    for name, parent in zip(controls[1:3], (root, eff)):
        ctl = edit[name]
        ctl.hide_select = False
        space = helper(name, ctl.matrix.copy(), edit[parent])
        ctl.parent = space
        center_offsets.append(space.name)
    coords = [edit[n].head.copy() for n in controls]
    count = sum(n.startswith("spine_C0_") for n in output)
    samples = []
    positions = []

    def weights(u):
        return ((1 - u)**3, 3 * u * (1 - u)**2, 3 * u * u * (1 - u), u**3)

    def point(u):
        return sum((p * w for p, w in zip(coords, weights(u))), Vector((0, 0, 0)))

    def location_ref(name, u):
        frame = Matrix.Translation(point(u))
        b = helper(name, frame)
        positions.append((b.name, weights(u)))
        return b.name

    previous = None
    for i in range(count):
        name = f"spine_C0_{i}_jnt"
        fraction = i / (count - 1)
        # Sample the rest arc at the requested relative length.
        low, high = 0.0, 1.0
        axis = coords[-1] - coords[0]
        for _ in range(20):
            u = (low + high) / 2
            if (point(u) - coords[0]).dot(axis) < fraction * axis.length_squared:
                low = u
            else:
                high = u
        u = fraction if i in {0, count - 1} else (low + high) / 2
        tangent_u = u + 0.001 if u < .999 else u - 0.001
        p = location_ref("Point." + str(i), u)
        q = location_ref("Tangent." + str(i), tangent_u)
        direction = (point(tangent_u) - point(u)) * (1 if tangent_u > u else -1)
        frame = direction.to_track_quat('Y', 'Z').to_matrix().to_4x4()
        frame.translation = point(u)
        spline = helper("Frame." + str(i), frame, edit[root])
        base = helper("FKBase." + str(i), frame)
        control = base
        if 0 < i < count - 1:
            ctl_name = prefix + "fk" + str(i - 1) + "_ctl"
            ctl = edit.get(ctl_name)
            if not ctl:
                ctl = edit.new(ctl_name)
                ctl.head, ctl.tail = base.head, base.tail
                ctl.use_deform = False
                for group in list(ctl.collections):
                    group.unassign(ctl)
                fk_group.assign(ctl)
            ctl.parent = base
            ctl.hide_select = False
            control = ctl
        bind = edit["MCH.Bind." + name]
        bind.parent = control
        samples.append((p, q, spline.name, base.name, previous, tangent_u > u))
        # Use a frame with the same basis as the spline even when the Maya
        # controller has different local rotation axes.
        motion = helper("Motion." + str(i), frame, control)
        previous = (spline.name, motion.name)

    attach = helper("Chest", edit[eff].matrix.copy(), edit[previous[1]])
    for label, actual in guide_names.items():
        if label.startswith("spine_C0") or not edit[actual].parent or edit[actual].parent.name != eff:
            continue
        edit[actual].parent = attach
        fk = edit.get("FK." + actual)
        if fk and fk.parent and fk.parent.name == eff:
            fk.parent = attach
    bpy.ops.object.mode_set(mode='POSE')
    pose = rig.pose.bones
    follow = pose[eff].constraints[0]
    follow.target_space = follow.owner_space = 'POSE'

    def constraint(owner, kind, target):
        c = pose[owner].constraints.new(kind)
        c.target, c.subtarget = rig, target
        c.target_space = c.owner_space = 'POSE'
        return c
    for name in center_offsets:
        c = constraint(name, 'COPY_LOCATION', center_name)
        c.target_space = c.owner_space = 'LOCAL'
        c.use_offset = True
    for name, ws in positions:
        accumulated = 0
        for target, w in zip(controls, ws):
            if w > 0:
                accumulated += w
                c = constraint(name, 'COPY_LOCATION', target)
                c.influence = w / accumulated
    for p, q, spline, base, previous, forward in samples:
        constraint(spline, 'COPY_LOCATION', p)
        c = constraint(spline, 'DAMPED_TRACK', q)
        c.track_axis = 'TRACK_Y' if forward else 'TRACK_NEGATIVE_Y'
        relative = constraint(base, 'COPY_TRANSFORMS', spline)
        if previous:
            relative.target_space = 'CUSTOM'
            relative.space_object, relative.space_subtarget = rig, previous[0]
            c = constraint(base, 'COPY_TRANSFORMS', previous[1])
            c.mix_mode = 'BEFORE_FULL'
    for i in range(1, count - 1):
        ctl = pose[prefix + "fk" + str(i - 1) + "_ctl"]
        if not ctl.custom_shape:
            template = pose[prefix + "fk0_ctl"]
            ctl.custom_shape = template.custom_shape
            ctl.use_custom_shape_bone_size = False
            ctl.bone.color.palette = 'CUSTOM'
            ctl.bone.color.custom.normal = template.bone.color.custom.normal
