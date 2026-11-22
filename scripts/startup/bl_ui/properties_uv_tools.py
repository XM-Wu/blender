# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

from bpy.types import Panel
import bpy


def draw_search_controls(layout):
    if bpy.ops.uv.texel_pack_control.poll():
        box = layout.box()
        box.label(text='Packing Progress', icon='TIME')
        status = bpy.context.scene.get('uv_texel_pack_progress', 'Preparing')
        for line in status.split('; '):
            box.label(text=line)
        box.label(text='Enter: Stop & Apply Best', icon='CHECKMARK')
        box.label(text='Esc: Cancel / Restore UVs', icon='CANCEL')


class UVToolsPanel:
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "uv_tools"


def draw_icon_buttons(layout, items, setup=None):
    from bl_operators.uv_tools import icon_id

    row = layout.row(align=True)
    row.alignment = 'LEFT'
    for operator, icon in items:
        button = row.column(align=True)
        button.ui_units_x = 3.0
        button.scale_y = 3.0
        button.operator_context = 'INVOKE_DEFAULT'
        props = button.operator(operator, text="", icon_value=icon_id(icon), icon_scale=2.5)
        if setup is not None:
            setup(props)


class UVTOOLS_PT_seam(UVToolsPanel, Panel):
    bl_label = "Seam"

    def draw(self, context):
        draw_icon_buttons(self.layout, (("uv_tools.mark_seam", "cut"), ("uv_tools.stitch", "stitch")))


class UVTOOLS_PT_unwrap(UVToolsPanel, Panel):
    bl_label = "Unwrap"

    def draw(self, context):
        draw_icon_buttons(self.layout, (("uv.unwrap", "unwrap"),))


class UVTOOLS_PT_texel_pack(UVToolsPanel, Panel):
    bl_label = "Texel Packing"

    def draw(self, context):
        from bl_operators.uv_tools import TEXEL_PACK_SETTINGS

        settings = context.scene.uv_tools_texel_pack

        def setup(props):
            for key in TEXEL_PACK_SETTINGS:
                setattr(props, key, getattr(settings, key))

        layout = self.layout
        draw_icon_buttons(layout, (("uv.texel_pack", "texel_pack"),), setup)
        row = layout.row()
        row.operator_context = 'INVOKE_DEFAULT'
        row.scale_y = 1.5
        props = row.operator('uv.pack_to_others', text='Pack to Others', icon='UV')
        setup(props)
        draw_search_controls(layout)
        layout.label(text="Island constraints: UV Editor sidebar > Texel Pack", icon='INFO')
        report = context.scene.get("uv_texel_pack_report")
        if report:
            col = layout.column(align=True)
            col.scale_y = 0.8
            for part in report.replace("; ", ", ").split(", "):
                col.label(text=part)


class UVToolsTexelPackSubPanel(UVToolsPanel):
    bl_parent_id = "UVTOOLS_PT_texel_pack"

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        self.draw_settings(layout, context.scene.uv_tools_texel_pack)


class UVTOOLS_PT_texel_pack_density(UVToolsTexelPackSubPanel, Panel):
    bl_label = "Texture & Density"

    def draw_settings(self, layout, settings):
        layout.prop(settings, "resolution_preset")
        col = layout.column(align=True)
        col.enabled = settings.resolution_preset == 'CUSTOM'
        col.prop(settings, "resolution_x")
        col.prop(settings, "resolution_y")
        layout.prop(settings, "precision")
        layout.separator()
        layout.prop(settings, "density_mode")
        if settings.density_mode == 'TARGET':
            layout.prop(settings, "target_density", text="Pixels per Meter")
        layout.prop(settings, "density_tolerance", text="Tolerance", slider=True)
        layout.prop(settings, "use_density_weights")
        if settings.density_mode == 'UNIFORM':
            layout.prop(settings, "match_locked_density")


