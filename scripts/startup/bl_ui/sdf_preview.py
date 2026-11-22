# SPDX-License-Identifier: GPL-2.0-or-later
"""GPU resources and drawing for the 2D and 3D SDF preview."""

from functools import lru_cache

import numpy as np

from . import sdf_field as field, sdf_wire
from .sdf_field import key_at, key_images
from .sdf_pick import geometry


_batches = {}
_texture = None
_texture_token = None
_coverage_pixels = _coverage_texture = _canvas_shader = _wire_shader = None


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
            """void main() {
                float coverage = texture(maskMap, uv).r;
                if (coverage <= 0.0) discard;
                color = vec4(highlightConflict(sdfBaseColor(texture(inkMap, uv).r)), coverage);
            }""")
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


def textures(mask, value, onion_mode, stroke):
    global _texture, _texture_token, _coverage_pixels, _coverage_texture
    painting = stroke and stroke.mask == mask and abs(stroke.key_value - value) < 1e-6
    if painting:
        token = id(stroke.bits)
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
        bits = (stroke.bits if painting else
                field.preview(keys, value, mask.coverage, mask.size))
        coverage = stroke.coverage if painting else field.decode(mask.coverage, mask.size)
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


def draw_surface_preview(ctx, mask, ink, coverage, ghost, compare_mode):
    import gpu
    from gpu_extras.batch import batch_for_shader

    dg = ctx.evaluated_depsgraph_get()
    for source in mask.sources:
        obj = source.object
        if not obj or obj.mode != 'SDF_PAINT' or not obj.visible_get(view_layer=ctx.view_layer, viewport=ctx.space_data):
            continue
        surface = geometry(source, dg)
        if surface is None:
            continue
        strength = wire_strength(ctx, obj)
        shader = bind_canvas_shader(ink, coverage, ghost, compare_mode, strength)
        if strength > 0:
            batch = sdf_wire.batch(source, dg, shader, True, surface, _batches)
        else:
            token = ('batch', dg.as_pointer(), obj.as_pointer(), source.faces, source.uv_name)
            if token not in _batches:
                _batches[token] = batch_for_shader(shader, 'TRIS', {
                    "pos": surface.positions.reshape(-1, 3), "texCoord": surface.uvs.reshape(-1, 2)})
            batch = _batches[token]
        if batch is None:
            continue
        # Wire-shaded viewports have no solid surface depth to hide rear edges.
        gpu.state.depth_mask_set(ctx.space_data.shading.type == 'WIREFRAME' or obj.display_type == 'WIRE')
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


def draw_uv_preview(ctx, mask, ink, coverage, ghost, compare_mode):
    import gpu
    from gpu_extras.batch import batch_for_shader

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
                    batch = sdf_wire.batch(source, dg, shader, False, None, _batches)
                    if batch:
                        batch.draw(shader)


def draw_canvas(ctx, is_3d, stroke):
    import gpu
    settings = ctx.scene.sdf_editor
    mask = settings.mask
    blend, depth, write = gpu.state.blend_get(), gpu.state.depth_test_get(), gpu.state.depth_mask_get()
    try:
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('LESS_EQUAL' if is_3d else 'NONE')
        gpu.state.depth_mask_set(False)
        ink, coverage, ghost, compare_mode = textures(mask, settings.value, settings.onion_skin, stroke)
        if is_3d:
            draw_surface_preview(ctx, mask, ink, coverage, ghost, compare_mode)
        else:
            draw_uv_preview(ctx, mask, ink, coverage, ghost, compare_mode)
    finally:
        gpu.state.blend_set(blend)
        gpu.state.depth_test_set(depth)
        gpu.state.depth_mask_set(write)


def invalidate_texture():
    global _texture_token
    _texture_token = None


def clear_batches():
    _batches.clear()


def clear_runtime():
    global _texture, _coverage_pixels, _coverage_texture, _canvas_shader, _wire_shader
    invalidate_texture()
    clear_batches()
    onion_texture.cache_clear()
    _texture = _coverage_pixels = _coverage_texture = _canvas_shader = _wire_shader = None
