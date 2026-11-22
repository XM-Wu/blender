# SPDX-License-Identifier: GPL-2.0-or-later

import bpy
from pathlib import Path
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, StringProperty
from bpy.types import Menu, Operator
from bpy_extras.io_utils import ExportHelper
from bpy_extras import view3d_utils
from math import ceil, cos, hypot, sin, tau
from mathutils import Vector
from mathutils.geometry import barycentric_transform

from . import sdf_field as field
from .sdf_field import key_at, key_images
from .sdf_pick import geometry, face_indices, brush_hit, hit_uv, viewport_picker
from .sdf_pen import PenPath
from . import sdf_controls, sdf_pick, sdf_preview, sdf_remap, sdf_settings
from .sdf_settings import redraw, remember_interaction, restore_interaction

import numpy as np


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


def draw_onion_control(layout, context):
    icon = 'ONIONSKIN_OFF' if context.scene.sdf_editor.onion_skin == 'OFF' else 'ONIONSKIN_ON'
    layout.prop(context.window_manager, "sdf_onion_skin", text="Onion Skin", icon=icon)


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
    inferred_sources = set()
    if target is not None and target.keys:
        sdf_remap.bind_legacy(target)
        inferred_sources = {src.object for src in target.sources if src.inferred_snapshot}
        for src in target.sources:
            layer = src.object.data.uv_layers.get(src.uv_name) if src.object else None
            if layer is None or not src.uv_snapshot:
                raise ValueError("UV layout changed. Rematch UV before editing the island selection")
            current = sdf_remap.capture(src.object.data, layer, face_indices(src.faces))
            if not np.array_equal(sdf_remap.unpack(src.uv_snapshot, len(current)), current):
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
        clear_surface_caches()
    mask.coverage = field.encode(coverage)
    for obj, uv_name, faces, snapshot in prepared:
        source = mask.sources.add()
        source.object, source.uv_name = obj, uv_name
        source.faces = ",".join(map(str, faces))
        source.uv_snapshot = snapshot
        source.inferred_snapshot = obj in inferred_sources
    sdf_pick.clear_views()


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
    accept_inferred: BoolProperty(name="Use inferred original UVs", options={'SKIP_SAVE'},
                                  description="I confirm these inferred face UVs match the original painted layout")
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
        if not self.reference_file and not self.previous_name and sdf_remap.needs_original(mask):
            layout.label(text="Matching the outline cannot verify the original face UVs", icon='INFO')
            layout.prop(self, "accept_inferred")
        layout.prop(self, "merge_white")
        layout.label(text="Reproject all keys of the selected mask", icon='KEY_HLT')

    def execute(self, context):
        mask = context.scene.sdf_editor.mask
        if self.target == 'NAMED' and not self.uv_name:
            self.report({'WARNING'}, "Choose a target UV map")
            return {'CANCELLED'}
        try:
            coverage, bindings, outputs = sdf_remap.prepare(
                mask, active=self.target == 'ACTIVE',
                target_name=self.uv_name if self.target == 'NAMED' else "", previous_name=self.previous_name,
                reference_file=self.reference_file, merge=self.merge_white, accept_inferred=self.accept_inferred)
        except (ValueError, OSError, RuntimeError) as ex:
            self.report({'WARNING'}, str(ex))
            return {'CANCELLED'}
        for src, name, snapshot in bindings:
            src.uv_name, src.uv_snapshot = name, snapshot
            src.inferred_snapshot = False
        mask.coverage = coverage
        for key, bitmap in zip(mask.keys, outputs):
            key.bitmap = bitmap
        sdf_preview.invalidate_texture()
        clear_surface_caches()
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
        clear_surface_caches()
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
            except ValueError as error:
                self.report({'ERROR'}, str(error))
                return {'CANCELLED'}
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
        except ValueError as error:
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
            return picker.pick(context, point)
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
        touched = paint_to(self, context, (event.mouse_region_x, event.mouse_region_y), pressure_radius(self, event))
        if touched and getattr(self, "needs_key", False):
            self.record_key()
        sdf_preview.invalidate_texture()
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
            self.fill_target = (s.mask.as_pointer(), s.value, s.white)
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
        global _stroke
        if _stroke is not self:
            return
        self.discard_recorded_key()
        if getattr(self, "pen", None) is not None:
            context.workspace.status_text_set(None)
        _stroke = None
        sdf_preview.invalidate_texture()
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
        sdf_preview.invalidate_texture()
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
        if not self.idle and _stroke is not self:
            return {'CANCELLED'}
        if context.mode != 'PAINT_SDF' or event.type in {'ESC', 'RIGHTMOUSE', 'WINDOW_DEACTIVATE'}:
            self.cancel(context)
            return {'CANCELLED'}
        if self.idle:
            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                uv = getattr(self, "fill_uv", None)
                settings = context.scene.sdf_editor
                if uv is not None and settings.mask and bpy.ops.sdf.fill.poll():
                    current = (settings.mask.as_pointer(), settings.value, settings.white)
                    if current == self.fill_target and fill_key(context, 'BUCKET', uv):
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


