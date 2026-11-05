# SPDX-FileCopyrightText: 2026 Blender Authors
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Texel Pack island constraints in the UV editor sidebar: direct editing of the constraints of the
selected islands, group and stack lists, and an overlay of group and stack bounds.

Constraints are the `uvpack_*` face attributes read by `uv.texel_pack`, written through the
`uv.texel_pack_constraint_*` operators so every edit affects whole islands.
"""

import bpy
import blf
import gpu
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    PointerProperty,
)
from gpu_extras.batch import batch_for_shader

ATTRIBUTES = {
    'ROTATION': 'uvpack_rotation',
    'FLIP': 'uvpack_flip',
    'SCALE': 'uvpack_scale',
    'DENSITY': 'uvpack_density',
    'GROUP': 'uvpack_group',
    'STACK': 'uvpack_stack',
    'LOCK': 'uvpack_lock',
}

_state_cache = {}
_handles = []


def _revision():
    from . import uv_tools
    return uv_tools._revision


def invalidate():
    from . import uv_tools
    uv_tools._invalidate()


def edit_meshes(context):
    """Meshes in edit mode, one object per mesh (also without a screen context)."""
    objects = getattr(context, "objects_in_mode_unique_data", None)
    if objects is None:
        objects = [ob for ob in context.view_layer.objects if ob.mode == 'EDIT']
    result, meshes = [], set()
    for ob in objects:
        if ob.type == 'MESH' and ob.mode == 'EDIT' and ob.data not in meshes:
            meshes.add(ob.data)
            result.append(ob)
    return result


# Kind indices of the records of `Mesh.texel_pack_info()`.
_KINDS = ('ROTATION', 'FLIP', 'SCALE', 'DENSITY', 'GROUP', 'STACK', 'LOCK')
_INFO_STRIDE = 9


def island_state(context):
    """
    Constraint values of the selected faces, and the faces and UV bounds of every group and
    stack among the visible faces. Cached until the next depsgraph update, undo or redo.

    Read through `Mesh.texel_pack_info()` rather than `bmesh`: creating Python BMesh elements
    adds a pointer layer to the BMesh, reallocating its data, which crashes a UV transform
    running while the sidebar or overlay redraws.
    """
    objects = edit_meshes(context)
    key = (_revision(), tuple(ob.name for ob in objects), context.scene.tool_settings.use_uv_select_sync)
    state = _state_cache.get('state')
    if state is not None and state['key'] == key:
        return state

    values = {kind: {} for kind in ATTRIBUTES}
    lists = {'GROUP': {}, 'STACK': {}}
    max_ids = {'GROUP': 0, 'STACK': 0}
    selected_faces = 0
    for ob in objects:
        info = ob.data.texel_pack_info()
        for i in range(0, len(info), _INFO_STRIDE):
            record = info[i:i + _INFO_STRIDE]
            kind = _KINDS[int(record[1])] if record[0] != 3.0 else None
            if record[0] == 0.0:
                value = round(record[2], 4) if kind == 'DENSITY' else int(record[2])
                counts = values[kind]
                counts[value] = counts.get(value, 0) + int(record[3])
            elif record[0] == 1.0:
                entry = lists[kind].setdefault(int(record[2]), {'faces': 0, 'selected': False, 'bounds': None})
                entry['faces'] += int(record[3])
                entry['selected'] |= record[4] != 0.0
                b = entry['bounds']
                entry['bounds'] = list(record[5:9]) if b is None else [
                    min(b[0], record[5]), min(b[1], record[6]), max(b[2], record[7]), max(b[3], record[8])]
            elif record[0] == 2.0:
                max_ids[kind] = max(max_ids[kind], int(record[2]))
            else:
                selected_faces += int(record[1])

    state = {'key': key, 'selected_faces': selected_faces, 'values': values, 'lists': lists,
             'max_ids': max_ids}
    _state_cache['state'] = state
    return state


def selected_value(kind, default):
    """Most common value among the selected faces, and whether they disagree."""
    counts = island_state(bpy.context)['values'][kind]
    if not counts:
        return default, False
    value = max(sorted(counts), key=lambda k: counts[k])
    return value, len(counts) > 1


def _set_constraint(kind, **value):
    if bpy.ops.uv.texel_pack_constraint_set.poll():
        bpy.ops.uv.texel_pack_constraint_set('EXEC_DEFAULT', kind=kind, **value)
    invalidate()


_ROTATION_ITEMS = (
    ('INHERIT', 'Default', 'Use the packing setting', 0),
    ('NONE', 'None', 'Keep the current orientation', 1),
    ('CARDINAL', '90°', 'Rotate by multiples of 90 degrees', 2),
    ('FREE', 'Free', 'Rotate to the smallest bounding box first', 3),
)
_FLIP_ITEMS = (
    ('INHERIT', 'Default', 'Use the packing setting', 0),
    ('FORBID', 'Forbid', 'Never mirror', 1),
    ('ALLOW', 'Allow', 'May be mirrored', 2),
)


class UVToolsIslandConstraints(bpy.types.PropertyGroup):
    """Constraints of the selected UV islands, edited in place."""
    rotation: EnumProperty(
        name='Rotation', items=_ROTATION_ITEMS,
        get=lambda self: selected_value('ROTATION', 0)[0],
        set=lambda self, value: _set_constraint('ROTATION', rotation=_ROTATION_ITEMS[value][0]))
    flip: EnumProperty(
        name='Flip', items=_FLIP_ITEMS, description='Mirroring changes tangent space handedness',
        get=lambda self: selected_value('FLIP', 0)[0],
        set=lambda self, value: _set_constraint('FLIP', flip=_FLIP_ITEMS[value][0]))
    keep_scale: BoolProperty(
        name='Keep Scale', description='Keep the current UV scale instead of following the texel density',
        get=lambda self: bool(selected_value('SCALE', 0)[0]),
        set=lambda self, value: _set_constraint('SCALE', keep_scale=value))
    density: FloatProperty(
        name='Density', description='Texel density multiplier',
        min=0.01, max=100.0, soft_min=0.1, soft_max=10.0,
        get=lambda self: float(selected_value('DENSITY', 1.0)[0]),
        set=lambda self, value: _set_constraint('DENSITY', density=value))
    lock: BoolProperty(
        name='Lock', description='Keep the islands in place, packing around them',
        get=lambda self: bool(selected_value('LOCK', 0)[0]),
        set=lambda self, value: _set_constraint('LOCK', lock=value))


class UVTOOLS_OT_texel_pack_new_id(bpy.types.Operator):
    bl_idname = 'uv_tools.texel_pack_new_id'
    bl_label = 'New from Selection'
    bl_description = 'Put the selected UV islands into a new group or stack'
    bl_options = {'REGISTER', 'UNDO'}

    kind: EnumProperty(items=(('GROUP', 'Group', ''), ('STACK', 'Stack', '')))

    @classmethod
    def poll(cls, context):
        return bpy.ops.uv.texel_pack_constraint_set.poll()

    def execute(self, context):
        invalidate()
        new_id = island_state(context)['max_ids'][self.kind] + 1
        value = {'group': new_id} if self.kind == 'GROUP' else {'stack': new_id}
        result = bpy.ops.uv.texel_pack_constraint_set('EXEC_DEFAULT', kind=self.kind, **value)
        invalidate()
        if result != {'FINISHED'}:
            self.report({'WARNING'}, 'No selected UV islands')
            return {'CANCELLED'}
        self.report({'INFO'}, '{:s} {:d}'.format(self.kind.title(), new_id))
        return {'FINISHED'}


# Theme bone color sets in an order where neighboring ids get clearly different hues.
_COLOR_SET_ORDER = (4, 3, 9, 6, 2, 8, 1, 11, 12, 5, 7, 10, 13, 14, 15, 16, 17, 18, 19, 20)


def _color_set(index, kind):
    # Stacks start further along the palette, so group 1 and stack 1 differ.
    offset = 0 if kind == 'GROUP' else 2
    return _COLOR_SET_ORDER[(index - 1 + offset) % len(_COLOR_SET_ORDER)]


def id_color(index, kind='GROUP'):
    """Color of a group or stack id, matching #id_icon."""
    sets = bpy.context.preferences.themes[0].bone_color_sets
    return tuple(sets[_color_set(index, kind) - 1].active)


