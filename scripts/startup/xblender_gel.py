# SPDX-License-Identifier: GPL-2.0-or-later
"""Bake a volumetric gel simulation and play it back from embedded mesh data."""

import math
import time

import bmesh
import bpy
import numpy as np
from bpy.props import (
    BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, PointerProperty, StringProperty,
)
from bpy.app.handlers import persistent
from bpy_extras import view3d_utils
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from _xblender_mesh_collision import SceneContacts
from _xblender_gel_native import live_kernels
from _xblender_live import PreviewClock, publish_sample, redraw_preview_controls
from _xblender_gel import Gel, contact, prism_tets

CACHE_TAG = "_xblender_gel_cache"
LIVE_TAG = "_xblender_gel_live"
_live = None
_live_error = ""
_keymaps = []

# Density, shear (Pa), bulk (Pa), velocity damping, friction, skin smoothing.
MATERIALS = {
    'FIRM': ("Firm Slime", (1000., 18000., 100000., 2.6, .55, 0.)),
    'MEDIUM': ("Medium Slime", (1000., 8500., 70000., 2.6, .55, 0.)),
    'SOFT': ("Soft Slime", (1000., 4400., 48000., 2.6, .55, 0.)),
    'EXTRA_SOFT': ("Extra Soft Slime", (1000., 2300., 30000., 2.6, .55, 0.)),
    'GRIP': ("Lift and Sway Slime", (950., 13500., 80000., 1.65, .54, 0.)),
    'BALLOON': ("Balloon", (80., 3200., 22000., 7.5, .38, .82)),
    'HARD_RUBBER': ("Hard Rubber", (1050., 120000., 650000., 2.6, .65, .2)),
}
SOLVER_FIELDS = ('density', 'shear', 'bulk', 'damping', 'friction')
MATERIAL_FIELDS = (*SOLVER_FIELDS, 'surface_smoothing')


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


def mode_changed(cfg, context):
    global _live_error
    obj = cfg.id_data
    was_running = _live is not None and _live.obj == obj
    if was_running:
        stop_live()
    if cfg.enabled and _live is None and obj.name in context.view_layer.objects and obj.mode == 'OBJECT':
        if (cfg.simulation_mode == 'TIMELINE' and not cache_modifier(obj)) or (
                was_running and cfg.simulation_mode == 'LIVE'):
            try:
                start_live(context, obj)
            except (ValueError, RuntimeError, np.linalg.LinAlgError) as exc:
                _live_error = str(exc)


class GelSettings(bpy.types.PropertyGroup):
    simulation_mode: EnumProperty(name="Simulation Mode", items=(
        ('LIVE', "Real Time", "Advance on the preview clock with the timeline paused"),
        ('TIMELINE', "Timeline", "Simulate at the scene frame rate")), default='LIVE', update=mode_changed)
    enabled: BoolProperty(name="Volumetric Gel")
    resolution: EnumProperty(name="Simulation Mesh", items=(('2', "Coarse", "85 nodes, 320 tetrahedra"),
                                                           ('3', "Fine", "325 nodes, 1280 tetrahedra")), default='3')
    settle_frames: IntProperty(name="Settle Frames", description="Simulate this many frames before playback begins",
                                         default=0, min=0, max=10000)
    use_grab: BoolProperty(name="Mouse Grab", default=True)
    grab_radius: FloatProperty(name="Grab Radius", default=.22, min=.0001, subtype='DISTANCE')
    density: FloatProperty(name="Density", description="Mass per unit volume in kg/m³",
                           default=1000, min=1, soft_max=2000)
    shear: FloatProperty(name="Shear Modulus", description="Resistance to a change in shape, in Pa",
                         default=1800, min=1, soft_max=20000)
    bulk: FloatProperty(name="Bulk Modulus", description="Resistance to a change in volume, in Pa",
                        default=22000, min=10, soft_max=100000)
    damping: FloatProperty(name="Velocity Damping", description="Velocity decay per second",
                           default=2.0, min=0, soft_max=10)
    friction: FloatProperty(name="Contact Friction", default=.55, min=0, soft_max=1)
    surface_smoothing: FloatProperty(name="Skin Smoothing", default=0., min=0., max=1., subtype='FACTOR',
                                     description="Soften fine folds while preserving the overall deformation")
    ground_mode: EnumProperty(name="Ground Plane", items=(
        ('AUTO', "Automatic", "Use Ground Height only when no scene mesh has Collision enabled"),
        ('OFF', "Off", "Only collide with configured objects"),
        ('ON', "Always", "Use Ground Height together with collision objects")), default='AUTO')
    floor: FloatProperty(name="Ground Height", description="World-space Z of the contact plane",
                         default=0, subtype='DISTANCE')
    incline: PointerProperty(name="Inclined Plane", type=bpy.types.Object,
                            description="Stationary contact plane through this object's origin with local +Z facing up")
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


