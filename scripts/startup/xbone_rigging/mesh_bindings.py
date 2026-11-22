# SPDX-License-Identifier: GPL-2.0-or-later
"""Manage mesh Armature modifiers for the current XBone."""
import bpy
from bpy.app.translations import pgettext_iface as iface_
from bpy.props import IntProperty, PointerProperty, StringProperty
from bpy.types import Operator, Panel, UIList


def is_xbone(obj):
    return obj is not None and obj.type == 'ARMATURE' and obj.data.is_xbone


def modifiers_for(obj, rig):
    return [m for m in obj.modifiers if m.type == 'ARMATURE' and m.object == rig]


def bound_meshes(scene, rig):
    return [obj for obj in scene.objects if obj.type == 'MESH' and modifiers_for(obj, rig)]


def current_rig(context):
    active = context.active_object
    if is_xbone(active):
        return active
    selected = [obj for obj in context.selected_objects if is_xbone(obj)]
    if selected:
        return selected[0] if len(selected) == 1 else None
    if active and active.type == 'MESH':
        targets = {m.object for m in active.modifiers if m.type == 'ARMATURE' and is_xbone(m.object)}
        if len(targets) == 1:
            return targets.pop()
        # Retain the target after "Select Bound Meshes" when the active mesh
        # has multiple armature modifiers.
        last = context.view_layer.xbone_last_binding_rig
        if last in targets:
            return last
    return None


class XBONE_OT_add_mesh_modifiers(Operator):
    bl_idname = "xbone.add_mesh_modifiers"
    bl_label = "Add Armature to Selected Meshes"
    bl_description = "Add an Armature modifier targeting this XBone to each selected mesh"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT' and current_rig(context) is not None and
                any(obj.type == 'MESH' for obj in context.selected_objects))

    def execute(self, context):
        rig = current_rig(context)
        added = existing = readonly = 0
        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue
            if modifiers_for(obj, rig):
                existing += 1
                continue
            if not obj.is_editable:
                readonly += 1
                continue
            mod = obj.modifiers.new(rig.name, 'ARMATURE')
            mod.object = rig
            if obj.get("_skin_armature") == rig and obj.skin_layers.prefix:
                obj["_skin_modifier"] = mod.persistent_uid
            added += 1
        self.report({'INFO'}, f"{rig.name}: {added} added, {existing} already bound, {readonly} read-only")
        return {'FINISHED'} if added else {'CANCELLED'}


class XBONE_OT_select_bound_meshes(Operator):
    bl_idname = "xbone.select_bound_meshes"
    bl_label = "Select Bound Meshes"
    bl_description = "Select the meshes using this XBone that are selectable in the current view layer"
    bl_options = {'UNDO'}
    mesh_name: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return context.mode in {'OBJECT', 'POSE'} and current_rig(context) is not None

    def execute(self, context):
        rig = current_rig(context)
        meshes = bound_meshes(context.scene, rig)
        if self.mesh_name:
            meshes = [obj for obj in meshes if obj.name == self.mesh_name]
        selectable = set(context.selectable_objects)
        chosen = [obj for obj in meshes if obj in selectable]
        if not chosen:
            self.report({'INFO'}, "No selectable bound meshes in the current view layer")
            return {'CANCELLED'}
        if context.mode == 'POSE':
            bpy.ops.object.mode_set(mode='OBJECT')
        layer = context.view_layer
        layer.xbone_last_binding_rig = rig
        for obj in context.selected_objects:
            obj.select_set(False, view_layer=layer)
        for obj in chosen:
            obj.select_set(True, view_layer=layer)
        if layer.objects.active not in chosen:
            layer.objects.active = chosen[0]
        self.report({'INFO'}, f"Selected {len(chosen)} of {len(meshes)} bound meshes")
        return {'FINISHED'}


