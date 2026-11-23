# SPDX-License-Identifier: GPL-2.0-or-later
"""Fit a Shifter XBiped guide to mesh landmarks in the character's reference pose."""
import json
import math
from pathlib import Path

import bpy
from bpy.app.translations import pgettext_iface as iface_
from bpy.props import BoolProperty, EnumProperty, PointerProperty
from bpy.types import Operator, Panel
from bpy_extras.view3d_utils import region_2d_to_origin_3d, region_2d_to_vector_3d
from mathutils import Matrix, Quaternion, Vector

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


def current_step(root):
    order = steps(root)
    placed = markers(root)
    missing = next((i for i, (key, _) in enumerate(order) if key not in placed), len(order))
    return min(root["step"], missing)


def sources(context, root):
    return [obj for obj in root["source_meshes"].values() if obj and obj.type == 'MESH'
            and context.view_layer.objects.get(obj.name) == obj]


def place(root, world_point):
    order = steps(root)
    index = current_step(root)
    key, label = order[index]
    marker = markers(root).get(key)
    if marker is None:
        marker = bpy.data.objects.new(label, None)
        root.users_collection[0].objects.link(marker)
        marker.parent = root
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
    marker.location = root.matrix_world.inverted() @ world_point
    root["step"] = index + 1
    return marker


def surface_center(context, root, origin, direction):
    """Use the first hit and its exit on the same mesh to estimate joint depth."""
    best, nearest = None, float('inf')
    dg = context.evaluated_depsgraph_get()
    epsilon = max(1e-8, root["height"] * 1e-5)
    for source in sources(context, root):
        if not source.visible_get(view_layer=context.view_layer):
            continue
        obj = source.evaluated_get(dg)
        matrix = obj.matrix_world
        inv = matrix.inverted_safe()
        ray = (inv.to_3x3() @ direction).normalized()
        hit, entry, normal, _ = obj.ray_cast(inv @ origin, ray)
        if not hit:
            continue
        front = matrix @ entry
        depth = (front - origin).dot(direction)
        if depth >= nearest:
            continue
        nearest, best = depth, front
        if normal.dot(ray) < 0:
            hit, end, back_normal, _ = obj.ray_cast(inv @ (front + direction * epsilon), ray)
            if hit and back_normal.dot(ray) > 0:
                best = (front + matrix @ end) * .5
    return best


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


def clear(context, root):
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
    bl_label = "Fit XBiped to Mesh"
    bl_description = "Place body landmarks on the selected meshes, then fit the XBiped guide"
    bl_options = {'REGISTER', 'UNDO'}
    symmetric: BoolProperty(name="Symmetrical Character", default=True)
    facing: EnumProperty(name="Character Faces", items=[(k, v, "") for k, v in
                         (("NEG_Y", "−Y"), ("POS_Y", "+Y"), ("POS_X", "+X"), ("NEG_X", "−X"))], default="NEG_Y")

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and not context.scene.xbone_marker_session and any(
            o.type == 'MESH' for o in context.selected_objects)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        meshes = [o for o in context.selected_objects if o.type == 'MESH' and len(o.data.vertices)]
        dg = context.evaluated_depsgraph_get()
        corners = [ev.matrix_world @ Vector(co)
                   for obj in meshes for ev in (obj.evaluated_get(dg),) for co in ev.bound_box]
        if not corners or max(p.z for p in corners) - min(p.z for p in corners) < 1e-5:
            self.report({'ERROR'}, "Select an upright character mesh")
            return {'CANCELLED'}
        lo, hi = (Vector([f(p[i] for p in corners) for i in range(3)]) for f in (min, max))
        coll = bpy.data.collections.new("XBiped Landmarks")
        coll["xbone_marker_collection"] = True
        context.scene.collection.children.link(coll)
        root = bpy.data.objects.new("XBiped Landmarks", None)
        coll.objects.link(root)
        root.location = ((hi.x + lo.x) / 2, (hi.y + lo.y) / 2, lo.z)
        root.rotation_euler.z = math.radians(FACING[self.facing])
        root["source_meshes"] = {str(i): o for i, o in enumerate(meshes)}
        root["height"], root["symmetric"], root["step"] = hi.z - lo.z, self.symmetric, 0
        root.hide_render = True
        root.empty_display_size = root["height"] * .02
        context.scene.xbone_marker_session = root
        view = (context.area if context.area and context.area.type == 'VIEW_3D' else
                next((a for a in context.screen.areas if a.type == 'VIEW_3D'), None))
        if view and not bpy.app.background:
            space = view.spaces.active
            space.region_3d.view_rotation = root.rotation_euler.to_quaternion() @ Quaternion((1, 0, 0), math.pi / 2)
            space.region_3d.view_perspective = 'ORTHO'
            region = next(r for r in view.regions if r.type == 'WINDOW')
            with context.temp_override(area=view, region=region):
                bpy.ops.view3d.view_selected(use_all_regions=False)
        return {'FINISHED'}


