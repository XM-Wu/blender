# SPDX-License-Identifier: GPL-2.0-or-later
"""Bake a volumetric gel simulation and play it back from embedded mesh data."""

import math

import bmesh
import bpy
import numpy as np
from bpy.props import (
    BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, PointerProperty, StringProperty,
)
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from _xblender_gel import Gel, contact, prism_tets

CACHE_TAG = "_xblender_gel_cache"


def transform(points, matrix):
    matrix = np.asarray(matrix)
    return points @ matrix[:3, :3].T + matrix[:3, 3]


def barycentric(points, triangles):
    a = triangles[:, 1] - triangles[:, 0]
    b = triangles[:, 2] - triangles[:, 0]
    delta = points - triangles[:, 0]

    def dot(x, y):
        return np.einsum("ij,ij->i", x, y)

    aa, ab, bb = dot(a, a), dot(a, b), dot(b, b)
    da, db = dot(delta, a), dot(delta, b)
    det = aa * bb - ab * ab
    u = (bb * da - ab * db) / det
    v = (aa * db - ab * da) / det
    return np.column_stack((1 - u - v, u, v))


def ray_samples(vertices, faces, origin, directions):
    tree = BVHTree.FromPolygons(vertices, faces, all_triangles=True)
    hits = np.empty_like(directions)
    indices = np.empty(len(directions), dtype=np.int32)
    start = Vector(origin)
    for i, direction in enumerate(directions):
        point, _normal, index, _distance = tree.ray_cast(start, Vector(direction))
        if point is None:
            raise ValueError("Cannot sample the gel surface from its center")
        hits[i] = point
        indices[i] = index
    triangles = faces[indices]
    return hits, triangles, barycentric(hits, vertices[triangles])


def smooth_shell(vertices, faces):
    """One Loop subdivision for transferring motion to the render surface."""
    count = len(vertices)
    oriented = faces[:, [[0, 1], [1, 2], [2, 0]]].reshape(-1, 2)
    edges, index = np.unique(np.sort(oriented, axis=1), axis=0, return_inverse=True)
    index = index.ravel()
    a, b = edges.T
    degree = np.bincount(edges.ravel(), minlength=count)
    beta = (.625 - (.375 + .25 * np.cos(2 * math.pi / degree)) ** 2) / degree
    stencil = np.zeros((count + len(edges), count))
    ids = np.arange(count)
    stencil[ids, ids] = 1 - degree * beta
    stencil[a, b], stencil[b, a] = beta[a], beta[b]
    mids = count + np.arange(len(edges))
    stencil[mids, a] = stencil[mids, b] = 3. / 8.
    np.add.at(stencil, (count + index, faces[:, [2, 0, 1]].ravel()), 1. / 8.)
    m, n, p = (count + index.reshape(-1, 3)).T
    i, j, k = faces.T
    refined = np.concatenate((np.column_stack((i, m, p)), np.column_stack((j, n, m)),
                              np.column_stack((k, p, n)), np.column_stack((m, n, p))))
    return stencil, refined


def pin_weights(obj, cfg):
    if not cfg.pin_group:
        return None
    group = obj.vertex_groups.get(cfg.pin_group)
    if group is None:
        raise ValueError("The pin group is missing")
    return np.array([next((g.weight for g in v.groups if g.group == group.index), 0.)
                     for v in obj.data.vertices])