def project_skin_contacts(positions, current, radius, floor, incline, edge_width=0.):
    if current is not None:
        _, n, d = contact(positions, *current)
        depth = radius - d
        push = np.maximum(depth, 0.)
        if edge_width > 0.:
            # A C1 transition removes the sharp normal break at the
            # capsule rim. The correction always stays outside the tool.
            push += np.maximum(edge_width - np.abs(depth), 0.) ** 2 / (4 * edge_width)
        positions += push[:, None] * n
    positions[:, 2] = np.maximum(positions[:, 2], floor)
    if incline is not None:
        positions += np.maximum(incline[1] - positions @ incline[0], 0)[:, None] * incline[0]


class Simulation:
    """One material state and skin binding for both baking and live preview."""

    def __init__(self, context, obj, cfg):
        scene = context.scene
        matrix, previous, pin_matrix = input_samples(context, obj, cfg)
        count = len(obj.data.vertices)
        source = np.empty(3 * count, dtype=np.float32)
        obj.data.vertices.foreach_get("co", source)
        source = source.reshape(-1, 3).astype(float)
        # Blender stores positions and scene acceleration in scene units. The
        # material law uses meters, Pa and kg/m³; convert back only for playback.
        meters = scene.unit_settings.scale_length if scene.unit_settings.system != 'NONE' else 1.
        world = transform(source, matrix) * meters
        self.contacts = SceneContacts(obj, meters)
        self.contacts.sample(context, 0.)
        floor = self.ground(cfg, meters)
        self.skin_previous = world.copy() if self.contacts.meshes else None
        if previous is not None:
            previous = previous * meters
        if len(world) and world[:, 2].min() < floor - 1e-5 * meters:
            raise ValueError("Start with the gel above the ground or lower Ground Height")
        incline = None
        if cfg.incline:
            if cfg.incline == obj:
                raise ValueError("Choose a separate object for the inclined plane")
            plane_matrix = cfg.incline.evaluated_get(context.evaluated_depsgraph_get()).matrix_world
            # Transform the plane normal as a covector (also handles sheared parent transforms).
            basis = np.asarray(plane_matrix.to_3x3())
            normal = np.linalg.solve(basis.T, (0., 0., 1.))
            normal /= np.linalg.norm(normal)
            if normal[2] <= 0:
                raise ValueError("The inclined plane's local +Z must face upward")
            offset = normal @ np.asarray(plane_matrix.translation) * meters
            if cfg.incline.session_uid not in self.contacts.meshes and np.min(world @ normal) < offset - 1e-5 * meters:
                raise ValueError("Start with the gel above the inclined plane")
            incline = normal, offset
        pinning = pin_weights(obj, cfg)
        nodes, tets, pinned, attach, weights, surface, stencil = simulation_mesh(obj, world, cfg, pinning)
        if pin_matrix is not None:
            if not np.any(pinned):
                raise ValueError("The pin target needs a pin group covering at least one simulation node")
            inv_target = pin_matrix.inverted()
            self.pin_local = transform(nodes[pinned] / meters, inv_target)
            self.skin_mask = pinning > 0
            self.skin_weights = pinning[self.skin_mask, None]
            self.skin_local = transform(world[self.skin_mask] / meters, inv_target)
        gel = Gel(nodes, tets, pinned, cfg.density, cfg.shear, cfg.bulk, cfg.damping, cfg.friction, surface)
        gel.kernels = live_kernels(gel)
        self.obj, self.cfg, self.scene = obj, cfg, scene
        self.source, self.world, self.meters = source, world, meters
        self.gel, self.previous, self.incline = gel, previous, incline
        self.stencil, self.attach, self.weights = stencil, attach, weights
        self.skin_filter = None
        self.material = tuple(getattr(cfg, field) for field in SOLVER_FIELDS)

    def ground(self, cfg, meters):
        enabled = cfg.ground_mode == 'ON' or (cfg.ground_mode == 'AUTO' and not self.contacts.meshes)
        return cfg.floor * meters if enabled else -np.inf

    def relax_skin(self, positions, amount):
        if self.skin_filter is None:
            edges = np.empty(2 * len(self.obj.data.edges), dtype=np.int32)
            self.obj.data.edges.foreach_get("vertices", edges)
            edges = edges.reshape(-1, 2)
            a, b = edges.T
            row, col = np.r_[a, b], np.r_[b, a]
            degree = np.bincount(row, minlength=len(self.world))
            order = np.argsort(row, kind='stable')
            zero_mean = self.world - self.world.mean(axis=0)
            offsets = np.r_[0, degree.cumsum()].astype(np.int32)
            self.skin_filter = (col[order], offsets, 1. / degree[:, None],
                                zero_mean, np.linalg.pinv(zero_mean))
        adjacent, offsets, inv_degree, basis, fit = self.skin_filter
        # Only relax the non-affine detail of the contact-resolved skin.
        # Translation, rotation and uniform squash or expansion are reproduced.
        affine = basis @ (fit @ positions) + positions.mean(axis=0)
        detail = np.ascontiguousarray(positions - affine)
        passes = 8
        if not (self.gel.kernels and self.gel.kernels.relax_skin(detail, offsets, adjacent, amount, passes)):
            for _ in range(passes):
                average = np.add.reduceat(detail[adjacent], offsets[:-1], axis=0) * inv_degree
                detail += amount * (average - detail)
        return affine + detail

    def step(self, context, seconds, grab=None, *, interactive=False):
        obj, cfg, gel = self.obj, self.cfg, self.gel
        meters, incline = self.meters, self.incline
        self.contacts.sample(context, seconds, gel, interactive)
        gel.mesh_contacts = self.contacts if self.contacts.meshes else None
        if cfg.incline and cfg.incline.session_uid in self.contacts.meshes:
            incline = None
        floor, radius = self.ground(cfg, meters), cfg.radius * meters
        gravity = np.array(self.scene.gravity) * meters if self.scene.use_gravity else np.zeros(3)
        material = tuple(getattr(cfg, field) for field in SOLVER_FIELDS)
        if material != self.material:
            gel.set_material(*material)
            self.material = material
        matrix, current, pin_matrix = input_samples(context, obj, cfg)
        if cfg.collider and cfg.collider.session_uid in self.contacts.meshes:
            current = None
        if current is not None:
            current = current * meters
            if self.previous is None:
                self.previous = current.copy()
        targets = None
        if pin_matrix is not None:
            targets = transform(self.pin_local, pin_matrix) * meters
            if targets[:, 2].min() < floor - 1e-5 * meters:
                raise ValueError("The animated pins move below the ground")
            if incline is not None and np.min(targets @ incline[0]) < incline[1] - 1e-5 * meters:
                raise ValueError("The animated pins move below the inclined plane")
        def advance(refinement):
            if interactive and current is not None:
                return gel.advance_live_collider(seconds, gravity, floor, self.previous, current, radius,
                                                 targets, incline, grab, refinement)
            gel.advance(seconds, gravity, floor, self.previous, current, radius, targets, incline,
                        grab, interactive=interactive, refinement=refinement)
            return current

        current = self.contacts.advance_live(gel, advance) if interactive and gel.mesh_contacts else advance(1)
        shell = (gel.x - gel.rest)[-self.stencil.shape[1]:]
        positions = self.world + np.einsum("ij,ijk->ik", self.weights, (self.stencil @ shell)[self.attach])
        if cfg.surface_smoothing > 0.:
            project_skin_contacts(positions, current, radius, floor, incline)
            positions = self.relax_skin(positions, cfg.surface_smoothing)
        if pin_matrix is not None:
            goal = transform(self.skin_local, pin_matrix) * meters
            positions[self.skin_mask] += self.skin_weights * (goal - positions[self.skin_mask])
        project_skin_contacts(positions, current, radius, floor, incline, .22 * radius * cfg.surface_smoothing)
        if gel.mesh_contacts:
            if self.skin_previous is None:
                self.skin_previous = positions.copy()
            mobile = np.ones(len(positions), dtype=bool)
            if pin_matrix is not None:
                mobile[self.skin_mask] = self.skin_weights[:, 0] < 1.
            self.contacts.project(self.skin_previous, positions, None, mobile, 0., 1., seconds, cfg.friction)
            self.skin_previous = positions.copy()
        else:
            self.skin_previous = None
        self.previous = current
        return transform(positions / meters, matrix.inverted()) - self.source


