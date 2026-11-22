# SPDX-License-Identifier: GPL-2.0-or-later

import bpy
from pathlib import Path
from bpy.app.handlers import persistent
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty,
                       PointerProperty, StringProperty)
from bpy.types import Menu, Operator, PropertyGroup
from bpy_extras.io_utils import ExportHelper
from bpy_extras import view3d_utils
from functools import lru_cache
from math import ceil, cos, hypot, sin, tau
from mathutils import Vector
from mathutils.geometry import barycentric_transform

from . import sdf_field as field
from .sdf_pick import SurfaceTree, ViewPicker
from .sdf_pen import PenPath
from . import sdf_controls, sdf_remap, sdf_wire

import numpy as np


def redraw(settings, context):
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type in {'IMAGE_EDITOR', 'VIEW_3D'}:
                area.tag_redraw()


# Preserve interaction state across Blender's scene-wide undo snapshots. Keep
# ordinary scene RNA storage so each scene retains its own saved settings.
_INTERACTION_PROPERTIES = ("radius", "white", "use_pressure", "show_wire", "wire_opacity", "onion_skin", "auto_key", "value", "mask_collapsed", "mask_offset")
_undo_interaction = {}


@persistent
def remember_interaction(_):
    _undo_interaction.clear()
    for scene in bpy.data.scenes:
        if scene.is_editable and scene.is_property_set("sdf_editor"):
            settings = scene.sdf_editor
            _undo_interaction[scene.session_uid] = (
                tuple(getattr(settings, prop) for prop in _INTERACTION_PROPERTIES),
                tuple(mask.name for mask in settings.masks), settings.index)


@persistent
def restore_interaction(_):
    for scene in bpy.data.scenes:
        saved = _undo_interaction.get(scene.session_uid)
        if saved is None or not scene.is_editable:
            continue
        values, masks, index = saved
        settings = scene.sdf_editor
        for prop, value in zip(_INTERACTION_PROPERTIES, values):
            if getattr(settings, prop) != value:
                setattr(settings, prop, value)
        # Undoing a mask creation/deletion restores its list and selection
        # together. Undoing paint keeps the user's currently selected mask.
        if settings.index != index and tuple(mask.name for mask in settings.masks) == masks:
            settings.index = index
    _undo_interaction.clear()


def draw_brush_ink(layout, settings):
    row = layout.row(align=True)
    row.operator_context = 'EXEC_REGION_WIN'
    for white, label in ((False, "Black"), (True, "White")):
        row.operator("sdf.brush_setting", text=label, depress=settings.white == white).action = label.upper()


def draw_brush_radius(layout, context):
    row = layout.row(align=True)
    row.prop(context.window_manager, "sdf_brush_radius", text="Radius")
    row.prop(context.window_manager, "sdf_pressure_enabled", text="", icon='STYLUS_PRESSURE', toggle=True)


def draw_wire_control(layout, context):
    wm = context.window_manager
    row = layout.row(align=True)
    row.prop(wm, "sdf_show_wire", text="", icon='SHADING_WIRE', toggle=True)
    if wm.sdf_show_wire:
        row.prop(wm, "sdf_wire_opacity", text="Wire", slider=True)


_ONION_MODES = (
    ('OFF', "Off", "Show the current SDF only", 0),
    ('PREVIOUS', "Previous Key", "Compare against the nearest earlier SDF key", 1),
    ('NEXT', "Next Key", "Compare against the nearest later SDF key", 2),
)


def draw_onion_control(layout, context):
    icon = 'ONIONSKIN_OFF' if context.scene.sdf_editor.onion_skin == 'OFF' else 'ONIONSKIN_ON'
    layout.prop(context.window_manager, "sdf_onion_skin", text="Onion Skin", icon=icon)


class SDFKey(PropertyGroup):
    value: FloatProperty()
    bitmap: StringProperty()


class SDFSource(PropertyGroup):
    object: PointerProperty(type=bpy.types.Object)
    uv_name: StringProperty()
    faces: StringProperty()
    uv_snapshot: StringProperty(options={'HIDDEN'})


class SDFMask(PropertyGroup):
    size: IntProperty()
    coverage: StringProperty()
    conflict_pair: FloatVectorProperty(size=2, default=(-1, -1), options={'HIDDEN'})
    sources: CollectionProperty(type=SDFSource)
    keys: CollectionProperty(type=SDFKey)


class SDFSettings(PropertyGroup):
    masks: CollectionProperty(type=SDFMask)
    mask_collapsed: BoolProperty(name="Collapse SDF Masks", update=redraw)
    mask_offset: IntProperty(min=0, options={'SKIP_SAVE'}, update=redraw)
    index: IntProperty(update=redraw)
    auto_key: BoolProperty(name="SDF Auto Key", default=True, update=redraw,
                           description="Create a key at the preview value when painting changes the image")
    value: FloatProperty(name="Preview", min=0, max=1, precision=4, update=redraw)
    radius: IntProperty(name="Radius (texels)", min=1, max=256, default=10, update=redraw,
                        description="Brush radius in texture pixels. Press F in either paint view to resize")
    onion_skin: EnumProperty(name="Onion Skin", items=_ONION_MODES, default='OFF', update=redraw)
    show_wire: BoolProperty(name="SDF Wireframe", default=False, update=redraw,
                            description="Show the mask mesh edges over the SDF in both paint views")
    wire_opacity: FloatProperty(name="Wire Opacity", min=0, max=1, default=.65, subtype='FACTOR', update=redraw)
    use_pressure: BoolProperty(name="Use Pressure for Radius", default=True, update=redraw)
    white: BoolProperty(name="White Brush", default=True, update=redraw)
    ink: EnumProperty(name="Brush Color", items=(
        ('BLACK', "Black", "Paint black", 0), ('WHITE', "White", "Paint white", 1)),
        get=lambda self: int(self.white), set=lambda self, value: setattr(self, "white", bool(value)))

    @property
    def mask(self):
        if 0 <= self.index < len(self.masks):
            return self.masks[self.index]


def key_at(mask, value):
    return next((k for k in mask.keys if abs(k.value - value) < 1e-6), None)


def key_images(mask):
    return tuple((k.value, k.bitmap) for k in mask.keys)


class SDFPaintMode:
    @classmethod
    def poll(cls, context):
        return context.mode == 'PAINT_SDF'


def ensure_workspace():
    workspace = next((w for w in bpy.data.workspaces if w.get("sdf_paint_layout")), None)
    if workspace is None:
        path = Path(__file__).resolve().parents[2] / "presets/workspaces/sdf_paint.blend"
        with bpy.data.libraries.load(str(path)) as (available, loaded):
            loaded.workspaces = ["SDF Paint"]
        workspace = loaded.workspaces[0]
        workspace.name = bpy.app.translations.pgettext_iface("SDF Paint")
        workspace["sdf_paint_layout"] = True
        workspace.object_mode = 'SDF_PAINT'
    return workspace


@persistent
def add_factory_workspace(_):
    if not bpy.context.preferences.app_template:
        ensure_workspace()


class SDF_OT_workspace(Operator):
    bl_idname = "sdf.workspace"
    bl_label = "SDF Paint Workspace"
    bl_description = "Open the workspace for painting shadow keys in 3D and UV space"

    def execute(self, context):
        context.window.workspace = ensure_workspace()
        return {'FINISHED'}


_picker = None


def active_picker(context):
    global _picker
    if not _picker or context.screen != _picker.screen or context.scene != _picker.scene:
        return None
    if (context.mode != 'PAINT_SDF' or _picker.area not in tuple(context.screen.areas) or
            _picker.area.ui_type != 'SDF'):
        _picker = None
        return None
    return _picker


