# SPDX-License-Identifier: GPL-2.0-or-later
"""Bounding-box UV fitting and alignment; all targets are resolved before writing UVs."""
import math
import bpy
import bmesh
from mathutils import Vector
from bpy.props import BoolProperty, EnumProperty, FloatVectorProperty, IntProperty, CollectionProperty, PointerProperty, StringProperty
from bpy_extras.bmesh_utils import bmesh_linked_uv_islands
from .uv_tools_transform import nodes_for


def bounds(points):
    return (min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points))


def snap_bounds(rect, steps, mode):
    result = []
    for i, value in enumerate(rect):
        step = steps[i % 2]
        x = value / step
        if mode == 'CLOSEST':
            n = math.floor(x + 0.5)
        elif (mode == 'LARGER') == (i < 2):
            n = math.floor(x + 1e-7)
        else:
            n = math.ceil(x - 1e-7)
        result.append(n * step)
    return tuple(result)


def transformed(points, target, action, axes='BOTH', proportional=False, anchor=(0.5, 0.5)):
    source = bounds(points)
    if not all(math.isfinite(v) for v in (*source, *target)):
        raise ValueError('Non-finite UV coordinates or target')
    dimensions = (source[2]-source[0], source[3]-source[1])
    target_dimensions = (target[2]-target[0], target[3]-target[1])
    affected = (axes != 'V', axes != 'U')
    if action == 'FIT':
        if any(affected[i] and (dimensions[i] < 1e-10 or target_dimensions[i] < 1e-10) for i in range(2)):
            raise ValueError('Fit needs non-zero source and target dimensions; use Align for points or lines')
        scale = [target_dimensions[i]/dimensions[i] if affected[i] else 1.0 for i in range(2)]
        if proportional:
            factor = min(scale) if axes == 'BOTH' else scale[0 if axes == 'U' else 1]
            scale = [factor, factor]
    else:
        if any(affected[i] and target_dimensions[i] < -1e-10 for i in range(2)):
            raise ValueError('No smaller grid interval exists for this selection')
        scale = [1.0, 1.0]
    source_anchor = [source[i] + dimensions[i]*anchor[i] for i in range(2)]
    target_anchor = [target[i] + target_dimensions[i]*anchor[i] if affected[i] else source_anchor[i] for i in range(2)]
    return [Vector(tuple(target_anchor[i] + (p[i]-source_anchor[i])*scale[i] for i in range(2))) for p in points]


class UVSelectionUnits(list):
    """Own the wrapped edit BMeshes for as long as their element references are used."""
    def __init__(self):
        super().__init__()
        self.bmeshes = []


def selected_units(context, grouping):
    """Multi = island's selected UVs, Single = all selected UVs, Vertex = unique UV points."""
    units, meshes = UVSelectionUnits(), []
    ts = context.scene.tool_settings
    for obj in context.objects_in_mode_unique_data:
        if obj.type != 'MESH':
            continue
        bm = bmesh.from_edit_mesh(obj.data)
        units.bmeshes.append(bm)
        bm.verts.index_update()
        layer = bm.loops.layers.uv.active
        if layer is None:
            continue
        meshes.append(obj.data)
        def visible(face):
            return not face.hide and (ts.use_uv_select_sync or face.select)
        def chosen(loop):
            if ts.use_uv_select_sync and not bm.uv_select_sync_valid:
                mode = ts.mesh_select_mode
                return (loop.face.select if mode[2] else
                        (loop.edge.select or loop.link_loop_prev.edge.select) if mode[1] else loop.vert.select)
            mode = ts.uv_select_mode
            if mode in {'FACE', 'ISLAND'}:
                return loop.face.uv_select
            if mode == 'EDGE':
                return loop.uv_select_edge or loop.link_loop_prev.uv_select_edge
            return loop.uv_select_vert
        for island in bmesh_linked_uv_islands(bm, layer):
            faces = [f for f in island if visible(f)]
            if not faces:
                continue
            _, nodes, coordinates = nodes_for(faces, layer)
            records = []
            for node, loops in nodes.items():
                if any(chosen(loop) for loop in loops):
                    records.append(([(loop, layer) for loop in loops], coordinates[node]))
            if records:
                units.extend([[record] for record in records] if grouping == 'VERTEX' else [records])
    if grouping == 'SINGLE' and units:
        units[:] = [[record for unit in units for record in unit]]
    return units, meshes


