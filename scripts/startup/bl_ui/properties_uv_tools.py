# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

from bpy.types import Panel


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


class TexelPackIdListPanel(TexelPackSidebarPanel):
    """List of the group or stack ids in use, with selection and assignment."""
    kind = 'GROUP'
    show_prop = "show_groups"
    noun = "Group"

    def draw_header(self, context):
        self.layout.prop(context.scene.uv_tools_texel_pack, self.show_prop, text="", icon='OVERLAY')

    def draw(self, context):
        from bl_operators.uv_tools_texel_pack import id_icon, island_state

        layout = self.layout
        entries = island_state(context)['lists'][self.kind]
        value_prop = self.kind.lower()
        col = layout.column(align=True)
        if not entries:
            col.label(text="No {:s}s".format(self.noun.lower()))
        for index, entry in sorted(entries.items()):
            row = col.box().row(align=True)
            row.label(text="{:s} {:d}".format(self.noun, index), icon=id_icon(index, self.kind))
            sub = row.row(align=True)
            sub.alignment = 'RIGHT'
            sub.label(text="{:d} faces{:s}".format(entry['faces'], "  •" if entry['selected'] else ""))
            props = row.operator("uv.texel_pack_constraint_select", text="", icon='RESTRICT_SELECT_OFF')
            props.kind = self.kind
            setattr(props, value_prop, index)
            props = row.operator("uv.texel_pack_constraint_set", text="", icon='ADD')
            props.kind = self.kind
            setattr(props, value_prop, index)
        row = layout.row(align=True)
        row.operator("uv_tools.texel_pack_new_id", text="New", icon='ADD').kind = self.kind
        row.operator("uv.texel_pack_constraint_clear", text="Remove", icon='REMOVE').kind = self.kind
        self.draw_extra(layout)

    def draw_extra(self, layout):
        pass


class IMAGE_PT_texel_pack_groups(TexelPackIdListPanel, Panel):
    bl_label = "Groups"


class IMAGE_PT_texel_pack_stacks(TexelPackIdListPanel, Panel):
    bl_label = "Stacks"
    kind = 'STACK'
    show_prop = "show_stacks"
    noun = "Stack"

    def draw_extra(self, layout):
        props = layout.operator("uv.texel_pack_constraint_set", text="Never Stack Selection", icon='CANCEL')
        props.kind = 'STACK'
        props.stack = -1
        layout.label(text="Unassigned: stacked automatically", icon='INFO')


classes = (
    UVTOOLS_PT_seam,
    UVTOOLS_PT_unwrap,
    UVTOOLS_PT_texel_pack,
    UVTOOLS_PT_texel_pack_density,
    UVTOOLS_PT_texel_pack_margin,
    UVTOOLS_PT_texel_pack_rotation,
    UVTOOLS_PT_texel_pack_target,
    IMAGE_PT_texel_pack_island,
    IMAGE_PT_texel_pack_groups,
    IMAGE_PT_texel_pack_stacks,
)