def store_mask(context, name, size, coverage, sources, target=None):
    if target is not None and target.keys:
        sdf_remap.bind_legacy(target)
        for src in target.sources:
            layer = src.object.data.uv_layers.get(src.uv_name) if src.object else None
            if layer is None or not src.uv_snapshot or not np.array_equal(
                    sdf_remap.unpack(src.uv_snapshot),
                    sdf_remap.capture(src.object.data, layer, face_indices(src.faces))):
                raise ValueError("UV layout changed. Rematch UV before editing the island selection")
    prepared = [(obj, uv, faces, sdf_remap.pack(sdf_remap.capture(obj.data, obj.data.uv_layers[uv], faces)))
                for obj, uv, faces in sources]
    settings = context.scene.sdf_editor
    if target is None:
        mask = settings.masks.add()
        mask.name = name.strip() or "Mask"
        mask.size = size
        settings.index = len(settings.masks) - 1
        settings.mask_offset = max(0, settings.index - 4)
    else:
        mask = target
        mask.sources.clear()
        _geometry.clear()
    mask.coverage = field.encode(coverage)
    for obj, uv_name, faces, snapshot in prepared:
        source = mask.sources.add()
        source.object, source.uv_name = obj, uv_name
        source.faces = ",".join(map(str, faces))
        source.uv_snapshot = snapshot
    _view_pickers.clear()


class SDF_OT_island_action(Operator):
    bl_idname = "sdf.island_action"
    bl_label = "UV Island Selection"
    bl_options = {'UNDO'}
    action: EnumProperty(items=[(v, v.title(), "") for v in ('TOGGLE', 'CONFIRM', 'CANCEL')])
    index: IntProperty()

    @classmethod
    def poll(cls, context):
        return active_picker(context) is not None

    def execute(self, context):
        global _picker
        picker = active_picker(context)
        if self.action == 'TOGGLE':
            if not 0 <= self.index < len(picker.islands):
                return {'CANCELLED'}
            picker.toggle(self.index)
        else:
            if self.action == 'CONFIRM':
                coverage = np.zeros((picker.size, picker.size), dtype=bool)
                sources = {}
                for i in sorted(picker.selected):
                    obj, name, faces, tris, _ = picker.islands[i]
                    faces, tris = picker.partial.get(i, (faces, tris))
                    sources.setdefault((obj, name), []).extend(faces)
                    for tri in tris:
                        field.raster_triangle(coverage, tri)
                if not coverage.any():
                    self.report({'WARNING'}, "Pick a UV island inside the 0–1 tile")
                    return {'CANCELLED'}
                try:
                    store_mask(context, picker.name, picker.size, coverage,
                               [(obj, uv, sorted(faces)) for (obj, uv), faces in sources.items()], picker.target)
                except ValueError as ex:
                    self.report({'WARNING'}, str(ex))
                    return {'CANCELLED'}
            _picker = None
            if picker.restore:
                picker.area.ui_type = picker.restore
        redraw(None, context)
        return {'FINISHED'}


def picker_controls(layout, picker):
    row = layout.row(align=True)
    row.label(text=bpy.app.translations.pgettext_iface("{} selected").format(len(picker.selected)), icon='UV_ISLANDSEL')
    ok = row.row(align=True)
    ok.enabled = bool(picker.selected)
    ok.operator("sdf.island_action", text="Apply Selection" if picker.target is not None else "Create Mask", icon='CHECKMARK').action = 'CONFIRM'
    row.operator("sdf.island_action", text="Cancel").action = 'CANCEL'


def start_island_picker(operator, context, name, size, uv_name="", target=None):
    global _picker
    from .sdf_islands import IslandPicker
    saved = {}
    if target is not None:
        for src in target.sources:
            if not src.object or src.object.type != 'MESH':
                operator.report({'ERROR'}, "The mask source mesh is missing")
                return {'CANCELLED'}
            faces = face_indices(src.faces)
            if faces and max(faces) >= len(src.object.data.polygons):
                operator.report({'ERROR'}, f"{src.object.name}: saved face indices no longer exist")
                return {'CANCELLED'}
            saved[src.object] = src.uv_name, faces
    candidates = (obj for obj in context.objects_in_mode if target is None or obj.data.uv_layers.active)
    objects = dict.fromkeys((*saved, *candidates))
    try:
        picker = IslandPicker(objects, uv_name, saved=saved)
    except ValueError as ex:
        operator.report({'ERROR'}, str(ex))
        return {'CANCELLED'}
    if not picker.islands:
        operator.report({'WARNING'}, "No visible UV islands")
        return {'CANCELLED'}
    picker.area = next((a for a in context.screen.areas if a.ui_type == 'SDF'), context.area)
    if not picker.area:
        operator.report({'WARNING'}, "Start island selection from a paint view")
        return {'CANCELLED'}
    picker.restore = None if picker.area.ui_type == 'SDF' else picker.area.ui_type
    if picker.restore:
        picker.area.ui_type = 'SDF'
    picker.screen, picker.scene = context.screen, context.scene
    picker.name, picker.size, picker.target = name, size, target
    _picker = picker
    redraw(None, context)
    return {'FINISHED'}


class SDF_OT_edit_islands(Operator):
    bl_idname = "sdf.edit_islands"
    bl_label = "Edit UV Islands"

    @classmethod
    def poll(cls, context):
        return bool(context.mode == 'PAINT_SDF' and context.scene.sdf_editor.mask and not active_picker(context))

    def execute(self, context):
        mask = context.scene.sdf_editor.mask
        return start_island_picker(self, context, mask.name, mask.size, target=mask)


class SDF_OT_add_mask(SDFPaintMode, Operator):
    bl_idname = "sdf.add_mask"
    bl_label = "Add SDF Mask"
    bl_options = {'REGISTER', 'UNDO'}
    name: StringProperty(name="Mask Name", default="Face")
    resolution: EnumProperty(name="Resolution", items=[(str(n), str(n), "") for n in (256, 512, 1024, 2048)],
                             default='512')
    uv_name: StringProperty(name="UV Map", description="Leave empty to use each mesh's active UV map")

    scope: EnumProperty(name="Include", items=(
        ('PICK', "Pick UV Islands", "Click islands in the SDF canvas to include or exclude them"),
        ('MESHES', "Paint Mode Meshes", "Use all visible faces of the meshes in SDF Paint"),
    ), default='MESHES')

    def invoke(self, context, event):
        if not self.properties.is_property_set("scope"):
            self.scope = 'PICK'
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop(self, "name")
        self.layout.prop(self, "resolution")
        self.layout.prop(self, "scope")
        self.layout.prop_search(self, "uv_name", context.active_object.data, "uv_layers", text="UV Map")
        self.layout.label(text="Empty uses each mesh's active UV map")

    def execute(self, context):
        size = int(self.resolution)
        if self.scope == 'PICK':
            return start_island_picker(self, context, self.name, size, self.uv_name)
        coverage = np.zeros((size, size), dtype=bool)
        sources = []
        for obj in context.objects_in_mode:
            if obj.type != 'MESH':
                continue
            mesh = obj.data
            selected = {face.index for face in mesh.polygons if not face.hide}
            if not selected:
                continue
            layers = mesh.uv_layers
            layer = layers.get(self.uv_name) if self.uv_name else layers.active
            if layer is None:
                self.report({'ERROR'}, f"{obj.name} has no matching UV map")
                return {'CANCELLED'}
            for tri in mesh.loop_triangles:
                if tri.polygon_index in selected:
                    field.raster_triangle(coverage, [layer.uv[i].vector[:] for i in tri.loops])
            sources.append((obj, layer.name, sorted(selected)))
        if not coverage.any():
            self.report({'ERROR'}, "Select complete UV faces or islands inside the 0–1 UV tile")
            return {'CANCELLED'}
        store_mask(context, self.name, size, coverage, sources)
        return {'FINISHED'}


@persistent
def bind_legacy_uvs(_):
    for scene in bpy.data.scenes:
        if scene.is_editable:
            for mask in scene.sdf_editor.masks:
                sdf_remap.bind_legacy(mask)