def solve(context, obj, cfg):
    scene = context.scene
    start, end = scene.frame_start - cfg.settle_frames, scene.frame_end
    count = len(obj.data.vertices)
    if count == 0 or (end - start + 1) * count >= 2 ** 31:
        raise ValueError("Unsupported gel mesh or bake range")
    scene.frame_set(start)
    sim = Simulation(context, obj, cfg)
    seconds = scene.render.fps_base / scene.render.fps
    samples = np.empty((end - start + 1, count, 3), dtype=np.float32)
    samples[0].fill(0)
    for frame in range(start + 1, end + 1):
        scene.frame_set(frame)
        samples[frame - start] = sim.step(context, seconds)
        context.window_manager.progress_update(frame - start)
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


def live_group(group, points, count):
    """Replace positions in the viewport only, after any baked displacements."""
    for io in ('INPUT', 'OUTPUT'):
        group.interface.new_socket(name="Geometry", in_out=io, socket_type='NodeSocketGeometry')
    nodes, link = group.nodes, group.links.new
    src = nodes.new('NodeGroupInput')
    info = nodes.new('GeometryNodeObjectInfo')
    info.inputs['Object'].default_value = points
    sample = nodes.new('GeometryNodeSampleIndex')
    sample.data_type = 'FLOAT_VECTOR'
    link(info.outputs['Geometry'], sample.inputs['Geometry'])
    link(nodes.new('GeometryNodeInputPosition').outputs[0], sample.inputs['Value'])
    link(nodes.new('GeometryNodeInputIndex').outputs[0], sample.inputs['Index'])
    size = nodes.new('GeometryNodeAttributeDomainSize')
    link(src.outputs[0], size.inputs['Geometry'])
    match = nodes.new('ShaderNodeMath')
    match.operation = 'COMPARE'
    match.inputs[1].default_value = count
    link(size.outputs['Point Count'], match.inputs[0])
    move = nodes.new('GeometryNodeSetPosition')
    link(src.outputs[0], move.inputs['Geometry'])
    link(match.outputs[0], move.inputs['Selection'])
    link(sample.outputs['Value'], move.inputs['Position'])
    link(move.outputs['Geometry'], nodes.new('NodeGroupOutput').inputs[0])


