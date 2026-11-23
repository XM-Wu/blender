# SPDX-License-Identifier: GPL-2.0-or-later
"""Fit a Shifter XBiped guide to freely positioned landmarks in the character's reference pose."""
import json
import math
from copy import copy
from functools import lru_cache
from pathlib import Path

import bpy
from bpy.app.handlers import persistent
from bpy.app.translations import pgettext_iface as iface_
from bpy.props import BoolProperty, EnumProperty, PointerProperty
from bpy.types import Operator, Panel
from mathutils import Matrix, Vector

from . import guides, rebuild, xbiped_limb


CENTER = (("pelvis", "Pelvis"), ("neck", "Base of Neck"), ("head", "Base of Head"), ("crown", "Top of Head"))
SIDE = (("shoulder", "Shoulder"), ("elbow", "Elbow"), ("wrist", "Wrist"), ("hand", "Middle Fingertip"),
        ("hip", "Hip Joint"), ("knee", "Knee"), ("ankle", "Ankle"), ("toe", "Toe Tip"))
FACING = {'NEG_Y': 0, 'POS_X': 90, 'POS_Y': 180, 'NEG_X': -90}
DIGITS = (("thumb", "Thumb", "thumb_{}0"), ("index", "Index Finger", "finger_{}0"),
          ("middle", "Middle Finger", "finger_{}1"), ("ring", "Ring Finger", "finger_{}2"),
          ("little", "Little Finger", "finger_{}3"))
DIGIT_LOCS = ("root", "0_loc", "1_loc", "2_loc")
FACE = (("mouth", "Mouth Base", "root"), ("mouth_pivot", "Mouth Rotation Center", "rotcenter"),
        ("jaw", "Jaw", "jaw"), ("upper_lip", "Upper Lip", "lipup"), ("lower_lip", "Lower Lip", "liplow"))


def digit_key(digit, i, side):
    return ("hand" if digit == "middle" and i == 3 else f"{digit}_{i}") + "." + side


def steps(root):
    sides = ('L',) if root["symmetric"] else ('L', 'R')
    order = ([(key, iface_(label)) for key, label in CENTER] +
            [(key + "." + side, iface_(label) + "." + side) for side in sides for key, label in SIDE])
    for detail in root.get("detail_order", "").split():
        if detail == 'FINGERS':
            for side in sides:
                for digit, label, _ in DIGITS:
                    for i, joint in enumerate(("Base Joint", "Second Joint", "Third Joint", "Fingertip")):
                        if digit != "middle" or i != 3:
                            order.append((digit_key(digit, i, side), f"{iface_(label)} · {iface_(joint)}.{side}"))
        elif detail == 'FACE':
            for side in sides:
                order.extend([(f"eye.{side}", iface_("Eye Center") + "." + side),
                              (f"look.{side}", iface_("Eye Aim Target") + "." + side)])
            order.extend((key, iface_(label)) for key, label, _ in FACE)
    return order


def markers(root):
    return {o["xbone_landmark"]: o for o in root.children if "xbone_landmark" in o}


def current_step(root, active=None):
    order = steps(root)
    if active and active.parent == root and "xbone_landmark" in active:
        for i, (key, _) in enumerate(order):
            if key == active["xbone_landmark"]:
                return i
    placed = markers(root)
    missing = next((i for i, (key, _) in enumerate(order) if key not in placed), len(order))
    # Saved sessions store the one-based number of the last visited marker.
    return min(root["step"], missing) - 1


def next_step(root, index):
    placed = markers(root)
    return next((i for i, (key, _) in enumerate(steps(root)) if i > index or key not in placed), None)


@lru_cache(maxsize=1)
def landmark_keys():
    keys = {"pelvis": ("spine_C0", "root"), "neck": ("neck_C0", "root"),
            "head": ("neck_C0", "head"), "crown": ("neck_C0", "eff")}
    for side in ("L", "R"):
        arm, leg = f"arm_{side}0", f"leg_{side}0"
        for name, component, locator in (
                ("shoulder", arm, "root"), ("elbow", arm, "elbow"), ("wrist", arm, "wrist"),
                ("hand", f"finger_{side}1", "2_loc"), ("hip", leg, "root"), ("knee", leg, "knee"),
                ("ankle", leg, "ankle"), ("toe", f"foot_{side}0", "2_loc")):
            keys[f"{name}.{side}"] = (component, locator)
        for digit, _, component in DIGITS:
            for i, loc in enumerate(DIGIT_LOCS):
                keys[digit_key(digit, i, side)] = (component.format(side), loc)
        keys[f"eye.{side}"] = (f"eye_{side}0", "root")
        keys[f"look.{side}"] = (f"eye_{side}0", "look")
    keys.update({key: ("mouth_C0", loc) for key, _, loc in FACE})
    return keys


