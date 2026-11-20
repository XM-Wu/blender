# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Rest-space assignment, symmetry and view tools for the skin layer editor."""

import bpy
import numpy as np
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, PointerProperty, StringProperty
from bpy.types import Operator, PropertyGroup
from mathutils.kdtree import KDTree
from . import skin_layers as skin


def group_for_bone(obj, name):
    group = obj.vertex_groups.get(name)
    if group is None:
        old = obj.vertex_groups.active_index
        group = obj.vertex_groups.new(name=name)
        if old >= 0:
            obj.vertex_groups.active_index = old
    if not group.skin_channel:
        skin.assign_channel(group, {g.skin_channel for g in obj.vertex_groups})
    return group


def mirror_geometry(mesh):
    """Only accept reciprocal rest-space matches, including fixed points at the seam."""
    n = len(mesh.vertices)
    positions = np.empty(n * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", positions)
    positions = positions.reshape(n, 3)
    pairs = np.full(n, -1, dtype=np.int32)
    if not n:
        return positions[:, 0], pairs
    tree = KDTree(n)
    for i, p in enumerate(positions):
        tree.insert(p, i)
    tree.balance()
    tol = max(1e-6, np.linalg.norm(np.ptp(positions, axis=0)) * 1e-4)
    for i, (x, y, z) in enumerate(positions):
        if abs(x) <= tol * 0.5:
            pairs[i] = i
        else:
            _, j, d = tree.find((-x, y, z))
            if d <= tol:
                pairs[i] = j
    matched = np.flatnonzero(pairs >= 0)
    pairs[matched[pairs[pairs[matched]] != matched]] = -1
    return positions[:, 0], pairs


def mirror_name(obj, group):
    name = bpy.utils.flip_name(group.name)
    target = skin.rig_modifier(obj).object.data.bones.get(name)
    if not target or not target.use_deform:
        raise ValueError("Missing mirrored deform bone: " + name)
    return name


class LayerTool:
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return bool(skin.rig_modifier(obj) and "_skin_source" in obj and obj.mode != 'EDIT')


class PAINT_OT_skin_reparent(LayerTool, Operator):
    bl_idname = "paint.skin_reparent"
    bl_label = "Move Layer to Group"
    bl_options = {'REGISTER', 'UNDO'}
    parent_key: StringProperty()

    def execute(self, context):
        obj = context.active_object
        state = obj.skin_layers
        layer = skin.current_layer(state)
        if self.parent_key == layer.parent_key:
            return {'CANCELLED'}
        if self.parent_key:
            parent = next((item for item in state.layers if item.key == self.parent_key), None)
            if not parent or not parent.is_group or any(item.key == parent.key for item in skin.subtree(state, layer)):
                self.report({'WARNING'}, "Choose a group outside this subtree")
                return {'CANCELLED'}
            parent.expanded = True
        skin.unique_storage(obj)
        layer.parent_key = self.parent_key
        state.layers.move(state.index, len(state.layers) - 1)
        state.index = len(state.layers) - 1
        skin.composite(obj)
        return {'FINISHED'}


class PaintRigTool:
    @classmethod
    def poll(cls, context):
        return context.mode == 'PAINT_WEIGHT' and bool(skin.rig_modifier(context.active_object))


class PAINT_OT_skin_rest_pose(PaintRigTool, Operator):
    bl_idname = "paint.skin_rest_pose"
    bl_label = "Reset Pose"
    bl_description = "Reset all local pose transforms to their rest values and continue posing"
    bl_options = {'UNDO'}

    def execute(self, context):
        from mathutils import Matrix

        rig = skin.rig_modifier(context.active_object).object
        if not rig.is_editable or (rig.data.pose_position == 'REST' and not rig.data.is_editable):
            self.report({'WARNING'}, "The rig is read-only")
            return {'CANCELLED'}
        if rig.data.pose_position == 'REST':
            rig.data.pose_position = 'POSE'
        identity = Matrix.Identity(4)
        for bone in rig.pose.bones:
            bone.matrix_basis = identity
        return {'FINISHED'}


class SkinPoseSelection(PropertyGroup):
    object: PointerProperty(type=bpy.types.Object)


class SkinPoseSession(PropertyGroup):
    source: PointerProperty(type=bpy.types.Object)
    rig: PointerProperty(type=bpy.types.Object)
    selection: CollectionProperty(type=SkinPoseSelection)
    rig_was_hidden: BoolProperty()
    rig_was_posing: BoolProperty()
    group_name: StringProperty()


def set_mode(mode):
    if bpy.ops.object.mode_set(mode=mode) != {'FINISHED'}:
        raise RuntimeError("Could not enter " + mode.replace('_', ' ').title())


def select_for_mode(context, obj, selection):
    layer = context.view_layer
    for selected in list(context.selected_objects):
        selected.select_set(False, view_layer=layer)
    for selected in selection:
        if selected and layer.objects.get(selected.name) == selected:
            selected.select_set(True, view_layer=layer)
    obj.select_set(True, view_layer=layer)
    layer.objects.active = obj


class PAINT_OT_skin_switch_pose(Operator):
    bl_idname = "paint.skin_switch_pose"
    bl_label = "Switch Between Rig and Weight Paint"
    bl_description = "Pose the bound armature or resume painting the original mesh"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        if context.mode == 'PAINT_WEIGHT':
            return bool(skin.rig_modifier(context.active_object))
        if context.mode != 'POSE':
            return False
        session = context.view_layer.skin_pose_session
        return session.rig == context.active_object and bool(session.source)

    def execute(self, context):
        layer = context.view_layer
        session = layer.skin_pose_session
        entering = context.mode == 'PAINT_WEIGHT'
        source = context.active_object if entering else session.source
        rig = skin.rig_modifier(source).object if entering else context.active_object
        target = rig if entering else source
        if layer.objects.get(target.name) != target or not target.is_editable:
            self.report({'WARNING'}, "Target is outside this view layer or is read-only")
            return {'CANCELLED'}
        if entering and rig.data.pose_position == 'REST' and not rig.data.is_editable:
            self.report({'WARNING'}, "The read-only armature is set to Rest Position")
            return {'CANCELLED'}
        hidden = entering and rig.hide_get(view_layer=layer)
        if hidden:
            rig.hide_set(False, view_layer=layer)
        view = context.space_data if context.area and context.area.type == 'VIEW_3D' else None
        if not target.visible_get(view_layer=layer, viewport=view) or target.hide_select:
            if hidden:
                rig.hide_set(True, view_layer=layer)
            self.report({'WARNING'}, "Make the target visible and selectable in this viewport")
            return {'CANCELLED'}

        previous = context.active_object
        active_group = source.vertex_groups.active
        group_name = active_group.name if active_group else ""
        selection = list(context.selected_objects)
        rig_was_posing = rig.mode == 'POSE'
        try:
            if entering or not session.rig_was_posing:
                set_mode('OBJECT')
            restore = [] if entering else [item.object for item in session.selection]
            select_for_mode(context, target, restore)
            mode = 'POSE' if entering else 'WEIGHT_PAINT'
            if target.mode != mode:
                set_mode(mode)
        except RuntimeError as exc:
            if target.mode != 'OBJECT' and layer.objects.active == target and not (entering and rig_was_posing):
                set_mode('OBJECT')
            select_for_mode(context, previous, selection)
            if previous.mode == 'OBJECT':
                set_mode('WEIGHT_PAINT' if entering else 'POSE')
            if hidden:
                rig.hide_set(True, view_layer=layer)
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}

        if entering:
            session.source, session.rig = source, rig
            session.rig_was_hidden, session.rig_was_posing = hidden, rig_was_posing
            session.group_name = group_name
            session.selection.clear()
            for obj in selection:
                session.selection.add().object = obj
            if rig.data.pose_position == 'REST':
                rig.data.pose_position = 'POSE'
            if view:
                view.overlay.show_bones = True
        else:
            group = source.vertex_groups.get(session.group_name)
            if group:
                source.vertex_groups.active_index = group.index
            if session.rig_was_hidden:
                rig.hide_set(True, view_layer=layer)
            layer.property_unset("skin_pose_session")
        return {'FINISHED'}