class LiveGel:
    STEP = 1 / 60

    def __init__(self, context, obj):
        self.obj, self.layer = obj, context.view_layer
        self.sim = Simulation(context, obj, obj.volumetric_gel)
        self.binding = self.signature()
        self.grip = None
        count = len(self.sim.source)
        mesh = bpy.data.meshes.new("Gel preview positions")
        self.points = self.group = None
        try:
            mesh.vertices.add(count)
            mesh.vertices.foreach_set("co", np.asarray(self.sim.source, dtype=np.float32).ravel())
            mesh.update()
            self.points = bpy.data.objects.new("Gel preview positions", mesh)
            self.points[LIVE_TAG] = True
            self.points.hide_render = self.points.hide_select = True
            # Unlinked: the Geometry Nodes dependency evaluates these private positions.
            self.group = bpy.data.node_groups.new("Live Volumetric Gel", 'GeometryNodeTree')
            self.group[LIVE_TAG] = True
            live_group(self.group, self.points, count)
            baked = cache_modifier(obj)
            slot = obj.modifiers.find(baked.name) + 1 if baked else 0
            self.mod = obj.modifiers.new("Live Volumetric Gel", 'NODES')
            self.mod.node_group = self.group
            self.mod.show_render = False
            obj.modifiers.move(obj.modifiers.find(self.mod.name), slot)
        except Exception:
            clean_live_data()
            if self.points is None and not mesh.users:
                bpy.data.meshes.remove(mesh)
            raise
        self.clock = PreviewClock(self.STEP, 3)

    def signature(self):
        cfg = self.obj.volumetric_gel
        return (self.obj.data, cfg.resolution, cfg.pin_group, cfg.pin_target, cfg.incline, cfg.collider)

    def grab_at(self, hit):
        """Pick a patch on the deformed surface, keeping its initial offset from the cursor."""
        sim = self.sim
        p = np.asarray(hit) * sim.meters
        ids = sim.gel.surface
        radii = np.linalg.norm(sim.gel.x[ids] - p, axis=1)
        radius = self.obj.volumetric_gel.grab_radius * sim.meters
        mask = (radii < radius) & ~sim.gel.pinned[ids]
        if not mask.any():
            closest = radii.argmin()
            if sim.gel.pinned[ids[closest]]:
                return False
            mask[closest] = True
        dist = radii[mask]
        weights = np.maximum(.12, 1. - (dist / max(radius, dist.min() + 1e-9)) ** 2)
        weights /= weights.max()
        self.grip = ids[mask], sim.gel.x[ids[mask]].copy() - p, weights
        self.pointer = p.copy()
        self.handle = p.copy()
        return True

    def check_setup(self, context):
        sim = self.sim
        if (context.scene != sim.scene or context.view_layer != self.layer or self.obj.mode != 'OBJECT' or
                self.signature() != self.binding or len(self.obj.data.vertices) != len(sim.source) or
                self.mod.node_group != self.group or not self.obj.volumetric_gel.enabled):
            raise ValueError("Gel preview setup changed")

    def step(self, context, seconds):
        self.check_setup(context)
        sim = self.sim
        if not self.obj.volumetric_gel.use_grab:
            self.grip = None
        spring = None
        if self.grip is not None:
            ids, offsets, weights = self.grip
            # Limit both target speed and spring extension. A speed cap alone
            # still lets the handle build unlimited tension against a floor or pin.
            extent = np.ptp(sim.gel.rest, axis=0).max()
            anchor = np.average(sim.gel.x[ids] - offsets, axis=0, weights=weights)
            pull = self.pointer - anchor
            goal = anchor + pull * min(1., .35 * extent / max(np.linalg.norm(pull), 1e-12))
            goal[2] = max(goal[2], sim.ground(sim.cfg, sim.meters) - offsets[:, 2].min())
            if sim.incline is not None:
                normal, height = sim.incline
                goal += max(0., height - np.min((goal + offsets) @ normal)) * normal
            motion = goal - self.handle
            self.handle += motion * min(1., 4 * extent * seconds / max(np.linalg.norm(motion), 1e-12))
            spring = ids, offsets + self.handle, weights
        with np.errstate(over='raise', invalid='raise', divide='raise'):
            positions = sim.source + sim.step(context, seconds, spring, interactive=True)
        self.publish(positions)

    def publish(self, positions):
        mesh = self.points.data
        mesh.vertices.foreach_set("co", np.ascontiguousarray(positions, dtype=np.float32).ravel())
        publish_sample(mesh, self.layer)

    def tick(self):
        _, steps, delay = self.clock.poll()
        if steps:
            self.step(bpy.context, steps * self.STEP)
        return delay


