# SPDX-License-Identifier: GPL-2.0-or-later
"""ngSkinTools 2 JSON interchange for layered skin weights."""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy.types import Operator
from bpy_extras.io_utils import ExportHelper, ImportHelper
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from mathutils.geometry import barycentric_transform

from . import skin_layers as skin
from .skin_layers_tools import group_for_bone


def maya_points(points, matrix, unit):
    """ngSkinTools stores world space coordinates, in Maya centimeters (Y up)."""
    m = np.asarray(matrix, dtype=np.float64)
    world = np.asarray(points) @ m[:3, :3].T + m[:3, 3]
    return world[..., [0, 2, 1]] * np.array((unit, unit, -unit))


def positions(obj, unit):
    coords = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
    obj.data.vertices.foreach_get("co", coords)
    return maya_points(coords.reshape(-1, 3), obj.matrix_world, unit)


def triangles(mesh):
    mesh.calc_loop_triangles()
    verts = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int32)
    loops = np.empty_like(verts)
    mesh.loop_triangles.foreach_get("vertices", verts)
    mesh.loop_triangles.foreach_get("loops", loops)
    return verts.reshape(-1, 3), loops.reshape(-1, 3)


def corner_uvs(mesh):
    layer = mesh.uv_layers.active
    if layer is None:
        raise ValueError(f"{mesh.name} needs an active UV map")
    values = np.empty(len(mesh.loops) * 2, dtype=np.float32)
    layer.uv.foreach_get("vector", values)
    if not np.isfinite(values).all():
        raise ValueError(f"{mesh.name} contains invalid UVs")
    return values.reshape(-1, 2)


def export_data(obj, unit):
    state, mesh = obj.skin_layers, obj.data
    rig = skin.rig_modifier(obj).object
    verts, loops = triangles(mesh)
    mesh_data = dict(triangles=verts.ravel().tolist(), vertPositions=positions(obj, unit).ravel().tolist())
    extra = {"groups": []}
    if mesh.uv_layers.active:
        extra["triangleUVs"] = corner_uvs(mesh)[loops].ravel().tolist()
    influences = []
    channel_index = {}
    for group in skin.channels(obj):
        bone = rig.data.bones[group.name]
        path = "|" + "|".join(p.name for p in [*reversed(bone.parent_recursive), bone])
        channel_index[group.skin_channel] = len(influences)
        pivot = maya_points(bone.head_local, rig.matrix_world, unit).tolist()
        influences.append(dict(index=len(influences), path=path, pivot=pivot, labelText="", labelSide=0))
        if "xbiped_legacy_name" in bone:
            extra.setdefault("legacyBones", {})[bone.name] = bone["xbiped_legacy_name"]
    if not influences:
        raise ValueError("The source mesh has no skin influences")
    layers = []
    ids = {layer.key: i for i, layer in enumerate(state.layers, 1)}
    for layer in state.layers:
        prefix = skin.layer_key(state, layer) + "_"
        children = [ids[l.key] for l in skin.children(state, layer.key)]
        if layer.is_group:
            extra["groups"].append(ids[layer.key])
        weights = {str(i): skin.read_values(mesh, prefix + str(ch)).tolist()
                   for ch, i in channel_index.items() if prefix + str(ch) in mesh.attributes}
        layers.append(dict(id=ids[layer.key], name=layer.name, enabled=layer.enabled, opacity=layer.opacity,
                           parentId=ids.get(layer.parent_key), children=children,
                           index=next(i for i, l in enumerate(skin.children(state, layer.parent_key)) if l == layer),
                           paintTarget=None, paintTargets=[], mask=skin.read_values(mesh, prefix + "mask", 1).tolist(),
                           influences=weights, effects=dict(mirrorMask=False, mirrorWeights=False,
                                                            mirrorDq=False, mirrorDirection=1)))
    return dict(mesh=mesh_data, influences=influences, layers=layers, xblender=extra)


def array(value, width, label, integer=False):
    data = np.asarray(value)
    if data.size % width or (integer and data.size and data.dtype.kind not in 'iu'):
        raise ValueError(f"Invalid {label}")
    data = data.astype(np.int64 if integer else np.float64).reshape(-1, width)
    if not np.isfinite(data).all():
        raise ValueError(f"Invalid {label}")
    return data


