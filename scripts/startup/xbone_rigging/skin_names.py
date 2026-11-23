# SPDX-License-Identifier: GPL-2.0-or-later
"""Anatomical names for XBiped's deform outputs. Solver and guide IDs remain stable."""

import json

import bpy


DIGITS = ("index", "middle", "ring", "pinky")
SCHEME = "xbiped_anatomical_names"


def limb_names(kind, upper, lower, supports):
    proximal, bend, distal = ("thigh", "knee", "calf") if kind == "leg" else ("upperarm", "elbow", "lowerarm")
    names = [proximal, *(f"{proximal}_roll_{i:02}" for i in range(1, upper + 1))]
    if supports:
        names.extend((bend + "_support_upper", bend, bend + "_support_lower"))
    else:
        names.append(bend)
    return [*names, distal, *(f"{distal}_roll_{i:02}" for i in range(1, lower + 1))]


def name_map(arm):
    locators = {b["biped_locator"]: b for b in arm.bones if "biped_locator" in b}
    names = {}
    for side in "LR":
        suffix = "_" + side.lower()
        for kind, end in (("leg", "foot"), ("arm", "hand")):
            comp = f"{kind}_{side}0"
            root = locators[comp]
            segments = limb_names(kind, root.get("div0", 2), root.get("div1", 2), root.get("supportJoints", True))
            for i, part in enumerate(segments):
                names[f"{comp}_{i}_jnt"] = part + suffix
            names[f"{comp}_end_jnt"] = end + suffix
        names[f"shoulder_{side}0_shoulder_jnt"] = "clavicle" + suffix
        names[f"foot_{side}0_0_jnt"] = "ball" + suffix
        names[f"foot_{side}0_1_jnt"] = "toe" + suffix
        for i, digit in enumerate(DIGITS):
            names[f"meta_{side}0_{i}_jnt"] = digit + "_metacarpal" + suffix
        for i, digit in enumerate((*DIGITS, "thumb")):
            comp = f"finger_{side}{i}" if digit != "thumb" else f"thumb_{side}0"
            for j in range(3):
                names[f"{comp}_{j}_jnt"] = f"{digit}_{j + 1:02}" + suffix
        names[f"eye_{side}0_eye_jnt"] = "eye" + suffix
        names[f"eye_{side}0_eyeOver_jnt"] = "eye_socket" + suffix
    for kind, default in (("spine", 5), ("neck", 3)):
        comp = kind + "_C0"
        for i in range(locators[comp].get("division", default)):
            part = ("pelvis" if i == 0 else f"spine_{i:02}") if kind == "spine" else f"neck_{i + 1:02}"
            names[f"{comp}_{i}_jnt"] = part
    names["neck_C0_head_jnt"] = "head"
    for old, new in (("jaw", "jaw"), ("liplow", "lip_lower"), ("lipup", "lip_upper"),
                     ("teethlow", "teeth_lower"), ("teethup", "teeth_upper")):
        names[f"mouth_C0_{old}_jnt"] = new
    for i in range(4):
        names[f"tongue_C0_{i}_jnt"] = f"tongue_{i+1:02}"
    return names


def apply(rig):
    bones = rig.data.bones
    names = {old: new for old, new in name_map(rig.data).items() if old in bones and bones[old].use_deform}
    for new in names.values():
        if new in bones:
            raise ValueError(f"Bone name already in use: {new}")
    # Blender's bone rename updates bound vertex groups, constraints, drivers and Actions.
    # Check every destination before the first rename: a colliding group would otherwise
    # start driving the mesh even when it previously had no matching bone.
    if any(o != rig and o.data == rig.data for o in bpy.data.objects):
        raise ValueError("Make the shared armature data single-user before renaming")
    bound = [o for o in bpy.data.objects if o.type in {'MESH', 'LATTICE', 'GREASEPENCIL'} and
             (o.find_armature() == rig or any(m.type in {'ARMATURE', 'GREASE_PENCIL_ARMATURE'} and
                                              m.object == rig for m in o.modifiers))]
    for obj in bound:
        if not obj.is_editable or not obj.data.is_editable:
            raise ValueError(f"Bound geometry is read-only: {obj.name}")
        collision = next((n for n in names.values() if n in obj.vertex_groups), None)
        if collision:
            raise ValueError(f"{obj.name} already has a vertex group named {collision}")
        if obj.data.users > 1 and any(o.data == obj.data and o not in bound for o in bpy.data.objects):
            raise ValueError(f"{obj.name} shares its vertex groups with geometry outside this rig")
    manifest = json.loads(rig["rigging_generated"]) if "rigging_generated" in rig else None
    for old, new in names.items():
        bone = bones[old]
        bone["xbiped_legacy_name"] = old
        bone.name = new
    if manifest:
        manifest["constraints"] = {names.get(n, n): value for n, value in manifest["constraints"].items()}
        for entry in manifest["drivers"]:
            for old, new in names.items():
                entry[0] = entry[0].replace(f'pose.bones["{old}"]', f'pose.bones["{new}"]')
        rig["rigging_generated"] = json.dumps(manifest)
    for scene in bpy.data.scenes:
        for view_layer in scene.view_layers:
            session = view_layer.skin_pose_session
            if session.rig == rig and session.group_name in names:
                session.group_name = names[session.group_name]
    rig.data[SCHEME] = True
    return len(names)
