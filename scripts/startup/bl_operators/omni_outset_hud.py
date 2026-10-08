# SPDX-FileCopyrightText: 2026 OmniOutset Authors
# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

import math
from collections import OrderedDict

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader


DEFAULT_FONT_SIZE = 18

_BOTTOM_MARGIN = 80
_TEXT_COLOR = (1.0, 1.0, 1.0, 1.0)
_SHIFT_COLOR = (1.0, 0.72, 0.08, 1.0)
_CTRL_COLOR = (0.25, 1.0, 0.35, 1.0)
_PANEL_BACKGROUND = (0.20, 0.20, 0.20, 0.75)
_PANEL_ACTIVE_BACKGROUND = (0.25, 0.25, 0.25, 0.90)
_PANEL_BORDER = (0.35, 0.35, 0.35, 0.90)
_PANEL_ACTIVE_BORDER = (0.35, 0.35, 0.35, 0.95)
_ROUNDED_BOX_CACHE_LIMIT = 48

_draw_handles = []
_uniform_color_shader = None
_rounded_box_cache = None


def _format_distance(value):
    if abs(value) < 0.005:
        value = 0.0
    return f"{value:.2f}"


def _get_preferences(context):
    window_manager = getattr(context, "window_manager", None)
    if window_manager is None:
        return None
    return getattr(window_manager, "omni_outset", None)


def _get_uniform_color_shader():
    global _uniform_color_shader
    if _uniform_color_shader is None:
        _uniform_color_shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    return _uniform_color_shader


def _get_rounded_box_cache():
    global _rounded_box_cache
    if _rounded_box_cache is None:
        _rounded_box_cache = OrderedDict()
    return _rounded_box_cache