def id_icon(index, kind='GROUP'):
    return 'COLORSET_{:02d}_VEC'.format(_color_set(index, kind))


def _overlay_entries(context):
    settings = context.scene.uv_tools_texel_pack
    if context.mode != 'EDIT_MESH' or context.area.ui_type != 'UV':
        return []
    if not context.space_data.overlay.show_overlays:
        return []
    lists = island_state(context)['lists']
    entries = []
    if settings.show_groups:
        entries += [('G', index, entry) for index, entry in sorted(lists['GROUP'].items())]
    if settings.show_stacks:
        entries += [('S', index, entry) for index, entry in sorted(lists['STACK'].items())]
    return entries


def _draw_bounds():
    context = bpy.context
    entries = _overlay_entries(context)
    if not entries:
        return
    view2d = context.region.view2d
    # Pad by a few pixels so boxes don't cover island edges.
    x0, y0 = view2d.region_to_view(0, 0)
    x1, y1 = view2d.region_to_view(5, 5)
    pad_u, pad_v = abs(x1 - x0), abs(y1 - y0)
    fill = gpu.shader.from_builtin('UNIFORM_COLOR')
    line = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    blend = gpu.state.blend_get()
    gpu.state.blend_set('ALPHA')
    try:
        for prefix, index, entry in entries:
            u0, v0, u1, v1 = entry['bounds']
            # Stacks are drawn slightly outside groups so both stay readable.
            grow = 1.0 if prefix == 'G' else 1.8
            u0, v0, u1, v1 = u0 - pad_u * grow, v0 - pad_v * grow, u1 + pad_u * grow, v1 + pad_v * grow
            color = id_color(index, 'GROUP' if prefix == 'G' else 'STACK')
            corners = ((u0, v0), (u1, v0), (u1, v1), (u0, v1))
            fill.bind()
            fill.uniform_float('color', (*color, 0.10 if prefix == 'G' else 0.05))
            batch_for_shader(fill, 'TRI_FAN', {'pos': corners}).draw(fill)
            line.bind()
            line.uniform_float('viewportSize', (context.region.width, context.region.height))
            line.uniform_float('lineWidth', (2.0 if entry['selected'] else 1.2) * context.preferences.system.ui_scale)
            line.uniform_float('color', (*color, 0.9))
            batch_for_shader(line, 'LINE_LOOP', {'pos': corners}).draw(line)
    finally:
        gpu.state.blend_set(blend)


