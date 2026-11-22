# SPDX-License-Identifier: GPL-2.0-or-later
"""Screen-space controls for the SDF image editor. Coordinates are in UI units."""

import bpy
from bpy.props import StringProperty
from bpy.types import Operator


# Older SDF workspaces saved the former Masks sidebar open. Leave the
# regular N sidebar available to users after the editor is initialized.
_spaces = set()
# Transient inline input, independent in each editor area.
_number_inputs = {}


def initialize_space(context):
    area = context.area
    token = area.as_pointer()
    if token in _spaces:
        return
    _spaces.add(token)
    if not context.space_data.show_region_ui:
        return
    window = context.window

    def close_legacy_sidebar():
        try:
            if area in tuple(window.screen.areas) and area.ui_type == 'SDF':
                with bpy.context.temp_override(window=window, area=area):
                    area.spaces.active.show_region_ui = False
        except ReferenceError:
            pass
    bpy.app.timers.register(close_legacy_sidebar)


def leave_space(area):
    _spaces.discard(area.as_pointer())


def text_width(label, scale):
    import blf
    blf.size(0, 12 * scale)
    return blf.dimensions(0, label)[0] / scale


class CanvasControls:
    def __init__(self, context):
        self.number_input = _number_inputs.get(context.area.as_pointer())
        self.scale = max(.5, context.preferences.system.ui_scale)
        region = context.region
        width, height = region.width / self.scale, region.height / self.scale
        left, right = 0, width
        for neighbor in context.area.regions:
            if neighbor.width <= 1 or neighbor.type not in {'UI', 'TOOLS'}:
                continue
            if neighbor.type == 'TOOLS':
                left = max(left, (neighbor.x + neighbor.width - region.x) / self.scale)
            else:
                right = min(right, (neighbor.x - region.x) / self.scale)
        left = max(0, left)
        right = max(left + 1, right)
        self.shapes, self.labels, self.targets = [], [], []
        self.track = None
        settings = context.scene.sdf_editor
        if right - left > 90 and height > 65:
            self.mask_panel(settings, left + 9, height - 12, min(242, right - left - 18), height)
        if settings.mask and right - left > 140 and height > 125:
            self.timeline(settings, left + 8, right - 8)

    bg = (.12, .12, .12, .98)
    header = (.17, .17, .17, 1)
    button = (.24, .24, .24, 1)
    blue = (.24, .40, .65, 1)
    fg = (.91, .91, .91, 1)
    dim = (.68, .68, .68, 1)

    def rect(self, box, color, action=None):
        x, y, w, h = box
        self.shapes.append((((x, y), (x + w, y), (x + w, y + h), (x, y + h)), color))
        if action:
            self.targets.append((box, action))

    def label(self, x, y, value, color=None, align='LEFT', limit=None):
        self.labels.append((x, y, value, color or self.fg, align, limit))

    def control(self, x, y, w, h, text, action):
        self.rect((x, y, w, h), self.button, action)
        self.label(x + w / 2, y + h / 2, text, align='CENTER')

    def mask_panel(self, s, x, top, w, height):
        tr = bpy.app.translations.pgettext_iface
        masks = s.masks
        # Keep the bottom track accessible in short split editors.
        count = min(len(masks), max(1, min(5, int((height - 175) / 22))))
        offset = min(s.mask_offset, max(0, len(masks) - count))
        pages = len(masks) > count
        head = 25
        detail = 47 if s.mask else 0
        body = max(24, 22 * count + 5) + detail + 23 * pages
        h = head if s.mask_collapsed else head + body
        self.rect((x, top - h, w, h), self.bg, ('block',))
        self.rect((x, top - head, w, head), self.header, ('fold',))
        ax, ay = x + 11, top - head / 2
        arrow = ((ax - 2, ay - 4), (ax + 3, ay), (ax - 2, ay + 4)) if s.mask_collapsed else (
            (ax - 4, ay + 2), (ax + 4, ay + 2), (ax, ay - 3))
        self.shapes.append((arrow, self.fg))
        self.label(x + 22, ay, tr("SDF Maps"), limit=w - 80)
        if s.mask_collapsed:
            return
        self.control(x + w - 47, top - 22, 20, 18, "+", ('add_mask',))
        if s.mask:
            self.control(x + w - 24, top - 22, 20, 18, "−", ('remove_mask',))
        y = top - head - 3
        if not masks:
            self.label(x + 9, y - 10, tr("Add SDF Mask"), self.dim, limit=w - 18)
        for index in range(offset, offset + count):
            y -= 22
            self.rect((x + 5, y, w - 10, 21), self.blue if index == s.index else self.bg, ('select', index))
            self.label(x + 12, y + 10, masks[index].name, limit=w - 38)
            if index == s.index:
                self.control(x + w - 29, y + 1, 23, 19, "…", ('details',))
        if pages:
            y -= 23
            self.control(x + 5, y, 24, 19, "‹", ('page', -count))
            self.control(x + w - 29, y, 24, 19, "›", ('page', count))
            self.label(x + w / 2, y + 10, f"{offset + 1}–{offset + count} / {len(masks)}", self.dim, 'CENTER')
        if s.mask and s.mask.sources:
            src = s.mask.sources[0]
            extra = f"  +{len(s.mask.sources) - 1}" if len(s.mask.sources) > 1 else ""
            self.label(x + 10, top - h + 34, f"{src.object.name if src.object else '—'} / {src.uv_name}{extra}",
                       self.dim, limit=w - 18)
            button_w = (w - 14) / 2
            self.control(x + 5, top - h + 3, button_w, 19, tr("Edit UV Islands"), ('edit_islands',))
            self.control(x + 9 + button_w, top - h + 3, button_w, 19, tr("Rematch UV"), ('rematch_uv',))

    def timeline(self, s, left, right):
        tr = bpy.app.translations.pgettext_iface
        width = right - left
        self.rect((left, 6, width, 75), self.bg, ('block',))
        if width > 190:
            self.label(left + 10, 65, tr("Preview"))
        self.rect((right - 119, 55, 21, 20), (.65, .15, .14, 1) if s.auto_key else self.button, ('auto_key',))
        self.label(right - 108.5, 65, "●" if s.auto_key else "○", align='CENTER')
        if self.number_input is None:
            self.control(right - 94, 55, 62, 20, f"{s.value:.4f}", ('number',))
        else:
            entry = self.number_input
            self.rect((right - 94, 55, 62, 20), self.blue, ('number',))
            display = entry.text if entry.selected else entry.text[:entry.caret] + "│" + entry.text[entry.caret:]
            self.label(right - 63, 65, display, align='CENTER')
        keys = sorted(k.value for k in s.mask.keys)
        on_key = any(abs(t - s.value) < 1e-6 for t in keys)
        self.control(right - 28, 55, 20, 20, "−" if on_key else "+", ('key',))
        x0, x1, axis = left + 14, right - 14, 40
        self.track = x0, x1
        self.rect((x0 - 5, axis - 15, x1 - x0 + 10, 29), self.bg, ('scrub',))
        current = x0 + (x1 - x0) * s.value
        self.rect((x0, axis - 2, x1 - x0, 4), self.button)
        self.rect((x0, axis - 2, current - x0, 4), self.blue)
        for t in ((0, .25, .5, .75, 1) if width > 280 else (0, .5, 1)):
            x = x0 + (x1 - x0) * t
            self.rect((x - .5, axis - 4, 1, 8), self.dim)
            self.label(x, 15, f"{t:g}", self.dim, 'CENTER')
        for t in keys:
            x = x0 + (x1 - x0) * t
            self.shapes.append((((x - 5, 30), (x, 35), (x + 5, 30), (x, 25)), (1, .67, .20, 1)))
        # The light handle is the current preview. Gold diamonds stay at their saved values.
        self.rect((current - 3, axis - 4, 6, 13), self.fg)
        self.shapes.append((((current - 3, axis - 4), (current + 3, axis - 4), (current, axis - 7)), self.fg))

    def at(self, x, y):
        x, y = x / self.scale, y / self.scale
        for (left, bottom, w, h), action in reversed(self.targets):
            if left <= x <= left + w and bottom <= y <= bottom + h:
                return action
        return None

    def value_at(self, x):
        start, end = self.track
        return min(1., max(0., (x / self.scale - start) / (end - start)))

    def draw(self):
        import blf
        import gpu
        from gpu_extras.batch import batch_for_shader
        points, colors, triangles = [], [], []
        for shape, color in self.shapes:
            i = len(points)
            points.extend((x * self.scale, y * self.scale) for x, y in shape)
            colors.extend([color] * len(shape))
            triangles.extend((i, i + j, i + j + 1) for j in range(1, len(shape) - 1))
        if not points:
            return
        blend, depth, write = gpu.state.blend_get(), gpu.state.depth_test_get(), gpu.state.depth_mask_get()
        try:
            gpu.state.blend_set('ALPHA')
            gpu.state.depth_test_set('NONE')
            gpu.state.depth_mask_set(False)
            shader = gpu.shader.from_builtin('SMOOTH_COLOR')
            batch_for_shader(shader, 'TRIS', {'pos': points, 'color': colors}, indices=triangles).draw(shader)
            blf.size(0, 12 * self.scale)
            for x, y, label, color, align, limit in self.labels:
                if limit is not None and text_width(label, self.scale) > limit:
                    while label and text_width(label + "…", self.scale) > limit:
                        label = label[:-1]
                    label += "…"
                if align == 'CENTER':
                    x -= text_width(label, self.scale) / 2
                blf.color(0, *color)
                blf.position(0, x * self.scale, (y - 4) * self.scale, 0)
                blf.draw(0, label)
        finally:
            gpu.state.blend_set(blend)
            gpu.state.depth_test_set(depth)
            gpu.state.depth_mask_set(write)


