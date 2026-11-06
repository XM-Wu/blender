# SPDX-FileCopyrightText: 2026 Blender Authors
# SPDX-License-Identifier: GPL-2.0-or-later

"""UV seam actions and area distortion overlays (no mesh/material modifications)."""
import math
import bpy
import bmesh
import blf
import gpu
from bpy.app.handlers import persistent
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
)
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
_checker_shader = None


class UVTOOLS_OT_checker(bpy.types.Operator):
    bl_idname = 'uv_tools.checker'
    bl_label = 'UV Checker Preview'
    bl_description = 'Toggle a UV checker on the selected meshes and UV canvas without changing materials or view framing'

    def execute(self, context):
        context.scene.uv_tools_checker = not context.scene.uv_tools_checker
        _diagnostic_redraw(None, context)
        return {'FINISHED'}


class UVTOOLS_GGT_checker(bpy.types.GizmoGroup):
    bl_idname = 'UVTOOLS_GGT_checker'
    bl_label = 'UV Checker Preview'
    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'WINDOW'
    bl_options = {'PERSISTENT', 'SCALE'}

    @classmethod
    def poll(cls, context):
        return context.space_data.mode == 'UV'

    def setup(self, context):
        self.button = self.gizmos.new('GIZMO_GT_button_2d')
        self.button.target_set_operator('uv_tools.checker')
        self.button.icon = 'TEXTURE'
        self.button.draw_options = {'BACKDROP', 'OUTLINE'}
        self.button.scale_basis = 16
        self.button.alpha = self.button.alpha_highlight = 1.0
        self.button.color_highlight = (0.8, 0.9, 1.0)
        self.button.use_tooltip = True

    def draw_prepare(self, context):
        scale = context.preferences.system.ui_scale
        toolbar = next((r for r in context.area.regions if r.type == 'TOOLS'), None)
        left = toolbar.width if toolbar else 0
        self.button.matrix_basis.translation = (left + 40 * scale, context.region.height - 40 * scale, 0)
        self.button.color = (0.3, 0.6, 1.0) if context.scene.uv_tools_checker else (0.7, 0.7, 0.7)


def _get_checker_shader():
    global _checker_shader
    if _checker_shader is None:
        interface = gpu.types.GPUStageInterfaceInfo('uv_checker_interface')
        interface.smooth('VEC2', 'uv')
        info = gpu.types.GPUShaderCreateInfo()
        info.push_constant('MAT4', 'mvp')
        info.push_constant('FLOAT', 'cells')
        info.vertex_in(0, 'VEC3', 'pos')
        info.vertex_in(1, 'VEC2', 'texCoord')
        info.vertex_out(interface)
        info.fragment_out(0, 'VEC4', 'fragColor')
        info.vertex_source('void main() { gl_Position = mvp * vec4(pos, 1.0); uv = texCoord; }')
        info.fragment_source('''
            void main() {
                vec2 p = uv * cells;
                vec2 w = max(fwidth(p), vec2(0.00001));
                // Analytically filter the periodic checker over the pixel footprint.
                vec2 a = abs(fract((p - 0.5 * w) * 0.5) - 0.5);
                vec2 b = abs(fract((p + 0.5 * w) * 0.5) - 0.5);
                vec2 integral = 2.0 * (a - b) / w;
                float checker = 0.5 - 0.5 * integral.x * integral.y;
                fragColor = vec4(vec3(mix(0.08, 0.65, checker)), 1.0);
            }
        ''')
        _checker_shader = gpu.shader.create_from_info(info)
    return _checker_shader


