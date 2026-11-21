# SPDX-License-Identifier: GPL-2.0-or-later
"""Native Selector editor for mGear polygon pickers."""
import json
import re
from functools import lru_cache
from pathlib import Path

import blf
import bpy
from bpy.props import EnumProperty, FloatVectorProperty, IntProperty, StringProperty
from bpy.types import Header, Menu, Operator
from bpy_extras.io_utils import ImportHelper, ExportHelper
from mathutils.geometry import intersect_line_line_2d

from . import selector_board as board


def is_rig(obj):
    return obj and obj.type == 'ARMATURE' and obj.get("rigging_role") != "GUIDE"


def character(context):
    obj = context.active_object
    return obj if is_rig(obj) and obj.mode in {'POSE', 'OBJECT'} else None


def refresh(context):
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type in {'SELECTOR', 'VIEW_3D', 'PROPERTIES', 'OUTLINER', 'DOPESHEET_EDITOR', 'GRAPH_EDITOR'}:
                area.tag_redraw()


def pose_mode(rig):
    if rig.mode != 'POSE':
        bpy.ops.object.mode_set(mode='POSE')


def select(rig, bones, mode):
    pose_mode(rig)
    bones = list(dict.fromkeys(bones))
    if mode == 'REPLACE':
        for p in rig.pose.bones:
            if p.select:
                p.select = False
    for p in bones:
        p.select = not p.select if mode == 'TOGGLE' else True
    active = next((p for p in reversed(bones) if p.select), None)
    if active is None:
        active = next((p for p in rig.pose.bones if p.select), None)
    rig.data.bones.active = active.bone if active else None


@lru_cache(maxsize=1)
def stock_actions():
    # Match the shipped script bodies exactly and dispatch known Blender equivalents.
    # A .pkr is data. Never evaluate Python received from a picker.
    actions = {}
    for item in json.loads(board.stock())["tabs"][0]["data"]["items"]:
        script = item.get("action_script")
        if not script:
            continue
        label = item["text"]
        if label in {"Select ALL", "Key ALL"}:
            actions[script] = (label,)
        elif "ikFkMatch_with_namespace" in script:
            name = re.search(r'"((?:arm|leg)_[LR]0_ik_ctl)"', script).group(1)
            actions[script] = ("match", name)
        elif "show_space_chage_list" in script:
            host, key, driven = re.findall(r'"([^"]+)"', script)[:3]
            actions[script] = ("space", host, key, driven)
    return actions


def space_property(rig, host, prop):
    bone = board.bindings(rig, [host]).get(host)
    if bone and prop in bone:
        return bone, prop
    if bone and "limb_control" in bone:
        ik = rig.pose.bones.get(bone["limb_control"])
        key = {"upvref": "pole_space", "ikref": "ik_space", "ikRotRef": "rotation_space"}.get(prop.split("_")[-1])
        if ik and key and key in ik:
            return ik, key
    return None, None


def action(context, rig, command):
    pose_mode(rig)
    kind, *params = command
    if kind == "match":
        name, = params
        target = board.bindings(rig, [name]).get(name)
        if not target or "blend_property" not in target:
            return False
        mode = 'FK' if rig[target["blend_property"]] >= .5 else 'IK'
        return bpy.ops.xbone.match_limb(control=target.name, mode=mode) == {'FINISHED'}
    if kind == "space":
        host, prop, driven = params
        bone, key = space_property(rig, host, prop)
        target = board.bindings(rig, [driven]).get(driven)
        if not bone or not target:
            return False
        refs = reference_names(bone, key)

        def menu(self, _context):
            for value, name in enumerate(refs):
                op = self.layout.operator("xbone.selector_space", text=name, depress=bone[key] == value)
                op.control, op.key, op.value = bone.name, key, value
                op.driven = target.name
        context.window_manager.popup_menu(menu, title=params[1])
        return True
    bones = [rig.pose.bones[n] for n in board.control_names(rig)]
    if kind == "Select ALL":
        select(rig, bones, 'REPLACE')
    elif kind == "Key ALL":
        for p in bones:
            rotation = {"QUATERNION": "rotation_quaternion", "AXIS_ANGLE": "rotation_axis_angle"}
            for path in ("location", rotation.get(p.rotation_mode, "rotation_euler"), "scale"):
                p.keyframe_insert(data_path=path, group=p.name)
    return True