class PAINT_OT_skin_front(PaintRigTool, Operator):
    bl_idname = "paint.skin_front"
    bl_label = "Bones In Front"
    bl_description = "Toggle the bound armature in front of geometry"
    bl_options = {'UNDO'}

    def execute(self, context):
        rig = skin.rig_modifier(context.active_object).object
        if not rig.is_editable:
            return {'CANCELLED'}
        rig.show_in_front = not rig.show_in_front
        if rig.show_in_front:
            if context.view_layer.objects.get(rig.name):
                rig.hide_set(False)
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.spaces.active.overlay.show_bones = True
        return {'FINISHED'}


class PAINT_OT_skin_closest(LayerTool, Operator):
    bl_idname = "paint.skin_closest"
    bl_label = "Assign from Closest Joint"
    bl_options = {'REGISTER', 'UNDO'}
    scope: EnumProperty(name="Joints", items=[('ALL', "All Deform Bones", ""), ('SELECTED', "Selected Bones", "")])

    def execute(self, context):
        obj = context.active_object
        layer = skin.current_layer(obj.skin_layers)
        if (error := skin.brush_error(obj)) or layer.is_group:
            self.report({'WARNING'}, error or "Choose a paint layer")
            return {'CANCELLED'}
        rig = skin.rig_modifier(obj).object
        bones = [bone for bone in rig.data.bones if bone.use_deform and
                 (self.scope == 'ALL' or rig.pose.bones[bone.name].select)]
        indices = np.flatnonzero(skin.paintable_vertices(obj.data))
        if not bones or not len(indices):
            self.report({'WARNING'}, "Choose deform bones and paintable vertices")
            return {'CANCELLED'}
        skin.unique_storage(obj)
        tree = KDTree(len(bones))
        for i, bone in enumerate(bones):
            tree.insert(rig.matrix_world @ bone.head_local, i)
        tree.balance()
        matrix = obj.matrix_world
        nearest = np.array([tree.find(matrix @ obj.data.vertices[int(i)].co)[1] for i in indices])
        prefix = skin.layer_key(obj.skin_layers, layer) + "_"
        targets = {group_for_bone(obj, bones[j].name).skin_channel: j for j in np.unique(nearest)}
        for group in skin.channels(obj):
            key = prefix + str(group.skin_channel)
            if key in obj.data.attributes or group.skin_channel in targets:
                weights = skin.read_values(obj.data, key)
                weights[indices] = nearest == targets.get(group.skin_channel, -1)
                skin.write_values(obj.data, key, weights)
        skin.composite(obj)
        return {'FINISHED'}