class PreviewInput:
    def __init__(self, value):
        self.text = f"{value:.4f}"
        self.caret = len(self.text)
        self.selected = True

    def edit(self, event, clipboard):
        if event.ctrl and event.type == 'A':
            self.selected = True
            return
        if event.type in {'HOME', 'END', 'LEFT_ARROW', 'RIGHT_ARROW'}:
            if event.type == 'HOME' or (self.selected and event.type == 'LEFT_ARROW'):
                self.caret = 0
            elif event.type == 'END' or self.selected:
                self.caret = len(self.text)
            else:
                self.caret = max(0, min(len(self.text), self.caret + (1 if event.type == 'RIGHT_ARROW' else -1)))
            self.selected = False
            return
        start, end = (0, len(self.text)) if self.selected else (self.caret, self.caret)
        if event.type in {'BACK_SPACE', 'DEL'}:
            insert = ""
            if not self.selected:
                if event.type == 'BACK_SPACE':
                    start = max(0, start - 1)
                else:
                    end = min(len(self.text), end + 1)
        else:
            insert = (clipboard() if event.ctrl and event.type == 'V' else event.ascii).strip().replace(",", ".")
            if not insert or any(c not in "0123456789." for c in insert):
                return
        candidate = self.text[:start] + insert + self.text[end:]
        if candidate.count(".") <= 1 and len(candidate) <= 10:
            self.text = candidate
            self.caret = start + len(insert)
            self.selected = False


