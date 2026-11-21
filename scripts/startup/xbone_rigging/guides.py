# SPDX-License-Identifier: GPL-2.0-or-later
"""Read the classic Shifter guide graph before creating any Blender objects."""

import json
import math

import bpy
from mathutils import Matrix, Vector


def template_points(path, scale, up_axis):
    with open(path, encoding="utf-8") as stream:
        data = json.load(stream)
    specs = {}
    settings = {}
    components = data["components_dict"]
    for name in data["components_list"]:
        component = components[name]
        positions = component["pos"]
        params = component["param_values"]
        kind = params["comp_type"]
        kind = {
            "EPIC_arm_02": "arm_2jnt_01", "EPIC_leg_02": "leg_2jnt_01",
            "EPIC_shoulder_01": "shoulder_01", "EPIC_neck_01": "neck_ik_01",
            "EPIC_foot_01": "foot_bk_01", "EPIC_control_01": "control_01",
            "EPIC_chain_01": "chain_01",
        }.get(kind, kind)
        locators = sorted((key for key in positions if key.endswith("_loc")),
                          key=lambda key: int(key.split("_")[0]))
        chains = {
            "arm_2jnt_01": ["root", "elbow", "wrist", "eff"],
            "leg_2jnt_01": ["root", "knee", "ankle", "eff"],
            "shoulder_01": ["root", "tip"],
            "spine_ik_01": ["root", "eff"],
            "EPIC_spine_01": ["root", "spineBase", "spineTop", "chest"],
            "neck_ik_01": ["root", "neck", "head", "eff"],
            "chain_01": ["root", *locators],
            "foot_bk_01": ["root", *locators],
            "control_01": ["root"],
            "eye_01": ["root"],
            "mouth_01": ["root", "rotcenter", "jaw"],
            "meta_01": ["root"],
        }
        if kind not in chains:
            raise ValueError("Unsupported Shifter component: " + kind)
        chain = chains[kind]
        parent_component = component["parent_fullName"]
        attachment = (parent_component, component["parent_localName"]) if parent_component else None
        for key in dict.fromkeys([*chain, *positions]):
            x, y, z = positions[key]
            point = Vector((x, -z, y) if up_axis == 'Y' else (x, y, z)) * scale
            if not all(math.isfinite(v) for v in point):
                raise ValueError("Invalid guide position in " + name)
            parent = (name, "root")
            if key == "root":
                parent = attachment
            elif key in chain:
                parent = (name, chain[chain.index(key) - 1])
            elif kind == "mouth_01":
                parent = (name, "jaw" if key == "liplow" else "root")
            deform = key in chain or kind in {"meta_01", "mouth_01"}
            if kind == "control_01":
                deform = key == "root" and params.get("joint", False)
            label = name if key == "root" else name + "_" + key
            limb = name if (kind, key) in {("arm_2jnt_01", "wrist"), ("leg_2jnt_01", "ankle")} else ""
            specs[name, key] = (label, point, parent, deform, limb)
            if limb:
                settings[name, key] = {
                    "ik_default": float(params.get("blend", key == "ankle")),
                    "ik_reference": (params.get("ikrefarray") or "").split(",")[0],
                    "pole_reference": (params.get("upvrefarray") or "").split(",")[0],
                }
            if kind == "eye_01" and key == "look":
                ref = (params.get("ikrefarray") or "").split(",")[0]
                settings[name, key] = {"eye_target": True, "eye_reference": ref}
            if kind == "foot_bk_01" and key == "root":
                settings[name, key] = {"foot_root": True}
    if not specs or any(parent and parent not in specs for _, _, parent, _, _ in specs.values()):
        raise ValueError("Incomplete guide hierarchy")
    # Reject cyclic templates before entering edit mode.
    resolved = set()
    for key in specs:
        path = set()
        while key and key not in resolved:
            if key in path:
                raise ValueError("Cyclic guide hierarchy")
            path.add(key)
            key = specs[key][2]
        resolved.update(path)
    buffers = data.get("ctl_buffers_dict") or {}
    shapes = {}
    for key in specs:
        name, locator = key
        params = components[name]["param_values"]
        prefix = params["comp_name"] + "_" + params["comp_side"].lower()
        slots = {}
        if locator == "root":
            slots["FK"] = [name + "_ctl", prefix + "_control"]
        if "arm_" in params["comp_type"] or "leg_" in params["comp_type"]:
            indices = {"root": 0, "elbow": 1, "knee": 1, "wrist": 2, "ankle": 2}
            if locator in indices:
                slots.setdefault("FK", []).append(prefix + "_fk" + str(indices[locator]))
            if locator in {"wrist", "ankle"}:
                slots["IK"] = [prefix + "_ik"]
            if locator in {"elbow", "knee"}:
                slots["Pole"] = [prefix + "_upv"]
        for role, candidates in slots.items():
            for candidate in candidates:
                buffer = buffers.get(candidate + "_controlBuffer")
                if not buffer:
                    continue
                basis = Matrix(buffer["crv_transform"]).transposed().to_3x3()
                # Some EPIC buffers were recorded in a Z-up rig within a Y-up guide.
                origin = Vector(buffer["crv_transform"][3][:3]) * scale
                converted = Vector((origin.x, -origin.z, origin.y))
                point = specs[key][1]
                if up_axis == 'Y' and (not origin.length or (converted - point).length < (origin - point).length):
                    basis = Matrix.Rotation(math.pi / 2, 3, 'X') @ basis
                curves = []
                for shape in buffer["shapes"].values():
                    points = [basis @ Vector(co) * scale for co in shape["points"]]
                    if len(points) >= 2:
                        curves.append((points, int(shape["degree"]), shape["form"] != "open"))
                if curves:
                    shapes[key, role] = curves
                break
    return specs, shapes, settings