@lru_cache(maxsize=1)
def default_positions():
    specs, *_ = guides.template_points(Path(__file__).with_name("biped.sgt"), 0.1, 'Y')
    keys = landmark_keys()
    return {name: specs[key][1].copy() for name, key in keys.items()}


def select_marker(context, marker):
    root = marker.parent
    if root.get("isolate_marker"):
        set_isolation(root, marker, True)
    select_object(context, marker)


def select_object(context, obj):
    for selected in context.selected_objects:
        selected.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj


def set_isolation(root, current, enabled):
    for obj in root.children:
        if "xbone_landmark" in obj or "xbone_mirrored_landmark" in obj:
            obj.hide_viewport = enabled and obj != current
    root["isolate_marker"] = enabled


def suggested_position(root, key, defaults, placed):
    component = landmark_keys()[key][0]
    if component.startswith(("finger_", "thumb_")) and not key.startswith("hand."):
        a, b = "wrist." + key[-1], "hand." + key[-1]
    elif component.startswith(("eye_", "mouth_")):
        a, b = "head", "crown"
    elif root.get("editing_guide"):
        return defaults[key]
    else:
        return defaults[key] * (root["height"] / defaults["crown"].z)
    frame = Segment(defaults[a], defaults[b], placed[a].location, placed[b].location)
    return frame.point(defaults[key])


def place_default(root, index, position=None):
    order = steps(root)
    key, label = order[index]
    placed = markers(root)
    marker = placed.get(key)
    if marker is None:
        if position is None:
            guide = root.get("editing_guide")
            defaults = guide_positions(guide) if guide else default_positions()
            position = suggested_position(root, key, defaults, placed)
        marker = bpy.data.objects.new(label, None)
        root.users_collection[0].objects.link(marker)
        marker.parent = root
        marker.location = position
        marker["xbone_landmark"] = key
        marker.empty_display_type = 'SPHERE'
        body_steps = len(CENTER) + len(SIDE) * (1 if root["symmetric"] else 2)
        marker.empty_display_size = root["height"] * (.004 if index >= body_steps else .009)
        marker.show_in_front = marker.show_name = True
        marker.hide_render = True
        if key == "pelvis":
            for child in root.children:
                if child.get("xbone_mirrored_landmark") and child.animation_data:
                    curve = child.animation_data.drivers.find("location", index=0)
                    if curve:
                        curve.driver.variables["center"].targets[0].id = marker
        if root["symmetric"] and key.endswith(".L"):
            make_mirror_preview(root, marker, label)
    root["step"] = index + 1
    return marker


def make_mirror_preview(root, marker, label):
    key = marker["xbone_landmark"]
    for old in list(root.children):
        if old.get("xbone_mirrored_landmark") == key:
            bpy.data.objects.remove(old, do_unlink=True)
    twin = bpy.data.objects.new(label[:-2] + ".R", None)
    root.users_collection[0].objects.link(twin)
    twin.parent = root
    twin["xbone_mirrored_landmark"] = key
    twin.empty_display_type, twin.empty_display_size = 'SPHERE', marker.empty_display_size
    twin.show_in_front, twin.hide_select, twin.hide_render = True, True, True
    for axis in range(3):
        driver = twin.driver_add("location", axis).driver
        var = driver.variables.new()
        var.name = "p"
        var.targets[0].id, var.targets[0].data_path = marker, f"location[{axis}]"
        if axis == 0:
            mid = driver.variables.new()
            mid.name = "center"
            mid.targets[0].id, mid.targets[0].data_path = markers(root)["pelvis"], "location[0]"
            driver.expression = "2*center-p"
        else:
            driver.expression = "p"


class Segment:
    def __init__(self, a, b, start, end):
        old, new = b - a, end - start
        if min(old.length, new.length) < 1e-6:
            raise ValueError("Adjacent joint markers must be separated")
        self.rotation = old.rotation_difference(new)
        self.scale = new.length / old.length
        self.offset = start - self.rotation @ a * self.scale

    def point(self, p):
        return self.offset + self.rotation @ p * self.scale

    def anchored(self, source, target):
        frame = copy(self)
        frame.offset = target - self.rotation @ source * self.scale
        return frame