class SDF_OT_mask_details(Operator):
    bl_idname = "sdf.mask_details"
    bl_label = "SDF Mask"
    bl_options = {'UNDO'}
    name: StringProperty(name="Mask")

    @classmethod
    def poll(cls, context):
        return bool(context.mode == 'PAINT_SDF' and context.scene.sdf_editor.mask)

    def invoke(self, context, event):
        self.name = context.scene.sdf_editor.mask.name
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        from .space_sdf import draw_sources
        self.layout.prop(self, "name")
        draw_sources(self.layout, context, context.scene.sdf_editor.mask)

    def execute(self, context):
        mask = context.scene.sdf_editor.mask
        if not mask:
            return {'CANCELLED'}
        mask.name = self.name.strip() or mask.name
        return {'FINISHED'}


class SDF_OT_canvas_control(Operator):
    bl_idname = "sdf.canvas_control"
    bl_label = "SDF Canvas Controls"
    bl_options = {'INTERNAL', 'BLOCKING'}

    @classmethod
    def poll(cls, context):
        return bool(context.area and context.area.ui_type ==
                    'SDF' and context.region and context.region.type == 'WINDOW' and context.mode == 'PAINT_SDF')

    def invoke(self, context, event):
        result = self.press(context, event)
        if result == {'RUNNING_MODAL'}:
            context.window_manager.modal_handler_add(self)
        return result

    def press(self, context, event):
        from .space_sdf import active_picker, key_at
        if active_picker(context):
            return {'PASS_THROUGH'}
        ui = CanvasControls(context)
        action = ui.at(event.mouse_region_x, event.mouse_region_y)
        if not action:
            return {'PASS_THROUGH'}
        s = context.scene.sdf_editor
        kind = action[0]
        if kind == 'scrub':
            self.original = s.value
            self.press_x = event.mouse_region_x
            self.track = (*ui.track, ui.scale)
            value = ui.value_at(event.mouse_region_x)
            if event.mouse_region_y / ui.scale < 36:
                keys = [k.value for k in s.mask.keys]
                nearest = min(keys, key=lambda t: abs(t - value), default=None)
                if nearest is not None and abs(nearest - value) * (ui.track[1] - ui.track[0]) <= 6:
                    value = nearest
            s.value = value
            return {'RUNNING_MODAL'}
        if kind == 'fold':
            s.mask_collapsed = not s.mask_collapsed
        elif kind == 'select':
            s.index = action[1]
        elif kind == 'page':
            s.mask_offset = max(0, min(len(s.masks) - 1, s.mask_offset + action[1]))
        elif kind == 'auto_key':
            bpy.ops.sdf.toggle_auto_key()
        elif kind == 'key':
            bpy.ops.sdf.key(remove=bool(key_at(s.mask, s.value)))
        elif kind == 'number':
            self.number_area = context.area.as_pointer()
            _number_inputs[self.number_area] = PreviewInput(s.value)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        elif kind == 'edit_islands':
            bpy.ops.sdf.edit_islands()
        elif kind == 'rematch_uv':
            bpy.ops.sdf.rematch_uv('INVOKE_DEFAULT')
        elif kind == 'details':
            bpy.ops.sdf.mask_details('INVOKE_DEFAULT')
        elif kind == 'add_mask':
            bpy.ops.sdf.add_mask('INVOKE_DEFAULT')
        elif kind == 'remove_mask':
            bpy.ops.sdf.remove_mask('INVOKE_DEFAULT')
        return {'FINISHED'}

    def cancel(self, context):
        _number_inputs.pop(getattr(self, "number_area", None), None)
        if context.area:
            context.area.tag_redraw()

    def modal(self, context, event):
        s = context.scene.sdf_editor
        area = getattr(self, "number_area", None)
        if area is not None:
            entry = _number_inputs.get(area)
            if not entry or not self.poll(context) or event.type in {'ESC', 'RIGHTMOUSE', 'WINDOW_DEACTIVATE'}:
                self.cancel(context)
                return {'CANCELLED'}
            # The release belonging to the opening click is not a confirmation.
            if event.value != 'PRESS':
                return {'RUNNING_MODAL'}
            if event.type in {'RET', 'NUMPAD_ENTER', 'LEFTMOUSE'}:
                if event.type == 'LEFTMOUSE' and CanvasControls(context).at(
                        event.mouse_region_x, event.mouse_region_y) == ('number',):
                    return {'RUNNING_MODAL'}
                try:
                    value = float(entry.text)
                except ValueError:
                    return {'RUNNING_MODAL'}
                s.value = min(1.0, max(0.0, value))
                self.cancel(context)
                return {'FINISHED'}
            entry.edit(event, lambda: context.window_manager.clipboard)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type in {'ESC', 'RIGHTMOUSE', 'WINDOW_DEACTIVATE'}:
            s.value = self.original
            return {'CANCELLED'}
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'} and event.mouse_region_x != self.press_x:
            self.press_x = None
            start, end, scale = self.track
            s.value = min(1., max(0., (event.mouse_region_x / scale - start) / (end - start)))
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            return {'FINISHED'}
        return {'RUNNING_MODAL'}