def geometry(data):
    coords = array(data["mesh"]["vertPositions"], 3, "source vertex positions")
    tris = array(data["mesh"]["triangles"], 3, "source triangles", integer=True)
    if not len(coords) or np.any(tris < 0) or np.any(tris >= len(coords)):
        raise ValueError("Invalid source vertex or triangle indices")
    return coords, tris


def surface_map(source, tris, surface, queries, target_positions):
    """One interpolation per vertex. Resolve equal UV hits by 3D distance, then triangle order."""
    valid = np.flatnonzero(np.linalg.norm(np.cross(surface[:, 1] - surface[:, 0],
                                                   surface[:, 2] - surface[:, 0]), axis=1) > 1e-18)
    if not len(valid):
        raise ValueError("No non-degenerate source triangles for this matching mode")
    corners = surface[valid].reshape(-1, 3)
    tree = BVHTree.FromPolygons(corners.tolist(), np.arange(len(corners)).reshape(-1, 3).tolist(), all_triangles=True)
    basis = tuple(Vector(v) for v in np.eye(3))
    indices = np.empty((len(queries), 3), dtype=np.int64)
    factors = np.empty((len(queries), 3), dtype=np.float64)
    for i, samples in enumerate(queries):
        best = None
        for sample in samples:
            hit = tree.find_nearest(sample)
            if hit[2] is None:
                raise ValueError(f"No source triangle near vertex {i}")
            hits = (hit,) if target_positions is None else tree.find_nearest_range(sample, hit[3] + 1e-7)
            for point, _, face, distance in hits or (hit,):
                face = valid[face]
                bary = np.clip(barycentric_transform(point, *map(Vector, surface[face]), *basis), 0, 1)
                total = bary.sum()
                if not np.isfinite(total) or total == 0:
                    raise ValueError("Cannot interpolate a degenerate source triangle")
                bary /= total
                spatial = 0 if target_positions is None else np.linalg.norm(
                    bary @ source[tris[face]] - target_positions[i])
                key = (round(distance, 7), spatial, face)
                if best is None or key < best:
                    best = key
                    indices[i], factors[i] = tris[face], bary
        if best is None:
            raise ValueError(f"Target vertex {i} has no UV coordinates")
    return indices, factors


def vertex_map(data, obj, mode, unit, uv_source):
    source, tris = geometry(data)
    if mode == 'vertexId':
        if len(source) != len(obj.data.vertices):
            raise ValueError("Vertex ID matching requires the same vertex count and order")
        if (obj.skin_layers.prefix and skin.topology_error(obj) and
                not np.allclose(source, positions(obj, unit), rtol=0, atol=1e-6)):
            raise ValueError("Vertex order cannot be verified after a topology edit; "
                             "choose Closest Surface Point or UV Space")
        return len(source), None
    target = positions(obj, unit)
    if mode == 'closestPoint':
        mapping = surface_map(source, tris, source[tris], target[:, None, :], None)
    elif mode == 'uvSpace':
        raw = data.get("xblender", {}).get("triangleUVs")
        if raw is not None:
            uv = array(raw, 2, "source UV coordinates")
            if len(uv) != tris.size:
                raise ValueError("Source UV count does not match triangles")
            uv = uv.reshape(-1, 3, 2)
        else:
            if uv_source is None or uv_source.type != 'MESH' or len(uv_source.data.vertices) != len(source):
                raise ValueError("File contains no UVs: choose the original source mesh with matching vertex IDs")
            tris, loops = triangles(uv_source.data)
            uv = corner_uvs(uv_source.data)[loops]
        surface = np.pad(uv, ((0, 0), (0, 0), (0, 1)))
        samples = [set() for _ in target]
        for loop, (u, v) in zip(obj.data.loops, corner_uvs(obj.data)):
            samples[loop.vertex_index].add((float(u), float(v), 0.0))
        mapping = surface_map(source, tris, surface, samples, target)
    else:
        raise ValueError("Unknown matching method")
    return len(source), mapping


