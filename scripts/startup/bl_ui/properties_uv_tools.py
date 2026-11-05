# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

from bpy.types import Panel


class UVToolsPanel:
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "uv_tools"


def draw_icon_buttons(layout, items):
    from bl_operators.uv_tools import icon_id

    row = layout.row(align=True)
    row.alignment = 'LEFT'
    for operator, icon in items:
        button = row.column(align=True)
        button.ui_units_x = 3.0
        button.scale_y = 3.0
        button.operator_context = 'INVOKE_DEFAULT'
        props = button.operator(operator, text="", icon_value=icon_id(icon), icon_scale=2.5)
        if operator == "uv.pack_islands":
            # Start without rotation; the native popup exposes all packing options.
            props.rotate = False


class UVTOOLS_PT_seam(UVToolsPanel, Panel):
    bl_label = "Seam"

    def draw(self, context):
        draw_icon_buttons(self.layout, (("uv_tools.mark_seam", "cut"), ("uv_tools.stitch", "stitch")))


class UVTOOLS_PT_unwrap(UVToolsPanel, Panel):
    bl_label = "Unwrap"

    def draw(self, context):
        draw_icon_buttons(self.layout, (("uv.unwrap", "unwrap"),))


class UVTOOLS_PT_distortion(UVToolsPanel, Panel):
    bl_label = "Distortion"

    def draw(self, context):
        self.layout.prop(context.scene, "uv_tools_distortion", text="Show UV / Model Distortion")
        self.layout.label(text="Red: compressed | Blue: stretched")
        self.layout.label(text="1 = normalized area; surface-weighted bins")


class UVTOOLS_PT_packing(UVToolsPanel, Panel):
    bl_label = "Packing"

    def draw(self, context):
        draw_icon_buttons(self.layout, (("uv.pack_islands", "pack"),))


classes = (
    UVTOOLS_PT_seam,
    UVTOOLS_PT_unwrap,
    UVTOOLS_PT_packing,
    UVTOOLS_PT_distortion,
)
