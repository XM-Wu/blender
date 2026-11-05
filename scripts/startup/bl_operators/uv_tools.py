# SPDX-FileCopyrightText: 2026 Blender Authors
# SPDX-License-Identifier: GPL-2.0-or-later

"""UV seam actions and area distortion overlays (no mesh/material modifications)."""
import math
import bpy
import bmesh
import blf
import gpu
from bpy.app.handlers import persistent
from bpy.props import BoolProperty
from gpu_extras.batch import batch_for_shader

_handles = []
_cache = {}
_shader = None
_overlay_shader = None
_icons = None
_BINS = 64
_revision = 0
_hud_cache = {}
_hud_record = None


def distortion_color(ratio, alpha=1.0):
    """Normalized UV/world area: compressed red, neutral gray, stretched blue."""
    neutral = (0.48, 0.50, 0.53)
    end = (0.95, 0.08, 0.06) if ratio < 1.0 else (0.02, 0.35, 1.0)
    t = min(1.0, abs(ratio - 1.0))
    return (*(a + (b - a) * t for a, b in zip(neutral, end)), alpha)


def normalized_ratios(areas):
    """Return ratios and surface-area-weighted bins; ignore zero-area geometry."""
    world_total = sum(world for world, uv in areas if world > 1e-20)
    uv_total = sum(uv for world, uv in areas if world > 1e-20)
    if world_total <= 1e-20 or uv_total <= 1e-20:
        return [], [0.0] * _BINS
    ratios = [(uv / world) * (world_total / uv_total) if world > 1e-20 else None
              for world, uv in areas]
    bins = [0.0] * _BINS
    for (world, _), ratio in zip(areas, ratios):
        if ratio is not None:
            bins[min(_BINS - 1, int(max(0.0, ratio) * _BINS / 2))] += world / world_total
    return ratios, bins


def _triangles(context, with_edges=False):
    objects = (context.objects_in_mode_unique_data if context.mode == 'EDIT_MESH'
               else context.selected_objects)
    result = []
    for obj in objects:
        if obj.type != 'MESH' or not obj.visible_get():
            continue
        matrix = obj.matrix_world
        if obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(obj.data)
            uv_layer = bm.loops.layers.uv.active
            if uv_layer is None:
                continue
            for loops in bm.calc_loop_triangles():
                if loops[0].face.hide:
                    continue
                result.append(([matrix @ loop.vert.co for loop in loops],
                               [loop[uv_layer].uv.copy() for loop in loops]))
        else:
            mesh = obj.data
            if mesh.uv_layers.active is None:
                continue
            mesh.calc_loop_triangles()
            uvs = mesh.uv_layers.active.data
            for tri in mesh.loop_triangles:
                if mesh.polygons[tri.polygon_index].hide:
                    continue
                item = ([matrix @ mesh.vertices[i].co for i in tri.vertices],
                        [uvs[i].uv.copy() for i in tri.loops])
                if with_edges:
                    face = mesh.polygons[tri.polygon_index]
                    def adjacent(a, b):
                        return (a-face.loop_start+1) % face.loop_total == b-face.loop_start
                    mask = [float(adjacent(tri.loops[(i+1)%3], tri.loops[(i+2)%3])
                                  or adjacent(tri.loops[(i+2)%3], tri.loops[(i+1)%3])) for i in range(3)]
                    item = (*item, mask)
                result.append(item)
    return result


def _engine_data(engine):
    return {'engine': engine, 'positions': [True] if engine.count else [],
            'count': engine.count, 'bins': engine.bins, 'compressed': engine.compressed,
            'stretched': engine.stretched, 'generation': engine.generation, 'batches': {}}