def reference_names(bone, key):
    if key.endswith("_space"):
        return bone.get(key.removesuffix("_space") + "_references", ())
    description = bone.id_properties_ui(key).as_dict().get("description", "")
    return description.removeprefix("Reference space: ").split(", ")


class XBONE_OT_selector_space(Operator):
    bl_idname = "xbone.selector_space"
    bl_label = "Change Reference Space"
    bl_options = {'UNDO'}
    control: StringProperty()
    driven: StringProperty()
    key: StringProperty()
    value: IntProperty()

    def execute(self, context):
        rig = character(context)
        p = rig.pose.bones.get(self.control) if rig else None
        target = rig.pose.bones.get(self.driven) if rig else None
        if not p or not target or self.key not in p or not 0 <= self.value < len(reference_names(p, self.key)):
            return {'CANCELLED'}
        matrix = target.matrix.copy()
        p[self.key] = self.value
        rig.update_tag()
        context.view_layer.update()
        target.matrix = matrix
        if context.scene.tool_settings.use_keyframe_insert_auto:
            p.keyframe_insert(data_path='["' + bpy.utils.escape_identifier(self.key) + '"]')
            rotation = {"QUATERNION": "rotation_quaternion", "AXIS_ANGLE": "rotation_axis_angle"}
            for path in ("location", rotation.get(target.rotation_mode, "rotation_euler"), "scale"):
                target.keyframe_insert(data_path=path, group=target.name)
        rig.update_tag()
        refresh(context)
        return {'FINISHED'}


def page_and_view(context):
    rig = character(context)
    page = board.current_page(rig, context.space_data)
    return page, board.view(page, context.space_data, context.region, rig.get("xbone_selector_images"))


def hit(page, xy):
    return next((i for i in reversed(range(len(page.buttons))) if page.buttons[i].contains(*xy)), -1)


def overlaps(button, rect):
    x0, y0, x1, y1 = rect
    left, bottom, right, top = button.bounds
    if left > x1 or bottom > y1 or right < x0 or top < y0:
        return False
    if any(x0 <= x <= x1 and y0 <= y <= y1 for x, y in button.points):
        return True
    corners = [(x0, y0), (x0, y1), (x1, y1), (x1, y0)]
    if any(button.contains(*p) for p in corners):
        return True
    return any(intersect_line_line_2d(p, button.points[i - 1], q, corners[j - 1]) is not None
               for i, p in enumerate(button.points) for j, q in enumerate(corners))


_marquees = {}


