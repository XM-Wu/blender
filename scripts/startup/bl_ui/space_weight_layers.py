# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

from bpy.types import Header, Menu, Panel, UIList
from bl_operators.skin_layers import children, current_layer, rig_modifier, subtree, used_channels


def draw_source(layout, obj):
    enabled = "_skin_source" in obj
    row = layout.row(align=True)
    row.operator("paint.skin_source", text="Native", depress=not enabled).layered = False
    row.operator("paint.skin_source", text="Skin Tools", depress=enabled).layered = True


class WEIGHT_LAYERS_HT_header(Header):
    bl_space_type = 'WEIGHT_LAYERS'

    def draw(self, context):
        self.layout.template_header()
        self.layout.label(text="Weight Layers")


class WEIGHT_LAYERS_UL_layers(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        parents = {layer.key: layer for layer in data.layers}
        parent = item.parent_key
        while parent in parents:
            row.separator(factor=1)
            parent = parents[parent].parent_key
        if item.is_group:
            row.prop(item, "expanded", text="", emboss=False, icon='TRIA_DOWN' if item.expanded else 'TRIA_RIGHT')
        row.prop(item, "enabled", text="", emboss=False, icon='HIDE_OFF' if item.enabled else 'HIDE_ON')
        row.prop(item, "name", text="", emboss=False, icon='FILE_FOLDER' if item.is_group else 'MOD_VERTEX_WEIGHT')

    def filter_items(self, context, data, propname):
        indices = {item.key: i for i, item in enumerate(data.layers)}
        order, flags = [0] * len(indices), [0] * len(indices)
        rank = 0

        def visit(parent, visible):
            nonlocal rank
            for item in reversed(children(data, parent)):
                i = indices[item.key]
                order[i] = rank
                flags[i] = self.bitflag_filter_item if visible else 0
                rank += 1
                visit(item.key, visible and item.expanded)
        visit("", True)
        return flags, order


class WEIGHT_LAYERS_UL_bones(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        obj = context.active_object
        group = obj.vertex_groups.get(item.name)
        used = used_channels(obj, current_layer(obj.skin_layers))
        icon = 'BONE_INFLUENCED' if group and group.skin_channel in used else 'BONE_DATA'
        layout.operator("paint.skin_influence", text=item.name, icon=icon, emboss=False).bone = item.name

    def filter_items(self, context, data, propname):
        obj = context.active_object
        state = obj.skin_layers
        search = state.search.casefold()
        used = used_channels(obj, current_layer(state)) if state.only_influenced else set()
        names = {g.name for g in obj.vertex_groups if g.skin_channel in used}
        return [self.bitflag_filter_item if bone.use_deform and search in bone.name.casefold()
                and (not state.only_influenced or bone.name in names) else 0
                for bone in data.bones], []


class VIEW3D_PT_weight_paint_rig(Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Weight Paint"
    bl_label = "Rig"

    @classmethod
    def poll(cls, context):
        return context.mode == 'PAINT_WEIGHT'

    def draw(self, context):
        layout = self.layout
        mod = rig_modifier(context.active_object)
        if not mod:
            layout.label(text="Requires one Armature modifier", icon='INFO')
            return
        layout.operator("paint.skin_front", depress=mod.object.show_in_front, icon='BONE_DATA')
        layout.operator("paint.skin_rest_pose", icon='LOOP_BACK')


class WEIGHT_LAYERS_MT_move(Menu):
    bl_label = "Move Layer to Group"

    def draw(self, context):
        state = context.active_object.skin_layers
        layer = current_layer(state)
        excluded = {node.key for node in subtree(state, layer)} | {layer.parent_key}
        if layer.parent_key:
            self.layout.operator("paint.skin_reparent", text="Root").parent_key = ""
        for item in state.layers:
            if item.is_group and item.key not in excluded:
                self.layout.operator("paint.skin_reparent", text=item.name, icon='FILE_FOLDER').parent_key = item.key


class WEIGHT_LAYERS_PT_layers(Panel):
    bl_space_type = 'WEIGHT_LAYERS'
    bl_region_type = 'WINDOW'
    bl_label = "Skin Layers"
    bl_options = {'HIDE_HEADER'}

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            layout.label(text="Select a mesh to paint.", icon='MESH_DATA')
            return
        layout.label(text=obj.name, icon='MESH_DATA')
        mod = rig_modifier(obj)
        state = obj.skin_layers
        if not mod:
            layout.label(text="Requires one Armature modifier", icon='INFO')
            if "_skin_source" in obj:
                layout.operator("paint.skin_source", text="Return to Native").layered = False
            return
        if not state.prefix:
            layout.operator("paint.skin_source", text="Create Layers from Weights", icon='ADD')
            return
        draw_source(layout, obj)
        tools = layout.column()
        tools.enabled = "_skin_source" in obj
        if context.mode == 'OBJECT':
            tools.operator("object.mode_set", text="Enter Weight Paint", icon='WPAINT_HLT').mode = 'WEIGHT_PAINT'
        row = tools.row()
        row.template_list("WEIGHT_LAYERS_UL_layers", "", state, "layers", state, "index", rows=5)
        ops = row.column(align=True)
        for action, icon in [('ADD', 'ADD'), ('DELETE', 'REMOVE'), ('DUPLICATE', 'DUPLICATE'),
                             ('UP', 'TRIA_UP'), ('DOWN', 'TRIA_DOWN')]:
            ops.operator("paint.skin_layer_edit", text="", icon=icon).action = action
        row = tools.row(align=True)
        row.operator("paint.skin_layer_edit", text="New Group", icon='FILE_FOLDER').action = 'GROUP'
        row.operator("paint.skin_layer_edit", text="Add Child").action = 'CHILD'
        row.operator("paint.skin_layer_edit", text="Merge Down").action = 'MERGE'
        if any(item.is_group for item in state.layers):
            row.menu("WEIGHT_LAYERS_MT_move", text="", icon='FILE_PARENT')
        layer = current_layer(state)
        if layer:
            tools.prop(layer, "opacity", slider=True)
        tools.separator()
        if layer.is_group:
            tools.label(text="Group Mask", icon='MOD_MASK')
        else:
            tools.row(align=True).prop(state, "target", expand=True)
        row = tools.row(align=True)
        row.prop(state, "search", text="", icon='VIEWZOOM')
        row.prop(state, "only_influenced", text="Influenced Only", toggle=True)
        tools.template_list("WEIGHT_LAYERS_UL_bones", "", mod.object.data, "bones", state, "bone_index", rows=5)
        tools.separator()
        tools.prop(state, "display", text="Display")
        tools.operator("paint.skin_flood", text="Flood Selection", icon='BRUSH_DATA')
        tools.operator("paint.skin_pick", icon='EYEDROPPER')
        tools.operator("paint.skin_mirror", text="Mirror", icon='MOD_MIRROR')
        if not layer.is_group:
            tools.operator_menu_enum("paint.skin_closest", "scope", text="Assign from Closest Joint")
        tools.operator("paint.skin_bake", icon='GROUP_VERTEX')


classes = (WEIGHT_LAYERS_HT_header, WEIGHT_LAYERS_UL_layers, WEIGHT_LAYERS_UL_bones, WEIGHT_LAYERS_MT_move, VIEW3D_PT_weight_paint_rig, WEIGHT_LAYERS_PT_layers)