class UVTOOLS_PT_texel_pack_margin(UVToolsTexelPackSubPanel, Panel):
    bl_label = "Margin"

    def draw_settings(self, layout, settings):
        layout.prop(settings, "margin")
        layout.prop(settings, "edge_padding")
        layout.prop(settings, "pixel_snap")
        mips = max(0, int(settings.margin).bit_length() - 1) if settings.margin >= 1 else 0
        layout.label(text="Safe up to mip level {:d}".format(mips), icon='INFO')


class UVTOOLS_PT_texel_pack_search(UVToolsTexelPackSubPanel, Panel):
    bl_label = 'Heuristic Search'

    def draw_settings(self, layout, settings):
        layout.prop(settings, 'heuristic_search')
        col = layout.column()
        col.enabled = settings.heuristic_search
        col.prop(settings, 'search_time')
        col.prop(settings, 'no_improvement_time')
        col.label(text='0 disables a time limit', icon='INFO')
        col.label(text='Enter applies best; Esc cancels')
        draw_search_controls(layout)


class UVTOOLS_PT_texel_pack_rotation(UVToolsTexelPackSubPanel, Panel):
    bl_label = "Rotation & Stacking"

    def draw_settings(self, layout, settings):
        layout.prop(settings, "rotation")
        layout.prop(settings, "allow_flip")
        layout.separator()
        layout.prop(settings, "use_stacking")
        col = layout.column()
        col.active = settings.use_stacking
        col.prop(settings, "stack_mirrored")
        col.prop(settings, "stack_tolerance", text="Tolerance")
        layout.prop(settings, "stack_offset")


class UVTOOLS_PT_texel_pack_target(UVToolsTexelPackSubPanel, Panel):
    bl_label = "Target & Groups"

    def draw_settings(self, layout, settings):
        layout.prop(settings, "target")
        if settings.target == 'FIXED':
            layout.prop(settings, "udim_tile")
        layout.prop(settings, "max_tiles")
        layout.prop(settings, "group_layout")
        layout.separator()
        layout.prop(settings, "only_selected")
        col = layout.column()
        col.active = settings.only_selected
        col.prop(settings, "keep_unselected")


class TexelPackSidebarPanel:
    """Texel Pack tab of the UV editor sidebar."""
    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Texel Pack"

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH' and context.space_data.show_uvedit


class IMAGE_PT_texel_pack_transform(TexelPackSidebarPanel, Panel):
    bl_label = 'Transform'
    bl_order = -10

    @classmethod
    def poll(cls, context):
        return context.space_data.mode == 'UV'

    def draw(self, context):
        layout = self.layout
        layout.enabled = context.mode == 'EDIT_MESH'
        draw_search_controls(layout)
        from bl_operators.uv_tools import icon_id
        def buttons(parent, items):
            row = parent.row(align=True)
            row.scale_y = 2.0
            row.scale_x = 1.3
            for mode, icon in items:
                row.operator('uv_tools.transform', text='', icon_value=icon_id(mode.lower()), icon_scale=1.5).mode = mode
        orientation = layout.box()
        orientation.label(text='Orientation')
        settings = context.scene.uv_tools_orientation
        orientation.prop(settings, 'reference', text='')
        if settings.reference == 'GEOMETRY':
            orientation.prop(settings, 'space')
            orientation.prop(settings, 'axis')
            orientation.prop(settings, 'fallback_axis')
            orientation.row(align=True).prop(settings, 'target', expand=True)
            row = orientation.row()
            row.scale_y = 1.8
            row.operator('uv_tools.transform', text='Align to 3D', icon_value=icon_id('orient_v')).mode = 'ORIENT_3D'
        else:
            if settings.reference == 'EDGES':
                orientation.label(text='Selected edges orient the whole island')
            buttons(orientation, (('ORIENT_H', ''), ('ORIENT_V', '')))
        rotations = orientation.row(align=True)
        rotations.scale_y = 1.8
        for mode, label in (('ROTATE_CW', '90°'), ('ROTATE_CCW', '90°'), ('ROTATE_180', '180°')):
            rotations.operator('uv_tools.transform', text=label, icon_value=icon_id(mode.lower()), icon_scale=1.6).mode = mode
        flips = orientation.row(align=True)
        for mode, label in (('FLIP_U', 'Flip U'), ('FLIP_V', 'Flip V')):
            flips.operator('uv_tools.transform', text=label).mode = mode
        mirror = layout.box()
        mirror.label(text="UV Mirror")
        mirror.prop(settings, 'mirror_axis', expand=True)
        mirror.label(text="Select both sides, then choose the target")
        row = mirror.row(align=True)
        for uv_axis, label_axis in (('X', 'U'), ('Y', 'V')):
            for sign, label in (('POS', '+'), ('NEG', '−')):
                op = row.operator('uv.copy_mirrored_faces', text=label + label_axis, translate=False)
                op.mesh_axis = sign + '_' + settings.mirror_axis
                op.uv_axis = uv_axis
        box = layout.box()
        box.label(text='Straighten')
        buttons(box, (('STRAIGHT', 'IPO_LINEAR'), ('STRAIGHT_H', 'ALIGN_JUSTIFY'), ('STRAIGHT_V', 'ALIGN_FLUSH')))
        buttons(box, (('RECT_H', 'MESH_PLANE'), ('RECT_V', 'UV_FACESEL')))