def _get_rounded_box_geometry(width, height, radius, border_width, resolution=12):
    cache = _get_rounded_box_cache()
    key = (
        int(float(width) // 5) * 5,
        int(float(height) // 2) * 2,
        round(float(radius), 3),
        round(float(border_width), 3),
        int(resolution),
    )
    cached = cache.get(key)
    if cached is not None:
        cache.move_to_end(key)
        return cached

    centers = [
        (width - radius, height - radius),
        (radius, height - radius),
        (radius, radius),
        (width - radius, radius),
    ]
    outer_verts = []
    inner_verts = []
    inner_radius = max(0.1, radius - border_width)
    for corner_index, (center_x, center_y) in enumerate(centers):
        start_angle = corner_index * (math.pi / 2)
        for step in range(resolution + 1):
            theta = start_angle + (math.pi / 2) * (step / resolution)
            cos_theta = math.cos(theta)
            sin_theta = math.sin(theta)
            outer_verts.append(
                (center_x + cos_theta * radius, center_y + sin_theta * radius)
            )
            inner_verts.append(
                (
                    center_x + cos_theta * inner_radius,
                    center_y + sin_theta * inner_radius,
                )
            )

    vertex_count = len(inner_verts)
    background_indices = []
    for index in range(vertex_count):
        background_indices.append(
            (0, index + 1, ((index + 1) % vertex_count) + 1)
        )

    border_indices = []
    for index in range(len(outer_verts)):
        outer_next = (index + 1) % len(outer_verts)
        inner_index = index + len(outer_verts)
        inner_next = outer_next + len(outer_verts)
        border_indices.extend(
            [
                (index, inner_index, outer_next),
                (inner_index, inner_next, outer_next),
            ]
        )

    cached = (
        tuple(outer_verts),
        tuple(inner_verts),
        tuple(background_indices),
        tuple(border_indices),
    )
    cache[key] = cached
    while len(cache) > _ROUNDED_BOX_CACHE_LIMIT:
        cache.popitem(last=False)
    return cached


def _draw_rounded_box(
    x,
    y,
    width,
    height,
    background_color,
    border_color,
    radius=8,
    border_width=1,
):
    radius = min(radius, width / 2, height / 2)
    outer_relative, inner_relative, background_indices, border_indices = (
        _get_rounded_box_geometry(
            width,
            height,
            radius,
            border_width,
        )
    )
    outer_verts = [(x + vx, y + vy) for vx, vy in outer_relative]
    inner_verts = [(x + vx, y + vy) for vx, vy in inner_relative]

    shader = _get_uniform_color_shader()
    shader.bind()
    gpu.state.blend_set("ALPHA")
    try:
        background_verts = [(x + width / 2, y + height / 2)] + inner_verts
        background_batch = batch_for_shader(
            shader,
            "TRIS",
            {"pos": background_verts},
            indices=background_indices,
        )
        shader.uniform_float("color", background_color)
        background_batch.draw(shader)

        border_batch = batch_for_shader(
            shader,
            "TRIS",
            {"pos": outer_verts + inner_verts},
            indices=border_indices,
        )
        shader.uniform_float("color", border_color)
        border_batch.draw(shader)
    finally:
        gpu.state.blend_set("NONE")


def _items(operator):
    translate = bpy.app.translations.pgettext_iface
    ctrl_active = getattr(operator, "_hud_ctrl", False)
    shift_active = getattr(operator, "_hud_shift", False)

    if getattr(operator, "_hud_kind", None) == "FACE":
        extrude_label = translate("Extrude Distance")
        outward_label = translate("Outward Offset")
        modifier_hint = (
            f'{translate("Ctrl: Outward Offset")}    '
            f'{translate("Shift: Precision")}'
        )
        return [
            (
                f"{extrude_label}: {_format_distance(operator.extrude_dist)}",
                "active" if not ctrl_active else "value",
            ),
            (
                f"{outward_label}: {_format_distance(operator.outward_offset)}",
                "active" if ctrl_active else "value",
            ),
            (
                modifier_hint,
                "active" if ctrl_active or shift_active else "hint",
            ),
        ]

    extrude_label = translate("Extrude")
    offset_label = translate("Offset")
    modifier_hint = (
        f'{translate("Ctrl: Offset")}    '
        f'{translate("Shift: Precision")}'
    )
    return [
        (
            f"{extrude_label}: {_format_distance(operator.extrude)}",
            "active" if not ctrl_active else "value",
        ),
        (
            f"{offset_label}: {_format_distance(operator.offset)}",
            "active" if ctrl_active else "value",
        ),
        (
            modifier_hint,
            "active" if ctrl_active or shift_active else "hint",
        ),
    ]


def _draw(operator, area, region):
    if not getattr(operator, "_hud_active", False):
        return

    context = bpy.context
    if context.area != area or context.region != region:
        return

    preferences = _get_preferences(context)
    if preferences is not None and not preferences.show_hud:
        return

    font_size = max(
        10,
        int(getattr(preferences, "hud_font_size", DEFAULT_FONT_SIZE)),
    )
    if getattr(operator, "_hud_ctrl", False):
        color = _CTRL_COLOR
    elif getattr(operator, "_hud_shift", False):
        color = _SHIFT_COLOR
    else:
        color = _TEXT_COLOR

    font_id = 0
    items = _items(operator)
    ui_scale = 1.3 * 0.96
    padding_x = 7 * ui_scale
    padding_y = 5 * ui_scale
    text_lift = 3 * ui_scale

    blf.size(font_id, font_size)
    dimensions = [blf.dimensions(font_id, text) for text, _role in items]
    widths = [width + (padding_x * 2) for width, _height in dimensions]
    box_height = max(height for _width, height in dimensions) + (padding_y * 2)
    total_width = sum(widths)
    x = (region.width - total_width) * 0.5
    y = _BOTTOM_MARGIN

    for (text, role), width in zip(items, widths):
        is_active = role == "active"
        background_color = (
            _PANEL_ACTIVE_BACKGROUND if is_active else _PANEL_BACKGROUND
        )
        border_color = _PANEL_ACTIVE_BORDER if is_active else _PANEL_BORDER
        _draw_rounded_box(
            x,
            y,
            width,
            box_height,
            background_color,
            border_color,
        )

        alpha_factor = 1.0 if is_active else 0.72
        blf.color(
            font_id,
            color[0],
            color[1],
            color[2],
            color[3] * alpha_factor,
        )
        blf.position(font_id, x + padding_x, y + padding_y + text_lift, 0)
        blf.draw(font_id, text)
        x += width


def tag_redraw(operator):
    area = getattr(operator, "_hud_area", None)
    if area is not None:
        try:
            area.tag_redraw()
        except ReferenceError:
            pass


def start(operator, context, kind):
    operator._hud_active = False
    operator._hud_handle = None
    operator._hud_kind = kind
    operator._hud_ctrl = False
    operator._hud_shift = False

    if bpy.app.background or context.area is None or context.region is None:
        return
    if context.area.type != "VIEW_3D" or context.region.type != "WINDOW":
        return

    preferences = _get_preferences(context)
    if preferences is not None and not preferences.show_hud:
        return

    operator._hud_area = context.area
    operator._hud_region = context.region
    operator._hud_active = True
    operator._hud_handle = bpy.types.SpaceView3D.draw_handler_add(
        _draw,
        (operator, context.area, context.region),
        "WINDOW",
        "POST_PIXEL",
    )
    _draw_handles.append(operator._hud_handle)
    context.area.tag_redraw()


def stop(operator):
    operator._hud_active = False
    handle = getattr(operator, "_hud_handle", None)
    if handle is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(handle, "WINDOW")
        except (ReferenceError, RuntimeError, ValueError):
            pass
        if handle in _draw_handles:
            _draw_handles.remove(handle)
        operator._hud_handle = None
    tag_redraw(operator)


def clear_handlers():
    global _rounded_box_cache, _uniform_color_shader

    for handle in list(_draw_handles):
        try:
            bpy.types.SpaceView3D.draw_handler_remove(handle, "WINDOW")
        except (ReferenceError, RuntimeError, ValueError):
            pass
    _draw_handles.clear()
    _rounded_box_cache = None
    _uniform_color_shader = None