def pause_playback(scene, *, restore):
    for win in bpy.context.window_manager.windows:
        if win.scene == scene and win.screen.is_animation_playing:
            with bpy.context.temp_override(window=win):
                if bpy.ops.screen.animation_cancel.poll():
                    bpy.ops.screen.animation_cancel(restore_frame=restore)
            break


class TimelineGel(LiveGel):
    """Follow scene time. On a seek, evaluate intermediate animated inputs in order."""

    def __init__(self, context, obj):
        scene = context.scene
        frame, sub = scene.frame_current, scene.frame_subframe
        self.first = scene.frame_start - obj.volumetric_gel.settle_frames
        try:
            scene.frame_set(self.first)
            super().__init__(context, obj)
        finally:
            scene.frame_set(frame, subframe=sub)
        self.head = self.first
        self.last = self.current = self.sim.source
        self.displayed = None
        self.settings = None
        self.timing = None

    def parameters(self):
        cfg = self.obj.volumetric_gel
        return (tuple(getattr(cfg, field) for field in MATERIAL_FIELDS), cfg.ground_mode, cfg.floor,
                cfg.radius, tuple(cfg.start), tuple(cfg.end), self.sim.scene.use_gravity, tuple(self.sim.scene.gravity))

    def tick(self):
        context = bpy.context
        self.check_setup(context)
        scene = self.sim.scene
        frame, sub = scene.frame_current, scene.frame_subframe
        request = (frame, sub)
        first = scene.frame_start - self.obj.volumetric_gel.settle_frames
        timing = (first, scene.frame_end, scene.render.fps_base, scene.render.fps)
        changed = self.timing is not None and self.timing != timing
        self.timing = timing
        self.first = first
        if self.displayed == request:
            if not changed and self.parameters() == self.settings:
                return 1 / 60
            changed = True
        target = min(max(frame + (sub > 0.), self.first), scene.frame_end)
        seconds = scene.render.fps_base / scene.render.fps
        deadline = time.monotonic() + .024
        try:
            if changed or target < self.head:
                scene.frame_set(self.first)
                self.sim = Simulation(context, self.obj, self.obj.volumetric_gel)
                self.last = self.current = self.sim.source
                self.head = self.first
            while self.head < target:
                f = self.head + 1
                if scene.frame_current != f or scene.frame_subframe:
                    scene.frame_set(f)
                self.last = self.current
                self.current = self.sim.source + self.sim.step(context, seconds)
                self.head = f
                if time.monotonic() >= deadline:
                    break
        finally:
            if (scene.frame_current, scene.frame_subframe) != request:
                scene.frame_set(frame, subframe=sub)
        if self.head == target:
            positions = self.current
            if sub and self.first <= frame < scene.frame_end:
                positions = (1. - sub) * self.last + sub * self.current
            self.publish(positions)
            self.displayed = request
            self.settings = self.parameters()
        else:
            self.publish(self.current)
            self.displayed = None
        return .001 if self.head < target else 1 / 60


