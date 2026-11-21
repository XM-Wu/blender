# SPDX-License-Identifier: GPL-2.0-or-later
"""Build the Shifter Biped skin hierarchy separately from the pose solver."""
import bisect
import json
import math
from pathlib import Path

import bpy
from mathutils import Matrix, Quaternion, Vector
from .limb_parameters import drive


def limb_samples(upper, lower, support):
    # Shifter arm_2jnt_01 and leg_2jnt_01 reserve support samples around the bend.
    if any(type(n) is not int or n < 0 for n in (upper, lower)):
        raise ValueError("Upper and lower divisions must be nonnegative integers")
    values = [i * 0.5 / (upper + 1) for i in range(upper + 1)]
    values.extend((0.49, 0.5, 0.51) if support else (0.501,))
    values.extend(0.5 + i * 0.5 / (lower + 1) for i in range(1, lower + 2))
    return [max(0.001, min(0.99, u)) for u in values]


def even_samples(count):
    if type(count) is not int or count < 2:
        raise ValueError("Spine and neck components need at least two divisions")
    return [i / (count - 1) for i in range(count)]


def read_frame(values, scale, basis):
    position = basis @ Vector(values[:3]) * scale
    rotation = basis.to_quaternion() @ Quaternion(values[3:])
    return Matrix.LocRotScale(position, rotation, None)


def plans(guide):
    with Path(__file__).with_name("biped_bind.json").open() as stream:
        reference = json.load(stream)
    locators = {b["biped_locator"]: b for b in guide.data.bones if "biped_locator" in b}
    required = {"body_C0", "global_C0", "neck_C0_neck", "spine_C0_eff"}
    for side in ("L0", "R0"):
        required.update(f"foot_{side}_{key}" for key in ("heel", "inpivot", "outpivot", "0_loc", "1_loc", "2_loc"))
        required.update(f"{kind}UI_{side}" for kind in ("arm", "leg"))
        for kind, mid, end in (("arm", "elbow", "wrist"), ("leg", "knee", "ankle")):
            root = kind + "_" + side
            tip = locators.get(root + "_" + end)
            if not tip or not tip.get("ik_limb") or not tip.parent or tip.parent != locators.get(
                    root + "_" + mid) or tip.parent.parent != locators.get(root):
                raise ValueError("Biped needs the two-joint guide chain for " + root)
    missing = required.difference(locators)
    if missing:
        raise ValueError("Missing Biped guide joints: " + ", ".join(sorted(missing)))
    scale = guide["biped_display_scale"]
    basis = Matrix.Rotation(math.pi / 2, 3, 'X') if guide["biped_display_up"] == 'Y' else Matrix.Identity(3)
    frames = {n: read_frame(v, scale, basis) for n, (_, v) in reference.items()}
    output = {}
    remap = {}

    def append(name, parent, frame, anchor, end=None, factor=0):
        if anchor not in locators or (end and end not in locators):
            raise ValueError("Missing Biped guide joint for " + name)
        bone = locators[anchor]
        original = Matrix([bone["biped_frame"][i:i + 4] for i in range(0, 16, 4)])
        rest = bone.matrix_local @ original.inverted_safe() @ frame
        if end:
            # Adapt intermediate pivots to edited guide spans as well as rotations.
            end_bone = locators[end]
            if (end_bone.head_local - bone.head_local).length < 1e-6:
                raise ValueError("Coincident Biped guide pivots: " + anchor + " and " + end)
            old_end = Vector([end_bone["biped_frame"][i] for i in (3, 7, 11)])
            baseline = original.inverted_safe() @ old_end
            rest.translation += (end_bone.head_local - bone.matrix_local @ baseline) * factor
        output[name] = (parent, rest, bone.name, locators[end].name if end else None, factor)

    split_components = {}
    for component in ('arm_L0', 'arm_R0', 'leg_L0', 'leg_R0', 'spine_C0', 'neck_C0'):
        if component not in locators:
            raise ValueError("Missing Biped component " + component)
        settings = locators[component]
        kind = component.split('_')[0]
        limb = kind in {'arm', 'leg'}
        if limb:
            old = limb_samples(2, 2, True)
            new = limb_samples(settings.get("div0", 2), settings.get("div1", 2),
                               settings.get("supportJoints", True))
        else:
            default = 5 if kind == 'spine' else 3
            old = even_samples(default)
            new = even_samples(settings.get("division", default))
        split_components[component] = len(old)
        for index, u in enumerate(new):
            hi = min(len(old) - 1, max(1, bisect.bisect_left(old, u)))
            low = hi - 1
            t = (u - old[low]) / (old[hi] - old[low])
            prefix = component + "_"
            frame = frames[prefix + str(low) + "_jnt"].lerp(frames[prefix + str(hi) + "_jnt"], t)
            parent = prefix + str(index - 1) + "_jnt" if index else reference[prefix + "0_jnt"][0]
            if limb:
                mid, tip = ('elbow', 'wrist') if kind == 'arm' else ('knee', 'ankle')
                anchor, end, factor = ((component, prefix + mid, u * 2) if u < .5 else
                                       (prefix + mid, prefix + tip, (u - .5) * 2))
            else:
                anchor, end, factor = component, prefix + ('eff' if kind == 'spine' else 'neck'), u
            append(prefix + str(index) + "_jnt", parent, frame, anchor, end, factor)
        remap[component + "_" + str(len(old) - 1) + "_jnt"] = component + "_" + str(len(new) - 1) + "_jnt"
    for name, (parent, _) in reference.items():
        component, side, suffix = name.split('_', 2)
        key = component + "_" + side
        index = suffix.removesuffix("_jnt")
        if key in split_components and index.isdigit():
            continue
        if component in {'arm', 'leg'}:
            loc = 'wrist' if component == 'arm' else 'ankle'
        elif component in {'finger', 'thumb', 'tongue', 'meta'}:
            loc = str(int(index) - 1) + '_loc' if int(index) else ''
        elif component == 'foot':
            loc = index + '_loc'
        elif component == 'neck':
            loc = 'head'
        elif component == 'mouth':
            loc = {'teethup': 'lipup', 'teethlow': 'liplow'}.get(index, index)
        else:
            loc = ''
        append(name, remap.get(parent, parent), frames[name], key + ('_' + loc if loc else ''))
    # Attach other segmented components to the final sample of their parent.
    for component in split_components:
        name = component + "_0_jnt"
        parent, *rest = output[name]
        output[name] = (remap.get(parent, parent), *rest)
    return output