def simulation_mesh(obj, rest, cfg, pinning):
    mesh = obj.data
    mesh.calc_loop_triangles()
    faces = np.array([face.vertices[:] for face in mesh.loop_triangles], dtype=np.int32)
    if len(faces) < 4 or not np.isfinite(rest).all():
        raise ValueError("Gel needs a closed mesh with nonzero volume")
    edges = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    unique_edges, inverse, counts = np.unique(np.sort(edges, axis=1), axis=0, return_inverse=True, return_counts=True)
    if (len(np.unique(faces)) != len(rest) or np.any(counts != 2) or
            np.any(np.bincount(inverse.ravel(), np.sign(edges[:, 1] - edges[:, 0])))):
        raise ValueError("Gel needs a closed, consistently wound surface without loose vertices")
    # The ray sampler needs one boundary; nested closed shells can have valid
    # edge incidence and winding while projecting onto the same directions.
    neighbors = [[] for _ in range(len(rest))]
    for a, b in unique_edges:
        neighbors[a].append(b)
        neighbors[b].append(a)
    visited = {0}
    pending = [0]
    while pending:
        for neighbor in neighbors[pending.pop()]:
            if neighbor not in visited:
                visited.add(neighbor)
                pending.append(neighbor)
    if len(visited) != len(rest):
        raise ValueError("Use one connected closed surface per gel")
    local = rest - rest.mean(axis=0)
    tri = local[faces]
    signed = np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2]))
    if abs(signed.sum()) <= 1e-12 * np.ptp(local, axis=0).max() ** 3:
        raise ValueError("Gel rest volume is zero")
    center = np.einsum("i,ij->j", signed, tri.sum(axis=1)) / (4 * signed.sum())
    offsets = local - center
    tri = offsets[faces]
    if np.any(np.sign(signed.sum()) * np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])) <= 0):
        raise ValueError("The entire gel surface must be visible from its volume centroid")
    center += rest.mean(axis=0)

    ico = bmesh.new()
    try:
        bmesh.ops.create_icosphere(ico, subdivisions=int(cfg.resolution), radius=1.)
        ico.verts.index_update()
        directions = np.array([v.co[:] for v in ico.verts])
        shell_faces = np.array([[v.index for v in f.verts] for f in ico.faces])
    finally:
        ico.free()
    shell, skin_triangles, skin_weights = ray_samples(offsets, faces, np.zeros(3), directions)
    layers = 2
    nodes = np.concatenate([np.zeros((1, 3))] + [shell * (i / layers) for i in range(1, layers + 1)])
    pinned = np.zeros(len(nodes), dtype=bool)
    if pinning is not None:
        pinned[-len(shell):] = (pinning[skin_triangles] * skin_weights).sum(axis=1) > .5
    unit = offsets / np.linalg.norm(offsets, axis=1, keepdims=True)
    stencil, refined = smooth_shell(shell, shell_faces)
    _, attach, blend = ray_samples(stencil @ shell, refined, np.zeros(3), unit)
    surface = shell_faces + len(nodes) - len(shell)
    return nodes + center, prism_tets(shell_faces, len(shell), layers), pinned, attach, blend, surface, stencil


class GelSettings(bpy.types.PropertyGroup):
    enabled: BoolProperty(name="Volumetric Gel")
    resolution: EnumProperty(name="Simulation Mesh", items=(('2', "Coarse", "85 nodes, 320 tetrahedra"),
                                                           ('3', "Fine", "325 nodes, 1280 tetrahedra")), default='3')
    settle_frames: IntProperty(name="Settle Frames", description="Simulate this many frames before playback begins",
                                         default=0, min=0, max=10000)
    density: FloatProperty(name="Density", description="Mass per unit volume in kg/m³",
                           default=1000, min=1, soft_max=2000)
    shear: FloatProperty(name="Shear Modulus", description="Resistance to a change in shape, in Pa",
                         default=1800, min=1, soft_max=20000)
    bulk: FloatProperty(name="Bulk Modulus", description="Resistance to a change in volume, in Pa",
                        default=22000, min=10, soft_max=100000)
    damping: FloatProperty(name="Velocity Damping", description="Velocity decay per second",
                           default=2.0, min=0, soft_max=10)
    friction: FloatProperty(name="Contact Friction", default=.55, min=0, soft_max=1)
    floor: FloatProperty(name="Ground Height", description="World-space Z of the contact plane",
                         default=0, subtype='DISTANCE')
    pin_group: StringProperty(name="Pin Group", description="Fix surface nodes with interpolated weight above 0.5")
    pin_target: PointerProperty(name="Pin Target", type=bpy.types.Object,
                                description="Move the pin group relative to this object at the start of the bake")
    collider: PointerProperty(name="Capsule Collider", type=bpy.types.Object)
    start: FloatVectorProperty(name="Capsule Start", description="First endpoint in collider local coordinates",
                               subtype='TRANSLATION', default=(0, 0, 0))
    end: FloatVectorProperty(name="Capsule End", description="Second endpoint in collider local coordinates",
                             subtype='TRANSLATION', default=(0, 0, 1))
    radius: FloatProperty(name="Capsule Radius", description="World-space radius of the collision capsule",
                          default=.1, min=.0001, subtype='DISTANCE')