def _draw_labels():
    context = bpy.context
    entries = _overlay_entries(context)
    if not entries:
        return
    view2d = context.region.view2d
    scale = context.preferences.system.ui_scale
    blf.size(0, int(13 * scale))
    blf.enable(0, blf.SHADOW)
    blf.shadow(0, 5, 0.0, 0.0, 0.0, 0.9)
    blf.shadow_offset(0, 1, -1)
    for prefix, index, entry in entries:
        u0, v0, u1, v1 = entry['bounds']
        if prefix == 'G':
            x, y = view2d.view_to_region(u0, v1, clip=False)
            x, y = x + 4 * scale, y + 6 * scale
        else:
            x, y = view2d.view_to_region(u1, v1, clip=False)
            x, y = x - blf.dimensions(0, 'S0')[0] - 8 * scale, y + 6 * scale
        blf.color(0, *id_color(index, 'GROUP' if prefix == 'G' else 'STACK'), 1.0)
        blf.position(0, x, y, 0)
        blf.draw(0, '{:s}{:d}'.format(prefix, index))
    blf.disable(0, blf.SHADOW)


def register():
    bpy.types.Scene.uv_tools_island = PointerProperty(type=UVToolsIslandConstraints)
    if not bpy.app.background:
        space = bpy.types.SpaceImageEditor
        _handles.append(space.draw_handler_add(_draw_bounds, (), 'WINDOW', 'POST_VIEW'))
        _handles.append(space.draw_handler_add(_draw_labels, (), 'WINDOW', 'POST_PIXEL'))


def unregister():
    for handle in _handles:
        bpy.types.SpaceImageEditor.draw_handler_remove(handle, 'WINDOW')
    _handles.clear()
    _state_cache.clear()
    del bpy.types.Scene.uv_tools_island


classes = (UVToolsIslandConstraints, UVTOOLS_OT_texel_pack_new_id)