def landmark_points(root, positions=None, symmetric=None):
    if symmetric is None:
        symmetric = root["symmetric"]
    order = steps({"symmetric": symmetric, "detail_order": root.get("detail_order", "")})
    pts = positions if positions is not None else {k: obj.location.copy() for k, obj in markers(root).items()}
    if any(k not in pts for k, _ in order):
        raise ValueError("Place all required markers first")
    pts = {k: pts[k] for k, _ in order}
    if symmetric:
        for key, _ in order:
            if key.endswith(".L"):
                pts[key[:-1] + "R"] = reflect_point(pts[key], pts["pelvis"])
    return pts


def reflect_point(point, pelvis):
    return Vector((2 * pelvis.x - point.x, point.y, point.z))


def fitted_template(root, positions=None, world=None, *, symmetric=None):
    pts = landmark_points(root, positions, symmetric)
    bottom = min(pts["toe.L"].z, pts["toe.R"].z)
    height = pts["crown"].z - bottom
    if not bottom < pts["pelvis"].z < pts["neck"].z < pts["head"].z < pts["crown"].z:
        raise ValueError("Check the vertical order of the toe, pelvis, neck and head markers")
    if any(pts[key + ".L"].x <= pts[key + ".R"].x for key in ("hip", "shoulder")):
        raise ValueError("Place the left shoulder and hip on the character's left (right in the front view)")
    path = Path(__file__).with_name("biped.sgt")
    # Read the source height in Maya units so all widgets scale with the character.
    with path.open(encoding="utf8") as stream:
        template_height = json.load(stream)["components_dict"]["neck_C0"]["pos"]["eff"][1]
    scale = height / template_height
    specs, shapes, settings, _, display = guides.template_points(path, scale, 'Y')
    ref = {key: entry[1] for key, entry in specs.items()}
    origin = Vector((pts["pelvis"].x, pts["pelvis"].y - ref["spine_C0", "root"].y, bottom))
    pts = {key: value - origin for key, value in pts.items()}
    torso = Segment(ref["spine_C0", "root"], ref["neck_C0", "root"], pts["pelvis"], pts["neck"])
    neck = Segment(ref["neck_C0", "root"], ref["neck_C0", "head"], pts["neck"], pts["head"])
    head = Segment(ref["neck_C0", "head"], ref["neck_C0", "eff"], pts["head"], pts["crown"])
    transforms = {}
    def limb(component, side, start, middle, end):
        a, b, c = (pts[n + "." + side] for n in (start, middle, end))
        axis = (c - a).normalized()
        if (c - a).length < 1e-6 or min((a - b).length, (c - b).length) < 1e-6:
            raise ValueError("Overlapping markers: " + component)
        # A perfectly straight reference limb still needs a stable IK bend plane.
        if (b - a - axis * (b - a).dot(axis)).length < (c - a).length * 1e-5:
            ra, rb, rc = (ref[component, n] for n in ("root", middle, end))
            rest = (rc - ra).normalized()
            bend = rb - ra - rest * (rb - ra).dot(rest)
            b = b + rest.rotation_difference(axis) @ bend.normalized() * ((c - a).length * .005)
        transforms[component, "upper"] = Segment(ref[component, "root"], ref[component, middle], a, b)
        transforms[component, "lower"] = Segment(ref[component, middle], ref[component, end], b, c)

    for side in ("L", "R"):
        arm, leg = "arm_" + side + "0", "leg_" + side + "0"
        foot, clavicle = "foot_" + side + "0", "shoulder_" + side + "0"
        limb(arm, side, "shoulder", "elbow", "wrist")
        limb(leg, side, "hip", "knee", "ankle")
        transforms[side, "hand"] = Segment(ref[arm, "wrist"], ref["finger_" + side + "1", "2_loc"],
                                           pts["wrist." + side], pts["hand." + side])
        transforms[side, "foot"] = Segment(ref[foot, "root"], ref[foot, "2_loc"],
                                           pts["ankle." + side], pts["toe." + side])
        transforms[side, "clavicle"] = Segment(ref[clavicle, "root"], ref[clavicle, "tip"],
                                               torso.point(ref[clavicle, "root"]), pts["shoulder." + side])
        if "thumb_0." + side in pts:
            for digit, _, pattern in DIGITS:
                component = pattern.format(side)
                for i in range(3):
                    a, b = DIGIT_LOCS[i:i + 2]
                    frame = Segment(ref[component, a], ref[component, b],
                                    pts[digit_key(digit, i, side)], pts[digit_key(digit, i + 1, side)])
                    transforms[component, a] = frame
                transforms[component, b] = frame
                if digit == "thumb":
                    for loc in ("root", "sizeRef"):
                        transforms[f"thumbRoll_{side}0", loc] = transforms[component, "root"]
        if "eye." + side in pts:
            eye = f"eye_{side}0"
            frame = Segment(ref[eye, "root"], ref[eye, "look"], pts["eye." + side], pts["look." + side])
            transforms[eye, "root"] = transforms[eye, "look"] = frame
    if "jaw" in pts:
        jaw = Segment(ref["mouth_C0", "rotcenter"], ref["mouth_C0", "jaw"], pts["mouth_pivot"], pts["jaw"])
        for key, _, loc in FACE:
            transforms["mouth_C0", loc] = jaw.anchored(ref["mouth_C0", loc], pts[key])
        rest_aim = (ref["eye_L0", "look"] + ref["eye_R0", "look"]) * .5
        aim = head.anchored(rest_aim, (pts["look.L"] + pts["look.R"]) * .5)
        for loc in ("root", "sizeRef"):
            transforms["eyeslook_C0", loc] = aim
    result, frames = {}, {}
    for key, (label, pos, parent, deform, chain) in specs.items():
        component, loc = key
        kind, index = component.split("_")
        side = index[0]
        seg = None
        if kind in {"body", "spine"}:
            seg = torso
        elif kind == "neck":
            seg = head if loc in {"head", "eff"} else neck
        elif kind in {"eye", "eyeslook", "mouth", "tongue"}:
            seg = head
        elif kind == "shoulder":
            seg = transforms[side, "clavicle"]
        elif kind in {"arm", "leg"}:
            end_part = "hand" if kind == "arm" else "foot"
            seg = (transforms[side, end_part] if loc == "eff" else
                   transforms[component, "upper" if loc == "root" else "lower"])
        elif kind in {"meta", "thumbRoll", "thumb", "finger"}:
            seg = transforms[side, "hand"]
        elif kind == "foot":
            seg = transforms[side, "foot"]
        if kind == "tongue" and "jaw" in pts:
            seg = jaw
        seg = transforms.get(key, seg)
        result[key] = (label, seg.point(pos) if seg else pos.copy(), parent, deform, chain)
        if seg:
            frames[key] = seg
    world = root.matrix_world if world is None else world
    return (result, shapes, settings, (scale, 'Y', ref), display), frames, world @ Matrix.Translation(origin)