class IMAGE_PT_texel_pack_island(TexelPackSidebarPanel, Panel):
    bl_label = "Island Constraints"

    def draw(self, context):
        from bl_operators.uv_tools_texel_pack import island_state, selected_value

        layout = self.layout
        state = island_state(context)
        if state['selected_faces'] == 0:
            layout.label(text="Select UV islands to edit", icon='INFO')
            return
        layout.use_property_split = True
        layout.use_property_decorate = False
        island = context.scene.uv_tools_island
        for prop, kind in (
                ("rotation", 'ROTATION'),
                ("flip", 'FLIP'),
                ("keep_scale", 'SCALE'),
                ("density", 'DENSITY'),
                ("lock", 'LOCK'),
        ):
            row = layout.row(align=True)
            row.prop(island, prop)
            if selected_value(kind, 0)[1]:
                row.label(text="", icon='DECORATE_ANIMATE')
        if any(selected_value(kind, 0)[1] for kind in ('ROTATION', 'FLIP', 'SCALE', 'DENSITY', 'LOCK')):
            layout.label(text="◆ differs, editing sets all", icon='INFO')
        layout.operator("uv.texel_pack_constraint_clear", text="Reset Constraints", icon='LOOP_BACK').kind = 'ALL'


class IMAGE_PT_texel_pack_fit_align(TexelPackSidebarPanel, Panel):
    bl_label = 'Fit & Align'
    bl_order = -9

    @classmethod
    def poll(cls, context):
        return context.space_data.mode == 'UV'

    def draw(self, context):
        from bl_operators.uv_tools import icon_id
        layout = self.layout
        settings = context.scene.uv_tools_fit_align
        layout.label(text='Match to')
        for targets in (('SELECTION','PIXEL','GRID'), ('TILE','TRIM','BOX')):
            row = layout.row(align=True)
            for target in targets:
                row.prop_enum(settings, 'target', target)
        snap = layout.row()
        snap.enabled = settings.target in {'PIXEL', 'GRID'}
        snap.prop(settings, 'snapping')
        layout.prop(settings, 'grouping')
        if settings.target == 'GRID':
            row = layout.row(align=True)
            row.prop(settings, 'grid_u')
            row.prop(settings, 'grid_v')
        elif settings.target == 'PIXEL':
            image = context.space_data.image
            if image and all(image.size):
                layout.label(text='Image: {} × {}'.format(*image.size))
            else:
                row = layout.row(align=True)
                row.prop(settings, 'resolution_x')
                row.prop(settings, 'resolution_y')
        elif settings.target == 'TILE':
            layout.prop(settings, 'tile')
        elif settings.target == 'BOX':
            layout.prop(settings, 'box_min')
            layout.prop(settings, 'box_max')
            row = layout.row()
            row.enabled = context.mode == 'EDIT_MESH'
            row.operator('uv_tools.fit_target', text='From Selection', icon='SELECT_SET').operation = 'BOX'
        elif settings.target == 'TRIM':
            layout.template_list('UI_UL_list', 'uv_fit_trims', settings, 'trims', settings, 'trim_index', rows=2)
            row = layout.row(align=True)
            add = row.row()
            add.enabled = context.mode == 'EDIT_MESH'
            add.operator('uv_tools.fit_target', text='From Selection', icon='ADD').operation = 'ADD_TRIM'
            row.operator('uv_tools.fit_target', text='', icon='REMOVE').operation = 'REMOVE_TRIM'
            if settings.trim_index < len(settings.trims):
                trim = settings.trims[settings.trim_index]
                layout.prop(trim, 'name')
                layout.prop(trim, 'minimum')
                layout.prop(trim, 'maximum')
        controls = layout.column()
        controls.enabled = context.mode == 'EDIT_MESH'
        for proportional in (False, True):
            group = controls.box()
            group.label(text='Proportional Fit' if proportional else 'Fit')
            buttons = group.row(align=True)
            buttons.enabled = settings.grouping != 'VERTEX'
            buttons.scale_y = 1.8
            buttons.scale_x = 1.7
            for axis in ('U','BOTH','V'):
                props = buttons.operator('uv_tools.fit_align', text='', icon_value=icon_id(('pfit_' if proportional else 'fit_') + axis.lower()), icon_scale=1.4)
                props.action, props.axes, props.proportional = 'FIT', axis, proportional
        align = controls.box()
        align.label(text='Align')
        def align_button(parent, anchor, axes='BOTH'):
            props = parent.operator('uv_tools.fit_align', text='', icon_value=icon_id('align_' + (anchor.lower() if axes == 'BOTH' else axes.lower())), icon_scale=2.0)
            props.action, props.anchor, props.axes = 'ALIGN', anchor, axes
        for index, anchors in enumerate((('TL','T','TR'),('L','C','R'),('BL','B','BR'))):
            row = align.row(align=True)
            row.alignment = 'LEFT'
            row.scale_y = 2.0
            for column in range(5):
                cell = row.column(align=True)
                cell.ui_units_x = 2.0
                if 1 <= column <= 3:
                    align_button(cell, anchors[column-1])
                elif index == 1:
                    align_button(cell, 'C', 'U' if column == 0 else 'V')
                else:
                    cell.label(text='')