def draw():
    import gpu
    from gpu_extras.batch import batch_for_shader

    context = bpy.context
    rig = character(context)
    if not rig:
        blf.size(0, 14)
        blf.position(0, 16, max(12, context.region.height - 32), 0)
        blf.color(0, .8, .8, .8, 1)
        blf.draw(0, "Choose a character in the header")
        return
    page, (scale, ox, oy) = page_and_view(context)
    resolved = board.bindings(rig, page.names)
    actions = stock_actions()
    vertices, colors, edges, edge_colors, labels = [], [], [], [], []
    for b in page.buttons:
        targets = [resolved[n] for n in b.controls if n in resolved]
        selected = targets and all(p.select for p in targets)
        unavailable = (b.controls and not targets) or (b.action and b.script not in actions)
        color = b.color if not unavailable else tuple(v * .45 for v in b.color[:3]) + (b.color[3],)
        points = [(x * scale + ox, y * scale + oy, 0) for x, y in b.points]
        vertices.extend(points[i] for i in b.triangles)
        colors.extend([color] * len(b.triangles))
        stroke = (1, 1, .7, 1) if selected else (.07, .07, .07, .85)
        for i, p in enumerate(points):
            edges.extend((points[i - 1], p))
            edge_colors.extend((stroke, stroke))
        if b.text:
            text = b.text
            command = actions.get(b.script)
            if command and command[0] == "space":
                owner, prop = space_property(rig, *command[1:3])
                if owner and owner[prop] > 0:
                    refs = reference_names(owner, prop)
                    if owner[prop] < len(refs):
                        text = refs[owner[prop]]
            labels.append((b, text))
    area_key = context.area.as_pointer()
    if area_key in _marquees:
        ax, ay, bx, by = _marquees[area_key]
        rect = ((ax, ay, 0), (bx, ay, 0), (bx, by, 0), (ax, by, 0))
        for i, p in enumerate(rect):
            edges.extend((rect[i - 1], p))
            edge_colors.extend(((1, 1, 1, 1),) * 2)
    shader = gpu.shader.from_builtin('SMOOTH_COLOR')
    old_blend = gpu.state.blend_get()
    gpu.state.blend_set('ALPHA')
    try:
        from gpu_extras.presets import draw_texture_2d
        for image, (left, bottom, right, top) in board.background_rects(page, rig.get("xbone_selector_images", {})):
            draw_texture_2d(gpu.texture.from_image(image), (left*scale+ox, bottom*scale+oy),
                            (right-left)*scale, (top-bottom)*scale, is_scene_linear_with_rec709_srgb_target=True)
        for typ, pos, col in (('TRIS', vertices, colors), ('LINES', edges, edge_colors)):
            if pos:
                batch_for_shader(shader, typ, {"pos": pos, "color": col}).draw(shader)
        for b, text in labels:
            blf.size(0, max(5, round(b.size * scale)))
            blf.color(0, *b.text_color)
            w, h = blf.dimensions(0, text)
            blf.position(0, b.center[0] * scale + ox - w / 2, b.center[1] * scale + oy - h / 2, 0)
            blf.draw(0, text)
    finally:
        gpu.state.blend_set(old_blend)


class SelectorOperator:
    @classmethod
    def poll(cls, context):
        return context.area and context.area.type == 'SELECTOR' and character(context) is not None


class XBONE_OT_selector_pick(SelectorOperator, Operator):
    bl_idname = "xbone.selector_pick"
    bl_label = "Pick Controls"
    bl_options = {'UNDO'}
    index: IntProperty(default=-1, options={'SKIP_SAVE'})
    mode: EnumProperty(items=[('REPLACE', "Replace", ""), ('ADD', "Add", ""), ('TOGGLE', "Toggle", "")],
                       options={'SKIP_SAVE'})

    def execute(self, context):
        rig = character(context)
        page = board.current_page(rig, context.space_data)
        if self.index < 0:
            select(rig, [], self.mode)
        elif self.index < len(page.buttons):
            item = page.buttons[self.index]
            if item.action:
                command = stock_actions().get(item.script)
                if not command or not action(context, rig, command):
                    self.report({'INFO'}, "This action has no Blender equivalent")
                    return {'CANCELLED'}
            elif item.controls:
                resolved = board.bindings(rig, item.controls)
                if not resolved:
                    self.report({'INFO'}, "No matching controls on this character")
                    return {'CANCELLED'}
                select(rig, resolved.values(), self.mode)
            else:
                return {'CANCELLED'}
        else:
            return {'CANCELLED'}
        refresh(context)
        return {'FINISHED'}

    def invoke(self, context, event):
        page, view = page_and_view(context)
        self.start = (event.mouse_region_x, event.mouse_region_y)
        self.mode = 'TOGGLE' if event.ctrl else 'ADD' if event.shift else 'REPLACE'
        self.index = hit(page, board.board_point(view, *self.start))
        if self.index >= 0:
            return self.execute(context)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        area = context.area.as_pointer()
        end = (event.mouse_region_x, event.mouse_region_y)
        if event.type == 'MOUSEMOVE':
            _marquees[area] = (*self.start, *end)
            context.area.tag_redraw()
        elif event.type in {'ESC', 'RIGHTMOUSE', 'LEFTMOUSE'}:
            _marquees.pop(area, None)
            context.area.tag_redraw()
            if event.type != 'LEFTMOUSE' or not character(context):
                return {'CANCELLED'}
            page, view = page_and_view(context)
            a, b = board.board_point(view, *self.start), board.board_point(view, *end)
            rect = min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])
            names = [n for item in page.buttons if not item.action and overlaps(item, rect) for n in item.controls]
            select(character(context), board.bindings(character(context), names).values(), self.mode)
            refresh(context)
            return {'FINISHED'}
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        if context.area:
            _marquees.pop(context.area.as_pointer(), None)
            context.area.tag_redraw()


