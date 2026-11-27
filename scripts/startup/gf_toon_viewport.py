# SPDX-License-Identifier: GPL-2.0-or-later
"""GF Toon viewport lighting and Workbench base-color display."""
import json

import bpy
from bpy.app.handlers import persistent


_owner = object()
_legacy_asset_name = "卡通悬案 · GF Toon (EEVEE)"
_studio = "gf_toon.exr"
_preview_defaults = {
    "use_scene_world": False,
    "use_scene_lights": True,
    "studio_light": _studio,
    "studiolight_rotate_z": 0.0,
    "studiolight_intensity": 0.0,
    "studiolight_background_alpha": 0.0,
    "use_studiolight_view_rotation": False,
    "render_pass": "COMBINED",
    "use_compositor": "DISABLED",
}


def toon_active(shading):
    return shading.type == 'MATERIAL' and bool(shading.gf_previous_preview)


def restore_preview(shading, target):
    previous = json.loads(shading.gf_previous_preview)
    shading.gf_previous_preview = ""
    # Studio-light RNA accesses different storage in solid and material modes.
    shading.type = 'MATERIAL'
    for key, value in previous.items():
        if key == "studio_light" and bpy.context.preferences.studio_lights.get(value) is None:
            continue
        setattr(shading, key, value)
    shading.type = target


class VIEW3D_OT_gf_toon_preview(bpy.types.Operator):
    bl_idname = "view3d.gf_toon_preview"
    bl_label = "xtoon"
    bl_description = "Preview materials with toon lighting and use a front light when no scene light is visible"

    @classmethod
    def poll(cls, context):
        return (context.area and context.area.type == 'VIEW_3D' and
                context.scene.render.engine != 'BLENDER_WORKBENCH')

    def execute(self, context):
        shading = context.space_data.shading
        if toon_active(shading):
            restore_preview(shading, 'MATERIAL')
        else:
            if context.preferences.studio_lights.get(_studio) is None:
                self.report({'ERROR'}, "xtoon studio light is missing")
                return {'CANCELLED'}
            shading.type = 'MATERIAL'
            if not shading.gf_previous_preview:
                shading.gf_previous_preview = json.dumps(
                    {key: getattr(shading, key) for key in _preview_defaults})
            for key, value in _preview_defaults.items():
                setattr(shading, key, value)
        context.area.tag_redraw()
        return {'FINISHED'}


def draw_toon_button(layout, shading, *, icons_only=False):
    label = "" if icons_only else VIEW3D_OT_gf_toon_preview.bl_label
    layout.operator("view3d.gf_toon_preview", text=label, icon='SHADING_XTOON', depress=toon_active(shading))


def draw_material_preview_button(layout, shading, *, icons_only=False):
    if toon_active(shading):
        layout.operator("view3d.gf_toon_preview", text="" if icons_only else "Material Preview",
                        icon='SHADING_TEXTURE', depress=False)
    elif icons_only:
        layout.prop_enum(shading, "type", value='MATERIAL', text="")
    else:
        layout.prop_enum(shading, "type", value='MATERIAL')


def draw_shading_modes(layout, shading, context, *, icons_only=False):
    label = {"text": ""} if icons_only else {}
    if toon_active(shading):
        for mode in ('WIREFRAME', 'SOLID'):
            layout.prop_enum(shading, "type", value=mode, **label)
        draw_material_preview_button(layout, shading, icons_only=icons_only)
        layout.prop_enum(shading, "type", value='RENDERED', **label)
    else:
        layout.prop(shading, "type", expand=True, **label)
    if context.scene.render.engine != 'BLENDER_WORKBENCH':
        draw_toon_button(layout, shading, icons_only=icons_only)


def gf_base_texture(material):
    tree = material.node_tree
    if tree is None:
        return None
    output = tree.get_output_node("EEVEE")
    if output is None:
        return None

    def sources(socket):
        return [link.from_node for link in socket.links if link.is_valid]

    pending = sources(output.inputs["Surface"])
    seen = set()
    while pending:
        node = pending.pop()
        if node in seen:
            continue
        seen.add(node)
        if node.type == "GROUP" and node.node_tree:
            group = node.node_tree
            if group.get("gf_toon") or group.name.startswith(("GF Toon (EEVEE)", _legacy_asset_name)):
                color = node.inputs.get("Base Color")
                queue = sources(color) if color else []
                visited = set()
                while queue:
                    source = queue.pop(0)
                    if source in visited:
                        continue
                    visited.add(source)
                    if source.type == "TEX_IMAGE" and source.image:
                        return source
                    for sock in source.inputs:
                        if sock.type in {"RGBA", "VECTOR"} or source.type == "REROUTE":
                            queue.extend(sources(sock))
        for sock in node.inputs:
            if sock.type == "SHADER" or node.type == "REROUTE":
                pending.extend(sources(sock))
    return None


def activate_gf_base_textures(context):
    materials = {slot.material for obj in context.view_layer.objects for slot in obj.material_slots
                 if slot.material and slot.material.is_editable}
    for material in materials:
        texture = gf_base_texture(material)
        if texture is not None and material.node_tree.nodes.active != texture:
            material.node_tree.nodes.active = texture


def update_flat_textures():
    context = bpy.context
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                shading = area.spaces.active.shading
                if shading.gf_previous_preview and shading.type != 'MATERIAL':
                    restore_preview(shading, shading.type)
        if any(area.type == 'VIEW_3D' and
               area.spaces.active.shading.type == 'SOLID' and
               area.spaces.active.shading.light == 'FLAT' and
               area.spaces.active.shading.color_type == 'TEXTURE'
               for area in window.screen.areas):
            with context.temp_override(window=window):
                activate_gf_base_textures(context)


@persistent
def subscribe(filepath=None):
    # Migrate only bundled local group names; preserve artist-assigned names and linked libraries.
    if filepath is not None:
        for group in bpy.data.node_groups:
            if group.library is None and group.name.split(".")[0] == _legacy_asset_name:
                group.name = group.name.replace(_legacy_asset_name, "GF Toon (EEVEE)", 1)
    bpy.msgbus.clear_by_owner(_owner)
    for prop in ("type", "light", "color_type"):
        bpy.msgbus.subscribe_rna(key=(bpy.types.View3DShading, prop), owner=_owner,
                                 args=(), notify=update_flat_textures)


def register():
    bpy.types.View3DShading.gf_use_outline = bpy.props.BoolProperty(
        name="NPR Outlines", default=True, description="Draw material outlines in this toon viewport",
        update=lambda self, context: context.area.tag_redraw() if context.area else None)
    bpy.types.View3DShading.gf_previous_preview = bpy.props.StringProperty(options={'HIDDEN'})
    bpy.types.View3DShading.gf_light_gain = bpy.props.FloatProperty(
        name="Light Multiplier", default=3.3, min=0.0, soft_max=10.0, max=1000.0,
        description="Scale lighting in this toon preview, including its fallback front light",
        update=lambda self, context: context.area.tag_redraw() if context.area else None)
    bpy.utils.register_class(VIEW3D_OT_gf_toon_preview)
    subscribe()
    bpy.app.handlers.load_post.append(subscribe)


def unregister():
    bpy.msgbus.clear_by_owner(_owner)
    bpy.app.handlers.load_post.remove(subscribe)
    bpy.utils.unregister_class(VIEW3D_OT_gf_toon_preview)
    del bpy.types.View3DShading.gf_previous_preview
    del bpy.types.View3DShading.gf_light_gain
    del bpy.types.View3DShading.gf_use_outline
