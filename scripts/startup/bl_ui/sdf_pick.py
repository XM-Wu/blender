# SPDX-License-Identifier: GPL-2.0-or-later
"""Viewport picking for the evaluated triangles displayed by the SDF editor."""

from functools import lru_cache
from typing import NamedTuple

from mathutils import Vector
from mathutils.bvhtree import BVHTree
from mathutils.geometry import barycentric_transform
import numpy as np


class Surface(NamedTuple):
    """One row per evaluated triangle, in Blender's polygon order."""
    face_indices: np.ndarray
    positions: np.ndarray
    uvs: np.ndarray


class SurfaceTree:
    def __init__(self, mesh, surface):
        # Include the whole evaluated mesh so unmasked faces can occlude
        # masked faces on the same object. BVH leaves are triangle indices.
        vertices = mesh_array(mesh.vertices, "co", 3, np.float32)
        triangles = mesh_array(mesh.loop_triangles, "vertices", 3, np.int32)
        faces = mesh_array(mesh.loop_triangles, "polygon_index", 1, np.int32)
        self.tree = BVHTree.FromPolygons(vertices, triangles, all_triangles=True)
        included = np.isin(faces, surface.face_indices)
        self.lookup = np.full(len(faces), -1, dtype=np.int32)
        self.lookup[included] = np.arange(len(surface.face_indices), dtype=np.int32)
        self.positions, self.uvs = surface.positions, surface.uvs

    def hit(self, index, point, matrix, obj):
        index = self.lookup[index]
        if index < 0:
            return None
        verts = tuple(map(Vector, self.positions[index]))
        uv = tuple(Vector((u, v, 0)) for u, v in self.uvs[index])
        return barycentric_transform(point, *verts, *uv), verts, uv, matrix, obj


class ViewPicker:
    def __init__(self, context, region, rv, mask):
        dg = context.evaluated_depsgraph_get()
        self.dg = dg
        self.width, self.height = region.width, region.height
        projection = np.asarray(rv.perspective_matrix, dtype=np.float64)
        self.inverse_projection = np.linalg.inv(projection)
        self.entries = []
        self.tiles = {}
        self.surface_trees = {}
        self.curve_blockers = {}
        sources = {s.object: s for s in mask.sources if s.object}
        for item in dg.object_instances:
            obj = item.object
            if not item.show_self or obj.type not in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}:
                continue
            original = obj.original
            owner = item.parent.original if item.is_instance and item.parent else original
            matrix = item.matrix_world.copy()
            bounds = np.asarray(obj.bound_box)
            bbox = bounds @ np.asarray(matrix)[:3, :3].T + np.asarray(matrix)[:3, 3]
            clip = bbox @ projection[:, :3].T + projection[:, 3]
            if np.all(clip[:, 3] <= 0):
                continue
            rect = (0, 0, self.width, self.height)
            if np.all(clip[:, 3] > 0):
                xy = (clip[:, :2] / clip[:, 3:4] + 1) * (.5 * np.array([self.width, self.height]))
                rect = (*xy.min(axis=0) - 1, *xy.max(axis=0) + 1)
            if rect[2] < 0 or rect[3] < 0 or rect[0] >= self.width or rect[1] >= self.height:
                continue
            source = None if item.is_instance else sources.get(original)
            # Do not retain the depsgraph's temporary instance Object pointer.
            evaluated = original.evaluated_get(dg)
            center = bbox.mean(axis=0)
            radius = np.linalg.norm(bbox - center, axis=1).max()
            self.entries.append((rect, original, owner, evaluated, matrix,
                                 matrix.inverted_safe(), center, radius, source))

    def pick(self, context, xy):
        x, y = xy
        if not (0 <= x < self.width and 0 <= y < self.height):
            return None
        tile = int(x) // 64, int(y) // 64
        if tile not in self.tiles:
            x0, y0 = tile[0] * 64, tile[1] * 64
            self.tiles[tile] = [e for e in self.entries if e[0][0] <= x0 + 64 and e[0][1] <= y0 + 64
                                and e[0][2] >= x0 and e[0][3] >= y0]
        # Compute clip-space endpoints in double precision before converting
        # to Blender's local-space floats (especially important in ortho).
        h = self.inverse_projection @ (2*x/self.width-1, 2*y/self.height-1, -1, 1)
        near = h[:3] / h[3]
        mid = h + self.inverse_projection[:, 2]
        direction = mid[:3] / mid[3] - near
        direction /= np.linalg.norm(direction)
        far = mid + self.inverse_projection[:, 2]
        best = np.linalg.norm(far[:3] / far[3] - near) if abs(far[3]) > 1e-15 else np.inf
        result = None
        for rect, obj, owner, evaluated, matrix, inverse, center, radius, source in self.tiles[tile]:
            if not (rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]):
                continue
            if not owner.visible_get(view_layer=context.view_layer, viewport=context.space_data):
                continue
            if obj.display_type in {'WIRE', 'BOUNDS'} and source is None:
                continue
            # Move the start to just before this object's bounds. Casting
            # from the far clip in ortho loses precision on small facial meshes.
            advance = max(0., (center - near) @ direction - radius * 1.001 - 1e-5)
            if advance > best:
                continue
            start = near + direction * advance
            local_start = inverse @ Vector(start)
            local_dir = inverse.to_3x3() @ Vector(direction)
            tree = None
            if source and obj.mode == 'SDF_PAINT':
                if obj not in self.surface_trees:
                    self.surface_trees[obj] = surface_tree(source, self.dg)
                tree = self.surface_trees[obj]
            if tree is None:
                if obj.display_type in {'WIRE', 'BOUNDS'}:
                    continue
                if obj.type == 'MESH':
                    ok, point, _, _ = evaluated.ray_cast(local_start, local_dir, depsgraph=self.dg)
                    if not ok:
                        continue
                else:
                    # Native object ray_cast requires an evaluated mesh. For
                    # converted text/curves copy a blocker tree, then release the mesh.
                    if obj not in self.curve_blockers:
                        mesh = evaluated.to_mesh()
                        try:
                            blocker = None
                            if mesh and mesh.polygons:
                                blocker = BVHTree.FromPolygons(
                                    [v.co for v in mesh.vertices], [tri.vertices[:] for tri in mesh.loop_triangles],
                                    all_triangles=True)
                            self.curve_blockers[obj] = blocker
                        finally:
                            evaluated.to_mesh_clear()
                    blocker = self.curve_blockers[obj]
                    if blocker is None:
                        continue
                    point, _, _, _ = blocker.ray_cast(local_start, local_dir)
                    if point is None:
                        continue
            else:
                point, _, index, _ = tree.tree.ray_cast(local_start, local_dir)
                if point is None:
                    continue
            depth = advance + (np.asarray(matrix @ point) - start) @ direction
            if depth < 0 or depth > best:
                continue
            best = depth
            result = tree.hit(index, point, matrix, obj) if tree else None
        return result


