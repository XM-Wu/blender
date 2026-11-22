# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Mesh attributes and rig bindings used by layered skin weights."""

import uuid

import bpy
import numpy as np


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
    if marker is None or len(mesh.vertices) != state.vertex_count:
        return "Mesh topology changed; restore the source topology before painting or baking"
    if not original_vertex_order(marker, state.vertex_count):
        return "Vertex order changed; restore the source topology before painting or baking"
    return None


def original_vertex_order(marker, vertex_count):
    if (marker is None or marker.domain != 'POINT' or marker.data_type != 'INT' or
            len(marker.data) != vertex_count):
        return False
    ids = np.empty(vertex_count, dtype=np.int32)
    marker.data.foreach_get("value", ids)
    return np.array_equal(ids, np.arange(1, vertex_count + 1, dtype=np.int32))