class PAINT_OT_skin_mirror(LayerTool, Operator):
    bl_idname = "paint.skin_mirror"
    bl_label = "Mirror Layer"
    bl_description = "Copy the current paint target across the local X axis using rest-space vertex pairs"
    bl_options = {'REGISTER', 'UNDO'}
    direction: EnumProperty(items=[('POSITIVE', "+X to −X", ""), ('NEGATIVE', "−X to +X", "")])

    def execute(self, context):
        obj = context.active_object
        if error := skin.brush_error(obj):
            self.report({'WARNING'}, error)
            return {'CANCELLED'}
        state, mesh = obj.skin_layers, obj.data
        layer = skin.current_layer(state)
        mask = layer.is_group or state.target == 'MASK'
        x, pair = mirror_geometry(mesh)
        valid = (pair >= 0) & (pair != np.arange(len(pair)))
        side = x > 0 if self.direction == 'POSITIVE' else x < 0
        src = np.flatnonzero(valid & side)
        if not len(src):
            self.report({'WARNING'}, "No matching vertices across local X")
            return {'CANCELLED'}
        groups = [g for g in skin.channels(obj) if g.skin_channel in skin.used_channels(obj, layer)]
        try:
            names = {} if mask else {g.skin_channel: mirror_name(obj, g) for g in groups}
        except ValueError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        skin.unique_storage(obj)
        prefix = skin.layer_key(state, layer) + "_"
        if mask:
            weights = skin.read_values(mesh, prefix + "mask", 1)
            weights[pair[src]] = weights[src]
            skin.write_values(mesh, prefix + "mask", weights)
        else:
            mapping = {j: group_for_bone(obj, name).skin_channel for j, name in names.items()}
            inputs = {j: skin.read_values(mesh, prefix + str(j))[src] for j in mapping}
            reverse = {dest: j for j, dest in mapping.items()}
            for j in mapping.keys() | reverse.keys():
                key = prefix + str(j)
                weights = skin.read_values(mesh, key)
                weights[pair[src]] = inputs.get(reverse.get(j), 0)
                skin.write_values(mesh, key, weights)
        skin.composite(obj)
        self.report({'INFO'}, "Mirrored %d vertex pairs; %d unmatched vertices" % (len(src), np.sum(pair < 0)))
        return {'FINISHED'}


