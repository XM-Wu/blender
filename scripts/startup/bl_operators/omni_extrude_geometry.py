# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Component extrusion, with stable vertex provenance and object-space symmetry."""

from itertools import product

import bmesh
from mathutils import Matrix, Vector
from mathutils.kdtree import KDTree


def mirror_planes(obj):
    planes = []
    for mod in obj.modifiers:
        if (mod.type != 'MIRROR' or not mod.show_viewport or
                not mod.show_in_editmode or not mod.use_clip):
            continue
        matrix = (mod.mirror_object.matrix_world.inverted_safe() @ obj.matrix_world
                  if mod.mirror_object else Matrix.Identity(4))
        for axis, enabled in enumerate(mod.use_axis):
            if enabled:
                planes.append((matrix, matrix.inverted_safe(), axis, mod.merge_threshold))
    return planes


def on_plane(co, plane):
    matrix, _inverse, axis, tolerance = plane
    return abs((matrix @ co)[axis]) <= tolerance


def clip_position(start, end, planes):
    end = end.copy()
    for matrix, inverse, axis, tolerance in planes:
        a, b = matrix @ start, matrix @ end
        if abs(a[axis]) <= tolerance or a[axis] * b[axis] < 0.0:
            b[axis] = 0.0
            end = inverse @ b
    return end


def symmetry_selection(bm, mesh, elements):
    """Expand whole components only; never select an unrelated connecting face/edge."""
    axes = (mesh.use_mirror_x, mesh.use_mirror_y, mesh.use_mirror_z)
    identity = Vector((1, 1, 1))
    signs = {v: identity.copy() for elem in elements for v in elem.verts}
    if not any(axes):
        return elements, signs, axes
    verts = list(bm.verts)
    tree = KDTree(len(verts))
    for i, v in enumerate(verts):
        tree.insert(v.co, i)
    tree.balance()
    pool = bm.faces if isinstance(elements[0], bmesh.types.BMFace) else bm.edges
    component_indices = {elem: i for i, elem in enumerate(pool)}
    result = set(elements)
    signs = {}
    # Match complete components, not independent nearest vertices. At split
    # seams/coincident strands a KD-tree may return vertices from different
    # components, even though a valid mirrored edge or face exists nearby.
    ordered = sorted(elements, key=lambda e: tuple(
        sum(v.co[i] for v in e.verts) / len(e.verts) if axes[i] else 0
        for i in range(3)), reverse=True)
    correspondences = {}
    used_components = {}
    for elem in ordered:
        source_verts = list(elem.verts)
        for sign_tuple in product(*[(1, -1) if enabled else (1,) for enabled in axes]):
            if sign_tuple == (1, 1, 1):
                counterpart, matched = elem, source_verts
            else:
                reflected = [Vector(tuple(v.co[i] * sign_tuple[i] for i in range(3)))
                             for v in source_verts]
                candidates = set()
                for _co, index, _distance in tree.find_range(reflected[0], 0.0001):
                    vertex = verts[index]
                    candidates.update(vertex.link_faces if isinstance(elem, bmesh.types.BMFace)
                                      else vertex.link_edges)
                best = None
                counterpart = None
                for candidate in candidates:
                    if (candidate.hide or len(candidate.verts) != len(source_verts) or
                            any(v.hide for v in candidate.verts)):
                        continue
                    used = used_components.get((sign_tuple, candidate))
                    if used is not None and used is not elem:
                        continue
                    candidate_verts = list(candidate.verts)
                    count = len(candidate_verts)
                    for step in (1, -1):
                        for start in range(count):
                            match = [candidate_verts[(start + step * i) % count] for i in range(count)]
                            errors = [(match[i].co - reflected[i]).length_squared for i in range(count)]
                            if any(error > 0.0001 ** 2 for error in errors):
                                continue
                            conflicts = sum(
                                correspondences.get((sign_tuple, v), match[i]) is not match[i]
                                for i, v in enumerate(source_verts))
                            connectivity = sum(abs(len(v.link_edges) - len(match[i].link_edges)) +
                                               abs(len(v.link_faces) - len(match[i].link_faces))
                                               for i, v in enumerate(source_verts))
                            score = (conflicts, sum(errors), connectivity, component_indices[candidate])
                            if best is None or score < best:
                                best, counterpart, matched = score, candidate, match
                if counterpart is None:
                    continue
                used_components[sign_tuple, counterpart] = elem
                for v, other in zip(source_verts, matched):
                    correspondences.setdefault((sign_tuple, v), other)
            result.add(counterpart)
            for v in matched:
                signs.setdefault(v, Vector(sign_tuple))
    # Stable order also makes independently extruded components deterministic.
    return [elem for elem in pool if elem in result], signs, axes


