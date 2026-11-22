# SPDX-License-Identifier: GPL-2.0-or-later
"""Build the Shifter Biped skin hierarchy separately from the pose solver."""
import bisect
import json
import math
from pathlib import Path

import bpy
from mathutils import Matrix, Quaternion, Vector


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


def attachment_joint(parent, output):
    """Find the skin output of the selected guide component, even on a renamed guide."""
    for anchor in (parent, *parent.parent_recursive):
        label = anchor.get("biped_locator")
        if not label:
            continue
        kind, side, *rest = label.split("_", 2)
        component = kind + "_" + side + "_"
        locator = rest[0] if rest else "root"
        candidates = [name for name in output if name.startswith(component)]
        if not candidates:
            continue
        if kind in {"arm", "leg"} and locator in {"wrist", "ankle", "eff"}:
            return component + "end_jnt"
        if kind == "neck" and locator in {"head", "eff"}:
            return component + "head_jnt"
        if kind == "eye":
            return component + "eye_jnt"
        # Division counts and edited guides change the set of available joints.
        # Measure from the attachment locator, not from the offset branch.
        return min(candidates, key=lambda name: (output[name][1].translation - anchor.head_local).length_squared)
    # Global/body control components have no skin output of their own.
    return "spine_C0_0_jnt"


def build(rig, guide, output, skeleton, mechanism, fk):
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

    followers = {}
    for name, (_, rest, anchor, end, _) in output.items():
        joint(name, rest, skeleton)
        # Roll-spline meshes supply the final transform of each limb division.
        if end and name.startswith(("arm_", "leg_")):
            continue
        followers[name] = joint("MCH.Bind." + name, rest, mechanism, edit[anchor])
    for name, (parent, *_) in output.items():
        if parent:
            edit[name].parent = edit[parent]
    custom_links = []
    for source in guide.data.bones:
        if "biped_locator" in source or source.name not in fk or not source.use_deform or not source.parent:
            continue
        parent = source.parent.name
        if "biped_locator" in source.parent:
            parent = attachment_joint(source.parent, output)
            # The deformation bone and its FK input must inherit the same rest
            # offset. XBone draws between joint pivots; keep the authored offset.
            for n in (source.name, fk[source.name], "MCH." + source.name, "MCH.Reach." + source.name):
                b = edit.get(n)
                if b and b.parent == edit[source.parent.name]:
                    b.use_connect = False
                    b.parent = edit[parent]
        # Draw the attachment also when the skin skeleton is hidden. Using
        # the solved pivots keeps this line correct for animated translations.
        offset = edit[source.name].head - edit[parent].head
        frame = offset.to_track_quat('Y', 'Z').to_matrix().to_4x4()
        frame.translation = edit[parent].head
        link = joint("Display.Link." + source.name, frame, mechanism)
        mechanism.unassign(edit[link])
        for collection in edit[fk[source.name]].collections:
            collection.assign(edit[link])
        edit[link].hide_select = True
        custom_links.append((link, parent, source.name, fk[source.name], max(offset.length, 1e-6)))
    bpy.ops.object.mode_set(mode='POSE')
    for name, target in followers.items():
        follow = rig.pose.bones[name].constraints.new('COPY_TRANSFORMS')
        follow.target, follow.subtarget = rig, target
        follow.owner_space = follow.target_space = 'POSE'
    if custom_links:
        from . import biped_display
        widget = biped_display.shape(rig.name + ".FK Link", [[(0, 0, 0), (0, 1, 0)]])
        for name, start, end, control, length in custom_links:
            link = rig.pose.bones[name]
            biped_display.configure(link, widget, "link")
            link.custom_shape_scale_xyz = (1, length, 1)
            origin = link.constraints.new('COPY_LOCATION')
            origin.target, origin.subtarget = rig, start
            aim = link.constraints.new('STRETCH_TO')
            aim.target, aim.subtarget = rig, end
            aim.rest_length, aim.volume = length, 'NO_VOLUME'
            driver = link.driver_add("hide").driver
            driver.type = 'AVERAGE'
            var = driver.variables.new()
            var.targets[0].id = rig
            var.targets[0].data_path = rig.pose.bones[control].path_from_id("hide")