def transfer(values, count, mapping):
    vec = np.asarray(values, dtype=np.float32)
    if vec.shape != (count,) or not np.isfinite(vec).all() or np.any(vec < 0) or np.any(vec > 1.000001):
        raise ValueError("Invalid layer weight or mask array")
    if mapping is None:
        return vec
    inds, bary = mapping
    return np.einsum('ij,ij->i', vec[inds], bary).astype(np.float32)


def plan_import(data, obj, mode, unit, uv_source):
    count, mapping = vertex_map(data, obj, mode, unit, uv_source)
    bones = skin.rig_modifier(obj).object.data.bones
    aliases = {b["xbiped_legacy_name"]: b for b in bones if "xbiped_legacy_name" in b}
    source_aliases = data.get("xblender", {}).get("legacyBones", {})
    influences = {}
    for influence in data["influences"]:
        index = str(influence["index"])
        if index in influences:
            raise ValueError(f"Duplicate influence {index}")
        name = (influence.get("path") or influence["name"]).split("|")[-1]
        short = name.rsplit(":", 1)[-1]
        bone = bones.get(name) or bones.get(short) or aliases.get(short)
        if bone is None and short in source_aliases:
            legacy = source_aliases[short]
            bone = aliases.get(legacy) or bones.get(legacy)
        influences[index] = (name, bone.name if bone and bone.use_deform else None)
    nodes = {l["id"]: l for l in data["layers"]}
    if not nodes or len(nodes) != len(data["layers"]) or None in nodes:
        raise ValueError("Missing layers or duplicate layer IDs")
    children = {}
    for lid, node in nodes.items():
        parent = node["parentId"]
        if parent is not None and parent not in nodes:
            raise ValueError("Unknown parent layer")
        children.setdefault(parent, []).append(lid)
    groups = set(data.get("xblender", {}).get("groups", []))

    def ordered(parent):
        return sorted(children.get(parent, ()), key=lambda lid: nodes[lid]["index"])
    pending = ordered(None)[::-1]
    plan = []
    bound = {}
    while pending:
        lid = pending.pop()
        node = nodes[lid]
        kids = ordered(lid)
        if "children" in node and set(node["children"]) != set(kids):
            raise ValueError("Inconsistent layer hierarchy")
        pending.extend(reversed(kids))
        is_group = bool(kids) or lid in groups
        if node.get("dq") or any(node.get("effects", {}).get(k) for k in ("mirrorWeights", "mirrorMask", "mirrorDq")):
            raise ValueError(f"Layer {node['name']} uses unsupported DQ weights or live mirror effects")
        opacity = float(node["opacity"])
        if not 0 <= opacity <= 1 or not isinstance(node["name"], str):
            raise ValueError("Invalid layer name or opacity")
        weights = {}
        for index, values in node["influences"].items():
            name, target = influences[index]
            vec = transfer(values, count, mapping)
            if target is None:
                if np.any(vec):
                    raise ValueError(f"No matching deform bone for {name}")
                continue
            if target in weights or bound.setdefault(target, index) != index:
                raise ValueError(f"Ambiguous bone mapping for {target}")
            weights[target] = vec
        if is_group and any(np.any(vec) for vec in weights.values()):
            raise ValueError(f"Group {node['name']} also has its own weights")
        mask = transfer(node["mask"], count, mapping) if node.get("mask") else None
        plan.append((lid, node["parentId"], node["name"], bool(node["enabled"]), opacity, is_group, mask, weights))
    if len(plan) != len(nodes):
        raise ValueError("Cyclic layer hierarchy")
    return plan


def check_binding(obj):
    mod = skin.rig_modifier(obj)
    if mod is None:
        raise ValueError("Select a mesh bound to one armature")
    if obj.skin_layers.prefix:
        if obj.get("_skin_armature") != mod.object or obj.get("_skin_modifier") != mod.persistent_uid:
            raise ValueError("Restore the original armature binding")
    return mod