def _select_result(bm, elements):
    bm.select_history.clear()
    for sequence in (bm.faces, bm.edges, bm.verts):
        for elem in sequence:
            elem.select_set(False)
    for elem in elements:
        if elem.is_valid:
            elem.select_set(True)
    bm.select_flush_mode()
    if elements and isinstance(elements[-1], bmesh.types.BMFace) and elements[-1].is_valid:
        bm.faces.active = elements[-1]


def _component_groups(elements, kind, together):
    if not together:
        return [[elem] for elem in elements]
    remaining = set(elements)
    groups = []
    for seed in elements:
        if seed not in remaining:
            continue
        remaining.remove(seed)
        group, pending = [], [seed]
        while pending:
            elem = pending.pop()
            group.append(elem)
            neighbors = ({f for e in elem.edges for f in e.link_faces} if kind == 'FACE'
                         else {e for v in elem.verts for e in v.link_edges})
            for neighbor in neighbors & remaining:
                remaining.remove(neighbor)
                pending.append(neighbor)
        groups.append(group)
    return groups


def extrude(obj, kind, distance, offset, divisions=1, together=True,
            direction='NORMAL', translation=(0, 0, 0), taper=1.0):
    # Import lazily: the operator module owns the distance solver and is also our caller.
    from .mesh_omni_outset import _face_normal_displacement, _safe_normalized

    bm = bmesh.from_edit_mesh(obj.data)
    bm.normal_update()
    source = bm.faces if kind == 'FACE' else bm.edges
    selected = [elem for elem in source if elem.select and not elem.hide]
    if not selected:
        return False
    # Allocate custom data before building dictionaries keyed by BMVert: adding
    # a layer can relocate elements and invalidate their previous hash values.
    layer = bm.verts.layers.int.new('__omni_extrude_source')
    selected, signs, symmetry_axes = symmetry_selection(bm, obj.data, selected)
    planes = mirror_planes(obj)
    linear = obj.matrix_world.to_3x3()
    inverse = linear.inverted_safe()
    normal_matrix = inverse.transposed()
    translation = inverse @ Vector(translation)
    groups = _component_groups(selected, kind, together)
    # A temporary integer layer is copied by BMesh extrusion. Coincident source
    # vertices remain distinct (coordinate matching cannot provide this guarantee).
    for index, vert in enumerate(bm.verts):
        vert[layer] = index
    prepared = []
    try:
        for group in groups:
            group_set = set(group)
            verts = {v for elem in group for v in elem.verts}
            edges = {e for elem in group for e in elem.edges} if kind == 'FACE' else set(group)
            boundary = {e for e in edges if kind == 'EDGE' or
                        sum(f in group_set for f in e.link_faces) == 1}
            # A clipped mirror seam is not an outside border for an inset.
            boundary = {e for e in boundary if not any(
                all(on_plane(v.co, p) for v in e.verts) for p in planes)}
            center = sum((linear @ v.co for v in verts), Vector()) / len(verts)
            motion = {}
            for v in verts:
                normals, outward = [], []
                if kind == 'FACE':
                    normals = [_safe_normalized(normal_matrix @ f.normal)
                               for f in v.link_faces if f in group_set]
                else:
                    faces = {f for e in v.link_edges if e in edges for f in e.link_faces}
                    normals = [_safe_normalized(normal_matrix @ f.normal) for f in faces]
                for e in v.link_edges:
                    if e not in boundary:
                        continue
                    faces = [f for f in e.link_faces if kind == 'EDGE' or f in group_set]
                    if len(faces) == 1:
                        face = faces[0]
                        loop = next(loop for loop in face.loops if loop.edge == e)
                        tangent = _safe_normalized(linear @ (loop.link_loop_next.vert.co - loop.vert.co))
                        outward.append(_safe_normalized(tangent.cross(normal_matrix @ face.normal)))
                    elif kind == 'EDGE' and not faces:
                        # Wire edges have no face normal. Project a deterministic
                        # reference onto their perpendicular plane, including vertical wires.
                        tangent = _safe_normalized(linear @ (e.verts[1].co - e.verts[0].co))
                        sign = signs.get(v, Vector((1, 1, 1)))
                        reference = _safe_normalized(linear @ Vector((0, 0, sign.z)))
                        if abs(tangent.dot(reference)) > 0.95:
                            reference = _safe_normalized(linear @ Vector((0, sign.y, 0)))
                        outward.append(_safe_normalized(reference - tangent * reference.dot(tangent)))
                normal = _face_normal_displacement(normals)
                out = _face_normal_displacement(outward)
                if kind == 'EDGE' and out.length < 1e-8:
                    # Interior edges extrude along the adjacent surface normal.
                    out = normal if normal.length > 1e-8 else Vector((0, 0, 1))
                if kind == 'EDGE' and normal.length < 1e-8:
                    normal = out.copy()
                primary = normal if direction == 'NORMAL' else out
                if direction in {'X', 'Y', 'Z'}:
                    primary = Vector(tuple(float(i == 'XYZ'.index(direction)) for i in range(3)))
                secondary = -out if kind == 'FACE' else normal
                delta = inverse @ (primary * distance + secondary * offset) + translation
                # Directional transforms are reflected from the selected side.
                # Normal/outward vectors already come from the mirrored geometry.
                if direction in {'X', 'Y', 'Z'}:
                    axis_delta = inverse @ (primary * distance) + translation
                else:
                    axis_delta = translation
                sign = signs.get(v, Vector((1, 1, 1)))
                delta += Vector(tuple(axis_delta[i] * (sign[i] - 1) for i in range(3)))
                motion[v[layer]] = (v.co.copy(), delta, inverse @ (linear @ v.co - center))
            prepared.append((group, motion))

        original_faces = set(bm.faces)
        tips = []
        count = max(1, int(divisions))
        for group, motion in prepared:
            current = group
            for segment in range(1, count + 1):
                if kind == 'FACE':
                    edges = {e for f in current for e in f.edges}
                    excluded = {e for e in edges if e.is_boundary and any(
                        all(on_plane(v.co, p) for v in e.verts) for p in planes)}
                    ret = bmesh.ops.extrude_face_region(
                        bm, geom=list(current) + list(edges), edges_exclude=excluded)
                else:
                    ret = bmesh.ops.extrude_edge_only(bm, edges=current)
                new_verts = {v for v in ret['geom'] if isinstance(v, bmesh.types.BMVert)}
                fraction = segment / count
                for v in new_verts:
                    start, delta, radial = motion[v[layer]]
                    end = start + fraction * (delta + radial * (taper - 1.0))
                    # Keep existing symmetry-plane vertices on that plane.
                    for axis, enabled in enumerate(symmetry_axes):
                        if enabled and abs(start[axis]) < 0.0001:
                            end[axis] = 0.0
                    v.co = clip_position(start, end, planes)
                elem_type = bmesh.types.BMFace if kind == 'FACE' else bmesh.types.BMEdge
                current = [elem for elem in ret['geom'] if isinstance(elem, elem_type)
                           and all(v in new_verts for v in elem.verts)]
            tips.extend(current)
        new_faces = [f for f in bm.faces if f not in original_faces]
        # Do not leave an internal wall in a Mirror modifier's clipping plane.
        seam_faces = [f for f in new_faces if any(all(on_plane(v.co, p) for v in f.verts)
                                                 for p in planes)]
        if seam_faces:
            bmesh.ops.delete(bm, geom=seam_faces, context='FACES_ONLY')
        bm.normal_update()
        from .mesh_omni_outset import (
            _align_faces_to_existing_neighbors, _orient_connected_faces_from_seeds,
            _set_edge_smoothing_by_angle,
        )
        new_faces = [f for f in new_faces if f.is_valid]
        anchors = _align_faces_to_existing_neighbors(bm, new_faces)
        _orient_connected_faces_from_seeds(bm, new_faces, anchors)
        _set_edge_smoothing_by_angle({e for f in new_faces for e in f.edges})
        _select_result(bm, [elem for elem in tips if elem.is_valid])
    finally:
        bm.verts.layers.int.remove(layer)
    bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=True)
    return True