def _draw_surface_preview(is_uv):
    context = bpy.context
    if not context.scene.uv_tools_checker:
        _draw_overlay(is_uv)
        return
    if not context.space_data.overlay.show_overlays or (is_uv and context.area.ui_type != 'UV'):
        return
    data = _cache.get(_key(context))
    shader = _get_checker_shader()
    blend, depth, mask = gpu.state.blend_get(), gpu.state.depth_test_get(), gpu.state.depth_mask_get()
    try:
        gpu.state.blend_set('NONE')
        gpu.state.depth_mask_set(False)
        gpu.state.depth_test_set('NONE' if is_uv else 'LESS_EQUAL')
        with gpu.matrix.push_pop_projection():
            shader.bind()
            shader.uniform_float('mvp', gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix())
            shader.uniform_float('cells', 32.0)
            if is_uv:
                batch_for_shader(shader, 'TRIS', {
                    'pos': ((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 0, 0), (1, 1, 0), (0, 1, 0)),
                    'texCoord': ((0, 0), (1, 0), (1, 1), (0, 0), (1, 1), (0, 1)),
                }).draw(shader)
            elif data and data.get('engine'):
                from .uv_tools_cache import CHUNK_SIZE
                engine = data['engine']
                for chunk in range((len(engine.triangles) + CHUNK_SIZE - 1) // CHUNK_SIZE):
                    key = ('checker', chunk)
                    if key not in data['batches']:
                        positions, _ = engine.chunk_arrays('positions', chunk)
                        uv = engine.uv[engine.triangles[chunk*CHUNK_SIZE:(chunk+1)*CHUNK_SIZE]].reshape((-1, 2)).astype('float32')
                        data['batches'][key] = batch_for_shader(shader, 'TRIS', {'pos': positions, 'texCoord': uv})
                    data['batches'][key].draw(shader)
    finally:
        gpu.state.blend_set(blend)
        gpu.state.depth_test_set(depth)
        gpu.state.depth_mask_set(mask)


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
            'stretched': engine.stretched, 'distortion_bins': engine.distortion_bins,
            'distortion_low': engine.distortion_low, 'distortion_high': engine.distortion_high, 'shape_bins': engine.shape_bins,
            'shape_over_two': engine.shape_over_two, 'shape_over_four': engine.shape_over_four, 'generation': engine.generation, 'batches': {}}


def _collect(context):
    from .uv_tools_cache import EditCache
    return _engine_data(EditCache(context))


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
            if context.scene.uv_tools_distortion or context.scene.uv_tools_checker:
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


