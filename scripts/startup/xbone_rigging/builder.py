# SPDX-License-Identifier: GPL-2.0-or-later

import math

import bpy
from mathutils import Vector


def widgets(rig):
    shapes = {}
    ring = [(math.cos(i * math.tau / 24), 0, math.sin(i * math.tau / 24)) for i in range(24)]
    cube = [(x, y, z) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]
    diamond = [(1, 0, 0), (0, 1, 0), (-1, 0, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]
    for name, vertices, edges in (
            ("FK", ring, [(i, (i + 1) % 24) for i in range(24)]),
            ("IK", cube, [(i, i ^ bit) for i in range(8) for bit in (1, 2, 4) if not i & bit]),
            ("Pole", diamond, [(i, j) for i in range(4) for j in ((i + 1) % 4, 4, 5)])):
        mesh = bpy.data.meshes.new(rig.name + "." + name)
        mesh.from_pydata(vertices, edges, [])
        shapes[name] = bpy.data.objects.new(mesh.name, mesh)
    return shapes


def limb_chains(bones):
    limbs = []
    used = set()
    for tip in bones:
        if not tip.get("ik_limb"):
            continue
        mid = tip.parent
        root = mid.parent if mid else None
        if root is None:
            raise ValueError("IK endpoint needs two parents: " + tip.name)
        chain = (root.name, mid.name, tip.name)
        if used.intersection(chain):
            raise ValueError("Overlapping IK limbs at " + tip.name)
        if min((tip.head_local - mid.head_local).length, (mid.head_local - root.head_local).length) < 0.0001:
            raise ValueError("Coincident IK pivots at " + tip.name)
        used.update(chain)
        limbs.append(chain)
    return limbs


def pole_point(a, b, c):
    axis = (c - a).normalized()
    bend = b - a - axis * (b - a).dot(axis)
    if bend.length < 0.0001:
        bend = axis.orthogonal() if axis.length else Vector((0, -1, 0))
    return b + bend.normalized() * ((a - b).length + (b - c).length) * 0.6


def copy_pose(owner, rig, target, kind='COPY_TRANSFORMS'):
    constraint = owner.constraints.new(kind)
    constraint.target = rig
    constraint.subtarget = target
    constraint.owner_space = constraint.target_space = 'POSE'
    return constraint


def orient_fk_ring(control, bone, primary_axis):
    """Fit the generic ring across the outgoing shaft without changing animation axes."""
    primary = bone.matrix_local.to_3x3().col["XYZ".index(primary_axis)]
    directions = [(child.head_local - bone.head_local).normalized() for child in bone.children
                  if (child.head_local - bone.head_local).length_squared > 1e-12]
    if directions:
        axis = max(directions, key=lambda direction: direction.dot(primary))
    elif bone.parent and (bone.head_local - bone.parent.head_local).length_squared > 1e-12:
        axis = (bone.head_local - bone.parent.head_local).normalized()
    else:
        axis = primary
    local_axis = bone.matrix_local.to_quaternion().inverted() @ axis
    control.custom_shape_rotation_euler = Vector((0, 1, 0)).rotation_difference(local_axis).to_euler()


def build(context, guide):
    chains = limb_chains(guide.data.bones)
    from . import biped_skeleton
    bind_plan = biped_skeleton.plans(guide) if "biped_display_scale" in guide else None
    if bind_plan and any(name in guide.data.bones for name in bind_plan):
        raise ValueError("A guide bone already uses a Shifter output joint name")
    if guide.get("xbiped"):
        from . import xbiped_limb
        xbiped_limb.set_limb_defaults(guide)
    # Copy rest data only. Preserve the guide for variations and keep bound rigs independent.
    arm = guide.data.copy()
    rig = bpy.data.objects.new(guide.name + ".Rig", arm)
    context.collection.objects.link(rig)
    rig.matrix_world = guide.matrix_world.copy()
    rig.show_in_front = True
    rig["rigging_role"] = "RIG"
    rig["rigging_guide"] = guide
    guide.select_set(False)
    rig.select_set(True)
    context.view_layer.objects.active = rig
    groups = {key: arm.collections.new(key) for key in ("Skeleton", "FK", "IK", "Mechanism")}
    originals = [b.name for b in arm.bones]
    bpy.ops.object.mode_set(mode='EDIT')
    edit = arm.edit_bones

    def move_to(bone, group):
        for collection in list(bone.collections):
            collection.unassign(bone)
        groups[group].assign(bone)

    def duplicate(name, source, parent, group):
        original = edit[source]
        bone = edit.new(name)
        bone.head, bone.tail, bone.roll = original.head, original.tail, original.roll
        bone.parent = parent
        bone.use_deform = False
        move_to(bone, group)
        return bone.name

    # Guide references and UI-host locators have no transform to animate.
    controls = {name for name in originals if edit[name].use_deform}
    controls.update(name for chain in chains for name in chain)
    eye_targets = {}
    for name in originals:
        src = edit[name]
        if src.get("eye_target") and src.parent:
            reference = src.get("eye_reference")
            eye_targets[name] = (src.parent.name, reference)
            controls.add(name)
            if reference in edit:
                controls.add(reference)
    for name in tuple(controls):
        controls.update(b.name for b in edit[name].parent_recursive)
    controls.difference_update(eye for eye, _ in eye_targets.values())
    fk = {}
    for name in originals:
        src = edit[name]
        move_to(src, "Skeleton")
        if name in controls:
            fk[name] = duplicate("FK." + name, name, src.parent, "FK")
    for name, (eye, reference) in eye_targets.items():
        edit[fk[name]].parent = edit.get(reference) or edit[eye].parent
    limbs = []
    origins = {}
    for root, mid, tip in chains:
        parent = edit[root].parent
        mechanism = []
        for name in (root, mid, tip):
            solver = duplicate("MCH." + name, name, parent, "Mechanism")
            mechanism.append(solver)
            parent = edit[solver]
        # FK rotations propagate down an independent chain during blending.
        edit[fk[mid]].parent = edit[fk[root]]
        edit[fk[tip]].parent = edit[fk[mid]]
        def space(prop):
            reference = edit.get(edit[tip].get(prop, ""))
            if reference and reference != edit[root] and edit[root] not in reference.parent_recursive:
                return reference
            return edit[root].parent

        target = duplicate("IK." + tip, tip, space("ik_reference"), "IK")
        if bind_plan:
            origins[target] = duplicate("MCH.Reach." + root, root, edit[root].parent, "Mechanism")
        pole = duplicate("Pole." + mid, mid, space("pole_reference"), "IK")
        offset = pole_point(*(edit[n].head for n in (root, mid, tip))) - edit[pole].head
        edit[pole].translate(offset)
        limbs.append(((root, mid, tip), mechanism, target, pole))
    bpy.ops.object.mode_set(mode='POSE')
    shapes = widgets(rig)
    pose = rig.pose.bones

    def style(name, shape, size, joint):
        control = pose[name]
        authored = arm.bones[joint].get("widget_" + shape)
        control.custom_shape = authored or shapes[shape]
        control.use_custom_shape_bone_size = False
        if authored:
            size = 1.0
        control.custom_shape_scale_xyz = (size,) * 3
        control.bone.color.palette = {"FK": "THEME04", "IK": "THEME03", "Pole": "THEME09"}[shape]

    for name in fk:
        if name not in eye_targets:
            follow = copy_pose(pose[name], rig, fk[name])
            follow.owner_space = follow.target_space = 'LOCAL'
        style(fk[name], "FK", max(0.015, pose[name].length * 0.24), name)
        if "biped_locator" not in arm.bones[name] and pose[fk[name]].custom_shape == shapes["FK"]:
            orient_fk_ring(pose[fk[name]], guide.data.bones[name], arm.xbone_primary_axis)
    for name, (eye, _) in eye_targets.items():
        aim = pose[eye].constraints.new('DAMPED_TRACK')
        aim.target = rig
        aim.subtarget = fk[name]
        aim.track_axis = 'TRACK_Y'
    for chain, mechanism, target, pole in limbs:
        length = (arm.bones[chain[0]].head_local - arm.bones[chain[1]].head_local).length
        style(target, "IK", length * 0.16, chain[-1])
        style(pole, "Pole", length * 0.1, chain[1])
        prop = "IK " + pose[chain[-1]].bone["ik_limb"]
        while prop in rig:
            prop += "_"
        endpoint = arm.bones[chain[-1]]
        rig[prop] = float(endpoint.get("ik_default", endpoint.name.endswith("_ankle")))
        pose[target]["blend_property"] = prop
        pose[target]["pole_control"] = pole
        pose[target]["fk_controls"] = [fk[name] for name in chain]
        pose[target]["solver_chain"] = mechanism
        pose[target]["joints"] = list(chain)
        rig.id_properties_ui(prop).update(min=0.0, max=1.0, default=rig[prop], description="FK at zero, IK at one")
        if bind_plan:
            from . import limb_parameters
            limb_parameters.configure(rig, pose[target], pose[origins[target]], mechanism,
                                      endpoint.get("maxstretch", 1.5))
        else:
            solver = pose[mechanism[-1]].constraints.new('IK')
            solver.target = solver.pole_target = rig
            solver.subtarget, solver.pole_subtarget = target, pole
            solver.chain_count = 3
            solver.use_tail = True
            solver.use_stretch = False
        blends = []
        for name, solved in zip(chain, mechanism):
            # The solver evaluates after constraints on its own endpoint. Orient
            # the deformation endpoint from the animator's target after solving.
            kind = 'COPY_LOCATION' if name == chain[-1] else 'COPY_TRANSFORMS'
            blend = copy_pose(pose[name], rig, solved, kind)
            blend.owner_space = blend.target_space = 'LOCAL'
            blends.append(blend)
        blends.append(copy_pose(pose[chain[-1]], rig, target, 'COPY_ROTATION'))
        for blend in blends:
            driver = blend.driver_add("influence").driver
            driver.type = 'AVERAGE'
            var = driver.variables.new()
            var.targets[0].id = rig
            var.targets[0].data_path = '["' + bpy.utils.escape_identifier(prop) + '"]'
        # Keep overlapping ankle widgets selectable only in their active mode.
        fk_names = [fk[n] for n in chain]
        fk_names.extend(fk[b.name] for b in endpoint.children if b.get("foot_root") and b.name in fk)
        for name in [*fk_names, target, pole]:
            driver = pose[name].driver_add("hide").driver
            var = driver.variables.new()
            var.name = "blend"
            var.targets[0].id = rig
            var.targets[0].data_path = '["' + bpy.utils.escape_identifier(prop) + '"]'
            driver.expression = "blend >= 1" if name in fk_names else "blend <= 0"
    groups["Mechanism"].is_visible = groups["Skeleton"].is_visible = False
    if "biped_display_scale" in guide:
        from . import biped_display
        widget_frames = biped_display.apply(rig, guide, fk)
        from . import biped_foot
        biped_foot.build(rig, guide)
        biped_skeleton.build(rig, guide, bind_plan, groups["Skeleton"], groups["Mechanism"], fk)
        from . import biped_spine
        biped_spine.build(rig, guide, bind_plan)
        from . import biped_components
        biped_components.connect(rig, guide)
        from . import biped_detail
        biped_detail.connect(rig, guide)
        from . import biped_two_bone
        biped_two_bone.build(rig, guide)
        from . import biped_endpoints
        biped_endpoints.build(rig, guide)
        from . import biped_roundness
        biped_roundness.build(rig, guide, bind_plan)
        if guide.get("xbiped"):
            biped_skeleton.bind_neutral_outputs(context, rig, bind_plan)
        for name, frame in widget_frames.items():
            p = rig.pose.bones[name]
            p.custom_shape.data.transform(p.bone.matrix_local.inverted_safe() @ frame)
    guide.hide_set(True)
    guide.hide_render = True
    return rig