class TexelPackIdListPanel(TexelPackSidebarPanel):
    """List of the group or stack ids in use, with selection and assignment."""
    kind = 'GROUP'
    show_prop = "show_groups"
    noun = "Group"

    def draw_header(self, context):
        self.layout.prop(context.scene.uv_tools_texel_pack, self.show_prop, text="", icon='OVERLAY')

    @classmethod
    def poll(cls, context):
        return context.space_data.mode == 'UV'

    def draw(self, context):
        from bl_operators.uv_tools_texel_pack import id_icon, group_entries, group_name, group_hidden
        layout = self.layout
        entries = group_entries(context, kind=self.kind)
        editing = context.mode == 'EDIT_MESH'
        layout.label(text='Editing Meshes' if editing else 'All Scene ' + self.noun + 's')
        for index, entry in sorted(entries.items()):
            row = layout.box().row(align=True)
            row.label(text=group_name(context.scene, index, self.kind), icon=id_icon(index, self.kind))
            def action(operator, icon):
                props = row.operator(operator, text='', icon=icon)
                props.kind, props.group = self.kind, index
                if operator == 'uv_tools.group_membership':
                    # UI invocations otherwise inherit New/Remove from the last run.
                    props.operation = 'ASSIGN'
                    props.choose = False
            action('uv_tools.group_rename', 'GREASEPENCIL')
            row.label(text=str(entry['faces']))
            action('uv_tools.group_visibility', 'HIDE_ON' if group_hidden(context, index, self.kind) else 'HIDE_OFF')
            if editing:
                props = row.operator('uv.texel_pack_constraint_select', text='', icon='RESTRICT_SELECT_OFF')
                props.kind = self.kind
                setattr(props, self.kind.lower(), index)
            else:
                action('uv_tools.group_select_objects', 'RESTRICT_SELECT_OFF')
            action('uv_tools.group_membership', 'ADD')
            if self.kind == 'GROUP':
                material_button = row.row(align=True)
                material_button.operator_context = 'INVOKE_DEFAULT'
                material_button.operator('uv_tools.group_material', text='', icon='MATERIAL').group = index
            action('uv_tools.group_delete', 'TRASH')
        if not entries:
            layout.label(text='No ' + self.noun.lower() + 's')
        row = layout.row(align=True)
        props = row.operator('uv_tools.group_membership', text='New', icon='ADD')
        props.kind, props.operation = self.kind, 'NEW'
        props.choose, props.group = False, 0
        props = row.operator('uv_tools.group_membership', text='Assign…', icon='FORWARD')
        props.kind, props.choose = self.kind, True
        props.operation, props.group = 'ASSIGN', 0
        props = layout.operator('uv_tools.group_membership', text='Remove Selection from Group', icon='REMOVE')
        props.kind, props.operation = self.kind, 'REMOVE'
        props.choose, props.group = False, 0
        if self.kind == 'GROUP':
            material_button = layout.row()
            material_button.operator_context = 'INVOKE_DEFAULT'
            material_button.operator('uv_tools.group_material', text='Assign Material by Group', icon='MATERIAL').group = 0
        extra = layout.column()
        extra.enabled = editing
        self.draw_extra(extra)

    def draw_extra(self, layout):
        pass