def _collect(context):
    if context.mode == 'EDIT_MESH':
        from .uv_tools_cache import EditCache
        return _engine_data(EditCache(context))
    triangles = _triangles(context, with_edges=True)
    areas = []
    for positions, uvs, _ in triangles:
        world = (positions[1] - positions[0]).cross(positions[2] - positions[0]).length * 0.5
        a, b = uvs[1] - uvs[0], uvs[2] - uvs[0]
        areas.append((world, abs(a.x * b.y - a.y * b.x) * 0.5))
    ratios, bins = normalized_ratios(areas)
    pos3d, posuv, colors = [], [], []
    densities, barycentrics, masks = [], [], []
    compressed = stretched = 0.0
    total = sum(a[0] for a in areas)
    for (positions, uvs, boundary), (world, _), ratio in zip(triangles, areas, ratios):
        if ratio is None:
            continue
        if ratio < 0.95:
            compressed += world / total
        elif ratio > 1.05:
            stretched += world / total
        pos3d.extend(tuple(p) for p in positions)
        posuv.extend((uv.x, uv.y, 0.0) for uv in uvs)
        colors.extend([distortion_color(ratio, 0.83)] * 3)
        densities.extend([ratio] * 3)
        barycentrics.extend(((1, 0, 0), (0, 1, 0), (0, 0, 1)))
        masks.extend([boundary] * 3)
    return {'positions': pos3d, 'uvs': posuv, 'colors': colors, 'bins': bins,
            'densities': densities, 'barycentrics': barycentrics, 'edge_masks': masks,
            'count': len(pos3d) // 3, 'compressed': compressed, 'stretched': stretched,
            'batches': {}}


def _key(context):
    return context.scene.as_pointer(), context.view_layer.as_pointer()


def _transforming(window):
    return any(op.bl_idname.startswith('TRANSFORM_OT_') for op in window.modal_operators)


def _tick():
    seen = set()
    for window in bpy.context.window_manager.windows:
        with bpy.context.temp_override(window=window):
            context = bpy.context
            key = _key(context)
            if key in seen:
                continue
            seen.add(key)
            changed = False
            if context.scene.uv_tools_distortion:
                objects = (context.objects_in_mode_unique_data if context.mode == 'EDIT_MESH'
                           else context.selected_objects)
                signature = (_revision, context.mode, tuple(sorted(
                    (obj.as_pointer(), obj.data.as_pointer(), obj.data.uv_layers.active_index)
                    for obj in objects if obj.type == 'MESH')))
                transforming = _transforming(window)
                previous = _cache.get(key)
                if (transforming and previous and 'engine' in previous
                        and previous['signature'][1:] == signature[1:]):
                    try:
                        engine = previous['engine']
                        changed = engine.update()
                        if changed:
                            _cache[key] = _engine_data(engine)
                    except (ReferenceError, ValueError, KeyError, RuntimeError):
                        _cache[key] = _collect(context)
                        changed = True
                elif (previous is None or previous['signature'] != signature
                      or previous.get('transforming', False)):
                    _cache[key] = _collect(context)
                    changed = True
                _cache[key]['signature'] = signature
                _cache[key]['transforming'] = transforming
            elif key in _cache:
                del _cache[key]
                changed = True
            if changed:
                for area in window.screen.areas:
                    if area.type in {'VIEW_3D', 'IMAGE_EDITOR'}:
                        area.tag_redraw()
    for key in set(_cache) - seen:
        del _cache[key]
    return 1.0 / 30.0


def _get_shader():
    global _shader
    if _shader is None:
        _shader = gpu.shader.from_builtin('SMOOTH_COLOR')
    return _shader


_wire_shader = None


def _get_wire_shader():
    global _wire_shader
    if _wire_shader is None:
        _wire_shader = gpu.shader.from_builtin('POLYLINE_SMOOTH_COLOR')
    return _wire_shader


def _get_overlay_shader():
    global _overlay_shader
    if _overlay_shader is None:
        interface = gpu.types.GPUStageInterfaceInfo('uv_distortion_interface')
        interface.smooth('FLOAT', 'raw_density')
        info = gpu.types.GPUShaderCreateInfo()
        info.push_constant('MAT4', 'mvp')
        info.push_constant('FLOAT', 'normalization')
        info.vertex_in(0, 'VEC3', 'pos')
        info.vertex_in(1, 'FLOAT', 'density')
        info.vertex_out(interface)
        info.fragment_out(0, 'VEC4', 'fragColor')
        info.vertex_source('void main() { gl_Position = mvp * vec4(pos, 1.0); raw_density = density; }')
        info.fragment_source("""
            void main() {
                if (raw_density < 0.0 || normalization <= 0.0) discard;
                float ratio = raw_density * normalization;
                vec3 endpoint = ratio < 1.0 ? vec3(0.95, 0.08, 0.06) : vec3(0.02, 0.35, 1.0);
                fragColor = vec4(mix(vec3(0.48, 0.50, 0.53), endpoint, min(1.0, abs(ratio-1.0))), 0.83);
            }
        """)
        _overlay_shader = gpu.shader.create_from_info(info)
    return _overlay_shader