def layer_path(state, layer):
    """Name and occurrence at each level, independent of regenerated layer IDs."""
    path = []
    while layer:
        siblings = skin.children(state, layer.parent_key)
        matches = [s.key for s in siblings if s.name == layer.name and s.is_group == layer.is_group]
        path.append((layer.name, layer.is_group, matches.index(layer.key)))
        layer = state.layers[skin.layer_index(state, layer.parent_key)] if layer.parent_key else None
    return tuple(reversed(path))


def append_layers(context, obj, plan, *, replace=False):
    mod = check_binding(obj)
    state, mesh = obj.skin_layers, obj.data
    if not obj.is_editable or not mesh.is_editable or obj.mode == 'EDIT':
        raise ValueError("The target mesh must be editable and outside Edit Mode")
    # All mapping and file validation has finished. Keep enough of the original
    # stack to undo partial attribute/group creation if Blender rejects a write.
    previous = {k: obj[k] for k in obj.keys() if k.startswith("_skin_")}
    settings = {k: getattr(state, k) for k in ("prefix", "vertex_count", "index", "influence", "bone_index")}
    attrs = set(mesh.attributes.keys())
    channels = {g.name: g.skin_channel for g in obj.vertex_groups}
    group_index = obj.vertex_groups.active_index
    previous_count = len(state.layers)
    old_prefix = state.prefix
    replace = bool(old_prefix and (replace or skin.topology_error(obj)))
    previous_path = layer_path(state, skin.current_layer(state)) if replace else None
    try:
        if replace:
            # Stage a new namespace. The old layers have no attributes in it, so
            # they contribute nothing during compositing and remain intact on failure.
            state.prefix = ".skin_" + skin.token()
            state.vertex_count = len(mesh.vertices)
            skin.copy_marker(mesh, None, state.prefix)
            obj["_skin_vertex_count"] = state.vertex_count
        elif state.prefix:
            skin.unique_storage(obj)
        else:
            skin.initialize(obj, mod)
        keys = {}
        for lid, parent, name, enabled, opacity, group, mask, weights in plan:
            layer = state.layers.add()
            layer.name, layer.key = name, skin.token()
            layer.parent_key, layer.is_group = keys.get(parent, ""), group
            layer["enabled"], layer["opacity"] = enabled, opacity
            keys[lid] = layer.key
            prefix = skin.layer_key(state, layer) + "_"
            skin.write_values(mesh, prefix + "mask", np.ones(len(mesh.vertices),
                              dtype=np.float32) if mask is None else mask)
            for bone, vec in weights.items():
                channel = group_for_bone(obj, bone).skin_channel
                skin.write_values(mesh, prefix + str(channel), vec)
        obj["_skin_source"] = state.prefix
        skin.ensure_paint_influence(obj, skin.channels(obj))
        state.index = len(state.layers) - 1
        skin.composite(obj)
    except Exception:
        while len(state.layers) > previous_count:
            state.layers.remove(len(state.layers) - 1)
        for key in set(mesh.attributes.keys()) - attrs:
            mesh.attributes.remove(mesh.attributes[key])
        for group in reversed(list(obj.vertex_groups)):
            if group.name not in channels:
                obj.vertex_groups.remove(group)
            elif group.skin_channel != channels[group.name]:
                group.skin_channel = channels[group.name]
        if group_index >= 0:
            obj.vertex_groups.active_index = group_index
        for key, value in settings.items():
            state[key] = value
        for key in list(obj.keys()):
            if key.startswith("_skin_") and key not in previous:
                del obj[key]
        for key, value in previous.items():
            obj[key] = value
        if previous_count and not replace:
            skin.composite(obj)
        raise
    if replace:
        for _ in range(previous_count):
            state.layers.remove(0)
        state.index = next((i for i, layer in enumerate(state.layers) if layer_path(state, layer) == previous_path),
                           len(state.layers) - 1)
        if not any(other != obj and other.type == 'MESH' and other.data == mesh and
                   other.skin_layers.prefix == old_prefix for other in bpy.data.objects):
            for key in attrs:
                if key == old_prefix or key.startswith(old_prefix + "_"):
                    mesh.attributes.remove(mesh.attributes[key])
    skin.activate_paint_tool(context, obj, True)


