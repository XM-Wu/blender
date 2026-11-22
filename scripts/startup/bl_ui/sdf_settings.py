# SPDX-License-Identifier: GPL-2.0-or-later
"""Persistent SDF data and per-scene interaction settings."""

import bpy
from bpy.app.handlers import persistent
from bpy.props import (
    BoolProperty, CollectionProperty, EnumProperty, FloatProperty, FloatVectorProperty,
    IntProperty, PointerProperty, StringProperty,
)
from bpy.types import PropertyGroup


def redraw(settings, context):
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type in {'IMAGE_EDITOR', 'VIEW_3D'}:
                area.tag_redraw()


# Preserve interaction state across Blender's scene-wide undo snapshots. Keep
# ordinary scene RNA storage so each scene retains its own saved settings.
_INTERACTION_PROPERTIES = ("radius", "white", "use_pressure", "show_wire", "wire_opacity", "onion_skin", "auto_key", "value", "mask_collapsed", "mask_offset")
_undo_interaction = {}


@persistent
def remember_interaction(_):
    _undo_interaction.clear()
    for scene in bpy.data.scenes:
        if scene.is_editable and scene.is_property_set("sdf_editor"):
            settings = scene.sdf_editor
            _undo_interaction[scene.session_uid] = (
                tuple(getattr(settings, prop) for prop in _INTERACTION_PROPERTIES),
                tuple(mask.name for mask in settings.masks), settings.index)


@persistent
def restore_interaction(_):
    for scene in bpy.data.scenes:
        saved = _undo_interaction.get(scene.session_uid)
        if saved is None or not scene.is_editable:
            continue
        values, masks, index = saved
        settings = scene.sdf_editor
        for prop, value in zip(_INTERACTION_PROPERTIES, values):
            if getattr(settings, prop) != value:
                setattr(settings, prop, value)
        # Undoing a mask creation/deletion restores its list and selection
        # together. Undoing paint keeps the user's currently selected mask.
        if settings.index != index and tuple(mask.name for mask in settings.masks) == masks:
            settings.index = index
    _undo_interaction.clear()


_ONION_MODES = (
    ('OFF', "Off", "Show the current SDF only", 0),
    ('PREVIOUS', "Previous Key", "Compare against the nearest earlier SDF key", 1),
    ('NEXT', "Next Key", "Compare against the nearest later SDF key", 2),
)


class SDFKey(PropertyGroup):
    value: FloatProperty()
    bitmap: StringProperty()


class SDFSource(PropertyGroup):
    object: PointerProperty(type=bpy.types.Object)
    uv_name: StringProperty()
    faces: StringProperty()
    uv_snapshot: StringProperty(options={'HIDDEN'})
    inferred_snapshot: BoolProperty(options={'HIDDEN'})


class SDFMask(PropertyGroup):
    size: IntProperty()
    coverage: StringProperty()
    conflict_pair: FloatVectorProperty(size=2, default=(-1, -1), options={'HIDDEN'})
    sources: CollectionProperty(type=SDFSource)
    keys: CollectionProperty(type=SDFKey)


class SDFSettings(PropertyGroup):
    masks: CollectionProperty(type=SDFMask)
    mask_collapsed: BoolProperty(name="Collapse SDF Masks", update=redraw)
    mask_offset: IntProperty(min=0, options={'SKIP_SAVE'}, update=redraw)
    index: IntProperty(update=redraw)
    auto_key: BoolProperty(name="SDF Auto Key", default=True, update=redraw,
                           description="Create a key at the preview value when painting changes the image")
    value: FloatProperty(name="Preview", min=0, max=1, precision=4, update=redraw)
    radius: IntProperty(name="Radius (texels)", min=1, max=256, default=10, update=redraw,
                        description="Brush radius in texture pixels. Press F in either paint view to resize")
    onion_skin: EnumProperty(name="Onion Skin", items=_ONION_MODES, default='OFF', update=redraw)
    show_wire: BoolProperty(name="SDF Wireframe", default=False, update=redraw,
                            description="Show the mask mesh edges over the SDF in both paint views")
    wire_opacity: FloatProperty(name="Wire Opacity", min=0, max=1, default=.65, subtype='FACTOR', update=redraw)
    use_pressure: BoolProperty(name="Use Pressure for Radius", default=True, update=redraw)
    white: BoolProperty(name="White Brush", default=True, update=redraw)
    ink: EnumProperty(name="Brush Color", items=(
        ('BLACK', "Black", "Paint black", 0), ('WHITE', "White", "Paint white", 1)),
        get=lambda self: int(self.white), set=lambda self, value: setattr(self, "white", bool(value)))

    @property
    def mask(self):
        if 0 <= self.index < len(self.masks):
            return self.masks[self.index]


classes = (SDFKey, SDFSource, SDFMask, SDFSettings)


def register_properties():
    bpy.types.Scene.sdf_editor = PointerProperty(type=SDFSettings)
    # WM-owned controls avoid property undo pushes from header number fields,
    # pressure toggles and the native F-key radial controller.
    bpy.types.WindowManager.sdf_brush_radius = IntProperty(
        name="Radius (texels)", min=1, max=256, default=10, options={'SKIP_SAVE'},
        get=lambda wm: bpy.context.scene.sdf_editor.radius,
        set=lambda wm, value: setattr(bpy.context.scene.sdf_editor, "radius", value))
    bpy.types.WindowManager.sdf_onion_skin = EnumProperty(
        name="Onion Skin", items=_ONION_MODES, options={'SKIP_SAVE'},
        get=lambda wm: next(number for ident, _, _, number in _ONION_MODES
                            if ident == bpy.context.scene.sdf_editor.onion_skin),
        set=lambda wm, number: setattr(bpy.context.scene.sdf_editor, "onion_skin", _ONION_MODES[number][0]))
    bpy.types.WindowManager.sdf_show_wire = BoolProperty(
        name="SDF Wireframe", default=False, options={'SKIP_SAVE'},
        description="Show the mask mesh edges over the SDF in both paint views",
        get=lambda wm: bpy.context.scene.sdf_editor.show_wire,
        set=lambda wm, value: setattr(bpy.context.scene.sdf_editor, "show_wire", value))
    bpy.types.WindowManager.sdf_wire_opacity = FloatProperty(
        name="Wire Opacity", min=0, max=1, default=.65, subtype='FACTOR', options={'SKIP_SAVE'},
        get=lambda wm: bpy.context.scene.sdf_editor.wire_opacity,
        set=lambda wm, value: setattr(bpy.context.scene.sdf_editor, "wire_opacity", value))
    bpy.types.WindowManager.sdf_pressure_enabled = BoolProperty(
        name="Use Pressure for Radius", default=True, options={'SKIP_SAVE'},
        get=lambda wm: bpy.context.scene.sdf_editor.use_pressure,
        set=lambda wm, value: setattr(bpy.context.scene.sdf_editor, "use_pressure", value))


def unregister_properties():
    _undo_interaction.clear()
    del bpy.types.WindowManager.sdf_brush_radius
    del bpy.types.WindowManager.sdf_onion_skin
    del bpy.types.WindowManager.sdf_show_wire
    del bpy.types.WindowManager.sdf_wire_opacity
    del bpy.types.WindowManager.sdf_pressure_enabled
    del bpy.types.Scene.sdf_editor