def _draw_overlay(is_uv):
    context = bpy.context
    if not context.scene.uv_tools_distortion:
        return
    if is_uv:
        if context.area.ui_type != 'UV' or not context.space_data.overlay.show_overlays:
            return
    elif not context.space_data.overlay.show_overlays:
        return
    data = _cache.get(_key(context))
    if not data or not data['positions']:
        return
    engine = data.get('engine')
    shader = _get_overlay_shader()
    name = 'uvs' if is_uv else 'positions'
    if not engine and name not in data['batches']:
        data['batches'][name] = batch_for_shader(shader, 'TRIS', {'pos': data[name], 'density': data['densities']})

    def draw():
        if engine:
            from .uv_tools_cache import CHUNK_SIZE
            shader.bind()
            shader.uniform_float('mvp', gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix())
            shader.uniform_float('normalization', engine.normalization)
            for chunk in range((len(engine.triangles) + CHUNK_SIZE - 1) // CHUNK_SIZE):
                if chunk not in engine.batches[name]:
                    positions, density = engine.chunk_arrays(name, chunk)
                    engine.batches[name][chunk] = {'faces': batch_for_shader(shader, 'TRIS',
                        {'pos': positions, 'density': density})}
                engine.batches[name][chunk]['faces'].draw(shader)
        else:
            shader.bind()
            shader.uniform_float('mvp', gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix())
            shader.uniform_float('normalization', 1.0)
            data['batches'][name].draw(shader)
    def draw_wires():
        if not engine and (is_uv or not (context.space_data.overlay.show_wireframes
                or any(obj.show_wire for obj in context.selected_objects if obj.type == 'MESH'))):
            return
        theme = context.preferences.themes[0].view_3d
        normal = (*theme.wire_edit[:3], 1.0)
        selected = (*theme.edge_select[:3], 1.0)
        seam = (*theme.seam[:3], 1.0)
        line_shader = _get_wire_shader()
        line_shader.bind()
        line_shader.uniform_float('viewportSize', (context.region.width, context.region.height))
        line_shader.uniform_float('lineWidth', context.preferences.system.ui_scale)
        if not engine:
            key = ('wires', name, normal, selected)
            if key not in data['batches']:
                import numpy as np
                triangles = np.asarray(data[name], dtype=np.float32).reshape((-1, 3, 3))
                masks = np.asarray(data['edge_masks'], dtype=bool).reshape((-1, 3, 3))[:, 0]
                endpoints = triangles[:, ((1, 2), (2, 0), (0, 1)), :][masks].reshape((-1, 3))
                data['batches'][key] = batch_for_shader(line_shader, 'LINES',
                    {'pos': endpoints, 'color': np.tile(normal, (len(endpoints), 1))})
            data['batches'][key].draw(line_shader)
            return
        for chunk, batches in engine.batches[name].items():
            key = (normal, selected, seam)
            if batches.get('wire_theme') != key:
                positions, colors = engine.wire_arrays(name, chunk, normal, selected, seam)
                batches['wires'] = batch_for_shader(line_shader, 'LINES', {'pos': positions, 'color': colors})
                batches['wire_theme'] = key
            batches['wires'].draw(line_shader)
    blend, depth, mask = gpu.state.blend_get(), gpu.state.depth_test_get(), gpu.state.depth_mask_get()
    try:
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('NONE' if is_uv else 'LESS_EQUAL')
        gpu.state.depth_mask_set(False)
        if is_uv:
            draw()
            draw_wires()
        else:
            # Offset in clip space, so coplanar edit-mode surfaces do not fight the overlay.
            # Unlike world-space normal offsets this is stable across scene and object scales.
            with gpu.matrix.push_pop_projection():
                projection = gpu.matrix.get_projection_matrix()
                for column in range(4):
                    projection[2][column] -= 0.000001 * projection[3][column]
                gpu.matrix.load_projection_matrix(projection)
                # Resolve the nearest overlay surface before blending any colors. Otherwise
                # back faces inside the depth bias can blend through the front faces.
                gpu.state.color_mask_set(False, False, False, False)
                gpu.state.depth_mask_set(True)
                try:
                    draw()
                finally:
                    gpu.state.color_mask_set(True, True, True, True)
                    gpu.state.depth_mask_set(False)
                draw()
                for column in range(4):
                    projection[2][column] -= 0.0000005 * projection[3][column]
                gpu.matrix.load_projection_matrix(projection)
                draw_wires()
    finally:
        gpu.state.blend_set(blend)
        gpu.state.depth_test_set(depth)
        gpu.state.depth_mask_set(mask)


def _rect(x, y, w, h, color):
    positions = [(x, y, 0), (x + w, y, 0), (x + w, y + h, 0), (x, y, 0), (x + w, y + h, 0), (x, y + h, 0)]
    _hud_record['positions'].extend(positions)
    _hud_record['colors'].extend([color] * 6)


def _text(x, y, text, size=11):
    if _hud_record is not None:
        _hud_record['texts'].append((x, y, text, size))
        return
    blf.size(0, size)
    blf.color(0, 0.86, 0.87, 0.90, 1.0)
    blf.position(0, x, y, 0)
    blf.draw(0, text)


def _build_histogram():
    context = bpy.context
    if context.area.ui_type != 'UV':
        return
    scale = context.preferences.system.ui_scale
    width = context.region.width
    if width < 240 * scale:
        return
    data = _cache.get(_key(context))
    enabled = context.scene.uv_tools_distortion
    h = 94 * scale
    left, right = 16 * scale, width - 16 * scale
    blend, depth = gpu.state.blend_get(), gpu.state.depth_test_get()
    try:
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('NONE')
        _rect(0, 0, width, h, (0.055, 0.06, 0.07, 0.97))
        status = 'ON' if enabled else 'OFF - enable Distortion in the UV header'
        _text(left, 75 * scale, 'UV Area Distortion  |  ' + status, int(11 * scale))
        span = right - left
        for i in range(_BINS):
            x = left + span * i / _BINS
            _rect(x, 24 * scale, span / _BINS + 0.5, 10 * scale, distortion_color((i + 0.5) * 2 / _BINS))
        if enabled and data and data['count']:
            peak = max(data['bins'])
            for i, value in enumerate(data['bins']):
                if value:
                    _rect(left + span * i / _BINS, 36 * scale, max(1, span / _BINS - 1),
                          30 * scale * value / peak, distortion_color((i + 0.5) * 2 / _BINS))
            if width > 650 * scale:
                _text(
                    left + 290 * scale, 75 * scale, f"Compressed {
                        data['compressed']:.1%}   Stretched {
                        data['stretched']:.1%}   ({
                        data['count']} triangles)", int(
                        10 * scale))
        elif enabled:
            _text(left, 47 * scale, 'Select a mesh with a non-degenerate UV map', int(11 * scale))
        _rect(left + span / 2, 22 * scale, 1 * scale, 46 * scale, (0.9, 0.9, 0.9, 0.7))
        for fraction, label in ((0, '0 / Compressed'), (.25, '0.5'), (.5, '1 / Neutral'),
                                (.75, '1.5'), (1, '2+ / Stretched')):
            blf.size(0, int(10 * scale))
            tw = blf.dimensions(0, label)[0]
            _text(min(right - tw, max(left, left + span * fraction - tw / 2)), 8 * scale, label, int(10 * scale))
    finally:
        gpu.state.blend_set(blend)
        gpu.state.depth_test_set(depth)