class IMAGE_PT_texel_pack_groups(TexelPackIdListPanel, Panel):
    bl_label = "Groups"

    @classmethod
    def poll(cls, context):
        return context.space_data.mode == 'UV'


class IMAGE_PT_texel_pack_stacks(TexelPackIdListPanel, Panel):
    bl_label = "Stacks"
    kind = 'STACK'
    show_prop = "show_stacks"
    noun = "Stack"

    def draw_extra(self, layout):
        layout.operator('uv_tools.stack_detect', text='Detect and Stack Groups', icon='UV_SYNC_SELECT')
        props = layout.operator("uv.texel_pack_constraint_set", text="Never Stack Selection", icon='CANCEL')
        props.kind = 'STACK'
        props.stack = -1
        layout.label(text="Unassigned: stacked automatically", icon='INFO')


class IMAGE_PT_texel_pack_search(TexelPackSidebarPanel, Panel):
    bl_label = 'Heuristic Search'
    bl_order = 20

    def draw(self, context):
        UVTOOLS_PT_texel_pack_search.draw_settings(self, self.layout, context.scene.uv_tools_texel_pack)


classes = (
    UVTOOLS_PT_seam,
    UVTOOLS_PT_unwrap,
    UVTOOLS_PT_texel_pack,
    UVTOOLS_PT_texel_pack_density,
    UVTOOLS_PT_texel_pack_margin,
    UVTOOLS_PT_texel_pack_search,
    UVTOOLS_PT_texel_pack_rotation,
    UVTOOLS_PT_texel_pack_target,
    IMAGE_PT_texel_pack_transform,
    IMAGE_PT_texel_pack_fit_align,
    IMAGE_PT_texel_pack_island,
    IMAGE_PT_texel_pack_groups,
    IMAGE_PT_texel_pack_stacks,
    IMAGE_PT_texel_pack_search,
)