class PAINT_OT_skin_export_layers(Operator, ExportHelper):
    bl_idname = "paint.skin_export_layers"
    bl_label = "Export Skin Layers"
    bl_description = "Export the full skin layer hierarchy, masks and weights as ngSkinTools 2 JSON"
    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return skin.rig_modifier(obj) is not None and bool(obj.skin_layers.layers) and obj.mode != 'EDIT'

    def execute(self, context):
        obj = context.active_object
        temporary = None
        try:
            check_binding(obj)
            edited = skin.topology_error(obj) is not None
            if edited:
                # Mesh edit operations carry point attributes to the edited vertices.
                # Export those surviving values with the geometry they now belong to.
                for layer in obj.skin_layers.layers:
                    mask = obj.data.attributes.get(skin.layer_key(obj.skin_layers, layer) + "_mask")
                    if mask is None or mask.domain != 'POINT' or mask.data_type != 'FLOAT':
                        raise ValueError(f"Layer data missing on the edited mesh ({layer.name}); "
                                         "import the JSON saved before editing topology")
            data = export_data(obj, context.scene.unit_settings.scale_length * 100)
            path = Path(self.filepath)
            with NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf8") as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            os.replace(temporary, path)
        except (OSError, ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
        suffix = " from the current mesh topology" if edited else ""
        self.report({'INFO'}, f"Exported {len(data['layers'])} skin layers{suffix}")
        return {'FINISHED'}


class PAINT_OT_skin_import_layers(Operator, ImportHelper):
    bl_idname = "paint.skin_import_layers"
    bl_label = "Import Skin Layers"
    bl_description = "Transfer layers onto the current mesh; replace the old stack when its topology has changed"
    bl_options = {'UNDO'}
    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})
    mode: EnumProperty(name="Vertex Matching", default='closestPoint', items=(
        ('closestPoint', "Closest Surface Point", "Interpolate on the closest source triangle in 3D"),
        ('uvSpace', "UV Space", "Interpolate on triangles of the active UV maps"),
        ('vertexId', "Vertex ID", "Copy weights by index for meshes with the same vertex order"),
    ))
    replace_existing: BoolProperty(name="Replace Existing Layers", default=False,
                                  description="Use the imported stack instead of appending above the existing layers")
    source_uv_mesh: StringProperty(name="Source UV Mesh",
                                   description="Original mesh with matching source vertex order for files without UVs")

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return skin.rig_modifier(obj) is not None and obj.is_editable and obj.data.is_editable and obj.mode != 'EDIT'

    def draw(self, context):
        obj = context.active_object
        if obj and obj.type == 'MESH' and obj.skin_layers.prefix:
            if skin.topology_error(obj):
                self.layout.label(text="Topology changed: import replaces existing layers", icon='INFO')
            else:
                self.layout.prop(self, "replace_existing")
        self.layout.prop(self, "mode")
        if self.mode == 'uvSpace':
            self.layout.prop_search(self, "source_uv_mesh", bpy.data, "objects", text="Source UV Mesh")

    def execute(self, context):
        obj = context.active_object
        try:
            check_binding(obj)
            with open(self.filepath, encoding="utf-8-sig") as stream:
                data = json.load(stream)
            source = bpy.data.objects.get(self.source_uv_mesh) if self.source_uv_mesh else None
            unit = context.scene.unit_settings.scale_length * 100
            plan = plan_import(data, obj, self.mode, unit, source)
            append_layers(context, obj, plan, replace=self.replace_existing)
        except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError, AttributeError, RuntimeError) as exc:
            self.report({'ERROR'}, f"Could not import skin layers: {exc}")
            return {'CANCELLED'}
        # A topology edit can discard the selection but leave paint masking enabled.
        # Retain nonempty selections and the authored masks on every skin layer.
        message = f"Imported {len(plan)} skin layers"
        empty = skin.empty_selection_mask(obj.data)
        if empty:
            setattr(obj.data, empty, False)
            label = "face" if empty == "use_paint_mask" else "vertex"
            message += f"; turned off empty {label} selection mask"
        self.report({'INFO'}, message)
        return {'FINISHED'}


classes = (PAINT_OT_skin_import_layers, PAINT_OT_skin_export_layers)