def _draw_histogram():
    global _hud_record
    context = bpy.context
    if context.area.ui_type != 'UV' or context.region.width < 240 * context.preferences.system.ui_scale:
        return
    data = _cache.get(_key(context))
    region_key = context.region.as_pointer()
    signature = (context.region.width, context.preferences.system.ui_scale,
                 context.scene.uv_tools_distortion, id(data))
    cached = _hud_cache.get(region_key)
    # Hold a reference to data as well: Python may otherwise reuse its id.
    if cached is None or cached[0] != signature or cached[3] is not data:
        record = {'positions': [], 'colors': [], 'texts': []}
        _hud_record = record
        try:
            _build_histogram()
        finally:
            _hud_record = None
        batch = batch_for_shader(_get_shader(), 'TRIS',
                                 {'pos': record['positions'], 'color': record['colors']})
        if len(_hud_cache) > 16:
            _hud_cache.clear()
        cached = (signature, batch, record['texts'], data)
        _hud_cache[region_key] = cached
    blend, depth = gpu.state.blend_get(), gpu.state.depth_test_get()
    try:
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('NONE')
        cached[1].draw(_get_shader())
        for text in cached[2]:
            _text(*text)
    finally:
        gpu.state.blend_set(blend)
        gpu.state.depth_test_set(depth)