class UVToolsTrim(bpy.types.PropertyGroup):
    name: StringProperty(name='Name', default='Trim')
    minimum: FloatVectorProperty(name='Min', size=2, default=(0, 0))
    maximum: FloatVectorProperty(name='Max', size=2, default=(1, 1))


class UVToolsFitAlignSettings(bpy.types.PropertyGroup):
    target: EnumProperty(name='Match to', default='SELECTION', items=(
        ('SELECTION', 'Selection', 'Use the full selection bounds'),
        ('PIXEL', 'Pixel', 'Snap bounds to pixels of the displayed image or fallback resolution'),
        ('GRID', 'Grid', 'Snap bounds to the explicit UV grid divisions below'),
        ('TILE', 'Tile', 'Use the specified UDIM tile'),
        ('TRIM', 'Trim', 'Use the active named trim rectangle'),
        ('BOX', 'Box', 'Use the custom UV rectangle')))
    snapping: EnumProperty(name='Snapping', items=(('CLOSEST','Closest','Round to nearest grid lines'),
        ('LARGER','Larger','Round bounds outwards'),('SMALLER','Smaller','Round bounds inwards')))
    grouping: EnumProperty(name='Grouping', items=(('MULTI','Multi','Transform selected UVs per island'),
        ('SINGLE','Single','Transform the selection as a whole'),('VERTEX','Vertex','Align each selected UV point independently')))
    grid_u: IntProperty(name='U Divisions', default=8, min=1, max=8192)
    grid_v: IntProperty(name='V Divisions', default=8, min=1, max=8192)
    resolution_x: IntProperty(name='Width', default=1024, min=1)
    resolution_y: IntProperty(name='Height', default=1024, min=1)
    tile: IntProperty(name='UDIM', default=1001, min=1001, max=2000)
    box_min: FloatVectorProperty(name='Min', size=2, default=(0,0))
    box_max: FloatVectorProperty(name='Max', size=2, default=(1,1))
    trims: CollectionProperty(type=UVToolsTrim)
    trim_index: IntProperty(default=0, min=0)


class UVTOOLS_OT_fit_target(bpy.types.Operator):
    bl_idname = 'uv_tools.fit_target'
    bl_label = 'Set Fit Target'
    bl_options = {'REGISTER', 'UNDO'}
    operation: EnumProperty(items=(('BOX','Box from Selection',''),('ADD_TRIM','Trim from Selection',''),('REMOVE_TRIM','Remove Trim','')))

    def execute(self, context):
        settings = context.scene.uv_tools_fit_align
        if self.operation == 'REMOVE_TRIM':
            if settings.trim_index >= len(settings.trims):
                return {'CANCELLED'}
            settings.trims.remove(settings.trim_index)
            settings.trim_index = max(0, min(settings.trim_index, len(settings.trims)-1))
            return {'FINISHED'}
        if context.mode != 'EDIT_MESH':
            return {'CANCELLED'}
        units, _ = selected_units(context, 'SINGLE')
        if not units:
            self.report({'WARNING'}, 'Select UVs to define the target rectangle')
            return {'CANCELLED'}
        rect = bounds([record[1] for record in units[0]])
        if min(rect[2]-rect[0], rect[3]-rect[1]) < 1e-10:
            self.report({'WARNING'}, 'Target rectangle must have positive width and height')
            return {'CANCELLED'}
        if self.operation == 'BOX':
            settings.box_min, settings.box_max = rect[:2], rect[2:]
        else:
            item = settings.trims.add()
            item.name = 'Trim {}'.format(len(settings.trims))
            item.minimum, item.maximum = rect[:2], rect[2:]
            settings.trim_index = len(settings.trims)-1
        return {'FINISHED'}