@lru_cache(maxsize=16)
def face_indices(data):
    return frozenset(map(int, data.split(",")))


# Evaluated triangles are shared by the viewport overlay and brush picking.
# Keep the face membership snapshot tied to the original topology.
_surfaces = {}
_trees = {}


def mesh_array(collection, prop, width, dtype):
    data = np.empty(len(collection) * width, dtype=dtype)
    collection.foreach_get(prop, data)
    return data if width == 1 else data.reshape(-1, width)


def geometry(source, depsgraph):
    """Evaluated triangle positions and UVs, in Blender's polygon order."""
    obj = source.object
    token = (depsgraph.as_pointer(), obj.as_pointer(), source.faces, source.uv_name)
    if token in _surfaces:
        return _surfaces[token]
    mesh = obj.evaluated_get(depsgraph).data
    layer = mesh.uv_layers.get(source.uv_name)
    surface = None
    if layer:
        originals = mesh_array(mesh.polygons, "original_index", 1, np.int32)
        # Deformation-only evaluation can share the base topology without
        # allocating an original-index layer. Check connectivity before using it.
        if len(originals) == len(obj.data.polygons) and np.all(originals == -1):
            if (np.array_equal(mesh_array(mesh.polygons, "loop_start", 1, np.int32),
                               mesh_array(obj.data.polygons, "loop_start", 1, np.int32)) and
                np.array_equal(mesh_array(mesh.loops, "vertex_index", 1, np.int32),
                               mesh_array(obj.data.loops, "vertex_index", 1, np.int32))):
                originals = np.arange(len(originals), dtype=np.int32)
        faces = mesh_array(mesh.loop_triangles, "polygon_index", 1, np.int32)
        keep = np.isin(originals[faces], tuple(face_indices(source.faces)))
        if keep.any():
            vertices = mesh_array(mesh.loop_triangles, "vertices", 3, np.int32)[keep]
            loops = mesh_array(mesh.loop_triangles, "loops", 3, np.int32)[keep]
            surface = Surface(
                faces[keep],
                mesh_array(mesh.vertices, "co", 3, np.float32)[vertices],
                mesh_array(layer.uv, "vector", 2, np.float32)[loops],
            )
    _surfaces[token] = surface
    return surface


_view_pickers = {}


def surface_tree(source, dg):
    surface = geometry(source, dg)
    if surface is None:
        return None
    key = id(surface)
    if key not in _trees:
        mesh = source.object.evaluated_get(dg).data
        _trees[key] = SurfaceTree(mesh, surface)
    return _trees[key]


def viewport_picker(context, region, mask):
    rv = context.region_data or context.space_data.region_3d
    key = (region.as_pointer(), region.width, region.height, context.evaluated_depsgraph_get().as_pointer(),
           tuple(tuple(row) for row in rv.perspective_matrix),
           tuple((s.as_pointer(), s.object.as_pointer() if s.object else 0) for s in mask.sources))
    picker = _view_pickers.get(key)
    if picker is None:
        if len(_view_pickers) >= 4:
            _view_pickers.clear()
        picker = _view_pickers[key] = ViewPicker(context, region, rv, mask)
    return picker


def brush_hit(context, region, xy, mask):
    return viewport_picker(context, region, mask).pick(context, xy)


def hit_uv(context, region, xy, mask):
    hit = brush_hit(context, region, xy, mask)
    return hit[0].xy if hit else None


def clear_views():
    _view_pickers.clear()


def clear_geometry():
    clear_views()
    _trees.clear()
    _surfaces.clear()
    face_indices.cache_clear()
