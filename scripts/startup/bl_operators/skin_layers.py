# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Layered skin authoring. Point attributes follow the mesh through deformation.
Only the Armature selected during initialization consumes the composite channels.
"""

import uuid

import bpy
import numpy as np
from bpy.props import (
    BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
    IntProperty, PointerProperty, StringProperty,
)
from bpy.types import Operator, PropertyGroup


def token():
    return uuid.uuid4().hex[:12]


def rig_modifier(obj):
    if not obj or obj.type != 'MESH':
        return None
    mods = [m for m in obj.modifiers if m.type == 'ARMATURE']
    if len(mods) == 1 and mods[0].object and mods[0].use_vertex_groups:
        return mods[0]
    return None


def channels(obj):
    mod = rig_modifier(obj)
    if not mod:
        return []
    bones = mod.object.data.bones
    return [g for g in obj.vertex_groups if g.skin_channel and
            (bone := bones.get(g.name)) and bone.use_deform]


def layer_key(state, layer):
    return state.prefix + "_" + layer.key


def read_values(mesh, key, default=0.0):
    attr = mesh.attributes.get(key)
    if attr is None:
        return np.full(len(mesh.vertices), default, dtype=np.float32)
    values = np.empty(len(mesh.vertices), dtype=np.float32)
    attr.data.foreach_get("value", values)
    return values


def write_values(mesh, key, values):
    attr = mesh.attributes.get(key)
    if attr is None:
        attr = mesh.attributes.new(key, 'FLOAT', 'POINT')
    attr.data.foreach_set("value", np.ascontiguousarray(values, dtype=np.float32))


def unique_storage(obj):
    # Linked duplicates have their own layer metadata but initially share point attributes.
    state = obj.skin_layers
    if obj.data.users < 2 or not any(
        other != obj and other.type == 'MESH' and other.data == obj.data and
        other.skin_layers.prefix == state.prefix for other in bpy.data.objects
    ):
        return
    old = state.prefix
    state.prefix = ".skin_" + token()
    mesh = obj.data
    for key in tuple(mesh.attributes.keys()):
        if key == old or key.startswith(old + "_"):
            if key == old:
                copy_marker(mesh, old, state.prefix)
            else:
                write_values(mesh, state.prefix + key[len(old):], read_values(mesh, key))
    if "_skin_source" in obj:
        obj["_skin_source"] = state.prefix


def copy_marker(mesh, source, destination):
    indices = np.arange(1, len(mesh.vertices) + 1, dtype=np.int32)
    if source:
        mesh.attributes[source].data.foreach_get("value", indices)
    mesh.attributes.new(destination, 'INT', 'POINT').data.foreach_set("value", indices)


def topology_error(obj):
    state = obj.skin_layers
    mesh = obj.data
    marker = mesh.attributes.get(state.prefix)
    if not marker or len(mesh.vertices) != state.vertex_count:
        return "Mesh topology changed; restore the source topology before painting or baking"
    ids = np.empty(len(mesh.vertices), dtype=np.int32)
    marker.data.foreach_get("value", ids)
    if not np.array_equal(ids, np.arange(1, len(ids) + 1, dtype=np.int32)):
        return "Vertex order changed; restore the source topology before painting or baking"
    return None


def current_layer(state):
    return state.layers[min(state.index, len(state.layers) - 1)] if state.layers else None


def children(state, parent=""):
    return [item for item in state.layers if item.parent_key == parent]


def subtree(state, item):
    yield item
    for child in children(state, item.key):
        yield from subtree(state, child)


def layer_index(state, key):
    return next(i for i, item in enumerate(state.layers) if item.key == key)


_influences = {}


@bpy.app.handlers.persistent
def clear_influence_cache(*_):
    _influences.clear()


def used_channels(obj, layer):
    cache_key = (obj.session_uid, obj.data.session_uid, layer.key)
    if cache_key not in _influences:
        if layer.is_group:
            used = set()
            for child in children(obj.skin_layers, layer.key):
                used.update(used_channels(obj, child))
        else:
            prefix = layer_key(obj.skin_layers, layer) + "_"
            used = {int(name[len(prefix):]) for name in obj.data.attributes.keys()
                    if name.startswith(prefix) and name[len(prefix):].isdigit()
                    and np.any(read_values(obj.data, name) > 0)}
        _influences[cache_key] = used
    return _influences[cache_key]


def update_preview(self, context):
    obj = self.id_data
    state = obj.skin_layers
    layer = current_layer(state)
    if not layer or "_skin_source" not in obj:
        return
    if state.display == 'MASK' or (state.display == 'LAYER' and (state.target == 'MASK' or layer.is_group)):
        key = layer_key(state, layer) + "_mask"
    elif state.display == 'FINAL':
        key = state.prefix + "_r" + str(state.influence)
    else:
        key = layer_key(state, layer) + "_" + str(state.influence)
    obj["_skin_preview"] = key
    obj["_skin_active_channel"] = state.influence
    obj.data.update_gpu_tag()
    obj.update_tag(refresh={'DATA'})


def compose_stack(obj, layers, joint_ids):
    mesh = obj.data
    results = {}
    transmission = np.ones(len(mesh.vertices), dtype=np.float32)
    for layer in layers:
        if not layer.enabled or layer.opacity <= 0:
            continue
        prefix = layer_key(obj.skin_layers, layer) + "_"
        if layer.is_group:
            weights, child_transmission = compose_stack(obj, children(obj.skin_layers, layer.key), joint_ids)
            coverage = 1 - child_transmission
        else:
            weights = {j: read_values(mesh, prefix + str(j)) for j in joint_ids
                       if mesh.attributes.get(prefix + str(j)) is not None}
            coverage = np.minimum(sum(weights.values()), 1.0)
        if not weights:
            continue
        k = layer.opacity * read_values(mesh, prefix + "mask", 1)
        remaining = 1 - k * coverage
        transmission *= remaining
        for values in results.values():
            values *= remaining
        for j, values in weights.items():
            if j in results:
                results[j] += k * values
            else:
                results[j] = k * values
    return results, transmission


def composite(obj):
    clear_influence_cache()
    state = obj.skin_layers
    mesh = obj.data
    results, _ = compose_stack(obj, children(state), {g.skin_channel for g in channels(obj)})
    result_prefix = state.prefix + "_r"
    for key in tuple(mesh.attributes.keys()):
        if key.startswith(result_prefix) and key[len(result_prefix):].isdigit():
            if int(key[len(result_prefix):]) not in results:
                mesh.attributes.remove(mesh.attributes[key])
    for j, values in results.items():
        write_values(mesh, result_prefix + str(j), values)
    update_preview(state, None)
    mesh.update()


def on_mix_changed(self, context):
    obj = self.id_data
    if obj.skin_layers.prefix:
        unique_storage(obj)
        composite(obj)


class SkinWeightLayer(PropertyGroup):
    parent_key: StringProperty()
    is_group: BoolProperty()
    expanded: BoolProperty(name="Show Children", default=True)
    key: StringProperty()
    enabled: BoolProperty(name="Visible", default=True, update=on_mix_changed)
    opacity: FloatProperty(name="Opacity", subtype='FACTOR', default=1, min=0, max=1,
                           update=on_mix_changed)


def select_bone_row(state, context):
    obj = state.id_data
    mod = rig_modifier(obj)
    if mod and "_skin_source" in obj and state.bone_index < len(mod.object.data.bones):
        select_influence(obj, mod.object.data.bones[state.bone_index])


def select_influence(obj, bone):
    if not bone.use_deform:
        return
    from .skin_layers_tools import group_for_bone
    group = group_for_bone(obj, bone.name)
    rig = rig_modifier(obj).object
    if rig.is_editable:
        for pb in rig.pose.bones:
            pb.select = pb.name == bone.name
        if rig.data.is_editable:
            rig.data.bones.active = bone
    obj.skin_layers.influence = group.skin_channel
    obj.skin_layers["bone_index"] = rig_modifier(obj).object.data.bones.find(bone.name)


class SkinWeightSettings(PropertyGroup):
    prefix: StringProperty()
    vertex_count: IntProperty()
    native_tool: StringProperty(default="builtin.brush")
    layers: CollectionProperty(type=SkinWeightLayer)
    index: IntProperty(min=0, update=update_preview)
    bone_index: IntProperty(min=0, update=select_bone_row)
    influence: IntProperty(min=0, update=update_preview)
    only_influenced: BoolProperty(name="Influenced Only", description="Show bones with a positive authored weight in this layer")
    search: StringProperty(name="Filter Bones", options={'TEXTEDIT_UPDATE'})
    target: EnumProperty(name="Paint Target", items=[
        ('WEIGHT', "Weight", "Paint joint weights"), ('MASK', "Mask", "Paint layer coverage")],
        update=update_preview)
    display: EnumProperty(name="Preview", items=[
        ('LAYER', "Layer", "Current layer"), ('MASK', "Mask", "Layer mask"),
        ('FINAL', "Final", "Composited joint weight")], update=update_preview)
    brush: EnumProperty(name="Brush", items=[
        ('REPLACE', "Replace", ""), ('ADD', "Add", ""), ('SCALE', "Scale", ""), ('SMOOTH', "Smooth", "")])
    value: FloatProperty(name="Weight", default=1, min=0, max=1)
    strength: FloatProperty(name="Strength", default=1.0, min=0, max=1)
    radius: IntProperty(name="Radius", subtype='PIXEL', min=1, max=2000, default=50)


def add_layer(obj, name, parent="", group=False):
    state = obj.skin_layers
    layer = state.layers.add()
    layer.name = name
    layer.key = token()
    layer.parent_key = parent
    layer.is_group = group
    write_values(obj.data, layer_key(state, layer) + "_mask",
                 np.ones(len(obj.data.vertices), dtype=np.float32))
    state.index = len(state.layers) - 1
    return layer


def assign_channel(group, used):
    while not group.skin_channel or group.skin_channel in used:
        group.skin_channel = int(uuid.uuid4().hex[:7], 16)
    used.add(group.skin_channel)


def initialize(obj, mod):
    state = obj.skin_layers
    mesh = obj.data
    state.prefix = ".skin_" + token()
    state.vertex_count = len(mesh.vertices)
    copy_marker(mesh, None, state.prefix)
    base = add_layer(obj, "Base")
    used = set()
    groups = {}
    for group in obj.vertex_groups:
        bone = mod.object.data.bones.get(group.name)
        if bone and bone.use_deform:
            assign_channel(group, used)
            groups[group.index] = group.skin_channel
    weights = {}
    for vertex in mesh.vertices:
        for membership in vertex.groups:
            channel = groups.get(membership.group)
            if channel and membership.weight:
                if channel not in weights:
                    weights[channel] = np.zeros(len(mesh.vertices), dtype=np.float32)
                weights[channel][vertex.index] = membership.weight
    for channel, values in weights.items():
        write_values(mesh, layer_key(state, base) + "_" + str(channel), values)
    state.influence = groups.get(obj.vertex_groups.active_index, next(iter(groups.values()), 0))
    chosen = next((g for g in obj.vertex_groups if g.skin_channel == state.influence and state.influence), None)
    if chosen:
        state["bone_index"] = mod.object.data.bones.find(chosen.name)
    obj["_skin_modifier"] = mod.persistent_uid
    obj["_skin_vertex_count"] = state.vertex_count
    obj["_skin_armature"] = mod.object


def activate_paint_tool(context, obj, layered):
    if context.mode != 'PAINT_WEIGHT' or not context.screen:
        return
    view = next((a for a in context.screen.areas if a.type == 'VIEW_3D'), None)
    if view is None:
        return
    with context.temp_override(area=view, region=next(r for r in view.regions if r.type == 'WINDOW')):
        current = context.workspace.tools.from_space_view3d_mode('PAINT_WEIGHT', create=False)
        if layered and current and current.idname != "builtin.skin_layer_brush":
            obj.skin_layers.native_tool = current.idname
        tool = "builtin.skin_layer_brush" if layered else obj.skin_layers.native_tool
        bpy.ops.wm.tool_set_by_id(name=tool, space_type='VIEW_3D')


class PAINT_OT_skin_source(Operator):
    bl_idname = "paint.skin_source"
    bl_label = "Weight Paint Source"
    bl_options = {'REGISTER', 'UNDO'}
    layered: BoolProperty(default=True)

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == 'MESH' and obj.is_editable and obj.mode != 'EDIT'

    def execute(self, context):
        obj = context.active_object
        if not self.layered:
            for key in ("_skin_source", "_skin_preview", "_skin_active_channel"):
                if key in obj:
                    del obj[key]
        else:
            mod = rig_modifier(obj)
            if not mod or not obj.data.is_editable:
                self.report({'ERROR'}, "Select an editable mesh with one vertex-group Armature modifier")
                return {'CANCELLED'}
            if not obj.skin_layers.prefix:
                initialize(obj, mod)
            elif (obj.get("_skin_modifier") != mod.persistent_uid or
                  obj.get("_skin_armature") != mod.object or topology_error(obj)):
                self.report({'ERROR'}, "Restore the original armature modifier and mesh topology")
                return {'CANCELLED'}
            unique_storage(obj)
            obj["_skin_source"] = obj.skin_layers.prefix
            composite(obj)
        activate_paint_tool(context, obj, self.layered)
        obj.data.update()
        obj.update_tag(refresh={'DATA'})
        return {'FINISHED'}


def remove_subtree(obj, root):
    state = obj.skin_layers
    keys = {item.key for item in subtree(state, root)}
    prefixes = tuple(state.prefix + "_" + key + "_" for key in keys)
    for name in tuple(obj.data.attributes.keys()):
        if name.startswith(prefixes):
            obj.data.attributes.remove(obj.data.attributes[name])
    for i in reversed(range(len(state.layers))):
        if state.layers[i].key in keys:
            state.layers.remove(i)


def duplicate_subtree(obj, item, parent):
    state = obj.skin_layers
    key, name, opacity, enabled, group = item.key, item.name, item.opacity, item.enabled, item.is_group
    child_keys = [child.key for child in children(state, key)]
    clone = add_layer(obj, name + " Copy", parent, group)
    clone_key = clone.key
    clone["opacity"], clone["enabled"] = opacity, enabled
    src, dst = state.prefix + "_" + key + "_", layer_key(state, clone) + "_"
    for attr in tuple(obj.data.attributes.keys()):
        if attr.startswith(src):
            write_values(obj.data, dst + attr[len(src):], read_values(obj.data, attr))
    for child_key in child_keys:
        duplicate_subtree(obj, state.layers[layer_index(state, child_key)], clone_key)
    return clone_key


def merge_down(obj, lower, upper):
    state = obj.skin_layers
    values, trans = compose_stack(obj, [lower, upper], {g.skin_channel for g in channels(obj)})
    name, parent = lower.name, lower.parent_key
    peers = [item.key for item in children(state, parent)]
    rank = peers.index(upper.key)
    next_key = peers[rank + 1] if rank + 1 < len(peers) else None
    upper_key = upper.key
    remove_subtree(obj, lower)
    remove_subtree(obj, state.layers[layer_index(state, upper_key)])
    merged = add_layer(obj, name, parent)
    prefix = layer_key(state, merged) + "_"
    # Only a non-normalized source can require separate mask coverage to preserve
    # both the output vector and its transmission to arbitrary lower siblings.
    coverage = 1 - trans
    total = sum(values.values())
    mask = np.where((coverage > 0) & (total > coverage + 1e-6), coverage, 1.0)
    write_values(obj.data, prefix + "mask", mask)
    for channel, weight in values.items():
        normalized = weight / mask
        write_values(obj.data, prefix + str(channel), normalized)
    if next_key:
        destination = layer_index(state, next_key)
        state.layers.move(len(state.layers) - 1, destination)
        state.index = destination


class PAINT_OT_skin_layer_edit(Operator):
    bl_idname = "paint.skin_layer_edit"
    bl_label = "Edit Skin Layer"
    bl_options = {'REGISTER', 'UNDO'}
    action: EnumProperty(items=[(x, x.title(), "") for x in
                                ('ADD', 'GROUP', 'CHILD', 'MERGE', 'DUPLICATE', 'DELETE', 'UP', 'DOWN')])

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == 'MESH' and "_skin_source" in obj

    def execute(self, context):
        obj = context.active_object
        state = obj.skin_layers
        if (error := topology_error(obj)):
            self.report({'ERROR'}, error)
            return {'CANCELLED'}
        unique_storage(obj)
        i = state.index
        layer = current_layer(state)
        parent, key = layer.parent_key, layer.key
        siblings = [item.key for item in children(state, parent)]
        rank = siblings.index(key)
        if self.action in {'ADD', 'GROUP'}:
            group = self.action == 'GROUP'
            item = add_layer(obj, "Group" if group else "Layer %d" % len(state.layers), parent, group)
            state.layers.move(layer_index(state, item.key), i + 1)
            state.index = i + 1
        elif self.action == 'CHILD':
            if not layer.is_group:
                wrapper = add_layer(obj, layer.name + " Group", parent, True)
                wrapper_key = wrapper.key
                state.layers.move(len(state.layers) - 1, i)
                state.layers[layer_index(state, key)].parent_key = wrapper_key
                key = wrapper_key
            state.layers[layer_index(state, key)].expanded = True
            add_layer(obj, "Child Layer", key)
        elif self.action == 'MERGE':
            if rank == 0:
                self.report({'WARNING'}, "No lower sibling in this group")
                return {'CANCELLED'}
            merge_down(obj, state.layers[layer_index(state, siblings[rank - 1])], layer)
        elif self.action == 'DUPLICATE':
            new_key = duplicate_subtree(obj, layer, parent)
            state.layers.move(layer_index(state, new_key), i + 1)
            state.index = i + 1
        elif self.action == 'DELETE':
            if len(list(subtree(state, layer))) == len(state.layers):
                self.report({'WARNING'}, "Keep at least one root layer")
                return {'CANCELLED'}
            remove_subtree(obj, layer)
            state.index = min(i, len(state.layers) - 1)
        else:
            next_rank = rank + (1 if self.action == 'UP' else -1)
            if not 0 <= next_rank < len(siblings):
                return {'CANCELLED'}
            destination = layer_index(state, siblings[next_rank])
            state.layers.move(i, destination)
            state.index = destination
        composite(obj)
        return {'FINISHED'}


class PAINT_OT_skin_influence(Operator):
    bl_idname = "paint.skin_influence"
    bl_label = "Paint Influence"
    bl_options = {'UNDO'}
    bone: StringProperty()

    def execute(self, context):
        obj = context.active_object
        mod = rig_modifier(obj)
        bone = mod.object.data.bones.get(self.bone) if mod else None
        if not bone or not bone.use_deform or "_skin_source" not in obj:
            return {'CANCELLED'}
        select_influence(obj, bone)
        return {'FINISHED'}


class PAINT_OT_skin_bake(Operator):
    bl_idname = "paint.skin_bake"
    bl_label = "Bake to Vertex Groups"
    bl_description = "Replace native deform weights with the layered result and retain the layer stack"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event, message="Replace native bone weights with the layered result?")

    def execute(self, context):
        obj = context.active_object
        if not rig_modifier(obj) or "_skin_source" not in obj or obj.mode == 'EDIT':
            return {'CANCELLED'}
        mod = rig_modifier(obj)
        if obj.get("_skin_armature") != mod.object or obj.get("_skin_modifier") != mod.persistent_uid:
            self.report({'ERROR'}, "Restore the original rig before baking")
            return {'CANCELLED'}
        if obj.data.users > 1 or topology_error(obj):
            self.report({'ERROR'}, "Bake requires a single-user mesh with the original topology")
            return {'CANCELLED'}
        bones = mod.object.data.bones
        groups = [g for g in obj.vertex_groups if (bone := bones.get(g.name)) and bone.use_deform]
        indices = list(range(len(obj.data.vertices)))
        locked = {g.index: np.zeros(len(indices), dtype=np.float32) for g in groups if g.lock_weight}
        if locked:
            for vertex in obj.data.vertices:
                for membership in vertex.groups:
                    if membership.group in locked:
                        locked[membership.group][vertex.index] = membership.weight
            for group in groups:
                if group.index in locked and not np.array_equal(
                    locked[group.index], read_values(obj.data, obj.skin_layers.prefix + "_r" + str(group.skin_channel))
                ):
                    self.report({'ERROR'}, "Unlock the conflicting vertex group: " + group.name)
                    return {'CANCELLED'}
        for g in groups:
            if g.lock_weight:
                continue
            values = read_values(obj.data, obj.skin_layers.prefix + "_r" + str(g.skin_channel))
            g.remove(indices)
            for i in np.flatnonzero(values):
                g.add([int(i)], float(values[i]), 'REPLACE')
        obj.data.update()
        return {'FINISHED'}


def paintable_vertices(mesh):
    allowed = np.ones(len(mesh.vertices), dtype=bool)
    if mesh.use_paint_mask_vertex:
        mesh.vertices.foreach_get("select", allowed)
    if mesh.use_paint_mask:
        faces = np.zeros(len(mesh.vertices), dtype=bool)
        for face in mesh.polygons:
            if face.select and not face.hide:
                faces[list(face.vertices)] = True
        allowed &= faces
    hidden = mesh.attributes.get(".hide_vert")
    if hidden:
        hidden_verts = np.empty(len(mesh.vertices), dtype=bool)
        hidden.data.foreach_get("value", hidden_verts)
        allowed &= ~hidden_verts
    return allowed


class SkinBrush:
    def __init__(self, obj, *, stroke=False):
        self.obj = obj
        self.mesh = mesh = obj.data
        state = obj.skin_layers
        self.layer = current_layer(state)
        self.prefix = layer_key(state, self.layer) + "_"
        self.mask = state.target == 'MASK' or self.layer.is_group
        self.active = "mask" if self.mask else str(state.influence)
        self.weights = {}
        self.original = {}
        self.allowed = paintable_vertices(mesh)
        groups = channels(obj)
        if not self.mask and not any(g.skin_channel == state.influence for g in groups):
            raise ValueError("Choose a deform bone to paint")
        self.mirror_pair = None
        if stroke and obj.use_mesh_mirror_x:
            from .skin_layers_tools import mirror_geometry, mirror_name, group_for_bone
            self.mirror_x, self.mirror_pair = mirror_geometry(mesh)
            self.mirror_active = self.active
            if not self.mask:
                group = next(g for g in groups if g.skin_channel == state.influence)
                other = group_for_bone(obj, mirror_name(obj, group))
                self.mirror_active = str(other.skin_channel)
                groups = channels(obj)
        for channel in (["mask"] if self.mask else [str(g.skin_channel) for g in groups]):
            key = self.prefix + channel
            if key in mesh.attributes or channel == self.active or (self.mirror_pair is not None and channel == self.mirror_active):
                self.weights[channel] = read_values(mesh, key, 1.0 if self.mask else 0.0)
                self.original[channel] = self.weights[channel].copy() if key in mesh.attributes else None
        self.nonzero_counts = {key: np.count_nonzero(values) for key, values in self.weights.items()} if not self.mask else {}
        # Upper layers are an affine transform of the current layer result. Cache both
        # sides once per stroke; dabs only recomposite vertices inside the brush.
        ids = [g.skin_channel for g in groups]
        self.contexts = []
        node = self.layer
        while True:
            siblings = children(state, node.parent_key)
            rank = next(i for i, item in enumerate(siblings) if item.key == node.key)
            below, bottom_t = compose_stack(obj, siblings[:rank], ids)
            above, top_t = compose_stack(obj, siblings[rank + 1:], ids)
            parent_mix = None
            if node.parent_key:
                node = state.layers[layer_index(state, node.parent_key)]
                parent_mix = node.opacity * read_values(mesh, layer_key(state, node) + "_mask", 1)
            self.contexts.append((below, bottom_t, above, top_t, parent_mix))
            if parent_mix is None:
                break
        self.group_coverage = None
        self.local = {int(k): v for k, v in self.weights.items() if k != "mask"}
        if self.layer.is_group:
            self.local, t = compose_stack(obj, children(state, self.layer.key), ids)
            self.group_coverage = 1 - t
        elif self.mask:
            self.local = {j: read_values(mesh, self.prefix + str(j)) for j in ids
                          if self.prefix + str(j) in mesh.attributes}
        self.layer_mask = self.weights["mask"] if self.mask else read_values(mesh, self.prefix + "mask", 1)
        self.output = {}
        self.edge_offsets = None
        self.adjacent = None

    def build_adjacency(self):
        if self.edge_offsets is not None:
            return
        edges = np.empty(len(self.mesh.edges) * 2, dtype=np.int32)
        self.mesh.edges.foreach_get("vertices", edges)
        a, b = edges.reshape(-1, 2).T
        origins = np.concatenate((a, b))
        destinations = np.concatenate((b, a))
        counts = np.bincount(origins, minlength=len(self.mesh.vertices))
        self.edge_offsets = np.concatenate(([0], np.cumsum(counts)))
        self.adjacent = destinations[np.argsort(origins, kind='stable')]

    def neighbors(self, i):
        return self.adjacent[self.edge_offsets[i]:self.edge_offsets[i + 1]]

    def smooth(self, indices, amount):
        self.build_adjacency()
        first = self.edge_offsets[indices]
        counts = self.edge_offsets[np.asarray(indices) + 1] - first
        valid = counts != 0
        vertices = np.asarray(indices)[valid]
        if not len(vertices):
            return
        counts = counts[valid]
        stops = np.cumsum(counts)
        starts = stops - counts
        edges = np.repeat(first[valid] - starts, counts) + np.arange(stops[-1])
        neighbors = self.adjacent[edges]
        strength = amount if np.isscalar(amount) else amount[valid]
        for values in self.weights.values():
            averages = np.add.reduceat(values[neighbors], starts) / counts
            values[vertices] += strength * (averages - values[vertices])

    def paint(self, indices, amount, mode):
        if not len(indices):
            return False
        state = self.obj.skin_layers
        active = self.weights[self.active]
        previous_counts = {key: np.count_nonzero(self.weights[key][indices]) for key in self.nonzero_counts}
        if mode == 'SMOOTH':
            self.smooth(indices, amount)
        else:
            old = active[indices]
            if mode == 'ADD':
                new = np.minimum(1.0, old + amount * state.value)
            elif mode == 'SCALE':
                new = old * (1.0 + amount * (state.value - 1.0))
            else:
                new = old + amount * (state.value - old)
            others = [v for k, v in self.weights.items() if k != self.active]
            if others:
                total = sum(v[indices] for v in others)
                scale = np.minimum(1, (1 - new) / np.maximum(total, 1e-12))
                for values in others:
                    values[indices] *= scale
            active[indices] = new
        for channel, values in self.weights.items():
            self.store_changed(self.prefix + channel, values, indices)
        k = self.layer.opacity * self.layer_mask[indices]
        coverage = (self.group_coverage[indices] if self.group_coverage is not None else
                    np.minimum(sum(values[indices] for values in self.local.values()), 1.0))
        remaining = 1 - k * coverage
        result = {j: values[indices] * k for j, values in self.local.items()}
        for below, bottom_t, above, top_t, parent_mix in self.contexts:
            for j in result.keys() | below.keys() | above.keys():
                value = result.get(j, 0)
                if j in below:
                    value = value + remaining * below[j][indices]
                result[j] = top_t[indices] * value
                if j in above:
                    result[j] += above[j][indices]
            remaining = top_t[indices] * remaining * bottom_t[indices]
            if parent_mix is not None:
                mix = parent_mix[indices]
                remaining = 1 - mix * (1 - remaining)
                for value in result.values():
                    value *= mix
        for j, value in result.items():
            key = state.prefix + "_r" + str(j)
            if j not in self.output:
                self.output[j] = read_values(self.mesh, key)
            self.output[j][indices] = value
            self.store_changed(key, self.output[j], indices)
        if not self.mask:
            for key, old in previous_counts.items():
                self.nonzero_counts[key] += np.count_nonzero(self.weights[key][indices]) - old
            cache_key = (self.obj.session_uid, self.mesh.session_uid)
            members = {int(key) for key, count in self.nonzero_counts.items() if count}
            _influences[(*cache_key, self.layer.key)] = members
            parent = self.layer.parent_key
            while parent:
                _influences.pop((*cache_key, parent), None)
                parent = state.layers[layer_index(state, parent)].parent_key
        self.mesh.update_gpu_tag()
        self.obj.update_tag(refresh={'DATA'})
        return True

    def store_changed(self, key, values, indices):
        # foreach_set crosses the RNA boundary once. For a tiny dab update only its cells.
        attr = self.mesh.attributes.get(key)
        if attr and len(indices) < min(32, len(values) // 32):
            for i in indices:
                attr.data[int(i)].value = float(values[i])
        else:
            write_values(self.mesh, key, values)

    def cancel(self):
        for channel, original in self.original.items():
            key = self.prefix + channel
            if original is not None:
                write_values(self.mesh, key, original)
            elif key in self.mesh.attributes:
                self.mesh.attributes.remove(self.mesh.attributes[key])
        composite(self.obj)

    def dab(self, context, event):
        from bpy_extras.view3d_utils import (
            region_2d_to_location_3d, region_2d_to_origin_3d, region_2d_to_vector_3d,
        )
        region, view = context.region, context.region_data
        mouse = (event.mouse_region_x, event.mouse_region_y)
        origin = region_2d_to_origin_3d(region, view, mouse)
        direction = region_2d_to_vector_3d(region, view, mouse)
        evaluated = self.obj.evaluated_get(context.evaluated_depsgraph_get())
        matrix = evaluated.matrix_world
        inv = matrix.inverted_safe()
        hit, location, _, face = evaluated.ray_cast(inv @ origin, inv.to_3x3() @ direction)
        if not hit or face < 0:
            return False
        world = matrix @ location
        state = self.obj.skin_layers
        edge_world = region_2d_to_location_3d(region, view, (mouse[0] + state.radius, mouse[1]), world)
        radius = max((edge_world - world).length, 1e-10)
        vertices = evaluated.data.vertices
        # Walk along the hit surface. Disconnected or overlapping shells stay independent.
        self.build_adjacency()
        queue = list(evaluated.data.polygons[face].vertices)
        visited = set()
        picked, falloff = [], []
        while queue:
            i = int(queue.pop())
            if i in visited:
                continue
            visited.add(i)
            if not self.allowed[i]:
                continue
            distance = (matrix @ vertices[i].co - world).length
            if distance >= radius:
                continue
            picked.append(i)
            t = 1 - distance / radius
            falloff.append(t * t * (3 - 2 * t))
            queue.extend(self.neighbors(i))
        pressure = event.pressure if event.is_tablet else 1.0
        mode = 'SMOOTH' if event.shift else state.brush
        amounts = np.asarray(falloff) * state.strength * pressure
        if self.mirror_pair is None or not picked:
            return self.paint(picked, amounts, mode)
        ids = np.asarray(picked)
        # Use the side with the strongest brush sample, measured in rest-space.
        sign = 1 if self.mirror_x[ids[np.argmax(amounts)]] >= 0 else -1
        keep = ((self.mirror_x[ids] * sign >= 0) | (self.mirror_pair[ids] < 0) |
                (self.mirror_pair[ids] == ids))
        ids, amounts = ids[keep], amounts[keep]
        self.paint(ids, amounts, mode)
        dest = self.mirror_pair[ids]
        matched = (dest >= 0) & (dest != ids)
        matched &= self.allowed[np.maximum(dest, 0)]
        original = self.active
        try:
            self.active = self.mirror_active
            self.paint(dest[matched], amounts[matched], mode)
        finally:
            self.active = original
        return True


def brush_error(obj):
    mod = rig_modifier(obj)
    if not mod or "_skin_source" not in obj:
        return "Select Skin Tools on a mesh with one Armature modifier"
    if obj.get("_skin_modifier") != mod.persistent_uid or obj.get("_skin_armature") != mod.object:
        return "Restore the original Armature modifier and rig before painting"
    state = obj.skin_layers
    node = current_layer(state)
    while node:
        if not node.enabled:
            return "Enable the active layer and its parent groups before painting"
        node = state.layers[layer_index(state, node.parent_key)] if node.parent_key else None
    return topology_error(obj)


class PAINT_OT_skin_flood(Operator):
    bl_idname = "paint.skin_flood"
    bl_label = "Flood Layer Target"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        if (error := brush_error(obj)):
            self.report({'WARNING'}, error)
            return {'CANCELLED'}
        unique_storage(obj)
        try:
            brush = SkinBrush(obj)
        except ValueError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        if not brush.paint(np.flatnonzero(brush.allowed), obj.skin_layers.strength, obj.skin_layers.brush):
            return {'CANCELLED'}
        return {'FINISHED'}


class PAINT_OT_skin_layer_stroke(Operator):
    bl_idname = "paint.skin_layer_stroke"
    bl_label = "Paint Skin Layer"
    bl_options = {'UNDO', 'BLOCKING'}

    def invoke(self, context, event):
        obj = context.active_object
        if context.mode != 'PAINT_WEIGHT' or not context.region_data:
            return {'CANCELLED'}
        if (error := brush_error(obj)):
            self.report({'WARNING'}, error)
            return {'CANCELLED'}
        evaluated = obj.evaluated_get(context.evaluated_depsgraph_get())
        marker = evaluated.data.attributes.get(obj.skin_layers.prefix)
        if marker and len(marker.data) == len(obj.data.vertices):
            ids = np.empty(len(marker.data), dtype=np.int32)
            marker.data.foreach_get("value", ids)
            matched = np.array_equal(ids, np.arange(1, len(ids) + 1, dtype=np.int32))
        else:
            matched = False
        if not matched:
            self.report({'WARNING'}, "Painting requires a viewport mesh with original vertex correspondence")
            return {'CANCELLED'}
        unique_storage(obj)
        try:
            self.stroke = SkinBrush(obj, stroke=True)
        except ValueError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        self.button = event.type
        self.changed = self.stroke.dab(context, event)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'ESC', 'RIGHTMOUSE'} or context.active_object != self.stroke.obj:
            self.cancel(context)
            return {'CANCELLED'}
        if event.type == self.button and event.value == 'RELEASE':
            return {'FINISHED'} if self.changed else {'CANCELLED'}
        if event.type == 'MOUSEMOVE':
            self.changed |= self.stroke.dab(context, event)
            context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        self.stroke.cancel()


classes = (
    SkinWeightLayer, SkinWeightSettings, PAINT_OT_skin_source, PAINT_OT_skin_layer_edit,
    PAINT_OT_skin_influence, PAINT_OT_skin_bake, PAINT_OT_skin_flood, PAINT_OT_skin_layer_stroke,
)


_subscription_owner = object()


def synchronize_tools():
    for window in bpy.context.window_manager.windows:
        with bpy.context.temp_override(window=window):
            context = bpy.context
            obj = context.active_object
            if context.mode != 'PAINT_WEIGHT' or not obj:
                continue
            tool = context.workspace.tools.from_space_view3d_mode('PAINT_WEIGHT', create=False)
            is_layer_brush = tool and tool.idname == "builtin.skin_layer_brush"
            layered = "_skin_source" in obj
            if bool(is_layer_brush) != layered:
                activate_paint_tool(context, obj, layered)


def queue_tool_sync():
    if not bpy.app.background and not bpy.app.timers.is_registered(synchronize_tools):
        bpy.app.timers.register(synchronize_tools)


@bpy.app.handlers.persistent
def subscribe_tools(_):
    bpy.msgbus.clear_by_owner(_subscription_owner)
    for key in ((bpy.types.LayerObjects, "active"), (bpy.types.Window, "workspace"), (bpy.types.Object, "mode")):
        bpy.msgbus.subscribe_rna(key=key, owner=_subscription_owner, args=(), notify=queue_tool_sync)
    queue_tool_sync()


def register():
    bpy.types.Object.skin_layers = PointerProperty(type=SkinWeightSettings)
    for handlers in (bpy.app.handlers.load_post, bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        handlers.append(clear_influence_cache)
    bpy.app.handlers.load_post.append(subscribe_tools)
    subscribe_tools(None)


def unregister():
    for handlers in (bpy.app.handlers.load_post, bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        handlers.remove(clear_influence_cache)
    clear_influence_cache()
    bpy.app.handlers.load_post.remove(subscribe_tools)
    bpy.msgbus.clear_by_owner(_subscription_owner)
    if bpy.app.timers.is_registered(synchronize_tools):
        bpy.app.timers.unregister(synchronize_tools)
    del bpy.types.Object.skin_layers