@persistent
def on_playback(scene, *_):
    # Playback-pre precedes installation of Blender's animation timer.
    if _live and _live.sim.scene == scene and not isinstance(_live, TimelineGel):
        def keep_paused():
            if _live and _live.sim.scene == scene and not isinstance(_live, TimelineGel):
                pause_playback(scene, restore=True)
            return None
        bpy.app.timers.register(keep_paused)
    elif not _live:
        obj = bpy.context.object
        if (obj and obj.type == 'MESH' and obj.is_editable and obj.mode == 'OBJECT' and
                obj.volumetric_gel.enabled and obj.volumetric_gel.simulation_mode == 'TIMELINE' and
                not cache_modifier(obj)):
            def begin():
                if not _live and bpy.context.scene == scene and obj.volumetric_gel.simulation_mode == 'TIMELINE':
                    mode_changed(obj.volumetric_gel, bpy.context)
                return None
            bpy.app.timers.register(begin)


def clean_live_data():
    # Also handles duplicated modifiers and transient data restored by undo/autosave.
    for obj in list(bpy.data.objects):
        if obj.type == 'MESH' and obj.is_editable:
            for mod in list(obj.modifiers):
                if mod.type == 'NODES' and mod.node_group and mod.node_group.get(LIVE_TAG):
                    obj.modifiers.remove(mod)
    for group in list(bpy.data.node_groups):
        if group.is_editable and group.get(LIVE_TAG) and not group.users:
            bpy.data.node_groups.remove(group)
    for obj in list(bpy.data.objects):
        if obj.is_editable and obj.get(LIVE_TAG) and obj.type == 'MESH' and obj.users == len(obj.users_collection):
            mesh = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if not mesh.users:
                bpy.data.meshes.remove(mesh)


def stop_live(from_timer=False):
    global _live
    if not from_timer and bpy.app.timers.is_registered(live_tick):
        bpy.app.timers.unregister(live_tick)
    if _live is not None:
        _live = None
        clean_live_data()
        redraw_preview_controls()


def live_tick():
    global _live_error
    try:
        return _live.tick() if _live else None
    except (ReferenceError, RuntimeError, ValueError, FloatingPointError, np.linalg.LinAlgError) as exc:
        _live_error = str(exc)
        print("Volumetric Gel preview:", _live_error)
        stop_live(from_timer=True)
        return None


def start_live(context, obj):
    global _live, _live_error
    if _live:
        raise ValueError("A gel preview is already running")
    _live_error = ""
    if obj.volumetric_gel.simulation_mode == 'TIMELINE':
        _live = TimelineGel(context, obj)
    else:
        pause_playback(context.scene, restore=False)
        _live = LiveGel(context, obj)
    bpy.app.timers.register(live_tick, first_interval=_live.STEP)
    redraw_preview_controls()