def hide_guides(scene, rig=None):
    for layer in scene.view_layers:
        if "xbone_landmark_hidden_guides" in layer:
            continue
        guides_to_hide = [obj for obj in layer.objects
                          if (obj.get("rigging_role") == "GUIDE" or obj == rig) and obj.visible_get(view_layer=layer)]
        layer["xbone_landmark_hidden_guides"] = {str(i): obj for i, obj in enumerate(guides_to_hide)}
        for obj in guides_to_hide:
            obj.hide_set(True, view_layer=layer)


@persistent
def resume_landmarks(_filepath):
    for scene in bpy.data.scenes:
        root = scene.xbone_marker_session
        if not root or scene.library or root.library:
            continue
        # Older files may have been saved before the first marker was created.
        context = bpy.context
        legacy_active_layer = scene == context.scene and "xbone_landmark_hidden_guides" not in context.view_layer
        hide_guides(scene)
        index = current_step(root)
        if index < 0 or legacy_active_layer:
            marker = place_default(root, max(0, index))
            if scene == context.scene and context.mode == 'OBJECT' and context.view_layer.objects.get(marker.name):
                select_marker(context, marker)


def clear(context, root):
    for layer in context.scene.view_layers:
        hidden = layer.get("xbone_landmark_hidden_guides")
        if hidden is not None:
            for obj in hidden.values():
                if obj and layer.objects.get(obj.name) == obj:
                    obj.hide_set(False, view_layer=layer)
            del layer["xbone_landmark_hidden_guides"]
    coll = next((c for c in root.users_collection if c.get("xbone_marker_collection")), None)
    context.scene.xbone_marker_session = None
    for obj in list(root.children):
        if "xbone_landmark" in obj or obj.get("xbone_mirrored_landmark"):
            bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.objects.remove(root, do_unlink=True)
    if coll and not coll.objects and not coll.children:
        bpy.data.collections.remove(coll)