class XBONE_OT_clear_mesh_modifiers(Operator):
    bl_idname = "xbone.clear_mesh_modifiers"
    bl_label = "Remove XBone Modifiers"
    bl_description = "Remove all modifiers referencing this XBone from meshes in the current scene"
    bl_options = {'UNDO'}
    target_uid: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return context.mode in {'OBJECT', 'POSE'} and current_rig(context) is not None

    def invoke(self, context, event):
        rig = current_rig(context)
        self.target_uid = str(rig.session_uid)
        count = len(bound_meshes(context.scene, rig))
        return context.window_manager.invoke_confirm(
            self, event, message=iface_("Remove modifiers targeting {rig} from {count} scene meshes?").format(
                rig=rig.name, count=count))

    def execute(self, context):
        rig = current_rig(context)
        if rig is None or (self.target_uid and str(rig.session_uid) != self.target_uid):
            self.report({'WARNING'}, "The target XBone changed")
            return {'CANCELLED'}
        removed = readonly = 0
        for obj in bound_meshes(context.scene, rig):
            for mod in modifiers_for(obj, rig):
                if not obj.is_editable or (obj.override_library and mod.is_override_data):
                    readonly += 1
                    continue
                owns_layers = obj.get("_skin_armature") == rig and obj.get("_skin_modifier") == mod.persistent_uid
                obj.modifiers.remove(mod)
                if owns_layers:
                    obj["_skin_modifier"] = -1
                    for key in ("_skin_source", "_skin_preview", "_skin_active_channel"):
                        if key in obj:
                            del obj[key]
                removed += 1
        self.report({'INFO'}, f"{rig.name}: {removed} modifiers removed, {readonly} read-only")
        return {'FINISHED'} if removed else {'CANCELLED'}


class RIGGING_UL_bound_meshes(UIList):
    def draw_filter(self, context, layout):
        layout.prop(self, "filter_name", text="", icon='VIEWZOOM')

    def filter_items(self, context, data, propname):
        rig = current_rig(context)
        search = self.filter_name.casefold()
        return [self.bitflag_filter_item if rig and obj.type == 'MESH' and
                search in obj.name.casefold() and modifiers_for(obj, rig) else 0
                for obj in data.objects], []

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.operator("xbone.select_bound_meshes", text=item.name, icon='MESH_DATA',
                        emboss=False, translate=False).mesh_name = item.name


class RIGGING_PT_mesh_bindings(Panel):
    bl_space_type = 'RIGGING'
    bl_region_type = 'WINDOW'
    bl_label = "Mesh Bindings"

    def draw(self, context):
        layout = self.layout
        rig = current_rig(context)
        if not rig:
            layout.label(text="Select an XBone to manage mesh bindings", icon='ARMATURE_DATA')
            return
        layout.label(text=rig.name, icon='ARMATURE_DATA', translate=False)
        layout.operator("xbone.add_mesh_modifiers", icon='MOD_ARMATURE')
        meshes = bound_meshes(context.scene, rig)
        if not meshes:
            layout.label(text="No bound meshes")
            return
        layout.template_list("RIGGING_UL_bound_meshes", "", context.scene, "objects",
                             context.view_layer, "xbone_binding_mesh_index", rows=min(6, len(meshes)))
        row = layout.row(align=True)
        row.operator("xbone.select_bound_meshes", icon='RESTRICT_SELECT_OFF')
        row.operator("xbone.clear_mesh_modifiers", icon='TRASH')


classes = (XBONE_OT_add_mesh_modifiers, XBONE_OT_select_bound_meshes, XBONE_OT_clear_mesh_modifiers,
           RIGGING_UL_bound_meshes, RIGGING_PT_mesh_bindings)


def register():
    bpy.types.ViewLayer.xbone_last_binding_rig = PointerProperty(type=bpy.types.Object, options={'SKIP_SAVE'})
    bpy.types.ViewLayer.xbone_binding_mesh_index = IntProperty(default=-1, options={'SKIP_SAVE'})


def unregister():
    del bpy.types.ViewLayer.xbone_binding_mesh_index
    del bpy.types.ViewLayer.xbone_last_binding_rig