@persistent
def before_file(*_):
    global _resume_uid
    _resume_uid = None
    stop_live()


@persistent
def after_load(*_):
    global _live_error
    _live_error = ""
    clean_live_data()


_resume_uid = None


@persistent
def before_undo(*_):
    global _resume_uid
    _resume_uid = _live.obj.session_uid if _live else None
    stop_live()


@persistent
def after_undo(*_):
    global _resume_uid, _live_error
    uid, _resume_uid = _resume_uid, None
    clean_live_data()
    if uid is not None:
        obj = next((o for o in bpy.context.view_layer.objects if o.session_uid == uid), None)
        if obj and obj.mode == 'OBJECT' and obj.volumetric_gel.enabled:
            try:
                start_live(bpy.context, obj)
            except (ValueError, RuntimeError, np.linalg.LinAlgError) as exc:
                _live_error = str(exc)


class GEL_OT_live(bpy.types.Operator):
    bl_idname = "object.volumetric_gel_live"
    bl_label = "Live Gel Simulation"
    running: BoolProperty()

    def execute(self, context):
        if not self.running:
            stop_live()
        else:
            obj = context.object
            if not obj or obj.type != 'MESH' or not obj.is_editable or obj.mode != 'OBJECT' or not obj.volumetric_gel.enabled:
                return {'CANCELLED'}
            try:
                start_live(context, obj)
            except (RuntimeError, ValueError, MemoryError, FloatingPointError, np.linalg.LinAlgError) as exc:
                self.report({'ERROR'}, str(exc))
                return {'CANCELLED'}
        return {'FINISHED'}


class GEL_OT_preset(bpy.types.Operator):
    bl_idname = "object.volumetric_gel_preset"
    bl_label = "Gel Material Preset"
    bl_options = {'UNDO'}
    material: EnumProperty(items=[(key, label, f"G {values[1] / 1000:g} kPa, K {values[2] / 1000:g} kPa")
                                  for key, (label, values) in MATERIALS.items()])

    @classmethod
    def poll(cls, context):
        obj = context.object
        return bool(obj and obj.type == 'MESH' and obj.is_editable and obj.volumetric_gel.enabled)

    def execute(self, context):
        cfg = context.object.volumetric_gel
        for field, value in zip(MATERIAL_FIELDS, MATERIALS[self.material][1]):
            setattr(cfg, field, value)
        return {'FINISHED'}


class GEL_OT_grab(bpy.types.Operator):
    bl_idname = "view3d.volumetric_gel_grab"
    bl_label = "Grab Gel"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return bool(_live and not isinstance(_live, TimelineGel) and context.area and context.area.type == 'VIEW_3D' and
                    context.region and context.region.type == 'WINDOW' and
                    context.active_object == _live.obj and _live.obj.volumetric_gel.use_grab and
                    _live.grip is None and context.mode == 'OBJECT')

    @staticmethod
    def ray(context, event):
        region, view = context.region, context.region_data
        xy = (event.mouse_x - region.x, event.mouse_y - region.y)
        return (view3d_utils.region_2d_to_origin_3d(region, view, xy),
                view3d_utils.region_2d_to_vector_3d(region, view, xy))

    def invoke(self, context, event):
        origin, direction = self.ray(context, event)
        ok, hit, _, _, obj, _ = context.scene.ray_cast(context.evaluated_depsgraph_get(), origin, direction)
        if not ok or obj != _live.obj or not _live.grab_at(hit):
            return {'PASS_THROUGH'}
        self.session = _live
        self.hit = hit.copy()
        self.normal = context.region_data.view_rotation @ Vector((0., 0., -1.))
        context.window.cursor_modal_set('HAND')
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        self.session.grip = None
        context.window.cursor_modal_restore()

    def modal(self, context, event):
        if (self.session != _live or self.session.grip is None or
                event.type in {'WINDOW_DEACTIVATE', 'ESC', 'RIGHTMOUSE'} or
                (event.type == 'LEFTMOUSE' and event.value == 'RELEASE')):
            self.cancel(context)
            return {'FINISHED'}
        if event.type == 'MOUSEMOVE' and context.region_data:
            origin, direction = self.ray(context, event)
            dot = direction.dot(self.normal)
            if abs(dot) > 1e-7:
                point = origin + direction * ((self.hit - origin).dot(self.normal) / dot)
                self.session.pointer = np.asarray(point) * self.session.sim.meters
        return {'RUNNING_MODAL'}