class SDF_OT_rematch_uv(Operator):
    bl_idname = "sdf.rematch_uv"
    bl_label = "Rematch UV"
    bl_description = "Move the mask and all painted keys to the new UV coordinates of the same surface"
    bl_options = {'UNDO'}
    target: EnumProperty(name="Target UV", items=(
        ('RECORDED', "Recorded Map", "Use the current coordinates of each recorded UV layer"),
        ('ACTIVE', "Active Map", "Use each mesh's active UV layer"),
        ('NAMED', "Named Map", "Use the specified UV layer on all source meshes")))
    uv_name: StringProperty(name="UV Map")
    previous_name: StringProperty(name="Original UV Override",
                                  description="Optional original layout to use instead of the saved UV snapshot")

    reference_file: StringProperty(name="Original Blend File", subtype='FILE_PATH',
                                   description="Optional pre-edit .blend or .blend1 containing the original mesh UVs")
    merge_white: BoolProperty(name="Merge White on Overlap",
                              description="If multiple surface points share a target texel, keep white if any was white")

    @classmethod
    def poll(cls, context):
        return bool(context.mode == 'PAINT_SDF' and context.scene.sdf_editor.mask and
                    not active_picker(context) and _stroke is None)

    def invoke(self, context, event):
        if not all(src.uv_snapshot for src in context.scene.sdf_editor.mask.sources) and bpy.data.filepath:
            backup = Path(bpy.data.filepath + "1")
            if backup.is_file():
                self.reference_file = str(backup)
        return context.window_manager.invoke_props_dialog(self, width=345)

    def draw(self, context):
        layout = self.layout
        mask = context.scene.sdf_editor.mask
        obj = next((src.object for src in mask.sources if src.object and src.object.type == 'MESH'), None)
        layout.prop(self, "target")
        if self.target == 'NAMED':
            if obj:
                layout.prop_search(self, "uv_name", obj.data, "uv_layers")
            else:
                layout.prop(self, "uv_name")
        layout.prop(self, "reference_file")
        if obj and not self.reference_file:
            layout.prop_search(self, "previous_name", obj.data, "uv_layers")
        else:
            layout.prop(self, "previous_name")
        layout.prop(self, "merge_white")
        layout.label(text="Reproject all keys of the selected mask", icon='KEY_HLT')

    def execute(self, context):
        global _texture_token
        mask = context.scene.sdf_editor.mask
        if self.target == 'NAMED' and not self.uv_name:
            self.report({'WARNING'}, "Choose a target UV map")
            return {'CANCELLED'}
        try:
            coverage, bindings, outputs = sdf_remap.prepare(
                mask, active=self.target == 'ACTIVE',
                target_name=self.uv_name if self.target == 'NAMED' else "", previous_name=self.previous_name,
                reference_file=self.reference_file, merge=self.merge_white)
        except (ValueError, OSError, RuntimeError) as ex:
            self.report({'WARNING'}, str(ex))
            return {'CANCELLED'}
        for src, name, snapshot in bindings:
            src.uv_name, src.uv_snapshot = name, snapshot
        mask.coverage = coverage
        for key, bitmap in zip(mask.keys, outputs):
            key.bitmap = bitmap
        _texture_token = None
        _geometry.clear()
        _view_pickers.clear()
        redraw(None, context)
        return {'FINISHED'}