def new_session(context, symmetric, height, rig=None):
    coll = bpy.data.collections.new("XBiped Landmarks")
    coll["xbone_marker_collection"] = True
    context.scene.collection.children.link(coll)
    root = bpy.data.objects.new("XBiped Landmarks", None)
    coll.objects.link(root)
    root["height"], root["symmetric"], root["step"] = height, symmetric, 0
    root.hide_render = True
    root.empty_display_size = height * .02
    context.scene.xbone_marker_session = root
    hide_guides(context.scene, rig)
    return root


def guide_positions(guide):
    bones = {b.get("biped_locator", b.name): b for b in guide.data.bones}
    points = {}
    for key, (component, loc) in landmark_keys().items():
        label = component if loc == "root" else f"{component}_{loc}"
        if label in bones:
            points[key] = bones[label].head_local.copy()
    return points


def remember_landmarks(root, guide):
    guide["landmark_symmetric"] = root["symmetric"]
    guide["landmark_details"] = root.get("detail_order", "")
    guide["landmark_isolate"] = root.get("isolate_marker", False)
    if "landmark_offsets" in guide:
        del guide["landmark_offsets"]


def refit_guide(context, guide, before, after, anchors):
    # Both fits start from the same canonical component axes. Applying only their
    # difference preserves manual refinements, custom branches and guide settings.
    def frames_in_guide(fit):
        (specs, _, _, (scale, _, _), _), frames, world = fit
        local = guide.matrix_world.inverted_safe() @ world
        result = {}
        for key, (label, point, *_) in specs.items():
            segment = frames.get(key)
            rotation = segment.rotation if segment else None
            size = scale * (segment.scale if segment else 1)
            result[label] = local @ Matrix.LocRotScale(point, rotation, Vector((size,) * 3))
        return result

    original, updated = frames_in_guide(before), frames_in_guide(after)
    delta = {label: updated[label] @ matrix.inverted_safe() for label, matrix in original.items()
             if updated[label] != matrix}
    existing = {b.get("biped_locator", b.name): b for b in guide.data.bones}
    if not delta and all((existing[k].head_local - v).length < 1e-6 for k, v in anchors.items()):
        return
    guide.hide_set(False)
    select_object(context, guide)
    bpy.ops.object.mode_set(mode='EDIT')
    # Both sides already have explicit positions. RNA transform callbacks must not
    # mirror one side over the other during the write, even if Edit Mode uses X Mirror.
    mirror = guide.data.use_mirror_x
    guide.data.use_mirror_x = False
    try:
        edit = guide.data.edit_bones
        targets = {}
        joins = {}
        for bone in edit:
            parent = bone
            while parent and parent.get("biped_locator", parent.name) not in original:
                parent = parent.parent
            if not parent:
                continue
            transform = delta.get(parent.get("biped_locator", parent.name))
            if transform is not None:
                targets[bone.name] = (transform @ bone.head, transform @ bone.tail,
                                      transform.to_3x3() @ bone.matrix.to_3x3().col[2])
            label = bone.get("biped_locator", bone.name)
            if label in anchors:
                head, tail, up = targets.get(bone.name, (bone.head, bone.tail, bone.matrix.to_3x3().col[2]))
                shift = anchors[label] - head
                if shift.length > 1e-6:
                    targets[bone.name] = (anchors[label], tail + shift, up.copy())
            joins[bone.name] = next((child.name for child in bone.children
                                     if (child.head - bone.tail).length < 1e-6), None)
        for name, child in joins.items():
            bone = edit[name]
            if name in targets:
                head, tail, up = targets[name]
                bone.head = head
                bone.tail = edit[child].head.copy() if child and child not in targets else tail
            elif child in targets:
                up = bone.matrix.to_3x3().col[2].copy()
            else:
                continue
            if child in targets:
                bone.tail = targets[child][0]
            bone.align_roll(up)
    finally:
        guide.data.use_mirror_x = mirror
        bpy.ops.object.mode_set(mode='OBJECT')


class XBONE_OT_start_landmarks(Operator):
    bl_idname = "xbone.start_landmarks"
    bl_label = "Place XBiped Landmarks"
    bl_description = "Start at the world origin and fit an XBiped guide using movable landmarks"
    bl_options = {'REGISTER', 'UNDO'}
    symmetric: BoolProperty(name="Symmetrical Character", default=True)
    facing: EnumProperty(name="Character Faces", items=[(k, v, "") for k, v in
                         (("NEG_Y", "−Y"), ("POS_Y", "+Y"), ("POS_X", "+X"), ("NEG_X", "−X"))], default="NEG_Y")

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and not context.scene.xbone_marker_session

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        root = new_session(context, self.symmetric, default_positions()["crown"].z)
        root.rotation_euler.z = math.radians(FACING[self.facing])
        select_marker(context, place_default(root, 0))
        return {'FINISHED'}