def new_guide(context, name):
    bpy.ops.object.xbone_add(enter_editmode=True)
    obj = context.edit_object
    obj.name = name
    obj.data.edit_bones.remove(obj.data.edit_bones.active)
    obj["rigging_role"] = "GUIDE"
    obj.show_in_front = True
    return obj


def joint(edit, name, point, parent=None, length=0.1):
    bone = edit.new(name)
    bone.head = point
    bone.tail = Vector(point) + Vector((0, length, 0))
    bone.parent = parent
    return bone


def import_template(context, template, name):
    specs, shapes, settings = template
    obj = new_guide(context, name)
    edit = obj.data.edit_bones
    names = {}
    span = max(p.z for _, p, *_ in specs.values()) - min(p.z for _, p, *_ in specs.values())
    for key, (label, point, _, deform, limb) in specs.items():
        bone = joint(edit, label, point, length=max(0.01, span * 0.018))
        bone.use_deform = deform
        names[key] = bone.name
        if limb:
            bone["ik_limb"] = limb
    references = {component + "_" + locator: actual for (component, locator), actual in names.items()}
    for key, properties in settings.items():
        for prop, value in properties.items():
            edit[names[key]][prop] = references.get(value, "") if prop.endswith("_reference") else value
    for key, (_, _, parent, _, _) in specs.items():
        if parent:
            edit[names[key]].parent = edit[names[parent]]
    # Orient local Y toward the next joint while preserving every joint pivot.
    for bone in edit:
        children = [child for child in bone.children if (child.head - bone.head).length > 0.0001]
        if children:
            bone.tail = children[0].head
    bpy.ops.object.mode_set(mode='OBJECT')
    for (key, role), curves in shapes.items():
        bone = obj.data.bones[names[key]]
        inverse = bone.matrix_local.to_3x3().inverted()
        curve = bpy.data.curves.new(bone.name + "." + role, 'CURVE')
        curve.dimensions = '3D'
        for coords, degree, cyclic in curves:
            spline = curve.splines.new('POLY' if degree == 1 else 'NURBS')
            spline.points.add(len(coords) - 1)
            for pt, co in zip(spline.points, coords):
                pt.co = (*(inverse @ co), 1.0)
            if spline.type == 'NURBS':
                spline.order_u = min(6, len(coords), degree + 1)
                spline.use_endpoint_u = not cyclic
            spline.use_cyclic_u = cyclic
        bone["widget_" + role] = bpy.data.objects.new(curve.name, curve)
    return obj
