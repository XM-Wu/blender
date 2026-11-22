# SPDX-License-Identifier: GPL-2.0-or-later
"""Maya 2027 Shifter rest curves, expressed in editable XBone guide frames."""
import json
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

# Display sRGB from the supplied Maya viewport. Keys are Maya override color indices.
PALETTE = {6: "0000ff", 13: "ff0100", 14: "00ff10", 17: "fdff88", 18: "64dcff", 23: "009954",
           "link": "787878"}


def xyz(value, scale, up):
    x, y, z = value
    return Vector((x, -z, y) if up == 'Y' else (x, y, z)) * scale


def shape(name, lines):
    verts, edges = [], []
    for line in lines:
        start = len(verts)
        verts.extend(line)
        edges.extend((i - 1, i) for i in range(start + 1, len(verts)))
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, edges, [])
    mesh.update()
    return bpy.data.objects.new(name, mesh)


def configure(pose_bone, widget, color):
    pose_bone.custom_shape = widget
    pose_bone.use_custom_shape_bone_size = False
    pose_bone.custom_shape_scale_xyz = (1, 1, 1)
    palette = pose_bone.bone.color
    palette.palette = 'CUSTOM'
    rgb = bytes.fromhex(PALETTE[color])
    # Blender darkens unselected custom-bone wires by 50 sRGB byte values.
    # Fit the display palette to the available range without clipping its hues.
    gain = min(1, 205 / max(rgb))
    palette.custom.normal = tuple((50 + channel * gain) / 255 for channel in rgb)
    palette.custom.select = (0.55, 1.0, 0.55)
    palette.custom.active = (1, 1, 1)


def apply(rig, guide, fk):
    scale = guide["biped_display_scale"]
    up = guide["biped_display_up"]
    with Path(__file__).with_name("biped_display.json").open(encoding="utf-8") as stream:
        source = json.load(stream)
    from .biped_skeleton import read_frame
    with Path(__file__).with_name("biped_pivots.json").open() as stream:
        pivots = json.load(stream)
    basis = Matrix.Rotation(1.5707963267948966, 3, 'X') if up == 'Y' else Matrix.Identity(3)
    arm = rig.data
    # Resolve by stored guide identity so renaming a joint before building is supported.
    bones = {b["biped_locator"]: b for b in arm.bones if "biped_locator" in b}
    transforms = {}
    for label, bone in bones.items():
        original = Matrix([bone["biped_frame"][i:i + 4] for i in range(0, 16, 4)])
        stretch = Matrix.Diagonal((1, bone.length / bone["biped_length"], 1, 1))
        transforms[label] = bone.matrix_local @ stretch @ original.inverted_safe()

    def point(label, value):
        return transforms[label] @ xyz(value, scale, up)

    widgets = {}
    control_names = {p.name for p in rig.pose.bones if p.custom_shape}
    overlays = arm.collections.new("Biped Display")
    extra = arm.collections.new("Additional FK")
    anchors = {label: bone.name for label, bone in bones.items()}
    bpy.ops.object.mode_set(mode='EDIT')
    edit = arm.edit_bones

    def helper(name, parent, head, tail=None):
        bone = edit.new("Display." + name)
        bone.head = head
        bone.tail = tail if tail is not None else head + Vector((0, scale / 2, 0))
        bone.parent = edit[parent]
        bone.use_deform = False
        bone.hide_select = True
        for collection in list(bone.collections):
            collection.unassign(bone)
        overlays.assign(bone)
        return bone.name

    links = []
    fk_outputs = {}
    for component, position in source["poles"].items():
        mid = component + ("_knee" if component.startswith("leg_") else "_elbow")
        if mid not in anchors:
            continue
        joint = anchors[mid]
        pole = edit.get("Pole." + joint)
        if pole is None:
            continue
        pole.translate(point(mid, position) - pole.head)
        length = (edit[joint].head - pole.head).length
        if length > 0.00001:
            name = helper(component + ".pole", pole.name, pole.head, edit[joint].head)
            links.append((name, joint, length))

    for name, data in source["controls"].items():
        label = data["anchor"]
        if label not in anchors:
            continue
        owner = anchors[label]
        role = data["role"]
        if role == "FK":
            target = fk.get(owner)
            if target and name in pivots and name.split("_")[0] in {
                    "finger", "thumb", "tongue", "foot", "meta", "mouth", "thumbRoll", "eye", "eyeslook",
                    "spine", "neck", "global", "local", "body", "shoulder", "leg", "arm"}:
                original = edit[target].matrix.copy()
                edit[target].matrix = transforms[label] @ read_frame(pivots[name], scale, basis)
                ref = edit.new("MCH.FKOutput." + owner)
                ref.head = original.translation
                ref.tail = ref.head + Vector((0, scale / 2, 0))
                ref.matrix = original
                ref.parent = edit[target]
                ref.use_deform = False
                for group in list(ref.collections):
                    group.unassign(ref)
                arm.collections["Mechanism"].assign(ref)
                fk_outputs[owner] = ref.name
        elif role == "display":
            target = helper(name, owner, edit[owner].head)
            if name in pivots:
                edit[target].matrix = transforms[label] @ read_frame(pivots[name], scale, basis)
        else:
            target = role + "." + owner
            if target in edit and name in pivots:
                edit[target].matrix = transforms[label] @ read_frame(pivots[name], scale, basis)
        if not target or target not in edit:
            continue
        frame = edit[target].matrix.inverted_safe()
        lines, _ = widgets.setdefault(target, ([], data["color"]))
        for line in data["lines"]:
            lines.append([frame @ point(label, p) for p in line])

    for n in control_names - widgets.keys():
        for collection in list(edit[n].collections):
            collection.unassign(edit[n])
        extra.assign(edit[n])
    bpy.ops.object.mode_set(mode='POSE')
    pose = rig.pose.bones
    for owner, ref in fk_outputs.items():
        follow = pose[owner].constraints.get("Copy Transforms")
        if follow:
            follow.subtarget = ref
            follow.owner_space = follow.target_space = 'POSE'
    for target, (lines, color) in widgets.items():
        configure(pose[target], shape(target, lines), color)
    for name, joint, length in links:
        configure(pose[name], shape(name, [[(0, 0, 0), (0, length, 0)]]), "link")
        constraint = pose[name].constraints.new('STRETCH_TO')
        constraint.target, constraint.subtarget = rig, joint
        constraint.rest_length, constraint.volume = length, 'NO_VOLUME'
        pole = pose["Pole." + joint]
        driver = pose[name].driver_add("hide").driver
        driver.type = 'AVERAGE'
        var = driver.variables.new()
        var.targets[0].id = rig
        var.targets[0].data_path = pole.path_from_id("hide")
    extra.is_visible = False
    return {name: arm.bones[name].matrix_local.copy() for name in widgets}