def live_header(self, context):
    if _live and context.scene == _live.sim.scene:
        self.layout.operator(GEL_OT_live.bl_idname, text="Stop Gel Preview", icon='PAUSE').running = False


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
        if _live and _live.obj == obj:
            stop_live()
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
        pause_playback(scene, restore=False)
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
        values = tuple(getattr(cfg, name) for name in MATERIAL_FIELDS)
        label = next((name for name, parameters in MATERIALS.values() if np.allclose(parameters, values)),
                     "Custom Material")
        layout.prop(cfg, "simulation_mode", expand=True)
        row = layout.row()
        row.label(text="Material Preset")
        row.operator_menu_enum(GEL_OT_preset.bl_idname, "material", text=label, icon='PRESET')
        live = _live is not None and _live.obj == obj
        row = layout.row()
        timeline = cfg.simulation_mode == 'TIMELINE'
        baked = cache_modifier(obj)
        row.enabled = live or (_live is None and not (timeline and baked))
        start_text = ("Baked Timeline" if baked else "Start Timeline Preview") if timeline else "Start Live Simulation"
        row.operator(GEL_OT_live.bl_idname, text="Stop Gel Preview" if live else start_text,
                     icon='PAUSE' if live else 'PLAY').running = not live
        if not timeline:
            layout.prop(cfg, "use_grab")
            if cfg.use_grab:
                layout.prop(cfg, "grab_radius")
                if live:
                    layout.label(text="Drag the gel with the left mouse button")
        if _live_error and not _live:
            layout.label(text=_live_error, icon='ERROR')
        setup = layout.column()
        setup.enabled = not live
        for prop in ('resolution', 'settle_frames'):
            setup.prop(cfg, prop)
        for prop in MATERIAL_FIELDS:
            layout.prop(cfg, prop)
        layout.prop(cfg, "ground_mode")
        if cfg.ground_mode != 'OFF':
            layout.prop(cfg, 'floor')
        setup = layout.column()
        setup.enabled = not live
        setup.prop(cfg, 'incline')
        setup.prop_search(cfg, "pin_group", obj, "vertex_groups")
        setup.prop(cfg, "pin_target")
        setup.prop(cfg, "collider")
        if cfg.collider:
            for prop in ('start', 'end', 'radius'):
                layout.prop(cfg, prop)
        row = layout.row(align=True)
        row.enabled = timeline or not live
        row.operator(GEL_OT_edit.bl_idname, text="Bake Gel", icon='PHYSICS').action = 'BAKE'
        if cache_modifier(obj):
            row.operator(GEL_OT_edit.bl_idname, text="Clear Bake", icon='X').action = 'CLEAR'



classes = (GelSettings, GEL_OT_edit, GEL_OT_live, GEL_OT_preset, GEL_OT_grab, GEL_PT_physics)
handlers = ((bpy.app.handlers.animation_playback_pre, on_playback),
            (bpy.app.handlers.load_pre, before_file), (bpy.app.handlers.save_pre, before_file),
            (bpy.app.handlers.load_post, after_load), (bpy.app.handlers.undo_pre, before_undo),
            (bpy.app.handlers.redo_pre, before_undo), (bpy.app.handlers.undo_post, after_undo),
            (bpy.app.handlers.redo_post, after_undo))


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Object.volumetric_gel = PointerProperty(type=GelSettings)
    for callbacks, fn in handlers:
        callbacks.append(fn)
    bpy.types.VIEW3D_HT_header.prepend(live_header)
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="3D View", space_type='VIEW_3D')
        item = km.keymap_items.new(GEL_OT_grab.bl_idname, 'LEFTMOUSE', 'PRESS', head=True)
        _keymaps.append((km, item))


def unregister():
    global _live
    if hasattr(bpy.data, "objects"):
        stop_live()
    else:
        if bpy.app.timers.is_registered(live_tick):
            bpy.app.timers.unregister(live_tick)
        _live = None
    for km, item in _keymaps:
        km.keymap_items.remove(item)
    _keymaps.clear()
    bpy.types.VIEW3D_HT_header.remove(live_header)
    for callbacks, fn in handlers:
        callbacks.remove(fn)
    del bpy.types.Object.volumetric_gel
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