def icon_id(name):
    """Small colored vector-style icons rasterized into Blender's preview system."""
    global _icons
    if _icons is None:
        from bpy.utils import previews
        _icons = previews.new()
        size, samples = 64, 4

        def line(x, y, a, b, r):
            dx, dy = b[0] - a[0], b[1] - a[1]
            t = max(0, min(1, ((x - a[0]) * dx + (y - a[1]) * dy) / (dx * dx + dy * dy)))
            return math.hypot(x - a[0] - t * dx, y - a[1] - t * dy) <= r
        for kind in ('cut', 'stitch', 'unwrap', 'pack'):
            pixels = []
            for iy in range(size):
                for ix in range(size):
                    accum = [0.0] * 4
                    for sy in range(samples):
                        for sx in range(samples):
                            x, y = (ix + (sx + .5) / samples) * 32 / size, (iy + (sy + .5) / samples) * 32 / size
                            c = (0, 0, 0, 0)
                            if kind == 'cut':
                                if line(x, y, (10, 11), (25, 28), 1.3) or line(x, y, (22, 11), (7, 28), 1.3):
                                    c = (.85, .92, 1, 1)
                                if any(abs(math.hypot(x - cx, y - 8) - 4) < 1.5 for cx in (8, 24)):
                                    c = (1, .35, .08, 1)
                                if math.hypot(x - 16, y - 18) < 2:
                                    c = (.1, .8, .9, 1)
                            elif kind == 'stitch':
                                if line(x, y, (10, 4), (10, 28), .8) or line(x, y, (22, 4), (22, 28), .8):
                                    c = (.12, .76, .94, 1)
                                for yy in (7, 13, 19, 25):
                                    if line(x, y, (7, yy - 2), (25, yy + 2), 1.1):
                                        c = (1, .64, .15, 1)
                            elif kind == 'unwrap':
                                # Six panels of an unfolded cube; warm top panel, cyan sides.
                                for index, (ox, oy) in enumerate(((12, 3), (12, 10), (12, 17), (12, 24), (5, 17), (19, 17))):
                                    if ox <= x <= ox + 7 and oy <= y <= oy + 7:
                                        c = (1, .58, .12, 1) if index == 3 else (.12, .7, .95, 1)
                                        if min(x-ox, ox+7-x, y-oy, oy+7-y) < .65:
                                            c = (.88, .95, 1, 1)
                            elif kind == 'pack':
                                # A tight atlas, distinct from the unfolded cube icon.
                                if 3 <= x <= 29 and 3 <= y <= 29 and min(x-3, 29-x, y-3, 29-y) < 1:
                                    c = (.88, .95, 1, 1)
                                for ox, oy, w, h, color in (
                                        (6, 6, 8, 19, (.12, .7, .95, 1)),
                                        (16, 6, 10, 8, (1, .58, .12, 1)),
                                        (16, 16, 10, 9, (.3, .85, .48, 1))):
                                    if ox <= x <= ox+w and oy <= y <= oy+h:
                                        c = color
                            for j in range(4):
                                accum[j] += c[j] / (samples * samples)
                    pixels.extend(accum)
            preview = _icons.new(kind)
            preview.image_size = (size, size)
            preview.image_pixels_float = pixels
            preview.icon_size = (size, size)
            preview.icon_pixels_float = pixels
    return _icons[name].icon_id


def _uv_area(context):
    return next((a for a in context.screen.areas if a.type == 'IMAGE_EDITOR' and a.ui_type == 'UV'), None)


class UVTOOLS_OT_mark_seam(bpy.types.Operator):
    bl_idname = 'uv_tools.mark_seam'
    bl_label = 'Mark Seam'
    bl_description = 'Mark selected mesh or UV edges as seams (undoable)'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH' and context.edit_object is not None

    def execute(self, context):
        area = _uv_area(context)
        if area and not context.scene.tool_settings.use_uv_select_sync:
            with context.temp_override(area=area, region=next(r for r in area.regions if r.type == 'WINDOW')):
                return bpy.ops.uv.mark_seam('EXEC_DEFAULT', clear=False)
        return bpy.ops.mesh.mark_seam('EXEC_DEFAULT', clear=False)


