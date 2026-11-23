# SPDX-License-Identifier: GPL-2.0-or-later
"""Fit a Shifter XBiped guide to freely positioned landmarks in the character's reference pose."""
import json
import math
from functools import lru_cache
from pathlib import Path

import bpy
from bpy.app.handlers import persistent
from bpy.app.translations import pgettext_iface as iface_
from bpy.props import BoolProperty, EnumProperty, PointerProperty
from bpy.types import Operator, Panel
from mathutils import Matrix, Vector

from . import guides, xbiped_limb


CENTER = (("pelvis", "Pelvis"), ("neck", "Base of Neck"), ("head", "Base of Head"), ("crown", "Top of Head"))
SIDE = (("shoulder", "Shoulder"), ("elbow", "Elbow"), ("wrist", "Wrist"), ("hand", "Middle Fingertip"),
        ("hip", "Hip Joint"), ("knee", "Knee"), ("ankle", "Ankle"), ("toe", "Toe Tip"))
FACING = {'NEG_Y': 0, 'POS_X': 90, 'POS_Y': 180, 'NEG_X': -90}


def steps(root):
    sides = ('L',) if root["symmetric"] else ('L', 'R')
    return ([(key, iface_(label)) for key, label in CENTER] +
            [(key + "." + side, iface_(label) + "." + side) for side in sides for key, label in SIDE])


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
def default_positions():
    specs, *_ = guides.template_points(Path(__file__).with_name("biped.sgt"), 0.1, 'Y')
    keys = {"pelvis": ("spine_C0", "root"), "neck": ("neck_C0", "root"),
            "head": ("neck_C0", "head"), "crown": ("neck_C0", "eff")}
    for side in ("L", "R"):
        arm, leg = f"arm_{side}0", f"leg_{side}0"
        for name, component, locator in (
                ("shoulder", arm, "root"), ("elbow", arm, "elbow"), ("wrist", arm, "wrist"),
                ("hand", f"finger_{side}1", "2_loc"), ("hip", leg, "root"), ("knee", leg, "knee"),
                ("ankle", leg, "ankle"), ("toe", f"foot_{side}0", "2_loc")):
            keys[f"{name}.{side}"] = (component, locator)
    return {name: specs[key][1].copy() for name, key in keys.items()}


def select_marker(context, marker):
    for obj in context.selected_objects:
        obj.select_set(False)
    marker.select_set(True)
    context.view_layer.objects.active = marker


def place_default(root, index):
    order = steps(root)
    key, label = order[index]
    marker = markers(root).get(key)
    if marker is None:
        marker = bpy.data.objects.new(label, None)
        root.users_collection[0].objects.link(marker)
        marker.parent = root
        defaults = default_positions()
        marker.location = defaults[key] * (root["height"] / defaults["crown"].z)
        marker["xbone_landmark"] = key
        marker.empty_display_type = 'SPHERE'
        marker.empty_display_size = root["height"] * .009
        marker.show_in_front = marker.show_name = True
        marker.hide_render = True
        if key == "pelvis":
            for child in root.children:
                if child.get("xbone_mirrored_landmark") and child.animation_data:
                    curve = child.animation_data.drivers.find("location", index=0)
                    if curve:
                        curve.driver.variables["center"].targets[0].id = marker
        if root["symmetric"] and key.endswith(".L"):
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
    root["step"] = index + 1
    return marker


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


def fitted_template(root):
    order = steps(root)
    placed = markers(root)
    if any(k not in placed for k, _ in order):
        raise ValueError("Place all required markers first")
    pts = {k: placed[k].location.copy() for k, _ in order}
    if root["symmetric"]:
        for key, _ in SIDE:
            p = pts[key + ".L"]
            pts[key + ".R"] = Vector((2 * pts["pelvis"].x - p.x, p.y, p.z))
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
        result[key] = (label, seg.point(pos) if seg else pos.copy(), parent, deform, chain)
        if seg:
            frames[key] = seg
    return (result, shapes, settings, (scale, 'Y', ref), display), frames, root.matrix_world @ Matrix.Translation(origin)


def hide_guides(scene):
    for layer in scene.view_layers:
        if "xbone_landmark_hidden_guides" in layer:
            continue
        guides_to_hide = [obj for obj in layer.objects
                          if obj.get("rigging_role") == "GUIDE" and obj.visible_get(view_layer=layer)]
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
        coll = bpy.data.collections.new("XBiped Landmarks")
        coll["xbone_marker_collection"] = True
        context.scene.collection.children.link(coll)
        root = bpy.data.objects.new("XBiped Landmarks", None)
        coll.objects.link(root)
        root.rotation_euler.z = math.radians(FACING[self.facing])
        root["height"], root["symmetric"], root["step"] = default_positions()["crown"].z, self.symmetric, 0
        root.hide_render = True
        root.empty_display_size = root["height"] * .02
        context.scene.xbone_marker_session = root
        hide_guides(context.scene)
        select_marker(context, place_default(root, 0))
        return {'FINISHED'}


class SessionOperator:
    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and context.scene.xbone_marker_session is not None


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
        select_marker(context, place_default(root, index))
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
    bl_label = "Discard Markers"
    bl_options = {'UNDO'}

    def execute(self, context):
        clear(context, context.scene.xbone_marker_session)
        return {'FINISHED'}


class XBONE_OT_fit_landmarks(SessionOperator, Operator):
    bl_idname = "xbone.fit_landmarks"
    bl_label = "Generate Fitted XBiped Guide"
    bl_options = {'UNDO'}

    def execute(self, context):
        root = context.scene.xbone_marker_session
        try:
            template, frames, world = fitted_template(root)
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        guide = guides.import_template(context, template, "XBiped Guide", at_origin=True, fit_frames=frames)
        guide.matrix_world = world
        guide["xbiped"] = True
        xbiped_limb.set_limb_defaults(guide)
        clear(context, root)
        context.view_layer.objects.active = guide
        guide.select_set(True)
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
        if i >= 0:
            layout.label(text=f"{i + 1}/{len(order)}  {order[i][1]}", icon='EMPTY_AXIS')
        if next_step(root, i) is not None:
            layout.operator("xbone.place_landmarks")
        placed = markers(root)
        if all(key in placed for key, _ in order):
            layout.operator("xbone.fit_landmarks", icon='ARMATURE_DATA')
        row = layout.row(align=True)
        back = row.row(align=True)
        back.enabled = i > 0
        back.operator("xbone.marker_back")
        row.operator("xbone.cancel_landmarks")


classes = (XBONE_OT_start_landmarks, XBONE_OT_place_landmarks,
           XBONE_OT_marker_back, XBONE_OT_cancel_landmarks, XBONE_OT_fit_landmarks, RIGGING_PT_landmarks)


def register():
    bpy.types.Scene.xbone_marker_session = PointerProperty(type=bpy.types.Object)
    bpy.app.handlers.load_post.append(resume_landmarks)


def unregister():
    bpy.app.handlers.load_post.remove(resume_landmarks)
    del bpy.types.Scene.xbone_marker_session