def input_samples(context, obj, cfg):
    graph = context.evaluated_depsgraph_get()
    matrix = obj.evaluated_get(graph).matrix_world.copy()
    if abs(matrix.to_3x3().determinant()) < 1e-12:
        raise ValueError("The gel object has a singular transform")
    ends = None
    if cfg.collider:
        if cfg.collider == obj:
            raise ValueError("Choose a separate collider object")
        ends = transform(np.array((cfg.start[:], cfg.end[:])), cfg.collider.evaluated_get(graph).matrix_world)
    pin_matrix = None
    if cfg.pin_target:
        if obj == cfg.pin_target:
            raise ValueError("Choose a separate pin target")
        pin_matrix = cfg.pin_target.evaluated_get(graph).matrix_world.copy()
        if abs(pin_matrix.to_3x3().determinant()) < 1e-12:
            raise ValueError("The pin target has a singular transform")
    return matrix, ends, pin_matrix


def solve(context, obj, cfg):
    scene = context.scene
    start, end = scene.frame_start - cfg.settle_frames, scene.frame_end
    count = len(obj.data.vertices)
    if count == 0 or (end - start + 1) * count >= 2 ** 31:
        raise ValueError("Unsupported gel mesh or bake range")
    scene.frame_set(start)
    matrix, previous, pin_matrix = input_samples(context, obj, cfg)
    source = np.empty(3 * count, dtype=np.float32)
    obj.data.vertices.foreach_get("co", source)
    source = source.reshape(-1, 3).astype(float)
    # Blender stores positions and scene acceleration in scene units. The
    # material law uses meters, Pa and kg/m³; convert back only for playback.
    meters = scene.unit_settings.scale_length if scene.unit_settings.system != 'NONE' else 1.
    world = transform(source, matrix) * meters
    floor = cfg.floor * meters
    radius = cfg.radius * meters
    if previous is not None:
        previous = previous * meters
    if len(world) and world[:, 2].min() < floor - 1e-5 * meters:
        raise ValueError("Start with the gel above the ground or lower Ground Height")
    pinning = pin_weights(obj, cfg)
    nodes, tets, pinned, attach, weights, surface, stencil = simulation_mesh(obj, world, cfg, pinning)
    if pin_matrix is not None:
        if not np.any(pinned):
            raise ValueError("The pin target needs a pin group covering at least one simulation node")
        inv_target = pin_matrix.inverted()
        pin_local = transform(nodes[pinned] / meters, inv_target)
        skin_mask = pinning > 0
        skin_local = transform(world[skin_mask] / meters, inv_target)
    gel = Gel(nodes, tets, pinned, cfg.density, cfg.shear, cfg.bulk, cfg.damping, cfg.friction, surface)
    gravity = np.array(scene.gravity) * meters if scene.use_gravity else np.zeros(3)
    seconds = scene.render.fps_base / scene.render.fps
    samples = np.empty((end - start + 1, count, 3), dtype=np.float32)
    samples[0].fill(0)
    for frame in range(start + 1, end + 1):
        scene.frame_set(frame)
        matrix, current, pin_matrix = input_samples(context, obj, cfg)
        if current is not None:
            current = current * meters
        targets = None
        if pin_matrix is not None:
            targets = transform(pin_local, pin_matrix) * meters
            if targets[:, 2].min() < floor - 1e-5 * meters:
                raise ValueError("The animated pins move below the ground")
        gel.advance(seconds, gravity, floor, previous, current, radius, targets)
        shell = (gel.x - gel.rest)[-stencil.shape[1]:]
        positions = world + np.einsum("ij,ijk->ik", weights, (stencil @ shell)[attach])
        if pin_matrix is not None:
            # Subdivision smooths the cage displacement; keep the detailed grip
            # attached exactly at weight one and blend its feathered border.
            goal = transform(skin_local, pin_matrix) * meters
            positions[skin_mask] += pinning[skin_mask, None] * (goal - positions[skin_mask])
        # Resolve remaining contact between simulation nodes on the detailed skin.
        if current is not None:
            _, n, d = contact(positions, *current)
            positions += np.maximum(radius - d, 0)[:, None] * n
        positions[:, 2] = np.maximum(positions[:, 2], floor)
        samples[frame - start] = transform(positions / meters, matrix.inverted()) - source
        context.window_manager.progress_update(frame - start)
        previous = current
    return start, samples


def cache_modifier(obj):
    return next((m for m in obj.modifiers if m.type == 'NODES' and m.node_group and m.node_group.get(CACHE_TAG)), None)