class UVTOOLS_OT_stitch(bpy.types.Operator):
    bl_idname = 'uv_tools.stitch'
    bl_label = 'Stitch UVs'
    bl_description = 'Stitch selected UV boundary edges, align islands and clear their seams'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (not bpy.app.background and context.mode == 'EDIT_MESH'
                and context.edit_object is not None and _uv_area(context) is not None)

    def execute(self, context):
        area = _uv_area(context)
        with context.temp_override(area=area, region=next(r for r in area.regions if r.type == 'WINDOW')):
            if not bpy.ops.uv.stitch.poll():
                self.report({'WARNING'}, 'The active mesh needs a UV map')
                return {'CANCELLED'}
            meshes = []
            for obj in context.objects_in_mode_unique_data:
                if obj.type != 'MESH':
                    continue
                bm = bmesh.from_edit_mesh(obj.data)
                layer = bm.loops.layers.uv.active
                if layer is not None:
                    meshes.append((obj.data, bm, layer))
            def coordinates():
                return [tuple(loop[layer].uv) for _, bm, layer in meshes for face in bm.faces for loop in face.loops]
            before = coordinates()
            seams_before = sum(edge.seam for _, bm, _ in meshes for edge in bm.edges)
            # Let Blender derive vertex/edge mode from the current UV/sync selection.
            # Forcing EDGE silently ignores a selection containing only UV vertices.
            result = bpy.ops.uv.stitch('EXEC_DEFAULT', use_limit=False, snap_islands=True, clear_seams=True)
            if result != {'FINISHED'}:
                return result
            cleared = 0
            sync = context.scene.tool_settings.use_uv_select_sync
            for mesh, bm, layer in meshes:
                dirty = False
                for edge in bm.edges:
                    if not edge.seam or edge.hide or len(edge.link_loops) != 2:
                        continue
                    a, b = edge.link_loops
                    selected = edge.select if sync else any(
                        loop.face.select and not loop.face.hide and loop[layer].select_edge for loop in (a, b))
                    if not selected or a.face.hide or b.face.hide:
                        continue
                    # A marked seam is not necessarily a UV split. Clear its mark if the
                    # selected sides already share identical endpoint UVs.
                    def uv_at(loop, vertex):
                        return (loop if loop.vert == vertex else loop.link_loop_next)[layer].uv
                    if all((uv_at(a, v)-uv_at(b, v)).length_squared < 1e-12 for v in edge.verts):
                        edge.seam = False
                        cleared += 1
                        dirty = True
                if dirty:
                    bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
            seams_after = sum(edge.seam for _, bm, _ in meshes for edge in bm.edges)
            if before == coordinates() and not cleared and seams_before == seams_after:
                self.report({'WARNING'}, 'No matching UV boundary selected; select corresponding vertices or edges on the same mesh')
                return {'CANCELLED'}
            _invalidate()
            return {'FINISHED'}


@persistent
def _invalidate(*_):
    global _revision
    _revision += 1


@persistent
def _load_pre(_):
    _cache.clear()
    _hud_cache.clear()


def register():
    bpy.types.Scene.uv_tools_distortion = BoolProperty(
        name='UV Area Distortion', default=False,
        description='UV/world area ratio normalized across visible edited or selected meshes: red < 1, gray = 1, blue > 1; histogram weighted by world surface area')
    bpy.app.handlers.load_pre.append(_load_pre)
    bpy.app.handlers.depsgraph_update_post.append(_invalidate)
    bpy.app.handlers.undo_post.append(_invalidate)
    bpy.app.handlers.redo_post.append(_invalidate)
    if not bpy.app.background:
        _handles.extend([
            (bpy.types.SpaceImageEditor, bpy.types.SpaceImageEditor.draw_handler_add(_draw_overlay, (True,), 'WINDOW', 'POST_VIEW')),
            (bpy.types.SpaceImageEditor, bpy.types.SpaceImageEditor.draw_handler_add(_draw_histogram, (), 'WINDOW', 'POST_PIXEL')),
            (bpy.types.SpaceView3D, bpy.types.SpaceView3D.draw_handler_add(_draw_overlay, (False,), 'WINDOW', 'POST_VIEW')),
        ])
        bpy.app.timers.register(_tick, first_interval=.3, persistent=True)


def unregister():
    global _shader, _icons, _overlay_shader, _wire_shader
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    for space, handle in _handles:
        space.draw_handler_remove(handle, 'WINDOW')
    _handles.clear()
    _cache.clear()
    _hud_cache.clear()
    _shader = None
    _overlay_shader = None
    _wire_shader = None
    if _icons is not None:
        from bpy.utils import previews
        previews.remove(_icons)
        _icons = None
    if _load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_load_pre)
    for handlers in (bpy.app.handlers.depsgraph_update_post, bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _invalidate in handlers:
            handlers.remove(_invalidate)
    del bpy.types.Scene.uv_tools_distortion


classes = (UVTOOLS_OT_mark_seam, UVTOOLS_OT_stitch)