ANCHORS = {'TL': (0,1), 'T': (.5,1), 'TR': (1,1), 'L': (0,.5), 'C': (.5,.5),
           'R': (1,.5), 'BL': (0,0), 'B': (.5,0), 'BR': (1,0)}


class UVTOOLS_OT_fit_align(bpy.types.Operator):
    bl_idname = 'uv_tools.fit_align'
    bl_label = 'Fit & Align UVs'
    bl_options = {'REGISTER', 'UNDO'}
    action: EnumProperty(items=(('FIT','Fit','Resize to target'),('ALIGN','Align','Move to target without changing shape')))
    axes: EnumProperty(items=(('U','U','Horizontal axis'),('BOTH','Both','Both axes'),('V','V','Vertical axis')), default='BOTH')
    proportional: BoolProperty(default=False)
    anchor: EnumProperty(items=tuple((k,k,'Target anchor '+k) for k in ANCHORS), default='C')

    @classmethod
    def description(cls, context, properties):
        if properties.action == 'FIT':
            return '{}fit {} to target bounds'.format('Proportionally ' if properties.proportional else '', properties.axes)
        return 'Align {} ({}) without scaling'.format(properties.anchor, properties.axes)

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH' and context.area and context.area.type == 'IMAGE_EDITOR'

    def execute(self, context):
        settings = context.scene.uv_tools_fit_align
        plans = []
        try:
            units, meshes = selected_units(context, settings.grouping)
            if not units:
                raise ValueError('Select UVs to fit or align')
            if self.action == 'FIT' and settings.grouping == 'VERTEX':
                raise ValueError('Use Multi or Single grouping to fit; Vertex is for alignment')
            selection = bounds([record[1] for unit in units for record in unit])
            for unit in units:
                points = [r[1] for r in unit]
                source = bounds(points)
                if settings.target == 'SELECTION':
                    target = selection
                elif settings.target in {'PIXEL', 'GRID'}:
                    if settings.target == 'PIXEL':
                        image = context.space_data.image
                        size = tuple(image.size) if image and all(image.size) else (settings.resolution_x, settings.resolution_y)
                    else:
                        size = settings.grid_u, settings.grid_v
                    target = snap_bounds(source, (1/size[0], 1/size[1]), settings.snapping)
                elif settings.target == 'TILE':
                    number = settings.tile - 1001
                    x, y = number % 10, number // 10
                    target = (x,y,x+1,y+1)
                elif settings.target == 'TRIM':
                    if settings.trim_index >= len(settings.trims):
                        raise ValueError('Create or select a trim rectangle first')
                    trim = settings.trims[settings.trim_index]
                    target = (*trim.minimum, *trim.maximum)
                else:
                    target = (*settings.box_min, *settings.box_max)
                if settings.target in {'TRIM','BOX'} and (target[2]<=target[0] or target[3]<=target[1]):
                    raise ValueError('Target maximum must be greater than minimum')
                result = transformed(points, target, self.action, self.axes, self.proportional, ANCHORS[self.anchor])
                for (loops, _), value in zip(unit, result):
                    for loop, layer in loops:
                        if loop[layer].pin_uv and (loop[layer].uv-value).length > 1e-6:
                            raise ValueError('Unpin affected UVs before fitting or aligning')
                        plans.append((loop, layer, value))
        except ValueError as error:
            self.report({'WARNING'}, str(error))
            return {'CANCELLED'}
        for loop, layer, value in plans:
            loop[layer].uv = value
        for mesh in meshes:
            bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
        return {'FINISHED'}


def register():
    bpy.types.Scene.uv_tools_fit_align = PointerProperty(type=UVToolsFitAlignSettings)


def unregister():
    del bpy.types.Scene.uv_tools_fit_align


classes = (UVToolsTrim, UVToolsFitAlignSettings, UVTOOLS_OT_fit_target, UVTOOLS_OT_fit_align)