class XBONE_OT_edit_landmarks(Operator):
    bl_idname = "xbone.edit_landmarks"
    bl_label = "Edit XBiped Landmarks"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        guide = obj.get("rigging_guide") if obj and obj.get("rigging_role") == "RIG" else obj
        return (not context.scene.xbone_marker_session and context.mode in {'OBJECT', 'POSE', 'EDIT_ARMATURE'}
                and guide and guide.get("rigging_role") == "GUIDE" and guide.get("xbiped")
                and guide.is_editable and guide.data.is_editable and context.view_layer.objects.get(guide.name) == guide)

    def execute(self, context):
        obj, mode = context.object, context.object.mode
        selection = list(context.selected_objects)
        guide = obj.get("rigging_guide") or obj
        if mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        context.view_layer.update()
        pts = guide_positions(guide)
        details = guide.get("landmark_details", "")
        missing = {key for key, _ in steps({"symmetric": False, "detail_order": details})} - pts.keys()
        if missing:
            self.report({'ERROR'}, "Guide is missing landmarks: " + ", ".join(sorted(missing)))
            if mode != 'OBJECT':
                bpy.ops.object.mode_set(mode=mode)
            return {'CANCELLED'}
        # The guide may have been edited since placement, including on its right side only.
        # Respect an explicit independent session; only mirror a guide that still matches.
        tolerance = max(1e-6, (pts["crown"] - pts["pelvis"]).length * 1e-5)
        symmetric = guide.get("landmark_symmetric", True) and all(
            key[:-1] + "R" in pts and (reflect_point(p, pts["pelvis"]) - pts[key[:-1] + "R"]).length <= tolerance
            for key, p in pts.items() if key.endswith(".L"))
        rig = obj if obj != guide else rebuild.target_for(context, guide)
        root = new_session(context, symmetric, max(.001, pts["crown"].z - pts["toe.L"].z), rig)
        # Markers live in guide space even when its parents introduce scale or shear.
        root.parent = guide
        root.matrix_parent_inverse = Matrix.Identity(4)
        root.matrix_basis = Matrix.Identity(4)
        context.view_layer.update()
        root["editing_guide"], root["return_object"], root["return_mode"] = guide, obj, mode
        root["return_selection"] = {str(i): ob for i, ob in enumerate(selection)}
        if rig:
            root["editing_rig"] = rig
        root["detail_order"] = details
        root["isolate_marker"] = guide.get("landmark_isolate", False)
        for i, (key, _) in enumerate(steps(root)):
            place_default(root, i, pts[key])
        select_marker(context, place_default(root, 0))
        return {'FINISHED'}


class SessionOperator:
    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and context.scene.xbone_marker_session is not None


class XBONE_OT_landmark_symmetry(SessionOperator, Operator):
    bl_idname = "xbone.landmark_symmetry"
    bl_label = "Mirror Left to Right"
    bl_description = "Switch between left-driven mirrored markers and independent left/right markers"
    bl_options = {'UNDO'}

    def execute(self, context):
        root = context.scene.xbone_marker_session
        placed = markers(root)
        if "pelvis" not in placed:
            return {'CANCELLED'}
        current = current_step(root, context.active_object)
        active_key = steps(root)[max(current, 0)][0]
        root["symmetric"] = not root["symmetric"]
        if root["symmetric"]:
            # Keep the independent edits if the artist previews symmetry and switches back.
            root["independent_right"] = {key: list(ob.location) for key, ob in placed.items() if key.endswith(".R")}
            for key, ob in placed.items():
                if key.endswith(".R"):
                    bpy.data.objects.remove(ob, do_unlink=True)
            for key, label in steps(root):
                if key.endswith(".L") and key in placed:
                    make_mirror_preview(root, placed[key], label)
            if active_key.endswith(".R"):
                active_key = active_key[:-1] + "L"
        else:
            for ob in list(root.children):
                if "xbone_mirrored_landmark" in ob:
                    bpy.data.objects.remove(ob, do_unlink=True)
            saved = root.get("independent_right", {})
            for i, (key, _) in enumerate(steps(root)):
                if key.endswith(".R") and key[:-1] + "L" in placed:
                    point = saved.get(key)
                    if point is None:
                        point = reflect_point(placed[key[:-1] + "L"].location, placed["pelvis"].location)
                    place_default(root, i, point)
            if "independent_right" in root:
                del root["independent_right"]
        placed = markers(root)
        if active_key not in placed:
            active_key = "pelvis"
        root["step"] = next(i + 1 for i, (key, _) in enumerate(steps(root)) if key == active_key)
        select_marker(context, placed[active_key])
        return {'FINISHED'}


