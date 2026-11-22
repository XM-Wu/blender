# SPDX-License-Identifier: GPL-2.0-or-later
"""Polygon-edge attributes for anti-aliased SDF wire previews."""

import numpy as np

from .sdf_pick import face_indices, mesh_array


def edge_distances(mesh, faces, selected, loops, cage):
    """Barycentric coordinates for real edges; constant one hides tessellation diagonals."""
    starts = mesh_array(mesh.polygons, "loop_start", 1, np.int32)
    counts = mesh_array(mesh.polygons, "loop_total", 1, np.int32)
    # Each component corresponds to the edge opposite the triangle corner.
    a, b = np.roll(loops, -1, axis=1), np.roll(loops, -2, axis=1)
    first = starts[faces[selected], None]
    size = counts[faces[selected], None]
    forward = (a + 1 - first) % size == b - first
    backward = (b + 1 - first) % size == a - first
    boundary = forward | backward
    if cage:
        original = mesh_array(mesh.polygons, "original_index", 1, np.int32)
        if np.any(original >= 0):
            # Subdivision adds polygon boundaries inside the same original
            # face. Keep the deformed cage and open borders readable.
            edge = mesh_array(mesh.loops, "edge_index", 1, np.int32)
            owners = np.repeat(original, counts)
            lo = np.full(len(mesh.edges), np.iinfo(np.int32).max, np.int32)
            hi = np.full(len(mesh.edges), -1, np.int32)
            np.minimum.at(lo, edge, owners)
            np.maximum.at(hi, edge, owners)
            visible = (lo != hi) | (lo < 0) | (np.bincount(edge, minlength=len(mesh.edges)) == 1)
            boundary &= visible[edge[np.where(forward, a, b)]]
    return np.where(boundary[:, None, :], np.eye(3, dtype=np.float32), np.float32(1)).reshape(-1, 3)


def batch(source, dg, shader, is_3d, surface, cache):
    from gpu_extras.batch import batch_for_shader

    obj = source.object
    token = ('wire', is_3d, dg.as_pointer(), obj.as_pointer(), source.faces, source.uv_name)
    if token in cache:
        return cache[token]
    mesh = obj.evaluated_get(dg).data if is_3d else obj.data
    uv = mesh.uv_layers.get(source.uv_name)
    if uv is None or len(uv.uv) != len(mesh.loops):
        return None
    faces = mesh_array(mesh.loop_triangles, "polygon_index", 1, np.int32)
    face_ids = surface.face_indices if is_3d else tuple(face_indices(source.faces))
    selected = np.isin(faces, face_ids)
    if not selected.any():
        return None
    loops = mesh_array(mesh.loop_triangles, "loops", 3, np.int32)[selected]
    if is_3d:
        positions = surface.positions.reshape(-1, 3)
        coords = surface.uvs.reshape(-1, 2)
    else:
        positions = coords = mesh_array(uv.uv, "vector", 2, np.float32)[loops].reshape(-1, 2)
    cache[token] = batch_for_shader(shader, 'TRIS', {
        'pos': positions,
        'texCoord': coords,
        'edgeCoord': edge_distances(mesh, faces, selected, loops, cage=is_3d),
    })
    return cache[token]


FRAGMENT = """
void main() {
    float ink = texture(inkMap, uv).r;
    float coverage = texture(maskMap, uv).r;
    if (coverage <= 0.0) discard;
    vec3 gradient = max(sqrt(dFdx(edgeUV)*dFdx(edgeUV) + dFdy(edgeUV)*dFdy(edgeUV)), vec3(1e-6));
    vec3 distancePx = edgeUV / gradient;
    float d = min(distancePx.x, min(distancePx.y, distancePx.z));
    float line = (1.0 - smoothstep(0.15, 1.05, d)) * opacity;
    if (wireOnly != 0 && line <= 0.0) discard;
    vec3 tint = mix(vec3(0.32, 0.70, 0.80), vec3(0.12, 0.34, 0.43), ink);
    color = vec4(highlightConflict(mix(sdfBaseColor(ink), tint, line)), coverage);
}
"""