def build(rig, guide, output, skeleton, mechanism):
    arm = rig.data
    guide_names = [b.name for b in skeleton.bones if "biped_locator" in b]
    bpy.ops.object.mode_set(mode='EDIT')
    edit = arm.edit_bones
    for name in guide_names:
        bone = edit[name]
        bone.use_deform = False
        skeleton.unassign(bone)
        mechanism.assign(bone)

    length = guide["biped_display_scale"] * 0.3

    def joint(name, frame, group, parent=None):
        bone = edit.new(name)
        bone.head = frame.translation
        bone.tail = bone.head + Vector((0, length, 0))
        bone.matrix = frame
        bone.parent = parent
        bone.use_deform = group == skeleton
        for coll in list(bone.collections):
            coll.unassign(bone)
        group.assign(bone)
        return bone.name

    probes = {}
    followers = {}
    bend_frames = {}
    rolls = {}
    neutral = {}
    ankle_roll = {}
    with Path(__file__).with_name("biped_pivots.json").open() as stream:
        reference_frames = json.load(stream)
    basis_change = Matrix.Rotation(math.pi / 2, 3, 'X') if guide["biped_display_up"] == 'Y' else Matrix.Identity(3)
    mid_controls = {}
    planes = {}
    for bone in guide.data.bones:
        component = bone.get("ik_limb")
        if component and bone.parent:
            name = "Display." + component + "_mid_ctl"
            if name in edit:
                edit[name].hide_select = False
                mid_controls[bone.parent.name] = name
                upper = bone.parent.parent.name
                planes[upper, name] = (bone.name, "IK." + bone.name)
                planes[name, bone.name] = (upper, "IK." + bone.name)
    for name, (_, rest, anchor, end, factor) in output.items():
        joint(name, rest, skeleton)
        anchor = mid_controls.get(anchor, anchor)
        end = mid_controls.get(end, end)
        parent = edit[anchor]
        if end and not name.startswith("spine_C0_"):
            span = (anchor, end)
            if span not in probes:
                axis = edit[end].head - parent.head
                basis = axis.to_track_quat('Y', 'Z').to_matrix().to_4x4() if axis.length else Matrix.Identity(4)
                if span in planes:
                    direction = axis.normalized()
                    toward = edit[planes[span][0]].head - parent.head
                    across = toward - direction * toward.dot(direction)
                    if across.length > 0.00001:
                        x = across.normalized()
                        basis = Matrix((x, direction, x.cross(direction))).transposed().to_4x4()
                    else:
                        del planes[span]
                basis.translation = parent.head
                carrier = joint("MCH.Span." + anchor, basis, mechanism, parent)
                probe = joint("MCH.RollRead." + anchor, basis, mechanism, edit[carrier])
                target = joint("MCH.RollEnd." + anchor, basis, mechanism, edit[end])
                probes[span] = (carrier, probe, target, basis, axis.length)
                if end in mid_controls.values():
                    aim = joint("MCH.RollBase." + anchor, basis, mechanism, parent.parent)
                    read = joint("MCH.RollBaseRead." + anchor, basis, mechanism, edit[carrier])
                    neutral[probe] = (aim, read, end, anchor)
                elif anchor in mid_controls.values() and edit[end].get("ik_limb", "").startswith("leg_"):
                    component = edit[end]["ik_limb"]
                    tip = guide.data.bones[end]
                    original = Matrix([tip["biped_frame"][i:i + 4] for i in range(0, 16, 4)])
                    correction = tip.matrix_local @ original.inverted_safe()
                    ref = correction @ read_frame(reference_frames[component +
                                                                   "_tws2_loc"], guide["biped_display_scale"], basis_change)
                    end_ref = correction @ read_frame(reference_frames[component +
                                                                       "_tws2_rot"], guide["biped_display_scale"], basis_change)
                    base = joint("MCH.AnkleBase." + end, ref, mechanism, edit[carrier])
                    read = joint("MCH.AnkleRead." + end, ref, mechanism, edit[base])
                    goal = joint("MCH.AnkleEnd." + end, end_ref, mechanism, edit[end])
                    ankle_roll[probe] = (read, goal, -1 if component.endswith("_R0") else 1)
            carrier, probe, _, basis, _ = probes[span]
            frame = basis.copy()
            frame.translation = rest.translation
            roll = joint("MCH.Roll." + name, frame, mechanism, edit[carrier])
            rolls[roll] = (probe, factor)
            parent = edit[roll]
        followers[name] = joint("MCH.Bind." + name, rest, mechanism, parent)
        if end and anchor in mid_controls.values() and factor == 0:
            bend_frames[name] = joint("MCH.Bend." + name, rest, mechanism, edit[anchor].parent.parent)
    for name, (parent, *_) in output.items():
        if parent:
            edit[name].parent = edit[parent]
    bpy.ops.object.mode_set(mode='POSE')
    pose = rig.pose.bones
    for aim, read, end, anchor in neutral.values():
        loc = pose[aim].constraints.new('COPY_LOCATION')
        loc.target, loc.subtarget = rig, anchor
        track = pose[aim].constraints.new('DAMPED_TRACK')
        track.target, track.subtarget, track.track_axis = rig, end, 'TRACK_Y'
        con = pose[read].constraints.new('COPY_ROTATION')
        con.target, con.subtarget = rig, aim
        con.owner_space = con.target_space = 'POSE'
    for read, goal, _ in ankle_roll.values():
        con = pose[read].constraints.new('COPY_ROTATION')
        con.target, con.subtarget = rig, goal
        con.owner_space = con.target_space = 'POSE'
    for (anchor, end), (carrier, probe, target, _, distance) in probes.items():
        span = pose[carrier].constraints.new('STRETCH_TO')
        span.target, span.subtarget = rig, end
        span.rest_length, span.volume = distance, 'NO_VOLUME'
        if (anchor, end) in planes:
            reference, control = planes[anchor, end]
            plane = pose[carrier].constraints.new('LOCKED_TRACK')
            plane.target, plane.subtarget = rig, reference
            plane.lock_axis, plane.track_axis = 'LOCK_Y', 'TRACK_X'
            driver = plane.driver_add("influence").driver
            driver.type = 'AVERAGE'
            var = driver.variables.new()
            var.targets[0].id = rig
            var.targets[0].data_path = '["' + bpy.utils.escape_identifier(pose[control]["blend_property"]) + '"]'
        con = pose[probe].constraints.new('COPY_ROTATION')
        con.target, con.subtarget = rig, target
        con.owner_space = con.target_space = 'POSE'
    for name, (probe, factor) in rolls.items():
        pb = pose[name]
        pb.rotation_mode = 'XYZ'
        driver = pb.driver_add("rotation_euler", 1).driver
        var = driver.variables.new()
        var.type = 'TRANSFORMS'
        var.name = "twist"
        target = var.targets[0]
        target.id, target.bone_target = rig, probe
        target.transform_space = 'LOCAL_SPACE'
        target.rotation_mode, target.transform_type = 'SWING_TWIST_Y', 'ROT_Y'
        driver.expression = f"twist * {factor:.12g}"
        if probe in neutral:
            var = driver.variables.new()
            var.name, var.type = "base", 'TRANSFORMS'
            target = var.targets[0]
            target.id, target.bone_target = rig, neutral[probe][1]
            target.transform_space = 'LOCAL_SPACE'
            target.rotation_mode, target.transform_type = 'SWING_TWIST_Y', 'ROT_Y'
            driver.expression += f"+base*{1 - factor:.12g}"
        elif probe in ankle_roll:
            read, _, sign = ankle_roll[probe]
            target.bone_target = read
            target.rotation_mode, target.transform_type = 'XYZ', 'ROT_X'
            driver.expression = f"twist*{factor * sign:.12g}"
    for name, target in followers.items():
        con = pose[name].constraints.new('COPY_TRANSFORMS')
        con.target, con.subtarget = rig, target
        con.owner_space = con.target_space = 'POSE'
        if name in bend_frames:
            center = pose[name].constraints.new('COPY_ROTATION')
            center.target, center.subtarget = rig, bend_frames[name]
            center.owner_space = center.target_space = 'POSE'
            center.influence = 0.5

    for tip in guide.data.bones:
        component = tip.get("ik_limb")
        if not component or tip.parent.name not in mid_controls:
            continue
        upper = tip.parent.parent
        mid = mid_controls[tip.parent.name]
        first = probes[upper.name, mid]
        second = probes[mid, tip.name]
        ctl = pose["IK." + tip.name]
        ctl["volume"] = 1.0
        ctl.id_properties_ui("volume").update(min=0.0, max=1.0, description="Squash/stretch volume")
        distance = pose[first[0]]
        distance["length_ratio"] = 1.0
        total = first[-1] + second[-1]
        drive(distance, '["length_ratio"]', f"(a*{first[-1]:.12g}+b*{second[-1]:.12g})/{total:.12g}",
              {"a": (distance, "SCALE_Y"), "b": (pose[second[0]], "SCALE_Y")})
        samples = limb_samples(upper.get("div0", 2), upper.get("div1", 2), upper.get("supportJoints", True))

        def coefficient(prop, i, fallback):
            profile = upper.get(prop)
            if not profile:
                return fallback * math.sin(math.pi * i / (len(samples) - 1))**2
            pos = i * (len(profile) - 1) / (len(samples) - 1)
            lo = int(pos)
            return profile[lo] + (profile[min(lo + 1, len(profile) - 1)] - profile[lo]) * (pos - lo)
        for i in range(len(samples)):
            follower = pose[followers[f"{component}_{i}_jnt"]]
            stretch = coefficient("st_profile", i, -0.5)
            squash = coefficient("sq_profile", i, 0.5)
            expr = f"max(0.0001,1+v*({stretch:.10g}*min(max(d-1,0),1)+{squash:.10g}*min(max(2*(1-d),0),1)))"
            for axis in (1, 2):  # Maya joint X is the longitudinal axis.
                drive(follower, "scale", expr, {"v": (ctl, "volume"), "d": (distance, "length_ratio")}, axis)