class SessionOperator:
    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and context.scene.xbone_marker_session is not None


class XBONE_OT_place_landmarks(SessionOperator, Operator):
    bl_idname = "xbone.place_landmarks"
    bl_label = "Place Markers in 3D View"
    bl_description = "Click the selected character meshes in order; Backspace goes back and Esc pauses"
    bl_options = {'UNDO', 'BLOCKING'}

    def prompt(self, context, root):
        i = current_step(root)
        order = steps(root)
        context.workspace.status_text_set(
            f"{i + 1}/{len(order)}  {order[i][1]}   ·   " + iface_("Click to place, Backspace to go back, Esc to pause"))

    def invoke(self, context, event):
        root = context.scene.xbone_marker_session
        if current_step(root) >= len(steps(root)) or not sources(context, root):
            return {'CANCELLED'}
        if not any(a.type == 'VIEW_3D' for a in context.screen.areas):
            self.report({'WARNING'}, "Open a 3D Viewport beside Rigging to place markers")
            return {'CANCELLED'}
        self.session = root.session_uid
        self.prompt(context, root)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        context.workspace.status_text_set(None)

    def modal(self, context, event):
        root = context.scene.xbone_marker_session
        if (not root or root.session_uid != self.session or event.type == 'ESC' or
                (event.type == 'RIGHTMOUSE' and not event.alt)):
            self.cancel(context)
            return {'FINISHED'}
        if event.type == 'BACK_SPACE' and event.value == 'PRESS':
            root["step"] = max(0, current_step(root) - 1)
            self.prompt(context, root)
        elif event.type == 'LEFTMOUSE' and event.value == 'PRESS' and not event.alt:
            for area in context.screen.areas:
                if area.type != 'VIEW_3D':
                    continue
                for region in area.regions:
                    xy = event.mouse_x - region.x, event.mouse_y - region.y
                    if region.type != 'WINDOW' or not (0 <= xy[0] < region.width and 0 <= xy[1] < region.height):
                        continue
                    with context.temp_override(area=area, region=region):
                        view = context.region_data
                        origin = region_2d_to_origin_3d(region, view, xy)
                        direction = region_2d_to_vector_3d(region, view, xy)
                        point = surface_center(context, root, origin, direction)
                    if point is None:
                        self.report({'INFO'}, "Click on a source mesh")
                        return {'RUNNING_MODAL'}
                    place(root, point)
                    if current_step(root) == len(steps(root)):
                        self.cancel(context)
                        return {'FINISHED'}
                    self.prompt(context, root)
                    return {'RUNNING_MODAL'}
        else:
            return {'PASS_THROUGH'}
        return {'RUNNING_MODAL'}


class XBONE_OT_marker_cursor(SessionOperator, Operator):
    bl_idname = "xbone.marker_cursor"
    bl_label = "Use 3D Cursor"
    bl_options = {'UNDO'}

    def execute(self, context):
        root = context.scene.xbone_marker_session
        if current_step(root) >= len(steps(root)):
            return {'CANCELLED'}
        place(root, context.scene.cursor.location)
        return {'FINISHED'}


class XBONE_OT_marker_back(SessionOperator, Operator):
    bl_idname = "xbone.marker_back"
    bl_label = "Previous Marker"
    bl_options = {'UNDO'}

    def execute(self, context):
        root = context.scene.xbone_marker_session
        root["step"] = max(0, current_step(root) - 1)
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
        i = current_step(root)
        if i < len(order):
            layout.label(text=f"{i + 1}/{len(order)}  {order[i][1]}", icon='EMPTY_AXIS')
            layout.operator("xbone.place_landmarks")
            layout.operator("xbone.marker_cursor")
        else:
            layout.label(text="Review markers in the viewport")
        placed = markers(root)
        if all(key in placed for key, _ in order):
            layout.operator("xbone.fit_landmarks", icon='ARMATURE_DATA')
        row = layout.row(align=True)
        back = row.row(align=True)
        back.enabled = i > 0
        back.operator("xbone.marker_back")
        row.operator("xbone.cancel_landmarks")


classes = (XBONE_OT_start_landmarks, XBONE_OT_place_landmarks, XBONE_OT_marker_cursor,
           XBONE_OT_marker_back, XBONE_OT_cancel_landmarks, XBONE_OT_fit_landmarks, RIGGING_PT_landmarks)


def register():
    bpy.types.Scene.xbone_marker_session = PointerProperty(type=bpy.types.Object)


def unregister():
    del bpy.types.Scene.xbone_marker_session