class XBONE_OT_selector_view(SelectorOperator, Operator):
    bl_idname = "xbone.selector_view"
    bl_label = "Navigate Selector"
    operation: EnumProperty(items=[(x, x.title(), "") for x in ('FRAME', 'IN', 'OUT', 'PAN')])

    def execute(self, context):
        space = context.space_data
        space.zoom, space.pan = 1, (0, 0)
        context.area.tag_redraw()
        return {'FINISHED'}

    def invoke(self, context, event):
        space = context.space_data
        if self.operation == 'FRAME':
            return self.execute(context)
        _, view = page_and_view(context)
        if self.operation == 'PAN':
            self.start = event.mouse_x, event.mouse_y
            self.original = tuple(space.pan)
            self.scale = view[0]
            context.window_manager.modal_handler_add(self)
            return {'RUNNING_MODAL'}
        x, y = event.mouse_region_x, event.mouse_region_y
        before = board.board_point(view, x, y)
        space.zoom *= 1.2 if self.operation == 'IN' else 1 / 1.2
        after = board.board_point(page_and_view(context)[1], x, y)
        space.pan = [p + a - b for p, a, b in zip(space.pan, after, before)]
        context.area.tag_redraw()
        return {'FINISHED'}

    def modal(self, context, event):
        if event.type == 'ESC':
            context.space_data.pan = self.original
        elif event.type == 'MOUSEMOVE':
            context.space_data.pan = [p + (n - s) / self.scale for p, n, s in
                                      zip(self.original, (event.mouse_x, event.mouse_y), self.start)]
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        elif event.type != 'MIDDLEMOUSE' or event.value != 'RELEASE':
            return {'RUNNING_MODAL'}
        context.area.tag_redraw()
        return {'FINISHED'}


class XBONE_OT_selector_character(Operator):
    bl_idname = "xbone.selector_character"
    bl_label = "Choose Character"
    name: StringProperty()

    def execute(self, context):
        rig = context.view_layer.objects.get(self.name)
        if not is_rig(rig) or not rig.visible_get() or context.mode not in {'OBJECT', 'POSE'}:
            return {'CANCELLED'}
        if context.object != rig and context.mode == 'POSE':
            bpy.ops.object.mode_set(mode='OBJECT')
        for obj in context.selected_objects:
            obj.select_set(False)
        rig.select_set(True)
        context.view_layer.objects.active = rig
        pose_mode(rig)
        refresh(context)
        return {'FINISHED'}


class SELECTOR_MT_characters(Menu):
    bl_label = "Character"

    def draw(self, context):
        for rig in context.view_layer.objects:
            if is_rig(rig) and rig.visible_get():
                self.layout.operator("xbone.selector_character", text=rig.name).name = rig.name