class XBONE_OT_landmark_details(SessionOperator, Operator):
    bl_idname = "xbone.landmark_details"
    bl_label = "Add Detail Landmarks"
    bl_options = {'UNDO'}
    detail: EnumProperty(items=(('FINGERS', "Match Finger Joints", ""), ('FACE', "Match Face", "")))

    def execute(self, context):
        root = context.scene.xbone_marker_session
        order = steps(root)
        placed = markers(root)
        previous = root.get("detail_order", "")
        if self.detail in previous.split() or any(k not in placed for k, _ in order):
            return {'CANCELLED'}
        root["detail_order"] = (previous + " " + self.detail).strip()
        try:
            guide = root.get("editing_guide")
            if guide:
                defaults = guide_positions(guide)
                required = {key for key, _ in steps({"symmetric": False, "detail_order": self.detail})}
                missing = required - defaults.keys()
                if missing:
                    raise ValueError("Guide is missing landmarks: " + ", ".join(sorted(missing)))
                # A refit already has a guide. Initialize the entire section so Update is
                # available immediately. Validate both sides before creating any objects.
                additions = [suggested_position(root, key, defaults, placed)
                             for key, _ in steps(root)[len(order):]]
                for i, point in enumerate(additions, len(order)):
                    place_default(root, i, point)
            marker = place_default(root, len(order))
        except ValueError as exc:
            root["detail_order"] = previous
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        select_marker(context, marker)
        return {'FINISHED'}


class XBONE_OT_isolate_landmark(SessionOperator, Operator):
    bl_idname = "xbone.isolate_landmark"
    bl_label = "Hide Other Landmarks"
    bl_options = {'UNDO'}

    def execute(self, context):
        root = context.scene.xbone_marker_session
        index = current_step(root, context.active_object)
        if index < 0:
            return {'CANCELLED'}
        marker = markers(root)[steps(root)[index][0]]
        set_isolation(root, marker, not root.get("isolate_marker", False))
        select_object(context, marker)
        return {'FINISHED'}


class XBONE_OT_place_landmarks(SessionOperator, Operator):
    bl_idname = "xbone.place_landmarks"
    bl_label = "Next Marker"
    bl_description = "Select the next landmark, creating it at the XBiped template position on its first visit"
    bl_options = {'UNDO'}

    def execute(self, context):
        root = context.scene.xbone_marker_session
        index = next_step(root, current_step(root, context.active_object))
        if index is None:
            return {'CANCELLED'}
        try:
            marker = place_default(root, index)
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        select_marker(context, marker)
        return {'FINISHED'}


class XBONE_OT_marker_back(SessionOperator, Operator):
    bl_idname = "xbone.marker_back"
    bl_label = "Previous Marker"
    bl_options = {'UNDO'}

    def execute(self, context):
        root = context.scene.xbone_marker_session
        index = current_step(root, context.active_object)
        if index <= 0:
            return {'CANCELLED'}
        select_marker(context, place_default(root, index - 1))
        return {'FINISHED'}


class XBONE_OT_cancel_landmarks(SessionOperator, Operator):
    bl_idname = "xbone.cancel_landmarks"
    bl_label = "Cancel Landmark Editing"
    bl_description = "Discard the temporary markers and return to the original object and mode"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene is not None and context.scene.xbone_marker_session is not None

    def invoke(self, context, event):
        # Keep Escape available to other editors, playback and mode-specific controls.
        if event.type == 'ESC' and (context.mode != 'OBJECT' or not context.area or
                                    context.area.type not in {'VIEW_3D', 'RIGGING'} or
                                    context.screen.is_animation_playing):
            return {'PASS_THROUGH'}
        return self.execute(context)

    def execute(self, context):
        root = context.scene.xbone_marker_session
        obj, mode = root.get("return_object"), root.get("return_mode")
        selected = list(root.get("return_selection", {}).values())
        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')
        clear(context, root)
        if obj and context.view_layer.objects.get(obj.name) and obj.visible_get():
            select_object(context, obj)
            for other in selected:
                if other and context.view_layer.objects.get(other.name) == other and other.visible_get():
                    other.select_set(True)
            if mode != 'OBJECT':
                bpy.ops.object.mode_set(mode=mode)
        return {'FINISHED'}