def playback_group(group, cache, start, frames, count):
    for io in ('INPUT', 'OUTPUT'):
        group.interface.new_socket(name="Geometry", in_out=io, socket_type='NodeSocketGeometry')

    def node(kind, label, x, y):
        n = group.nodes.new(kind)
        n.label = label
        n.location = (x, y)
        return n

    def bind(value, socket):
        if isinstance(value, (int, float)):
            socket.default_value = value
        else:
            group.links.new(value, socket)

    def operation(kind, op, a, b, x, y):
        n = node(kind, "", x, y)
        n.operation = op
        bind(a, n.inputs[0])
        bind(b, n.inputs[1])
        return n.outputs[0]

    src = node('NodeGroupInput', "Input mesh", 0, 400).outputs[0]
    info = node('GeometryNodeObjectInfo', "Baked displacements", 0, -250)
    info.inputs['Object'].default_value = cache
    pos = node('GeometryNodeInputPosition', "Displacement", 220, -350).outputs[0]
    index = node('GeometryNodeInputIndex', "Vertex", 400, -180).outputs[0]
    frame = node('GeometryNodeInputSceneTime', "Timeline", 0, 100).outputs['Frame']
    math_node = 'ShaderNodeMath'
    int_node = 'FunctionNodeIntegerMath'
    local = operation(math_node, 'SUBTRACT', frame, start, 200, 100)
    local = operation(math_node, 'MAXIMUM', local, 0, 400, 100)
    local = operation(math_node, 'MINIMUM', local, frames - 1, 600, 100)
    lower = node('FunctionNodeFloatToInt', "First frame", 800, 150)
    lower.rounding_mode = 'FLOOR'
    bind(local, lower.inputs[0])
    first = lower.outputs[0]
    fraction = operation(math_node, 'SUBTRACT', local, first, 1020, 240)
    first = operation(int_node, 'MULTIPLY', first, count, 1000, 40)
    first = operation(int_node, 'ADD', first, index, 1200, 40)
    second = operation(int_node, 'ADD', first, count, 1400, -120)
    offsets = []
    for i, offset_index in enumerate((first, second)):
        sample = node('GeometryNodeSampleIndex', "Frame offset", 1630, 60 - 250 * i)
        sample.data_type = 'FLOAT_VECTOR'
        sample.domain = 'POINT'
        sample.clamp = True
        bind(info.outputs['Geometry'], sample.inputs['Geometry'])
        bind(pos, sample.inputs['Value'])
        bind(offset_index, sample.inputs['Index'])
        offsets.append(sample.outputs['Value'])
    mix = node('ShaderNodeMix', "Subframe interpolation", 1850, 100)
    mix.data_type = 'VECTOR'
    # Mix sockets have repeated names. The enabled sockets match the data type.
    for key, value in zip(('Factor', 'A', 'B'), (fraction, *offsets)):
        bind(value, next(s for s in mix.inputs if s.name == key and s.enabled))
    size = node('GeometryNodeAttributeDomainSize', "Validate vertex count", 1600, 440)
    bind(src, size.inputs['Geometry'])
    match = operation(math_node, 'COMPARE', size.outputs['Point Count'], count, 1830, 440)
    move = node('GeometryNodeSetPosition', "Apply gel displacements", 2080, 320)
    bind(src, move.inputs['Geometry'])
    bind(match, move.inputs['Selection'])
    bind(next(s for s in mix.outputs if s.enabled), move.inputs['Offset'])
    out = node('NodeGroupOutput', "Mesh", 2300, 320)
    bind(move.outputs['Geometry'], out.inputs[0])


def discard_cache(group, cache):
    if group and not group.users:
        bpy.data.node_groups.remove(group)
    if (cache and cache.type == 'MESH' and cache.get(CACHE_TAG) and
            cache.users == len(cache.users_collection) <= 1):
        mesh = cache.data
        bpy.data.objects.remove(cache, do_unlink=True)
        if not mesh.users:
            bpy.data.meshes.remove(mesh)


def stored_object(group):
    return next((n.inputs['Object'].default_value for n in group.nodes if n.bl_idname == 'GeometryNodeObjectInfo'), None)