class PAINT_OT_skin_pick(LayerTool, Operator):
    bl_idname = "paint.skin_pick"
    bl_label = "Pick Bone from Surface"
    bl_description = "Click the mesh to select the strongest interpolated influence"
    bl_options = {'UNDO', 'BLOCKING'}

    def invoke(self, context, event):
        context.window.cursor_modal_set('EYEDROPPER')
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        context.window.cursor_modal_restore()

    def modal(self, context, event):
        if event.type in {'RIGHTMOUSE', 'ESC'}:
            self.cancel(context)
            return {'CANCELLED'}
        if event.type != 'LEFTMOUSE' or event.value != 'PRESS':
            return {'RUNNING_MODAL'}
        from bpy_extras.view3d_utils import region_2d_to_origin_3d, region_2d_to_vector_3d
        from mathutils.interpolate import poly_3d_calc
        obj = context.active_object
        if not self.poll(context):
            self.cancel(context)
            return {'CANCELLED'}
        for area in context.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            r = next(r for r in area.regions if r.type == 'WINDOW')
            xy = (event.mouse_x - r.x, event.mouse_y - r.y)
            if not (0 <= xy[0] < r.width and 0 <= xy[1] < r.height):
                continue
            with context.temp_override(area=area, region=r):
                view = context.region_data
                ob = obj.evaluated_get(context.evaluated_depsgraph_get())
                inv = ob.matrix_world.inverted_safe()
                origin = region_2d_to_origin_3d(r, view, xy)
                ray = region_2d_to_vector_3d(r, view, xy)
                hit, point, _, face = ob.ray_cast(inv @ origin, inv.to_3x3() @ ray)
                if not hit or face < 0 or face >= len(ob.data.polygons):
                    continue
                ids = list(ob.data.polygons[face].vertices)
                coords = [ob.data.vertices[i].co for i in ids]
                factors = poly_3d_calc(coords, point)
                state = obj.skin_layers
                layer = skin.current_layer(state)
                prefix = state.prefix + "_r" if state.display == 'FINAL' or layer.is_group else skin.layer_key(state, layer) + "_"
                strongest, best = None, 0.0
                for group in skin.channels(obj):
                    attr = ob.data.attributes.get(prefix + str(group.skin_channel))
                    if attr:
                        value = sum(f * attr.data[i].value for f, i in zip(factors, ids))
                        if value > best:
                            strongest, best = group.name, value
                if strongest:
                    skin.select_influence(obj, skin.rig_modifier(obj).object.data.bones[strongest])
                    self.cancel(context)
                    return {'FINISHED'}
        self.report({'INFO'}, "No weighted surface at this position")
        return {'RUNNING_MODAL'}


def register():
    bpy.types.ViewLayer.skin_pose_session = PointerProperty(type=SkinPoseSession)


def unregister():
    del bpy.types.ViewLayer.skin_pose_session


classes = (SkinPoseSelection, SkinPoseSession, PAINT_OT_skin_switch_pose, PAINT_OT_skin_reparent, PAINT_OT_skin_rest_pose, PAINT_OT_skin_front, PAINT_OT_skin_closest, PAINT_OT_skin_mirror, PAINT_OT_skin_pick)
