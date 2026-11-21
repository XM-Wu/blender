# SPDX-License-Identifier: GPL-2.0-or-later
"""Shifter guide curves sampled from Maya 2027. The overlay resolves live edit pivots."""
from array import array
import json
from pathlib import Path

from mathutils import Matrix, Vector


def install(obj, names, preset, scale, up):
    with Path(__file__).with_name("guide_display.json").open() as stream:
        source = json.load(stream)
    reference = source["templates"][preset]
    nodes = reference["nodes"]
    conversion = Matrix.Rotation(1.5707963267948966, 3, 'X') if up == 'Y' else Matrix.Identity(3)
    ids = {}
    glyphs = {}
    auxiliary = []

    def position(node):
        return conversion @ Vector(nodes[node][1][12:15]) * scale

    def reference_axes(bone):
        frame = bone.get("biped_frame")
        matrix = Matrix([frame[i:i + 4] for i in range(0, 16, 4)]) if frame else bone.matrix_local
        return matrix.to_3x3().inverted_safe()

    def curves(node, axes, offset):
        index, values, _ = nodes[node]
        rotation = Matrix([values[i:i + 4] for i in range(0, 16, 4)]).transposed().to_3x3()
        transform = axes @ conversion @ rotation
        packed = array('f')
        for color, points in source["shapes"][index]:
            packed.extend((color, len(points)))
            for point in points:
                packed.extend(offset + transform @ Vector(point) * scale)
        return packed

    for (component, loc), actual in names.items():
        key = component + "_" + loc
        bone = obj.data.bones[actual]
        index = len(ids)
        ids[key] = index
        bone["shifter_guide_id"] = index
        if key in nodes:
            axes = reference_axes(bone)
            glyphs[str(index)] = curves(key, axes, Vector((0, 0, 0)))
        elif loc == "sizeRef":
            # Maya stores this build reference as a hidden transform.
            glyphs[str(index)] = array('f', [0, 0])

    for key, (_, _, parent) in nodes.items():
        if key in ids or parent not in ids:
            continue
        component, kind = key.rsplit("_", 1)
        root = component + "_root"
        if kind == "blade":
            target = next((component + "_" + n for n in ("tan0", "0_loc", "tip", "eff")
                           if component + "_" + n in ids), None)
            if not target or root not in ids:
                continue
            anchor = obj.data.bones[names[component, "root"]]
            axes = reference_axes(anchor)
            direction = axes @ (position(target) - position(root)).normalized()
            record = [1, len(ids), ids[root], ids[target], 0, 0, *direction, 0]
            offset = axes @ (position(key) - position(root))
        elif kind == "upv":
            is_arm = component.startswith("arm_")
            mid = "elbow" if is_arm else "knee"
            end = component + ("_wrist" if is_arm else "_ankle")
            eff = component + "_eff"
            middle = component + "_" + mid
            if not all(n in ids for n in (root, middle, end, eff)):
                continue
            anchor = obj.data.bones[names[component, mid]]
            axes = reference_axes(anchor)
            delta = position(key) - position(middle)
            length = max((position(n) - position(root)).length for n in (middle, end, eff))
            record = [2, len(ids), ids[middle], ids[end], ids[root], ids[eff],
                      *(axes @ delta.normalized()), delta.length / max(length, 1e-8)]
            offset = Vector((0, 0, 0))
        else:
            continue
        auxiliary.extend(record)
        ids[key] = len(ids)
        glyphs[str(ids[key])] = curves(key, axes, offset)

    paths = []
    for degree, controls in reference["paths"]:
        if all(key in ids for key in controls):
            paths.extend((degree, len(controls), *(ids[key] for key in controls)))
    obj["shifter_guide_shapes"] = glyphs
    obj["shifter_guide_ids"] = {actual: ids[component + "_" + loc] for (component, loc), actual in names.items()}
    obj["shifter_guide_paths"] = array('i', paths)
    obj["shifter_guide_aux"] = array('f', auxiliary)
    obj["shifter_guide_radius"] = scale * 0.125
    obj["show_shifter_guide"] = True


def ensure(obj):
    if "show_shifter_guide" in obj:
        return
    if "biped_display_scale" in obj:
        names = {}
        with Path(__file__).with_name("biped.sgt").open(encoding="utf8") as stream:
            components = json.load(stream)["components_dict"]
        by_label = {bone.get("biped_locator", bone.name): bone.name for bone in obj.data.bones}
        for component, guide in components.items():
            for loc in guide["pos"]:
                label = component if loc == "root" else component + "_" + loc
                if label in by_label:
                    names[component, loc] = by_label[label]
        install(obj, names, "biped", obj["biped_display_scale"], obj["biped_display_up"])
    else:
        obj["show_shifter_guide"] = True
