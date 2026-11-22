# SPDX-License-Identifier: GPL-2.0-or-later
"""Bind SDF key images to base mesh face corners and resample them after UV edits."""

import base64
import zlib

import bpy

import numpy as np

from . import sdf_field as field


# The vertex identifies a corner within its face, including at UV seams.
CORNER = np.dtype([("face", "<i4"), ("vertex", "<i4"), ("uv", "<f4", (2,))])


def capture(mesh, layer, faces):
    records = []
    for index in sorted(faces):
        if not 0 <= index < len(mesh.polygons):
            raise ValueError("A mask face no longer exists")
        for loop in mesh.polygons[index].loop_indices:
            records.append((index, mesh.loops[loop].vertex_index, layer.uv[loop].vector[:]))
    return np.asarray(records, dtype=CORNER)


def pack(records):
    return base64.b85encode(zlib.compress(records.tobytes())).decode("ascii")


def unpack(text):
    return np.frombuffer(zlib.decompress(base64.b85decode(text)), dtype=CORNER)


def triangles(mesh, records, target):
    source_uv = {(int(r['face']), int(r['vertex'])): r['uv'] for r in records}
    faces = set(records['face'].tolist())
    for tri in mesh.loop_triangles:
        face = tri.polygon_index
        if face in faces:
            yield (np.array([source_uv[face, v] for v in tri.vertices]),
                   np.array([target.uv[i].vector[:] for i in tri.loops]))


def backup_records(mask, path, previous_name):
    # Read only the named mesh data into a temporary Main. No objects or
    # materials from the reference project become part of the user's file.
    records = []
    with bpy.types.BlendData.temp_data() as data:
        names = {src.object.data.name for src in mask.sources if src.object and src.object.type == 'MESH'}
        with data.libraries.load(bpy.path.abspath(path)) as (available, loaded):
            missing = names.difference(available.meshes)
            if missing:
                raise ValueError(f"Missing reference mesh: {', '.join(sorted(missing))}")
            loaded.meshes = sorted(names)
        meshes = {mesh.name: mesh for mesh in loaded.meshes}
        for src in mask.sources:
            if not src.object or src.object.type != 'MESH':
                raise ValueError("Missing source mesh")
            mesh = meshes[src.object.data.name]
            layer = mesh.uv_layers.get(previous_name or src.uv_name)
            if layer is None:
                raise ValueError(f"{mesh.name}: original UV map is missing from the reference file")
            records.append(capture(mesh, layer, map(int, src.faces.split(","))))
    return records


def sample_indices(old_uv, old_mask):
    size = len(old_mask)
    xy = old_uv * size
    pixels = np.floor(xy).astype(np.int64)
    x, y = pixels.T
    valid = (x >= 0) & (x < size) & (y >= 0) & (y < size)
    result = np.full(len(x), size * size, np.int32)
    result[valid] = y[valid] * size + x[valid]
    # A sample along an island outline may round to a center just outside
    # the old mask. Choose the closest covered neighbor for that edge sample.
    edge = np.flatnonzero(valid)
    edge = edge[~old_mask[y[edge], x[edge]]]
    if len(edge):
        best = np.full(len(edge), np.inf)
        for dx, dy in ((-1,0),(1,0),(0,-1),(0,1),(-1,-1),(-1,1),(1,-1),(1,1)):
            nx = np.clip(x[edge]+dx, 0, size-1)
            ny = np.clip(y[edge]+dy, 0, size-1)
            dist = (nx+.5-xy[edge,0])**2 + (ny+.5-xy[edge,1])**2
            take = old_mask[ny,nx] & (dist < best)
            best[take] = dist[take]
            result[edge[take]] = ny[take]*size + nx[take]
    return result