def _get_overlay_shader():
    global _overlay_shader
    if _overlay_shader is None:
        interface = gpu.types.GPUStageInterfaceInfo('uv_distortion_interface')
        interface.smooth('FLOAT', 'raw_density')
        interface.smooth('FLOAT', 'raw_shape')
        interface.flat('FLOAT', 'raw_face_density')
        interface.flat('FLOAT', 'island_index')
        info = gpu.types.GPUShaderCreateInfo()
        info.push_constant('MAT4', 'mvp')
        info.push_constant('FLOAT', 'normalization')
        info.push_constant('INT', 'metric_kind')
        info.sampler(0, 'FLOAT_2D', 'island_norms')
        info.vertex_in(0, 'VEC3', 'pos')
        info.vertex_in(1, 'FLOAT', 'density')
        info.vertex_in(2, 'FLOAT', 'shape_ratio')
        info.vertex_in(3, 'FLOAT', 'face_density')
        info.vertex_in(4, 'FLOAT', 'island_id')
        info.vertex_out(interface)
        info.fragment_out(0, 'VEC4', 'fragColor')
        info.vertex_source('void main() { gl_Position = mvp * vec4(pos, 1.0); raw_density = density; raw_shape = shape_ratio; raw_face_density = face_density; island_index = island_id; }')
        info.fragment_source("""
            void main() {
                if (raw_density < 0.0) discard;
                if (metric_kind == 1) {
                    vec3 neutral = vec3(0.48, 0.50, 0.53);
                    vec3 yellow = vec3(1.0, 0.72, 0.08);
                    vec3 red = vec3(0.95, 0.08, 0.06);
                    vec3 color = raw_shape < 2.0 ? mix(neutral, yellow, clamp(raw_shape-1.0,0.0,1.0)) :
                        mix(yellow, red, clamp((raw_shape-2.0)/2.0,0.0,1.0));
                    fragColor = vec4(color, 0.83);
                    return;
                }
                float ratio;
                if (metric_kind == 2) {
                    int index = int(island_index);
                    int width = textureSize(island_norms, 0).x;
                    float factor = texelFetch(island_norms, ivec2(index % width, index / width), 0).r;
                    ratio = sqrt(max(0.0, raw_face_density * factor));
                }
                else {
                    if (normalization <= 0.0) discard;
                    ratio = raw_density * normalization;
                }
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
        data['batches'][name] = batch_for_shader(shader, 'TRIS', {'pos': data[name], 'density': data['densities'], 'shape_ratio': data['shape_vertices']})

    def draw():
        shader.bind()
        shader.uniform_int('metric_kind', {'DENSITY': 0, 'SHAPE': 1, 'DISTORTION': 2}[context.scene.uv_tools_distortion_metric])
        if engine:
            from .uv_tools_cache import CHUNK_SIZE
            shader.bind()
            shader.uniform_float('mvp', gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix())
            shader.uniform_float('normalization', engine.normalization)
            shader.uniform_sampler('island_norms', engine.normalization_texture())
            for chunk in range((len(engine.triangles) + CHUNK_SIZE - 1) // CHUNK_SIZE):
                if chunk not in engine.batches[name]:
                    positions, density = engine.chunk_arrays(name, chunk)
                    face_density, island_id = engine.chunk_island_values(chunk)
                    engine.batches[name][chunk] = {'faces': batch_for_shader(shader, 'TRIS',
                        {'pos': positions, 'density': density, 'shape_ratio': engine.chunk_shape(chunk),
                         'face_density': face_density, 'island_id': island_id})}
                engine.batches[name][chunk]['faces'].draw(shader)
        else:
            shader.bind()
            shader.uniform_float('mvp', gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix())
            shader.uniform_float('normalization', 1.0)
            data['batches'][name].draw(shader)

    blend, depth, mask = gpu.state.blend_get(), gpu.state.depth_test_get(), gpu.state.depth_mask_get()
    try:
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('NONE' if is_uv else 'LESS_EQUAL')
        gpu.state.depth_mask_set(False)
        draw()

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


def shape_color(value):
    neutral, yellow, red = (0.48, 0.50, 0.53), (1.0, .72, .08), (.95, .08, .06)
    a, b = (neutral, yellow) if value < 2 else (yellow, red)
    t = min(1.0, max(0.0, value-1 if value < 2 else (value-2)/2))
    return (*(x+(y-x)*t for x,y in zip(a,b)), 1.0)


def _diagnostic_redraw(_self, context):
    if context.scene.uv_tools_distortion and context.scene.uv_tools_checker:
        context.scene.uv_tools_checker = False
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type in {'VIEW_3D', 'IMAGE_EDITOR'}:
                area.tag_redraw()


def _checker_redraw(_self, context):
    if context.scene.uv_tools_checker:
        context.scene.uv_tools_distortion = False
    _diagnostic_redraw(None, context)


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
    shape = context.scene.uv_tools_distortion_metric == 'SHAPE'
    island_relative = context.scene.uv_tools_distortion_metric == 'DISTORTION'
    title = ('Shape Stretch (principal stretch ratio)' if shape else
             'Distortion (island-relative area scale)' if island_relative else 'Density (relative UV area ratio)')
    color_at = (lambda f: shape_color(1+3*f)) if shape else (lambda f: distortion_color(2*f))
    left, right = 16*scale, width-16*scale
    span = right-left
    _rect(0, 0, width, 94*scale, (.055,.06,.07,.97))
    _text(left, 75*scale, title + (' | ON' if enabled else ' | OFF'), int(11*scale))
    for i in range(_BINS):
        _rect(left+span*i/_BINS, 24*scale, span/_BINS+.5, 10*scale, color_at((i+.5)/_BINS))
    if enabled and data and data['count']:
        bins = data['shape_bins'] if shape else data['distortion_bins'] if island_relative else data['bins']
        peak = max(bins)
        for i,value in enumerate(bins):
            if value:
                _rect(left+span*i/_BINS, 36*scale, max(1,span/_BINS-1),
                      30*scale*value/peak, color_at((i+.5)/_BINS))
        if width > 800*scale:
            summary = (f"Area >2x: {data['shape_over_two']:.1%}  >4x: {data['shape_over_four']:.1%}" if shape else
                       f"Compressed: {data['distortion_low']:.1%}  Expanded: {data['distortion_high']:.1%}" if island_relative else
                       f"Lower density: {data['compressed']:.1%}  Higher density: {data['stretched']:.1%}")
            _text(left+400*scale,75*scale,summary,int(10*scale))
    elif enabled:
        _text(left,47*scale,'Select a mesh with a UV map',int(11*scale))
    neutral_x = left if shape else left+span/2
    _rect(neutral_x,22*scale,scale,46*scale,(.9,.9,.9,.7))
    labels = ((0,'1 / Shape preserved'),(1/3,'2'),(2/3,'3'),(1,'4+ / High distortion')) if shape else (
        (0,'0 / Compressed' if island_relative else '0 / Low density'),(.25,'0.5'),
        (.5,'1 / Neutral' if island_relative else '1 / Average'),(.75,'1.5'),
        (1,'2+ / Expanded' if island_relative else '2+ / High density'))
    for fraction,label in labels:
        blf.size(0,int(10*scale))
        tw=blf.dimensions(0,label)[0]
        _text(min(right-tw,max(left,left+span*fraction-tw/2)),8*scale,label,int(10*scale))


def _draw_histogram():
    global _hud_record
    context = bpy.context
    if context.area.ui_type != 'UV' or context.region.width < 240 * context.preferences.system.ui_scale:
        return
    data = _cache.get(_key(context))
    region_key = context.region.as_pointer()
    signature = (context.region.width, context.preferences.system.ui_scale,
                 context.scene.uv_tools_distortion, context.scene.uv_tools_distortion_metric, id(data))
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
        for kind in ('cut', 'stitch', 'unwrap', 'pack', 'texel_pack'):
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
                            elif kind == 'texel_pack':
                                # Islands snapped to a texel grid, with a stacked pair.
                                if 3 <= x <= 29 and 3 <= y <= 29 and (min(x-3, 29-x, y-3, 29-y) < 1 or (
                                        (x - 3) % 6.5 < .45 or (y - 3) % 6.5 < .45)):
                                    c = (.45, .52, .6, 1)
                                for ox, oy, w, h, color in (
                                        (5, 5, 11, 11, (.12, .7, .95, 1)),
                                        (18, 5, 9, 17, (1, .58, .12, 1)),
                                        (7, 18, 8, 8, (.3, .85, .48, 1)),
                                        (5, 16, 8, 8, (.2, .62, .36, 1))):
                                    if ox <= x <= ox+w and oy <= y <= oy+h:
                                        c = color
                                        if min(x-ox, ox+w-x, y-oy, oy+h-y) < .7:
                                            c = (.9, .96, 1, 1)
                            for j in range(4):
                                accum[j] += c[j] / (samples * samples)
                    pixels.extend(accum)
            preview = _icons.new(kind)
            preview.image_size = (size, size)
            preview.image_pixels_float = pixels
            preview.icon_size = (size, size)
            preview.icon_pixels_float = pixels
    return _icons[name].icon_id


def _redraw_uv_editors(_self, context):
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'IMAGE_EDITOR':
                area.tag_redraw()


def _set_resolution_preset(self, _context):
    if self.resolution_preset != 'CUSTOM':
        self.resolution_x = self.resolution_y = int(self.resolution_preset)


_ISLAND_ROTATION_ITEMS = (
    ('INHERIT', 'Default', 'Use the packing setting'),
    ('NONE', 'None', 'Keep the current orientation'),
    ('CARDINAL', '90°', 'Rotate by multiples of 90 degrees'),
    ('FREE', 'Free', 'Rotate to the smallest bounding box first'),
)

# Settings passed on to `uv.texel_pack`, named like its properties.
TEXEL_PACK_SETTINGS = (
    'resolution_x', 'resolution_y', 'precision', 'margin', 'edge_padding', 'pixel_snap',
    'density_mode', 'target_density', 'density_tolerance', 'use_density_weights',
    'match_locked_density', 'rotation', 'allow_flip', 'use_stacking', 'stack_mirrored',
    'stack_tolerance', 'stack_offset', 'target', 'udim_tile', 'max_tiles', 'group_layout',
    'only_selected', 'keep_unselected',
)


class UVToolsTexelPackSettings(bpy.types.PropertyGroup):
    """Scene settings of the Texel Packing panel, stored in the blend file."""
    resolution_preset: EnumProperty(
        name='Texture Size',
        items=[(str(size), str(size), '') for size in (512, 1024, 2048, 4096, 8192)] +
        [('CUSTOM', 'Custom', 'Width and height set separately')],
        default='1024', update=_set_resolution_preset)
    resolution_x: IntProperty(name='Width', default=1024, min=16, max=32768)
    resolution_y: IntProperty(name='Height', default=1024, min=16, max=32768)
    precision: EnumProperty(name='Precision', default='MEDIUM', items=(
        ('LOW', 'Low', 'Occupancy grid of at most 512 cells'),
        ('MEDIUM', 'Medium', 'Occupancy grid of at most 1024 cells'),
        ('HIGH', 'High', 'Occupancy grid of at most 2048 cells'),
        ('EXACT', 'Exact', 'One occupancy cell per texture pixel'),
    ))
    margin: FloatProperty(
        name='Margin', default=4.0, min=0.0, max=1024.0, soft_max=64.0, subtype='PIXEL',
        description='Minimum distance between islands in pixels (mip level N needs 2^N)')
    edge_padding: FloatProperty(
        name='Edge Padding', default=2.0, min=0.0, max=1024.0, soft_max=64.0, subtype='PIXEL',
        description='Minimum distance between islands and the tile border in pixels')
    pixel_snap: BoolProperty(name='Pixel Snap', default=True, description='Move islands by whole pixels only')
    density_mode: EnumProperty(name='Texel Density', default='UNIFORM', items=(
        ('UNIFORM', 'Uniform', 'Give all islands the same texel density and fill the tiles'),
        ('RELATIVE', 'Keep Relative', 'Keep the relative UV scale of islands and fill the tiles'),
        ('TARGET', 'Target', 'Use a fixed texel density, overflowing into further tiles'),
    ))
    target_density: FloatProperty(
        name='Target Density', default=512.0, min=0.001, soft_min=1.0, soft_max=8192.0,
        description='Texel density in pixels per meter')
    density_tolerance: FloatProperty(
        name='Density Tolerance', default=0.0, min=0.0, max=0.5, subtype='FACTOR',
        description='How much islands may shrink below the common density to make them fit')
    use_density_weights: BoolProperty(
        name='Density Weights', default=True,
        description='Multiply the density of islands by their density constraint')
    match_locked_density: BoolProperty(
        name='Match Locked Density', default=True,
        description='With locked islands and uniform density, use their density instead of filling the tiles')
    rotation: EnumProperty(name='Rotation', default='CARDINAL', items=_ISLAND_ROTATION_ITEMS[1:])
    allow_flip: BoolProperty(
        name='Allow Flip', default=False,
        description='Allow mirroring islands without their own flip constraint (changes tangent space handedness)')
    use_stacking: BoolProperty(name='Stack Identical', default=True,
                               description='Overlap islands with identical shapes')
    stack_mirrored: BoolProperty(
        name='Stack Mirrored', default=False,
        description='Also overlap mirror images of islands (changes tangent space handedness)')
    stack_tolerance: FloatProperty(
        name='Stack Tolerance', default=0.01, min=0.0001, max=0.2, soft_max=0.1, subtype='FACTOR',
        description='Shape difference allowed for stacking, relative to the island size')
    stack_offset: IntProperty(
        name='Stack Offset', default=0, min=0, max=99, soft_max=10,
        description='Move stacked copies this many UDIM tiles away, for baking')
    target: EnumProperty(name='Pack To', default='CLOSEST', items=(
        ('CLOSEST', 'Closest Tile', 'Start at the tile closest to the islands'),
        ('ACTIVE', 'Active Tile', 'Start at the active image tile, or the tile under the 2D cursor'),
        ('FIXED', 'Fixed Tile', 'Start at the given UDIM tile'),
    ))
    udim_tile: IntProperty(name='UDIM Tile', default=1001, min=1001, max=2000)
    max_tiles: IntProperty(name='Max Tiles', default=1, min=1, max=100, soft_max=10,
                           description='Number of UDIM tiles islands may use')
    group_layout: EnumProperty(name='Groups', default='MIXED', items=(
        ('MIXED', 'Mixed', 'Ignore island groups'),
        ('CLUSTER', 'Cluster', 'Place islands of the same group next to each other'),
        ('TILE_PER_GROUP', 'Tile per Group', 'Give every group its own UDIM tile'),
    ))
    only_selected: BoolProperty(name='Selected Only', default=True, description='Only pack selected islands')
    keep_unselected: BoolProperty(
        name='Avoid Unselected', default=False,
        description='Keep unselected islands in place and pack around them')

    show_groups: BoolProperty(
        name='Show Groups', default=False, update=_redraw_uv_editors,
        description='Outline every island group in the UV editor with its color and id')
    show_stacks: BoolProperty(
        name='Show Stacks', default=False, update=_redraw_uv_editors,
        description='Outline every manual island stack in the UV editor with its color and id')


def _uv_area(context):
    return next((a for a in context.screen.areas if a.type == 'IMAGE_EDITOR' and a.ui_type == 'UV'), None)


class UVTOOLS_OT_select_mode(bpy.types.Operator):
    bl_idname = 'uv_tools.select_mode'
    bl_label = 'UV Selection Mode'
    bl_description = 'Choose vertex, edge, face or island selection exclusively'
    bl_options = {'REGISTER', 'UNDO'}

    type: EnumProperty(items=(('VERTEX', 'Vertex', ''), ('EDGE', 'Edge', ''),
                              ('FACE', 'Face', ''), ('ISLAND', 'Island', '')))

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        settings = context.tool_settings
        area = _uv_area(context)
        if area is None:
            return {'CANCELLED'}
        target = 'FACE' if self.type == 'ISLAND' else self.type
        with context.temp_override(area=area, region=next(r for r in area.regions if r.type == 'WINDOW')):
            settings.use_uv_select_island = False
            if settings.use_uv_select_sync:
                desired = tuple(target == mode for mode in ('VERTEX', 'EDGE', 'FACE'))
                result = ({'FINISHED'} if tuple(settings.mesh_select_mode) == desired else
                          bpy.ops.mesh.select_mode(type='VERT' if target == 'VERTEX' else target))
            else:
                result = ({'FINISHED'} if settings.uv_select_mode == target else bpy.ops.uv.select_mode(type=target))
            if result != {'FINISHED'}:
                return result
            # Keep the stored UV mode aligned too, including when sync is later disabled.
            if settings.uv_select_mode != target:
                settings.uv_select_mode = target
            settings.use_uv_select_island = self.type == 'ISLAND'
        for window in context.window_manager.windows:
            for editor in window.screen.areas:
                if editor.type in {'IMAGE_EDITOR', 'VIEW_3D'}:
                    editor.tag_redraw()
        return {'FINISHED'}


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


def _migrate_uv_selection_keymaps():
    """Route legacy saved UV keymaps through the same exclusive modes as the header."""
    configs = bpy.context.window_manager.keyconfigs
    seen = set()
    for config in (configs.default, configs.active, configs.user, configs.addon):
        if config is None or config.as_pointer() in seen:
            continue
        seen.add(config.as_pointer())
        keymap = config.keymaps.get('UV Editor')
        if keymap is None:
            continue
        for item in keymap.keymap_items:
            if item.shift or item.ctrl or item.alt or item.oskey or item.any:
                continue
            mode = None
            if item.idname in {'mesh.select_mode', 'uv.select_mode'}:
                mode = getattr(item.properties, 'type', None)
                if mode == 'VERT':
                    mode = 'VERTEX'
            elif (item.idname == 'wm.context_toggle' and
                  item.properties.data_path == 'tool_settings.use_uv_select_island'):
                mode = 'ISLAND'
            if mode in {'VERTEX', 'EDGE', 'FACE', 'ISLAND'}:
                item.idname = 'uv_tools.select_mode'
                item.properties.type = mode
    return None


def register():
    bpy.utils.register_class(UVTOOLS_GGT_checker)
    bpy.types.Scene.uv_tools_checker = BoolProperty(
        name='UV Checker', default=False, update=_checker_redraw,
        description='Preview a checker on the UV canvas and selected meshes without altering materials')
    bpy.types.Scene.uv_tools_texel_pack = PointerProperty(type=UVToolsTexelPackSettings)
    bpy.types.Scene.uv_tools_distortion = BoolProperty(
        name='UV Diagnostics', default=False, update=_diagnostic_redraw,
        description='Show the selected UV metric on the UVs and model')
    bpy.types.Scene.uv_tools_distortion_metric = EnumProperty(
        name='Diagnostic Metric', default='DISTORTION', update=_diagnostic_redraw,
        items=(('DISTORTION', 'Distortion', 'Square root of polygon UV/world area ratio normalized within each UV island', 0, 2),
               ('DENSITY', 'Density', 'Relative UV/world area ratio: red below average, blue above average', 0, 0),
               ('SHAPE', 'Shape Stretch', 'Ratio of principal stretches: 1 preserves shape; independent of uniform UV scale; does not detect mirrored orientation', 0, 1)))
    bpy.app.handlers.load_pre.append(_load_pre)
    bpy.app.handlers.depsgraph_update_post.append(_invalidate)
    bpy.app.handlers.undo_post.append(_invalidate)
    bpy.app.handlers.redo_post.append(_invalidate)
    if not bpy.app.background:
        _handles.extend([
            (bpy.types.SpaceImageEditor, bpy.types.SpaceImageEditor.draw_handler_add(_draw_surface_preview, (True,), 'WINDOW', 'PRE_OVERLAY')),
            (bpy.types.SpaceImageEditor, bpy.types.SpaceImageEditor.draw_handler_add(_draw_histogram, (), 'WINDOW', 'POST_PIXEL')),
            (bpy.types.SpaceView3D, bpy.types.SpaceView3D.draw_handler_add(_draw_surface_preview, (False,), 'WINDOW', 'PRE_OVERLAY')),
        ])
        bpy.app.timers.register(_tick, first_interval=.3, persistent=True)
        bpy.app.timers.register(_migrate_uv_selection_keymaps, first_interval=1.0)


def unregister():
    global _shader, _icons, _overlay_shader
    global _checker_shader
    bpy.utils.unregister_class(UVTOOLS_GGT_checker)
    del bpy.types.Scene.uv_tools_checker
    _checker_shader = None
    if bpy.app.timers.is_registered(_migrate_uv_selection_keymaps):
        bpy.app.timers.unregister(_migrate_uv_selection_keymaps)
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    for space, handle in _handles:
        space.draw_handler_remove(handle, 'WINDOW')
    _handles.clear()
    _cache.clear()
    _hud_cache.clear()
    _shader = None
    _overlay_shader = None
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
    del bpy.types.Scene.uv_tools_distortion_metric
    del bpy.types.Scene.uv_tools_texel_pack


classes = (UVToolsTexelPackSettings, UVTOOLS_OT_select_mode, UVTOOLS_OT_mark_seam, UVTOOLS_OT_stitch,
           UVTOOLS_OT_checker)