class XBONE_OT_selector_page(SelectorOperator, Operator):
    bl_idname = "xbone.selector_page"
    bl_label = "Show Picker Page"
    index: IntProperty(min=0)

    def execute(self, context):
        space = context.space_data
        space.page = self.index
        space.zoom, space.pan = 1, (0, 0)
        context.area.tag_redraw()
        return {'FINISHED'}


class SELECTOR_MT_pages(Menu):
    bl_label = "Picker Pages"

    def draw(self, context):
        rig = character(context)
        for i, page in enumerate(board.pages(board.layout_source(rig))):
            self.layout.operator("xbone.selector_page", text=page.name).index = i


class XBONE_OT_selector_import(SelectorOperator, Operator, ImportHelper):
    bl_idname = "xbone.selector_import"
    bl_label = "Import mGear Picker"
    bl_options = {'UNDO'}
    filename_ext = ".pkr"
    filter_glob: StringProperty(default="*.pkr", options={'HIDDEN'})

    def execute(self, context):
        try:
            source = Path(self.filepath).read_text(encoding="utf-8-sig")
            pages = board.pages(source)
        except (OSError, ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
            self.report({'ERROR'}, f"Invalid polygon picker: {exc}")
            return {'CANCELLED'}
        rig = character(context)
        images, loaded, missing = {}, {}, []
        directory = Path(self.filepath).parent
        for page in pages:
            for slot, path, _, _ in page.backgrounds:
                filename = Path(path.replace(chr(92), "/"))
                candidates = (directory / filename.name, directory / filename)
                file = next((p for p in candidates if p.is_file()), None)
                if file:
                    try:
                        if file not in loaded:
                            image = bpy.data.images.load(str(file))
                            image.pack()
                            loaded[file] = image
                        images[slot] = loaded[file]
                        continue
                    except RuntimeError:
                        pass
                missing.append(filename.name)
        rig["xbone_selector_images"] = images
        rig[board.BOARD_KEY] = source
        if missing:
            self.report({'WARNING'}, "Missing picker backgrounds: " + ", ".join(missing))
        context.space_data.page = 0
        context.space_data.zoom, context.space_data.pan = 1, (0, 0)
        refresh(context)
        return {'FINISHED'}


class XBONE_OT_selector_export(SelectorOperator, Operator, ExportHelper):
    bl_idname = "xbone.selector_export"
    bl_label = "Export mGear Picker"
    filename_ext = ".pkr"
    filter_glob: StringProperty(default="*.pkr", options={'HIDDEN'})

    def execute(self, context):
        try:
            Path(self.filepath).write_text(board.layout_source(character(context)), encoding="utf8")
        except OSError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class XBONE_OT_selector_edit(SelectorOperator, Operator):
    bl_idname = "xbone.selector_edit"
    bl_label = "Edit Picker Button"
    bl_options = {'UNDO'}
    operation: EnumProperty(items=[(x, x.title(), "") for x in ('ADD', 'REBIND', 'REMOVE', 'MOVE')])
    index: IntProperty(default=-1)
    position: FloatVectorProperty(size=2)
    label: StringProperty(name="Label")

    def invoke(self, context, event):
        if self.operation == 'ADD':
            selected = [p.name for p in character(context).pose.bones if p.select]
            if not selected:
                self.report({'INFO'}, "Select pose controls to bind first")
                return {'CANCELLED'}
            self.label = selected[0] if len(selected) == 1 else "Group"
            return context.window_manager.invoke_props_dialog(self)
        if self.operation == 'MOVE':
            context.area.header_text_set("Click to place the button  •  Esc to cancel")
            context.window_manager.modal_handler_add(self)
            return {'RUNNING_MODAL'}
        return self.execute(context)

    def draw(self, context):
        self.layout.prop(self, "label")

    def modal(self, context, event):
        if event.type in {'ESC', 'RIGHTMOUSE'}:
            context.area.header_text_set(None)
            return {'CANCELLED'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            self.position = board.board_point(page_and_view(context)[1], event.mouse_region_x, event.mouse_region_y)
            context.area.header_text_set(None)
            return self.execute(context)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        rig = character(context)
        data = json.loads(board.layout_source(rig))
        tab = data["tabs"][min(context.space_data.page, len(data["tabs"]) - 1)]
        items = tab["data"]["items"]
        controls = [p.name for p in rig.pose.bones if p.select]
        if self.operation in {'ADD', 'REBIND'} and not controls:
            self.report({'INFO'}, "Select pose controls to bind first")
            return {'CANCELLED'}
        if self.operation == 'ADD':
            width = max(24, min(100, len(self.label) * 4))
            items.append(dict(text=self.label, text_size=12, controls=controls, position=list(self.position), handles=[
                         [-width, -12], [width, -12], [width, 12], [-width, 12]], color=[64, 136, 164, 255]))
        elif not 0 <= self.index < len(items):
            return {'CANCELLED'}
        elif self.operation == 'REMOVE':
            items.pop(self.index)
        elif self.operation == 'MOVE':
            items[self.index]["position"] = list(self.position)
        elif self.operation == 'REBIND':
            item = items[self.index]
            item["controls"] = controls
            item.pop("action_mode", None)
            item.pop("action_script", None)
        rig[board.BOARD_KEY] = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        refresh(context)
        return {'FINISHED'}


class XBONE_OT_selector_context(SelectorOperator, Operator):
    bl_idname = "xbone.selector_context"
    bl_label = "Picker Button Menu"

    def invoke(self, context, event):
        page, view = page_and_view(context)
        pos = board.board_point(view, event.mouse_region_x, event.mouse_region_y)
        index = hit(page, pos)

        def menu(self, _context):
            choices = [('ADD', "Add Button from Selected Controls")]
            if index >= 0:
                choices.extend([('REBIND', "Bind to Selected Controls"),
                               ('MOVE', "Move Button"), ('REMOVE', "Remove Button")])
            self.layout.operator_context = 'INVOKE_DEFAULT'
            for op, label in choices:
                props = self.layout.operator("xbone.selector_edit", text=label)
                props.operation, props.position, props.index = op, pos, index
        context.window_manager.popup_menu(menu, title="Selector")
        return {'FINISHED'}


class SELECTOR_MT_layout(Menu):
    bl_label = "Layout"

    def draw(self, context):
        self.layout.operator("xbone.selector_import", text="Import Picker (.pkr)")
        self.layout.operator("xbone.selector_export", text="Export Picker (.pkr)")
        self.layout.operator("xbone.selector_view", text="Frame All").operation = 'FRAME'


class SELECTOR_HT_header(Header):
    bl_space_type = 'SELECTOR'

    def draw(self, context):
        layout = self.layout
        layout.template_header()
        rig = character(context)
        layout.menu("SELECTOR_MT_characters", text=rig.name if rig else "Character", icon='ARMATURE_DATA')
        if rig:
            layout.menu("SELECTOR_MT_pages", text=board.current_page(rig, context.space_data).name)
            layout.menu("SELECTOR_MT_layout")


classes = (XBONE_OT_selector_space, XBONE_OT_selector_pick, XBONE_OT_selector_view,
           XBONE_OT_selector_character, SELECTOR_MT_characters, XBONE_OT_selector_page,
           SELECTOR_MT_pages, XBONE_OT_selector_import, XBONE_OT_selector_export, XBONE_OT_selector_edit,
           XBONE_OT_selector_context, SELECTOR_MT_layout, SELECTOR_HT_header)
_register, _unregister = bpy.utils.register_classes_factory(classes)
_handler = None


def register():
    global _handler
    _register()
    _handler = bpy.types.SpaceSelector.draw_handler_add(draw, (), 'WINDOW', 'POST_PIXEL')


def unregister():
    global _handler
    bpy.types.SpaceSelector.draw_handler_remove(_handler, 'WINDOW')
    _handler = None
    _unregister()