def prepare(mask, *, active=False, target_name="", previous_name="", reference_file="", merge=False):
    """Build the complete replacement before mutating any RNA data."""
    size = mask.size
    old_mask = field.decode(mask.coverage, size)
    lookup = np.full((size, size), -1, np.int32)
    interior = np.zeros_like(old_mask)
    old_footprint = np.zeros_like(old_mask)
    conflicts = []
    bindings = []
    reference = backup_records(mask, reference_file, previous_name) if reference_file else None
    for source_index, src in enumerate(mask.sources):
        obj = src.object
        if not obj or obj.type != 'MESH':
            raise ValueError("Missing source mesh")
        mesh = obj.data
        layers = mesh.uv_layers
        target = layers.active if active else layers.get(target_name or src.uv_name)
        if target is None:
            raise ValueError(f"{obj.name}: target UV map is missing")
        faces = set(map(int, src.faces.split(",")))
        new = capture(mesh, target, faces)
        if reference is not None:
            old = reference[source_index]
        elif src.uv_snapshot and not previous_name:
            old = unpack(src.uv_snapshot)
        else:
            previous = layers.get(previous_name or src.uv_name)
            if previous is None:
                raise ValueError(f"{obj.name}: original UV map is missing")
            old = capture(mesh, previous, faces)
        if (not np.array_equal(old['face'], new['face']) or
                not np.array_equal(old['vertex'], new['vertex'])):
            raise ValueError(f"{obj.name}: face topology no longer matches the saved corners")
        bindings.append((src, target.name, pack(new)))
        for old_uv, new_uv in triangles(mesh, old, target):
            field.raster_triangle(old_footprint, old_uv)
            grid = field.triangle_grid(size, new_uv)
            if grid is None:
                continue
            region, inside, edges, area = grid
            yi, xi = np.nonzero(inside)
            if not len(xi):
                continue
            weights = np.stack([edge[yi, xi] for edge in edges], axis=1) / area
            source = sample_indices(weights @ old_uv, old_mask)
            y, x = yi + region[0].start, xi + region[1].start
            previous = lookup[y, x]
            inner = weights.min(axis=1) > 1e-8
            overlaps = (previous >= 0) & (previous != source)
            if not merge:
                overlaps &= inner & interior[y, x]
            if overlaps.any():
                conflicts.append((y[overlaps], x[overlaps], previous[overlaps], source[overlaps]))
            take = (previous == -1) | (inner & ~interior[y, x])
            lookup[y[take], x[take]] = source[take]
            interior[y, x] |= inner
    if (not np.array_equal(old_footprint, old_mask) and
            any((field.decode(key.bitmap, size) & old_mask).any() for key in mask.keys)):
        raise ValueError("Original UV layout does not match the mask. Choose the original UV map")
    coverage = lookup >= 0
    if not coverage.any():
        raise ValueError("Target UVs do not cover any pixels inside the 0–1 tile")
    outputs = []
    for key in mask.keys:
        original = field.decode(key.bitmap, size)
        samples = np.append((original & old_mask).ravel(), False)

        result = original & ~(coverage | old_mask)
        result[coverage] = samples[lookup[coverage]]
        for y, x, first, second in conflicts:
            if merge:
                result[y, x] |= samples[first] | samples[second]
            elif np.any(samples[first] != samples[second]):
                raise ValueError(f"Conflicting target UVs at key {key.value:.4f}. Unstack UVs or enable Merge White")
        outputs.append(field.encode(result))
    return field.encode(coverage), bindings, outputs


def bind_legacy(mask):
    """Only adopt a legacy layer if it reproduces the saved mask footprint."""
    if not mask.sources or all(src.uv_snapshot for src in mask.sources):
        return
    footprint = np.zeros((mask.size, mask.size), bool)
    bindings = []
    for src in mask.sources:
        obj = src.object
        if not obj or obj.type != 'MESH':
            return
        faces = set(map(int, src.faces.split(",")))
        if obj.data.is_editmode:
            import bmesh
            bm = bmesh.from_edit_mesh(obj.data)
            layer = bm.loops.layers.uv.get(src.uv_name)
            if layer is None:
                return
            bm.faces.index_update()
            bm.verts.index_update()
            records = np.asarray([(f.index, loop.vert.index, loop[layer].uv[:])
                                  for f in bm.faces if f.index in faces for loop in f.loops], dtype=CORNER)
            if set(records['face']) != faces:
                return
            for tri in bm.calc_loop_triangles():
                if tri[0].face.index in faces:
                    field.raster_triangle(footprint, [loop[layer].uv[:] for loop in tri])
        else:
            layer = obj.data.uv_layers.get(src.uv_name)
            if (layer is None or len(layer.uv) != len(obj.data.loops) or not faces or
                    min(faces) < 0 or max(faces) >= len(obj.data.polygons)):
                return
            records = capture(obj.data, layer, faces)
            for old_uv, _ in triangles(obj.data, records, layer):
                field.raster_triangle(footprint, old_uv)
        if not src.uv_snapshot:
            bindings.append((src, records))
    if np.array_equal(footprint, field.decode(mask.coverage, mask.size)):
        for src, records in bindings:
            src.uv_snapshot = pack(records)