def install_cache(obj, start, samples):
    frames, count, _ = samples.shape
    name = obj.name + " - Gel Cache"
    mesh = bpy.data.meshes.new(name)
    cache = group = added = None
    mod = cache_modifier(obj)
    old = mod.node_group if mod else None
    old_cache = stored_object(old) if old else None
    try:
        mesh.vertices.add(frames * count)
        mesh.vertices.foreach_set("co", samples.ravel())
        mesh.update()
        cache = bpy.data.objects.new(name, mesh)
        cache[CACHE_TAG] = True
        cache.hide_render = True
        cache.hide_select = True
        obj.users_collection[0].objects.link(cache)
        cache.hide_set(True)
        group = bpy.data.node_groups.new("Volumetric Gel Playback", 'GeometryNodeTree')
        group[CACHE_TAG] = True
        playback_group(group, cache, start, frames, count)
        if mod is None:
            mod = added = obj.modifiers.new("Volumetric Gel", 'NODES')
        obj.modifiers.move(obj.modifiers.find(mod.name), 0)
        mod.node_group = group
    except Exception:
        if added is not None:
            obj.modifiers.remove(added)
        discard_cache(group, cache)
        if cache is None and not mesh.users:
            bpy.data.meshes.remove(mesh)
        raise
    if old:
        discard_cache(old, old_cache)


class GEL_OT_edit(bpy.types.Operator):
    bl_idname = "object.volumetric_gel_edit"
    bl_label = "Edit Volumetric Gel"
    bl_description = "Simulate a closed, star-shaped mesh using tetrahedral finite elements"
    bl_options = {'REGISTER', 'UNDO'}
    action: EnumProperty(items=[(k, v, "") for k, v in (
        ('ADD', "Add"), ('REMOVE', "Remove"), ('CLEAR', "Clear Bake"), ('BAKE', "Bake"))])

    @classmethod
    def poll(cls, context):
        obj = context.object
        return bool(obj and obj.type == 'MESH' and obj.is_editable and context.mode == 'OBJECT')

    def execute(self, context):
        obj = context.object
        cfg = obj.volumetric_gel
        if self.action == 'ADD':
            if not cfg.enabled and len(obj.data.vertices) and not cfg.is_property_set("floor"):
                bottom = min((obj.matrix_world @ Vector(corner)).z for corner in obj.bound_box)
                cfg.floor = min(0., bottom)
            cfg.enabled = True
            return {'FINISHED'}
        if not cfg.enabled:
            return {'CANCELLED'}
        if self.action != 'BAKE':
            mod = cache_modifier(obj)
            if mod:
                group = mod.node_group
                cache = stored_object(group)
                obj.modifiers.remove(mod)
                discard_cache(group, cache)
            if self.action == 'REMOVE':
                obj.property_unset("volumetric_gel")
            return {'FINISHED'}
        scene = context.scene
        frame, subframe = scene.frame_current, scene.frame_subframe
        context.window_manager.progress_begin(0, max(1, scene.frame_end - scene.frame_start + cfg.settle_frames))
        try:
            with np.errstate(over='raise', invalid='raise', divide='raise'):
                start, samples = solve(context, obj, cfg)
            install_cache(obj, start, samples)
        except (ValueError, RuntimeError, MemoryError, FloatingPointError, np.linalg.LinAlgError) as exc:
            self.report({'ERROR'}, str(exc) or "Insufficient memory for gel bake")
            return {'CANCELLED'}
        finally:
            scene.frame_set(frame, subframe=subframe)
            context.window_manager.progress_end()
        return {'FINISHED'}


class GEL_PT_physics(bpy.types.Panel):
    bl_label = "Volumetric Gel"
    bl_idname = "PHYSICS_PT_volumetric_gel"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "physics"

    @classmethod
    def poll(cls, context):
        obj = context.object
        return bool(obj and obj.type == 'MESH' and obj.volumetric_gel.enabled)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        obj = context.object
        cfg = obj.volumetric_gel
        for prop in ('resolution', 'settle_frames', 'density', 'shear', 'bulk', 'damping', 'friction', 'floor'):
            layout.prop(cfg, prop)
        layout.prop_search(cfg, "pin_group", obj, "vertex_groups")
        layout.prop(cfg, "pin_target")
        layout.prop(cfg, "collider")
        if cfg.collider:
            for prop in ('start', 'end', 'radius'):
                layout.prop(cfg, prop)
        row = layout.row(align=True)
        row.operator(GEL_OT_edit.bl_idname, text="Bake Gel", icon='PHYSICS').action = 'BAKE'
        if cache_modifier(obj):
            row.operator(GEL_OT_edit.bl_idname, text="Clear Bake", icon='X').action = 'CLEAR'


classes = (GelSettings, GEL_OT_edit, GEL_PT_physics)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Object.volumetric_gel = PointerProperty(type=GelSettings)


def unregister():
    del bpy.types.Object.volumetric_gel
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