class XBONE_OT_fit_landmarks(SessionOperator, Operator):
    bl_idname = "xbone.fit_landmarks"
    bl_label = "Generate Fitted XBiped Guide"
    bl_options = {'UNDO'}

    def execute(self, context):
        root = context.scene.xbone_marker_session
        guide, rig = root.get("editing_guide"), root.get("editing_rig")
        if "editing_guide" in root and guide is None:
            self.report({'ERROR'}, "The source guide was removed")
            return {'CANCELLED'}
        context.view_layer.update()
        try:
            points = landmark_points(root)
            after = fitted_template(root, points)
            if guide:
                before = fitted_template(root, guide_positions(guide), guide.matrix_world, symmetric=False)
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        if guide:
            local = guide.matrix_world.inverted_safe() @ root.matrix_world
            anchors = {}
            for key, point in points.items():
                component, locator = landmark_keys()[key]
                label = component if locator == "root" else component + "_" + locator
                anchors[label] = local @ point
            refit_guide(context, guide, before, after, anchors)
        else:
            template, frames, world = after
            guide = guides.import_template(context, template, "XBiped Guide", at_origin=True, fit_frames=frames)
            guide.matrix_world = world
            guide["xbiped"] = True
            xbiped_limb.set_limb_defaults(guide)
        was_editing = "editing_guide" in root
        remember_landmarks(root, guide)
        clear(context, root)
        guide.hide_set(False)
        if rig and context.view_layer.objects.get(rig.name):
            rig.hide_set(True)
            guide["rigging_target"] = rig
        select_object(context, guide)
        if was_editing:
            self.report({'INFO'}, "Guide updated. Use Apply Guide and Rebuild to update the rig.")
        return {'FINISHED'}


class RIGGING_PT_landmarks(Panel):
    bl_label = "XBiped Landmarks"
    bl_space_type = 'RIGGING'
    bl_region_type = 'WINDOW'

    @classmethod
    def poll(cls, context):
        return context.scene.xbone_marker_session is not None

    def draw(self, context):
        layout = self.layout
        root = context.scene.xbone_marker_session
        order = steps(root)
        i = current_step(root, context.active_object)
        edit_session = "editing_guide" in root
        if edit_session:
            row = layout.row(align=True)
            row.operator("xbone.landmark_symmetry", text="Mirror Left Markers to Right", depress=bool(root["symmetric"]))
            row.operator("xbone.cancel_landmarks", text="Cancel", icon='CANCEL')
        if i >= 0:
            layout.label(text=f"{i + 1}/{len(order)}  {order[i][1]}", icon='EMPTY_AXIS')
            layout.operator("xbone.isolate_landmark", depress=bool(root.get("isolate_marker")))
        if next_step(root, i) is not None:
            layout.operator("xbone.place_landmarks")
        placed = markers(root)
        if all(key in placed for key, _ in order):
            for detail, label in (('FINGERS', "Match Finger Joints"), ('FACE', "Match Face")):
                if detail not in root.get("detail_order", "").split():
                    layout.operator("xbone.landmark_details", text=label).detail = detail
            if root.get("editing_guide"):
                layout.operator("xbone.fit_landmarks", text="Update XBiped Guide", icon='ARMATURE_DATA')
            else:
                layout.operator("xbone.fit_landmarks", icon='ARMATURE_DATA')
        row = layout.row(align=True)
        back = row.row(align=True)
        back.enabled = i > 0
        back.operator("xbone.marker_back")
        if not edit_session:
            row.operator("xbone.cancel_landmarks", text="Cancel", icon='CANCEL')


classes = (XBONE_OT_start_landmarks, XBONE_OT_edit_landmarks, XBONE_OT_landmark_symmetry, XBONE_OT_landmark_details,
           XBONE_OT_isolate_landmark, XBONE_OT_place_landmarks,
           XBONE_OT_marker_back, XBONE_OT_cancel_landmarks, XBONE_OT_fit_landmarks, RIGGING_PT_landmarks)


def register():
    bpy.types.Scene.xbone_marker_session = PointerProperty(type=bpy.types.Object)
    bpy.app.handlers.load_post.append(resume_landmarks)


def unregister():
    bpy.app.handlers.load_post.remove(resume_landmarks)
    del bpy.types.Scene.xbone_marker_session