class SDF_OT_remove_mask(SDFPaintMode, Operator):
    bl_idname = "sdf.remove_mask"
    bl_label = "Delete Mask and Keys"
    bl_options = {'UNDO'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        s = context.scene.sdf_editor
        if s.mask is None:
            return {'CANCELLED'}
        _geometry.clear()
        _view_pickers.clear()
        s.masks.remove(s.index)
        s.index = max(0, min(s.index, len(s.masks) - 1))
        return {'FINISHED'}


def paint_base(mask, value):
    key = key_at(mask, value)
    if key:
        return field.decode(key.bitmap, mask.size)
    return field.preview(key_images(mask), value, mask.coverage, mask.size)


def write_key(mask, value, bits):
    bitmap = field.encode(bits)
    key = key_at(mask, value)
    if key is None:
        key = mask.keys.add()
        key.value = value
    key.bitmap = bitmap


class SDF_OT_toggle_auto_key(SDFPaintMode, Operator):
    bl_idname = "sdf.toggle_auto_key"
    bl_label = "SDF Auto Key"

    def execute(self, context):
        settings = context.scene.sdf_editor
        settings.auto_key = not settings.auto_key
        return {'FINISHED'}


class SDF_OT_key(SDFPaintMode, Operator):
    bl_idname = "sdf.key"
    bl_label = "Edit SDF Key"
    bl_options = {'UNDO'}
    remove: BoolProperty()

    @classmethod
    def description(cls, context, properties):
        return "Remove the current key" if properties.remove else "Add a paintable key at this value"

    def execute(self, context):
        s = context.scene.sdf_editor
        mask = s.mask
        if mask is None:
            return {'CANCELLED'}
        key = key_at(mask, s.value)
        if self.remove and key:
            mask.keys.remove(next(i for i, item in enumerate(mask.keys) if item == key))
        elif not self.remove and key is None:
            write_key(mask, s.value, paint_base(mask, s.value))
        else:
            return {'CANCELLED'}
        redraw(s, context)
        return {'FINISHED'}


class SDF_OT_jump(SDFPaintMode, Operator):
    bl_idname = "sdf.jump"
    bl_label = "Go to SDF Key"
    value: FloatProperty()

    def execute(self, context):
        context.scene.sdf_editor.value = self.value
        return {'FINISHED'}


def active_sdf_tool(context):
    tools = context.workspace.tools
    if context.area.type == 'VIEW_3D':
        tool = tools.from_space_view3d_mode('PAINT_SDF')
    elif context.area.ui_type == 'SDF':
        tool = tools.from_space_image_mode('SDF')
    else:
        return False
    return tool.idname if tool else None


def bucket_active(context):
    return active_sdf_tool(context) == 'builtin.sdf_fill'


class SDF_OT_fill(Operator):
    bl_idname = "sdf.fill"
    bl_label = "Fill SDF Key"
    bl_options = {'UNDO'}
    mode: EnumProperty(items=(
        ('BUCKET', "Connected Region", "Fill matching connected texels at the click"),
        ('ALL', "Entire Mask", "Fill all texels in the current mask"),
        ('NEG_X', "Local X < 0", ""), ('POS_X', "Local X > 0", ""),
        ('LOW_U', "U < 0.5", ""), ('HIGH_U', "U > 0.5", "")))
    uv: FloatVectorProperty(size=2, options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        s = context.scene.sdf_editor
        return bool(context.mode == 'PAINT_SDF' and not active_picker(context) and s.mask and (s.auto_key or key_at(s.mask, s.value)))

    def execute(self, context):
        return {'FINISHED'} if fill_key(context, self.mode, self.uv) else {'CANCELLED'}


def fill_key(context, mode, uv):
    s = context.scene.sdf_editor
    mask = s.mask
    original = paint_base(mask, s.value)
    bits = original.copy()
    coverage = field.decode(mask.coverage, mask.size)
    if mode == 'BUCKET':
        if not field.flood_fill(bits, coverage, uv, s.white):
            return False
    else:
        target = coverage
        if mode in {'LOW_U', 'HIGH_U'}:
            u = (np.arange(mask.size) + .5) / mask.size
            target = coverage & ((u < .5) if mode == 'LOW_U' else (u > .5))
        elif mode in {'NEG_X', 'POS_X'}:
            target = np.zeros_like(coverage)
            sign = 1 if mode == 'POS_X' else -1
            dg = context.evaluated_depsgraph_get()
            for source in mask.sources:
                if not source.object or source.object.mode != 'SDF_PAINT':
                    continue
                surface = geometry(source, dg)
                if surface is None:
                    continue
                _, positions, coords = surface
                for sides, uv in zip(positions[:, :, 0] * sign, coords):
                    if sides.max() > 0:
                        field.raster_triangle(target, uv, sides if sides.min() <= 0 else None)
            target &= coverage
        bits[target] = s.white
    if np.array_equal(original, bits):
        return False
    write_key(mask, s.value, bits)
    redraw(s, context)
    return True


class SDF_MT_fill(Menu):
    bl_label = "Fill"

    def draw(self, context):
        layout = self.layout
        layout.operator("wm.tool_set_by_id", text="Bucket Fill", icon='BRUSH_DATA').name = "builtin.sdf_fill"
        layout.separator()
        layout.operator("sdf.fill", text="Entire Mask").mode = 'ALL'
        halves = (('NEG_X', 'Local X < 0'), ('POS_X', 'Local X > 0')) if context.area.type == 'VIEW_3D' else (
            ('LOW_U', 'U < 0.5'), ('HIGH_U', 'U > 0.5'))
        for mode, label in halves:
            layout.operator("sdf.fill", text=label).mode = mode


def focus_conflict(context, error):
    settings = context.scene.sdf_editor
    settings.mask.conflict_pair = (error.start, error.end)
    settings.value = error.start
    redraw(settings, context)


class SDF_OT_check_conflicts(Operator):
    bl_idname = "sdf.check_conflicts"
    bl_label = "Check Conflicts"
    bl_description = "Jump to the first white-to-black transition and highlight its pixels"
    clear: BoolProperty(default=False, options={'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        mask = context.scene.sdf_editor.mask
        return bool(context.mode == 'PAINT_SDF' and mask and mask.keys)

    def execute(self, context):
        mask = context.scene.sdf_editor.mask
        if not self.clear:
            try:
                field.check_keys(key_images(mask), mask.coverage, mask.size)
            except field.KeyConflict as error:
                focus_conflict(context, error)
                self.report({'WARNING'}, str(error))
                return {'FINISHED'}
            self.report({'INFO'}, "No SDF key conflicts")
        mask.conflict_pair = (-1, -1)
        redraw(None, context)
        return {'FINISHED'}


class SDF_OT_export(Operator, ExportHelper):
    bl_idname = "sdf.export"
    bl_label = "Export SDF"
    filename_ext = ".png"
    filter_glob: StringProperty(default="*.png", options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        mask = context.scene.sdf_editor.mask
        return context.mode == 'PAINT_SDF' and mask is not None and bool(mask.keys)

    def invoke(self, context, event):
        mask = context.scene.sdf_editor.mask
        try:
            field.check_keys(key_images(mask), mask.coverage, mask.size)
        except field.KeyConflict as error:
            focus_conflict(context, error)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        return ExportHelper.invoke(self, context, event)

    def execute(self, context):
        mask = context.scene.sdf_editor.mask
        try:
            if not self.filepath.strip() or self.filepath.endswith(("/", "\\")):
                raise ValueError("Choose a PNG filename before exporting")
            path = Path(bpy.path.abspath(self.filepath))
            if path.is_dir():
                raise ValueError("Choose a PNG filename before exporting")
            if not path.parent.is_dir():
                raise ValueError(f"Export folder does not exist: {path.parent}")
            values = field.thresholds(key_images(mask), mask.coverage, mask.size)
            field.write_png(path, values)
        except field.KeyConflict as error:
            focus_conflict(context, error)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        except (ValueError, OSError) as ex:
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}
        mask.conflict_pair = (-1, -1)
        redraw(None, context)
        return {'FINISHED'}


@lru_cache(maxsize=16)
def face_indices(data):
    return frozenset(map(int, data.split(",")))


# Evaluated triangles are shared by the viewport overlay and brush picking.
# Keep the face membership snapshot tied to the original topology.
_geometry = {}


def mesh_array(collection, prop, width, dtype):
    data = np.empty(len(collection) * width, dtype=dtype)
    collection.foreach_get(prop, data)
    return data if width == 1 else data.reshape(-1, width)


def geometry(source, depsgraph):
    """Evaluated triangle positions and UVs, in Blender's polygon order."""
    obj = source.object
    token = (depsgraph.as_pointer(), obj.as_pointer(), source.faces, source.uv_name)
    if token in _geometry:
        return _geometry[token]
    mesh = obj.evaluated_get(depsgraph).data
    layer = mesh.uv_layers.get(source.uv_name)
    surface = None
    if layer:
        originals = mesh_array(mesh.polygons, "original_index", 1, np.int32)
        # Deformation-only evaluation can share the base topology without
        # allocating an original-index layer. Check connectivity before using it.
        if len(originals) == len(obj.data.polygons) and np.all(originals == -1):
            if (np.array_equal(mesh_array(mesh.polygons, "loop_start", 1, np.int32),
                               mesh_array(obj.data.polygons, "loop_start", 1, np.int32)) and
                np.array_equal(mesh_array(mesh.loops, "vertex_index", 1, np.int32),
                               mesh_array(obj.data.loops, "vertex_index", 1, np.int32))):
                originals = np.arange(len(originals), dtype=np.int32)
        faces = mesh_array(mesh.loop_triangles, "polygon_index", 1, np.int32)
        keep = np.isin(originals[faces], tuple(face_indices(source.faces)))
        if keep.any():
            vertices = mesh_array(mesh.loop_triangles, "vertices", 3, np.int32)[keep]
            loops = mesh_array(mesh.loop_triangles, "loops", 3, np.int32)[keep]
            surface = (faces[keep], mesh_array(mesh.vertices, "co", 3, np.float32)[vertices],
                       mesh_array(layer.uv, "vector", 2, np.float32)[loops])
    _geometry[token] = surface
    return surface


_view_pickers = {}


def surface_tree(source, dg):
    surface = geometry(source, dg)
    if surface is None:
        return None
    key = ('pick', id(surface))
    if key not in _geometry:
        mesh = source.object.evaluated_get(dg).data
        _geometry[key] = SurfaceTree(mesh, surface, mesh_array)
    return _geometry[key]


def viewport_picker(context, region, mask):
    rv = context.region_data or context.space_data.region_3d
    key = (region.as_pointer(), region.width, region.height, context.evaluated_depsgraph_get().as_pointer(),
           tuple(tuple(row) for row in rv.perspective_matrix),
           tuple((s.as_pointer(), s.object.as_pointer() if s.object else 0) for s in mask.sources))
    picker = _view_pickers.get(key)
    if picker is None:
        if len(_view_pickers) >= 4:
            _view_pickers.clear()
        picker = _view_pickers[key] = ViewPicker(context, region, rv, mask)
    return picker


def brush_hit(context, region, xy, mask):
    return viewport_picker(context, region, mask).pick(context, xy, surface_tree)


def hit_uv(context, region, xy, mask):
    hit = brush_hit(context, region, xy, mask)
    return hit[0].xy if hit else None


def draw_brush_cursor(context, xy):
    import gpu
    from gpu_extras.batch import batch_for_shader

    settings = context.scene.sdf_editor
    mask = settings.mask
    if not in_paint_region(context) or active_picker(context) or bucket_active(context):
        return
    region = context.region
    if context.area.ui_type == 'SDF' and sdf_controls.CanvasControls(context).at(xy[0]-region.x, xy[1]-region.y):
        return
    brush_radius = _stroke.live_radius if _stroke else settings.radius
    radius = brush_radius / (mask.size if mask else 512)
    u, v = Vector((settings.radius, 0)), Vector((0, settings.radius))
    if context.area.ui_type == 'SDF':
        origin = region.view2d.view_to_region(0, 0, clip=False)
        unit = region.view2d.view_to_region(1, 1, clip=False)
        u.x = (unit[0] - origin[0]) * radius
        v.y = (unit[1] - origin[1]) * radius
    elif mask:
        local_xy = (xy[0] - region.x, xy[1] - region.y)
        hit = brush_hit(context, region, local_xy, mask)
        if hit:
            center, verts, uv, matrix, _obj = hit
            rv = context.region_data or context.space_data.region_3d
            def project(du, dv):
                point = barycentric_transform(center + Vector((du, dv, 0)), *uv, *verts)
                return view3d_utils.location_3d_to_region_2d(region, rv, matrix @ point)
            if (uv[1] - uv[0]).cross(uv[2] - uv[0]).length_squared > 1e-18:
                origin, right, top = project(0, 0), project(radius, 0), project(0, radius)
                if origin is not None and right is not None and top is not None:
                    u, v = right - origin, top - origin
    ring = [u * cos(tau * i / 64) + v * sin(tau * i / 64) for i in range(65)]
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    shader.bind()
    ink = float(settings.white)
    for offset, value in ((1.5, 1 - ink), (0, ink)):
        coords = [Vector(xy) + d.normalized() * (max(2, d.length) + offset) for d in ring]
        shader.uniform_float("color", (value, value, value, 1))
        batch_for_shader(shader, 'LINE_STRIP', {'pos': coords}).draw(shader)



_stroke = None
_texture = None
_texture_token = None
_coverage_pixels = _coverage_texture = _canvas_shader = _wire_shader = None


def in_paint_region(context):
    return (context.mode == 'PAINT_SDF' and context.area and context.region and context.region.type == 'WINDOW'
            and (context.area.ui_type == 'SDF' or context.area.type == 'VIEW_3D'))


class SDF_OT_select_tool(Operator):
    bl_idname = "sdf.select_tool"
    bl_label = "Select SDF Drawing Tool"
    tool: EnumProperty(items=(('BRUSH', "SDF Brush", ""), ('PEN', "SDF Pen", "")))

    @classmethod
    def poll(cls, context):
        return bool(in_paint_region(context) and not active_picker(context))

    def execute(self, context):
        name = 'builtin.sdf_paint' if self.tool == 'BRUSH' else 'builtin.sdf_pen'
        return bpy.ops.wm.tool_set_by_id(name=name)


class SDF_OT_brush_setting(Operator):
    bl_idname = "sdf.brush_setting"
    bl_label = "Change SDF Brush"
    action: EnumProperty(items=[(v, v.title(), "") for v in ('SMALLER', 'LARGER', 'SWAP', 'BLACK', 'WHITE')])

    @classmethod
    def poll(cls, context):
        return bool(in_paint_region(context))

    def execute(self, context):
        settings = context.scene.sdf_editor
        if self.action == 'SWAP':
            settings.white = not settings.white
        elif self.action in {'BLACK', 'WHITE'}:
            settings.white = self.action == 'WHITE'
        elif self.action == 'LARGER':
            settings.radius += max(1, round(settings.radius * .2))
        else:
            settings.radius -= max(1, round(settings.radius / 6))
        return {'FINISHED'}


def continuous_hits(a, b):
    if a is None or b is None or a[4] != b[4]:
        return False
    # Two shared corners in geometry *and* UV space identify a continuous
    # edge (or the same triangle). A seam or an occluding mesh starts a new dab.
    return sum(any(p == q and uv == other_uv for q, other_uv in zip(b[1], b[2]))
               for p, uv in zip(a[1], a[2])) >= 2


class SDF_OT_radius(Operator):
    bl_idname = "sdf.radius"
    bl_label = "Adjust SDF Brush Radius"

    @classmethod
    def poll(cls, context):
        return bool(in_paint_region(context))

    def invoke(self, context, event):
        bpy.ops.wm.radial_control('INVOKE_DEFAULT', data_path_primary="window_manager.sdf_brush_radius")
        return {'FINISHED'}


def pressure_radius(stroke, event):
    if getattr(event, "value", None) == 'RELEASE' and getattr(stroke, "previous", None) is not None:
        return stroke.live_radius
    if getattr(stroke, "use_pressure", True) and getattr(event, "is_tablet", False):
        pressure = max(0.0, min(1.0, event.pressure))
        if pressure == 0 and getattr(event, "value", None) == 'RELEASE':
            return getattr(stroke, "live_radius", 0.0)
        return stroke.radius * pressure
    return stroke.radius


def paint_to(stroke, context, current, radius):
    if context.area.ui_type == 'SDF' and sdf_controls.CanvasControls(context).at(*current):
        stroke.previous = stroke.previous_uv = None
        return False
    touched = False
    previous_radius = stroke.live_radius if stroke.previous is not None else radius
    stroke.live_radius = radius
    start = stroke.previous or current
    if context.area.type == 'VIEW_3D':
        region = context.region
        picker = viewport_picker(context, region, stroke.mask)
        def sample(point):
            return picker.pick(context, point, surface_tree)
        def segment(left, first, r0, right, last, r1):
            nonlocal touched
            if continuous_hits(first, last):
                touched |= field.stroke_segment(stroke.bits, stroke.coverage, first[0].xy, last[0].xy, r1, stroke.white, r0)
            elif hypot(right[0] - left[0], right[1] - left[1]) > .5 and (first or last):
                mid = ((left[0] + right[0]) * .5, (left[1] + right[1]) * .5)
                middle = sample(mid)
                segment(left, first, r0, mid, middle, (r0 + r1) / 2)
                segment(mid, middle, (r0 + r1) / 2, right, last, r1)
            elif last and r1 > 0:
                touched |= field.stamp(stroke.bits, stroke.coverage, last[0].xy, r1, stroke.white)
        steps = max(1, ceil(hypot(current[0] - start[0], current[1] - start[1]) / 2))
        left, first = start, getattr(stroke, 'previous_hit', None)
        for i in range(1, steps + 1):
            right = tuple(a + (b - a) * i / steps for a, b in zip(start, current))
            last = sample(right)
            segment(left, first, previous_radius + (radius - previous_radius) * (i - 1) / steps,
                    right, last, previous_radius + (radius - previous_radius) * i / steps)
            left, first = right, last
        stroke.previous_hit = first
    else:
        uv = context.region.view2d.region_to_view(*(round(v) for v in current))
        touched = field.stroke_segment(stroke.bits, stroke.coverage, stroke.previous_uv or uv, uv, radius, stroke.white, previous_radius)
        stroke.previous_uv = uv
    stroke.previous = current
    return touched


class SDF_OT_stroke(Operator):
    bl_idname = "sdf.stroke"
    bl_label = "Paint SDF Key"
    bl_options = {'UNDO', 'BLOCKING'}

    @classmethod
    def poll(cls, context):
        return bool(in_paint_region(context))

    def dab(self, context, event):
        global _texture_token
        touched = paint_to(self, context, (event.mouse_region_x, event.mouse_region_y), pressure_radius(self, event))
        if touched and getattr(self, "needs_key", False):
            self.record_key()
        _texture_token = None
        # Blender queues intermediate pen/mouse samples before the final move.
        # Paint all samples, but request just one redraw for this event batch.
        if getattr(event, "type", None) != 'INBETWEEN_MOUSEMOVE':
            redraw(None, context)

    def invoke(self, context, event):
        global _stroke
        if _stroke is not None:
            return {'CANCELLED'}
        s = context.scene.sdf_editor
        picker = active_picker(context)
        self.idle = bool(picker or not s.mask or not (s.auto_key or key_at(s.mask, s.value)))
        if self.idle:
            # Own the whole press/drag/release gesture even with nothing to paint.
            # Returning PASS_THROUGH here would let the transform keymaps drag the mesh.
            if picker and context.area == picker.area:
                uv = context.region.view2d.region_to_view(event.mouse_region_x, event.mouse_region_y)
                hits = picker.hits(uv)
                if len(hits) == 1:
                    picker.toggle(hits[0])
                    redraw(None, context)
                elif hits:
                    def menu(menu, _context):
                        for i in hits:
                            op = menu.layout.operator("sdf.island_action", text=picker.islands[i][4],
                                                      depress=i in picker.selected)
                            op.action, op.index = 'TOGGLE', i
                    context.window_manager.popup_menu(menu, title="Overlapping UV Islands")
                    return {'CANCELLED'}
            elif not picker:
                self.report({'INFO'}, "Add a mask to paint" if not s.mask else "Add or select a paint key")
            context.window_manager.modal_handler_add(self)
            return {'RUNNING_MODAL'}
        if bucket_active(context):
            xy = (event.mouse_region_x, event.mouse_region_y)
            uv = (hit_uv(context, context.region, xy, s.mask) if context.area.type == 'VIEW_3D' else
                  context.region.view2d.region_to_view(*xy))
            self.fill_uv = tuple(uv) if uv is not None else None
            self.idle = True
            context.window_manager.modal_handler_add(self)
            return {'RUNNING_MODAL'}
        self.mask = s.mask
        self.key_value = s.value
        self.needs_key = key_at(self.mask, self.key_value) is None
        self.created_key = False
        self.base_bits = paint_base(s.mask, s.value)
        self.bits = self.base_bits.copy()
        self.coverage = field.decode(s.mask.coverage, s.mask.size)
        self.white, self.radius = s.white, s.radius
        self.use_pressure = s.use_pressure
        self.live_radius = self.radius
        self.previous = None
        self.previous_uv = None
        self.previous_hit = None
        self.paint_region = context.region
        _stroke = self
        self.pen = None
        if active_sdf_tool(context) == 'builtin.sdf_pen':
            self.pen = PenPath()
            context.workspace.status_text_set(bpy.app.translations.pgettext_iface(
                "Click: add point   •   Drag: curve handle   •   Backspace: remove point   •   Enter: finish   •   Esc: cancel"))
            self.pen_add(context, event)
        else:
            self.dab(context, event)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def record_key(self):
        write_key(self.mask, self.key_value, self.base_bits)
        self.needs_key = False
        self.created_key = True

    def discard_recorded_key(self):
        if getattr(self, "created_key", False):
            for index, key in enumerate(self.mask.keys):
                if abs(key.value - self.key_value) < 1e-6:
                    self.mask.keys.remove(index)
                    break
            self.created_key = False
            self.needs_key = True

    def finish_paint(self, context):
        changed = self.created_key or not np.array_equal(self.bits, self.base_bits)
        if changed:
            write_key(self.mask, self.key_value, self.bits)
            self.created_key = False
        self.cancel(context)
        return {'FINISHED'} if changed else {'CANCELLED'}

    def cancel(self, context):
        global _stroke, _texture_token
        self.discard_recorded_key()
        if getattr(self, "pen", None) is not None:
            context.workspace.status_text_set(None)
        _stroke = _texture_token = None
        redraw(None, context)

    def pen_add(self, context, event):
        point = (event.mouse_region_x, event.mouse_region_y)
        if context.area.type == 'VIEW_3D':
            uv = hit_uv(context, context.region, point, self.mask)
        else:
            if sdf_controls.CanvasControls(context).at(*point):
                return
            uv = context.region.view2d.region_to_view(*point)
        if uv is None:
            return
        x, y = np.floor(np.asarray(uv) * self.mask.size).astype(int)
        if not (0 <= x < self.mask.size and 0 <= y < self.mask.size and self.coverage[y, x]):
            return
        if self.needs_key:
            self.record_key()
        self.pen.base = self.bits.copy()
        self.pen.add(point, pressure_radius(self, event))
        self.pen_preview(context)

    def pen_preview(self, context, rebuild=False):
        global _texture_token
        nodes = self.pen.nodes
        if rebuild:
            self.bits = self.base_bits.copy()
            indices = range(1, len(nodes))
        else:
            self.bits[:] = self.pen.base
            indices = (len(nodes)-1,) if len(nodes) > 1 else ()
        for index in indices:
            self.previous = self.previous_uv = self.previous_hit = None
            for point, radius in self.pen.samples(index):
                paint_to(self, context, point, radius)
        _texture_token = None
        redraw(None, context)

    def pen_modal(self, context, event):
        pen = self.pen
        if event.type in {'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            if len(pen.nodes) >= 2:
                return self.finish_paint(context)
            self.cancel(context)
            return {'CANCELLED'}
        remove_point = event.type == 'BACK_SPACE' or (event.type == 'Z' and event.ctrl)
        if remove_point and event.value == 'PRESS' and pen.nodes:
            pen.nodes.pop()
            if not pen.nodes:
                self.discard_recorded_key()
            pen.dragging = False
            self.pen_preview(context, rebuild=True)
        elif event.type == 'LEFTMOUSE':
            if event.value == 'PRESS':
                self.pen_add(context, event)
            elif event.value == 'RELEASE':
                pen.dragging = False
        elif event.type == 'MOUSEMOVE':
            point = (event.mouse_region_x, event.mouse_region_y)
            if pen.dragging:
                pen.tangent(point, pressure_radius(self, event))
                self.pen_preview(context)
            else:
                pen.hover = point
                context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if context.mode != 'PAINT_SDF' or event.type in {'ESC', 'RIGHTMOUSE', 'WINDOW_DEACTIVATE'}:
            self.cancel(context)
            return {'CANCELLED'}
        if self.idle:
            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                uv = getattr(self, "fill_uv", None)
                if uv is not None and bpy.ops.sdf.fill.poll() and fill_key(context, 'BUCKET', uv):
                    return {'FINISHED'}
                return {'CANCELLED'}
            return {'RUNNING_MODAL'}
        if self.pen is not None:
            return self.pen_modal(context, event)
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self.dab(context, event)
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            self.dab(context, event)
            return self.finish_paint(context)
        return {'RUNNING_MODAL'}


def grayscale_texture(bits):
    import gpu
    pixels = np.asarray(bits, dtype=np.float32, order='C')
    return gpu.types.GPUTexture((pixels.shape[1], pixels.shape[0]), format='R8',
                                data=gpu.types.Buffer('FLOAT', pixels.size, pixels.ravel()))


def canvas_shader(wire=False):
    import gpu
    global _canvas_shader, _wire_shader
    cached = _wire_shader if wire else _canvas_shader
    if cached is None:
        interface = gpu.types.GPUStageInterfaceInfo("sdf_wire_uv" if wire else "sdf_canvas_uv")
        interface.smooth('VEC2', "uv")
        info = gpu.types.GPUShaderCreateInfo()
        info.vertex_in(0, 'VEC3', "pos")
        info.vertex_in(1, 'VEC2', "texCoord")
        if wire:
            interface.no_perspective('VEC3', "edgeUV")
            info.vertex_in(2, 'VEC3', "edgeCoord")
            info.push_constant('FLOAT', "opacity")
            info.push_constant('INT', "wireOnly")
        info.vertex_out(interface)
        info.push_constant('MAT4', "mvp")
        info.sampler(0, 'FLOAT_2D', "inkMap")
        info.sampler(1, 'FLOAT_2D', "maskMap")
        info.sampler(2, 'FLOAT_2D', "onionMap")
        info.push_constant('INT', "compareMode")
        info.fragment_out(0, 'VEC4', "color")
        edge_init = "edgeUV = edgeCoord;" if wire else ""
        info.vertex_source("void main() { " + edge_init + " uv = texCoord; gl_Position = mvp * vec4(pos, 1.0); }")
        base_color = """
            vec3 sdfBaseColor(float ink) {
                if (compareMode != 1) return vec3(ink);
                float refInk = texture(onionMap, uv).r;
                vec3 red = mix(vec3(0.42, 0.012, 0.015), vec3(0.98, 0.24, 0.18), refInk);
                return mix(vec3(ink), red, 0.52);
            }
            vec3 highlightConflict(vec3 base) {
                if (compareMode < 2) return base;
                ivec2 size = textureSize(inkMap, 0);
                ivec2 p = clamp(ivec2(floor(uv * vec2(size))), ivec2(0), size - 1);
                bool ink = texelFetch(inkMap, p, 0).r > 0.5;
                bool other = texelFetch(onionMap, p, 0).r > 0.5;
                bool bad = compareMode == 2 ? (ink && !other) : (other && !ink);
                return bad && texelFetch(maskMap, p, 0).r > 0.5 ? vec3(1.0, 0.0, 0.0) : base;
            }
        """
        fragment = sdf_wire.FRAGMENT if wire else (
            "void main() { color = vec4(highlightConflict(sdfBaseColor(texture(inkMap, uv).r)), texture(maskMap, uv).r); }")
        info.fragment_source(base_color + fragment)
        cached = gpu.shader.create_from_info(info)
        if wire:
            _wire_shader = cached
        else:
            _canvas_shader = cached
    return cached


def onion_reference(mask, value, mode):
    if mode == 'PREVIOUS':
        return max((key for key in mask.keys if value - key.value >= 1e-6),
                   key=lambda key: key.value, default=None)
    if mode == 'NEXT':
        return min((key for key in mask.keys if key.value - value >= 1e-6),
                   key=lambda key: key.value, default=None)
    return None


@lru_cache(maxsize=1)
def onion_texture(bitmap, size):
    return grayscale_texture(field.decode(bitmap, size))


def textures(mask, value, onion_mode):
    global _texture, _texture_token, _coverage_pixels, _coverage_texture
    painting = _stroke and _stroke.mask == mask and abs(_stroke.key_value - value) < 1e-6
    if painting:
        token = id(_stroke.bits)
    else:
        keys = key_images(mask)
        token = (mask.coverage, mask.size, keys, value)
    start, end = mask.conflict_pair
    reference = None
    compare_mode = 0
    if start >= 0 and (abs(value-start) < 1e-6 or abs(value-end) < 1e-6):
        if key_at(mask, start) and key_at(mask, end) and not any(start < k.value < end for k in mask.keys):
            at_start = abs(value-start) < 1e-6
            reference = key_at(mask, end if at_start else start)
            compare_mode = 2 if at_start else 3
    if reference is None:
        reference = onion_reference(mask, value, onion_mode)
        compare_mode = int(reference is not None)
    ghost = onion_texture(reference.bitmap, mask.size) if reference is not None else None
    if token != _texture_token:
        bits = (_stroke.bits if painting else
                field.preview(keys, value, mask.coverage, mask.size))
        coverage = _stroke.coverage if painting else field.decode(mask.coverage, mask.size)
        if coverage is not _coverage_pixels:
            _coverage_texture = grayscale_texture(coverage)
            _coverage_pixels = coverage
        _texture = grayscale_texture(bits)
        _texture_token = token
    return _texture, _coverage_texture, ghost, compare_mode


def wire_strength(context, obj=None):
    space = context.space_data
    settings = context.scene.sdf_editor
    if not space.overlay.show_overlays:
        return 0.0
    if obj and (space.overlay.show_wireframes or obj.show_wire or
                space.shading.type == 'WIREFRAME' or obj.display_type == 'WIRE'):
        return settings.wire_opacity * space.overlay.wireframe_opacity
    return settings.wire_opacity if settings.show_wire else 0.0


def bind_canvas_shader(ink, coverage, ghost, compare_mode, strength=0.0, wire_only=False):
    shader = canvas_shader(wire=strength > 0)
    shader.bind()
    shader.uniform_sampler("inkMap", ink)
    shader.uniform_sampler("maskMap", coverage)
    shader.uniform_sampler("onionMap", ink if ghost is None else ghost)
    shader.uniform_int("compareMode", compare_mode)
    if strength > 0:
        shader.uniform_float("opacity", strength)
        shader.uniform_int("wireOnly", int(wire_only))
    return shader


def draw_canvas(is_3d):
    import gpu
    from gpu_extras.batch import batch_for_shader
    ctx = bpy.context
    settings = ctx.scene.sdf_editor
    if ctx.mode != 'PAINT_SDF' or (not is_3d and ctx.area.ui_type != 'SDF'):
        return
    picker = active_picker(ctx)
    if picker and ctx.area == picker.area and not is_3d:
        picker.draw(ctx.region)
        return
    mask = settings.mask
    if not mask:
        return
    blend, depth, write = gpu.state.blend_get(), gpu.state.depth_test_get(), gpu.state.depth_mask_get()
    try:
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('LESS_EQUAL' if is_3d else 'NONE')
        gpu.state.depth_mask_set(False)
        ink, coverage, ghost, compare_mode = textures(mask, settings.value, settings.onion_skin)
        if is_3d:
            dg = ctx.evaluated_depsgraph_get()
            for source in mask.sources:
                obj = source.object
                if not obj or obj.mode != 'SDF_PAINT' or not obj.visible_get(view_layer=ctx.view_layer, viewport=ctx.space_data):
                    continue
                surface = geometry(source, dg)
                if surface is not None:
                    strength = wire_strength(ctx, obj)
                    shader = bind_canvas_shader(ink, coverage, ghost, compare_mode, strength)
                    if strength > 0:
                        batch = sdf_wire.batch(source, dg, shader, True, surface, _geometry, mesh_array, face_indices)
                    else:
                        token = ('batch', dg.as_pointer(), obj.as_pointer(), source.faces, source.uv_name)
                        if token not in _geometry:
                            _geometry[token] = batch_for_shader(shader, 'TRIS', {
                                "pos": surface[1].reshape(-1, 3), "texCoord": surface[2].reshape(-1, 2)})
                        batch = _geometry[token]
                    # Wire-shaded viewports have no solid surface depth to hide rear edges.
                    gpu.state.depth_mask_set(strength > 0 and (ctx.space_data.shading.type == 'WIREFRAME' or obj.display_type == 'WIRE'))
                    # Match Blender's overlay polygon offset. Equal depth is
                    # unstable on angled faces when the viewport jitters its samples.
                    with gpu.matrix.push_pop_projection(), gpu.matrix.push_pop():
                        projection = gpu.matrix.get_projection_matrix()
                        if projection[3][3] > .5:
                            projection[2][3] -= .00001 * ctx.region_data.view_distance
                        else:
                            projection[2][3] *= 1.0025
                        gpu.matrix.load_projection_matrix(projection)
                        gpu.matrix.multiply_matrix(obj.evaluated_get(dg).matrix_world)
                        shader.uniform_float("mvp", projection @ gpu.matrix.get_model_view_matrix())
                        batch.draw(shader)
        else:
            shader = bind_canvas_shader(ink, coverage, ghost, compare_mode)
            shader.uniform_float("mvp", gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix())
            corners = ((0, 0), (1, 0), (1, 1), (0, 1))
            view = ctx.region.view2d
            positions = [(*view.view_to_region(*uv, clip=False), 0) for uv in corners]
            batch_for_shader(shader, 'TRIS', {"pos": positions, "texCoord": corners},
                             indices=((0, 1, 2), (0, 2, 3))).draw(shader)
            strength = wire_strength(ctx)
            if strength > 0:
                shader = bind_canvas_shader(ink, coverage, ghost, compare_mode, strength, wire_only=True)
                dg = ctx.evaluated_depsgraph_get()
                origin, end = positions[0], positions[2]
                with gpu.matrix.push_pop():
                    gpu.matrix.translate(origin)
                    gpu.matrix.scale((end[0]-origin[0], end[1]-origin[1], 1))
                    shader.uniform_float("mvp", gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix())
                    for source in mask.sources:
                        if source.object and source.object.type == 'MESH':
                            batch = sdf_wire.batch(source, dg, shader, False, None, _geometry, mesh_array, face_indices)
                            if batch:
                                batch.draw(shader)
    finally:
        gpu.state.blend_set(blend)
        gpu.state.depth_test_set(depth)
        gpu.state.depth_mask_set(write)


def draw_sources(layout, context, mask):
    for source in mask.sources:
        obj = source.object
        layout.label(text=f"{obj.name if obj else '(missing)'} / {source.uv_name}", icon='GROUP_UVS')
        if obj and obj.mode != 'SDF_PAINT':
            layout.label(text="Include this mesh when entering SDF Paint", icon='INFO')
        elif obj:
            evaluated = obj.evaluated_get(context.evaluated_depsgraph_get()).data
            if source.uv_name not in evaluated.uv_layers:
                layout.label(text="Restore the recorded UV map to paint in 3D", icon='INFO')
            elif geometry(source, context.evaluated_depsgraph_get()) is None:
                layout.label(text="No evaluated faces map to this mask", icon='INFO')


def draw_header(layout, context):
    if context.mode != 'PAINT_SDF':
        layout.label(text="Select mesh objects and enter SDF Paint", icon='INFO')
        return
    sdf_controls.initialize_space(context)
    picker = active_picker(context)
    if picker and picker.area == context.area:
        picker_controls(layout, picker)
        return
    s = context.scene.sdf_editor
    draw_brush_ink(layout, s)
    draw_brush_radius(layout, context)
    draw_wire_control(layout, context)
    draw_onion_control(layout, context)
    layout.menu("SDF_MT_fill", text="Fill")
    layout.operator("sdf.check_conflicts", icon='VIEWZOOM')
    if s.mask and s.mask.conflict_pair[0] >= 0:
        layout.operator("sdf.check_conflicts", text="Hide Markers", icon='X').clear = True
    export = layout.row(align=True)
    export.operator_context = 'INVOKE_REGION_WIN'
    export.operator("sdf.export", text="", icon='EXPORT')


def draw_canvas_controls():
    context = bpy.context
    if context.area.ui_type == 'SDF' and context.mode == 'PAINT_SDF' and not active_picker(context):
        sdf_controls.CanvasControls(context).draw()


@persistent
def invalidate_geometry(scene, depsgraph):
    global _picker
    _view_pickers.clear()
    if _picker and _picker.scene == scene:
        changed = {update.id.original for update in depsgraph.updates if update.is_updated_geometry}
        if any(obj not in scene.objects[:] or obj in changed or obj.data in changed for obj in _picker.objects):
            _picker = None
    if _geometry and any(update.is_updated_geometry for update in depsgraph.updates):
        _geometry.clear()


@persistent
def clear_runtime(*args):
    global _texture_token, _texture, _stroke, _picker, _coverage_pixels, _coverage_texture, _canvas_shader, _wire_shader
    onion_texture.cache_clear()
    _coverage_pixels = _coverage_texture = _canvas_shader = _wire_shader = None
    _picker = None
    _stroke = _texture_token = _texture = None
    _geometry.clear()
    _view_pickers.clear()
    for fn in (face_indices, field.fields, field.signed, field.decode):
        fn.cache_clear()


def hover_cursor(xy):
    if not _stroke:
        draw_brush_cursor(bpy.context, xy)


def stroke_cursor():
    ctx = bpy.context
    if _stroke and ctx.region == _stroke.paint_region and _stroke.pen is not None:
        _stroke.pen.draw()
        return
    if _stroke and ctx.region == _stroke.paint_region and _stroke.previous:
        # POST_PIXEL uses region coordinates, the paint-cursor callback uses window coordinates.
        import gpu
        r = ctx.region
        with gpu.matrix.push_pop():
            gpu.matrix.translate((-r.x, -r.y, 0))
            draw_brush_cursor(ctx, (r.x + _stroke.previous[0], r.y + _stroke.previous[1]))


@persistent
def reset_canvas_spaces(*_args):
    sdf_controls._spaces.clear()
    sdf_controls._number_inputs.clear()


_handles = []
_cursors = []


@persistent
def install_cursors(*_args):
    if not _cursors:
        for space in ('VIEW_3D', 'IMAGE_EDITOR'):
            _cursors.append(bpy.types.WindowManager.draw_cursor_add(hover_cursor, (), space, 'WINDOW'))


@persistent
def remove_cursors(*_args):
    for handle in _cursors:
        bpy.types.WindowManager.draw_cursor_remove(handle)
    _cursors.clear()


def register_runtime():
    bpy.types.Scene.sdf_editor = PointerProperty(type=SDFSettings)
    # WM-owned controls avoid property undo pushes from header number fields,
    # pressure toggles and the native F-key radial controller.
    bpy.types.WindowManager.sdf_brush_radius = IntProperty(
        name="Radius (texels)", min=1, max=256, default=10, options={'SKIP_SAVE'},
        get=lambda wm: bpy.context.scene.sdf_editor.radius,
        set=lambda wm, value: setattr(bpy.context.scene.sdf_editor, "radius", value))
    bpy.types.WindowManager.sdf_onion_skin = EnumProperty(
        name="Onion Skin", items=_ONION_MODES, options={'SKIP_SAVE'},
        get=lambda wm: next(number for ident, _, _, number in _ONION_MODES
                            if ident == bpy.context.scene.sdf_editor.onion_skin),
        set=lambda wm, number: setattr(bpy.context.scene.sdf_editor, "onion_skin", _ONION_MODES[number][0]))
    bpy.types.WindowManager.sdf_show_wire = BoolProperty(
        name="SDF Wireframe", default=False, options={'SKIP_SAVE'},
        description="Show the mask mesh edges over the SDF in both paint views",
        get=lambda wm: bpy.context.scene.sdf_editor.show_wire,
        set=lambda wm, value: setattr(bpy.context.scene.sdf_editor, "show_wire", value))
    bpy.types.WindowManager.sdf_wire_opacity = FloatProperty(
        name="Wire Opacity", min=0, max=1, default=.65, subtype='FACTOR', options={'SKIP_SAVE'},
        get=lambda wm: bpy.context.scene.sdf_editor.wire_opacity,
        set=lambda wm, value: setattr(bpy.context.scene.sdf_editor, "wire_opacity", value))
    bpy.types.WindowManager.sdf_pressure_enabled = BoolProperty(
        name="Use Pressure for Radius", default=True, options={'SKIP_SAVE'},
        get=lambda wm: bpy.context.scene.sdf_editor.use_pressure,
        set=lambda wm, value: setattr(bpy.context.scene.sdf_editor, "use_pressure", value))
    for space, stage, is_3d in ((bpy.types.SpaceImageEditor, 'POST_PIXEL', False),
                                (bpy.types.SpaceView3D, 'POST_VIEW', True)):
        _handles.append((space, space.draw_handler_add(draw_canvas, (is_3d,), 'WINDOW', stage)))
        _handles.append((space, space.draw_handler_add(stroke_cursor, (), 'WINDOW', 'POST_PIXEL')))
    image_space = bpy.types.SpaceImageEditor
    _handles.append((image_space, image_space.draw_handler_add(draw_canvas_controls, (), 'WINDOW', 'POST_PIXEL')))
    install_cursors()
    bpy.app.handlers.load_pre.append(reset_canvas_spaces)
    bpy.app.handlers.load_pre.append(remove_cursors)
    bpy.app.handlers.load_post.append(bind_legacy_uvs)
    for handlers in (bpy.app.handlers.undo_pre, bpy.app.handlers.redo_pre):
        handlers.append(remember_interaction)
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        handlers.append(restore_interaction)
    bpy.app.handlers.load_post.append(install_cursors)
    bpy.app.handlers.load_post_fail.append(install_cursors)
    for handlers in (bpy.app.handlers.load_pre, bpy.app.handlers.undo_pre, bpy.app.handlers.redo_pre):
        handlers.append(clear_runtime)
    bpy.app.handlers.load_factory_startup_post.append(add_factory_workspace)
    bpy.app.handlers.depsgraph_update_post.append(invalidate_geometry)


def unregister_runtime():
    for space, handle in _handles:
        space.draw_handler_remove(handle, 'WINDOW')
    _handles.clear()
    remove_cursors()
    bpy.app.handlers.load_pre.remove(reset_canvas_spaces)
    bpy.app.handlers.load_pre.remove(remove_cursors)
    bpy.app.handlers.load_post.remove(bind_legacy_uvs)
    for handlers in (bpy.app.handlers.undo_pre, bpy.app.handlers.redo_pre):
        handlers.remove(remember_interaction)
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        handlers.remove(restore_interaction)
    _undo_interaction.clear()
    del bpy.types.WindowManager.sdf_brush_radius
    del bpy.types.WindowManager.sdf_onion_skin
    del bpy.types.WindowManager.sdf_show_wire
    del bpy.types.WindowManager.sdf_wire_opacity
    del bpy.types.WindowManager.sdf_pressure_enabled
    bpy.app.handlers.load_post.remove(install_cursors)
    bpy.app.handlers.load_post_fail.remove(install_cursors)
    for handlers in (bpy.app.handlers.load_pre, bpy.app.handlers.undo_pre, bpy.app.handlers.redo_pre):
        handlers.remove(clear_runtime)
    bpy.app.handlers.load_factory_startup_post.remove(add_factory_workspace)
    bpy.app.handlers.depsgraph_update_post.remove(invalidate_geometry)
    clear_runtime()
    del bpy.types.Scene.sdf_editor


classes = (SDFKey, SDFSource, SDFMask, SDFSettings, SDF_OT_workspace, SDF_OT_add_mask, SDF_OT_edit_islands, SDF_OT_rematch_uv, SDF_OT_remove_mask,
           SDF_OT_toggle_auto_key, SDF_OT_key, SDF_OT_jump, SDF_OT_check_conflicts, SDF_OT_export, SDF_OT_radius, SDF_OT_select_tool, SDF_OT_brush_setting, SDF_OT_island_action, SDF_OT_stroke, SDF_OT_fill, SDF_MT_fill,
           sdf_controls.SDF_OT_mask_details, sdf_controls.SDF_OT_canvas_control)