def draw_canvas(is_3d):
    context = bpy.context
    if context.mode != 'PAINT_SDF' or (not is_3d and context.area.ui_type != 'SDF'):
        return
    picker = active_picker(context)
    if picker and picker.area == context.area and not is_3d:
        picker.draw(context.region)
        return
    if context.scene.sdf_editor.mask:
        sdf_preview.draw_canvas(context, is_3d, _stroke)


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


def clear_surface_caches():
    sdf_pick.clear_geometry()
    sdf_preview.clear_batches()


@persistent
def invalidate_geometry(scene, depsgraph):
    global _picker
    sdf_pick.clear_views()
    if _picker and _picker.scene == scene:
        changed = {update.id.original for update in depsgraph.updates if update.is_updated_geometry}
        if any(obj not in scene.objects[:] or obj in changed or obj.data in changed for obj in _picker.objects):
            _picker = None
    if any(update.is_updated_geometry for update in depsgraph.updates):
        clear_surface_caches()


@persistent
def clear_runtime(*args):
    global _stroke, _picker
    _stroke = _picker = None
    sdf_pick.clear_geometry()
    sdf_preview.clear_runtime()
    for fn in (field.fields, field.signed, field.decode):
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


# Ordering matters: snapshot interaction state before clearing transient state.
_HANDLERS = (
    ('load_pre', reset_canvas_spaces),
    ('load_pre', remove_cursors),
    ('load_pre', clear_runtime),
    ('load_post', bind_legacy_uvs),
    ('load_post', install_cursors),
    ('load_post_fail', install_cursors),
    ('undo_pre', remember_interaction),
    ('undo_pre', clear_runtime),
    ('redo_pre', remember_interaction),
    ('redo_pre', clear_runtime),
    ('undo_post', restore_interaction),
    ('redo_post', restore_interaction),
    ('load_factory_startup_post', add_factory_workspace),
    ('depsgraph_update_post', invalidate_geometry),
)


def register_runtime():
    sdf_settings.register_properties()
    for space, stage, is_3d in ((bpy.types.SpaceImageEditor, 'POST_PIXEL', False),
                                (bpy.types.SpaceView3D, 'POST_VIEW', True)):
        _handles.append((space, space.draw_handler_add(draw_canvas, (is_3d,), 'WINDOW', stage)))
        _handles.append((space, space.draw_handler_add(stroke_cursor, (), 'WINDOW', 'POST_PIXEL')))
    image_space = bpy.types.SpaceImageEditor
    _handles.append((image_space, image_space.draw_handler_add(draw_canvas_controls, (), 'WINDOW', 'POST_PIXEL')))
    install_cursors()
    for event, handler in _HANDLERS:
        getattr(bpy.app.handlers, event).append(handler)


def unregister_runtime():
    for space, handle in _handles:
        space.draw_handler_remove(handle, 'WINDOW')
    _handles.clear()
    remove_cursors()
    for event, handler in reversed(_HANDLERS):
        getattr(bpy.app.handlers, event).remove(handler)
    clear_runtime()
    sdf_settings.unregister_properties()


classes = (
    *sdf_settings.classes,
    SDF_OT_workspace,
    SDF_OT_add_mask, SDF_OT_edit_islands, SDF_OT_island_action, SDF_OT_rematch_uv, SDF_OT_remove_mask,
    SDF_OT_toggle_auto_key, SDF_OT_key, SDF_OT_jump,
    SDF_OT_check_conflicts, SDF_OT_export,
    SDF_OT_radius, SDF_OT_select_tool, SDF_OT_brush_setting, SDF_OT_stroke, SDF_OT_fill, SDF_MT_fill,
    sdf_controls.SDF_OT_mask_details, sdf_controls.SDF_OT_canvas_control,
)
