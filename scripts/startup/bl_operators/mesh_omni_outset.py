# SPDX-FileCopyrightText: 2026 OmniOutset Authors
# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Built-in port of the OmniOutset add-on (ZXY, GPL-3.0-or-later):
# face extrusion along normals with uniform side walls, and equidistant edge extrusion.

import bmesh
import bpy
from math import cos, radians, sqrt
from mathutils import Matrix, Vector

from . import omni_outset_hud as hud
from . import omni_outset_translation as translation

i18n_contexts = bpy.app.translations.contexts

VERT_MATCH_EPSILON = 0.0001
DRAG_REFERENCE_EPSILON = 0.000000000001
COORD_KEY_DIGITS = 6
BASE_DRAG_UNITS_PER_PIXEL = 0.0025
PRECISION_DRAG_MULTIPLIER = 0.2
MIN_DRAG_SENSITIVITY = 0.000000000001
MAX_DRAG_SENSITIVITY = 100000.0
DEFAULT_DRAG_SENSITIVITY_MULTIPLIER = 1.0
MIN_DRAG_SENSITIVITY_MULTIPLIER = 0.0
MAX_DRAG_SENSITIVITY_MULTIPLIER = 1.0
HARD_EDGE_ANGLE_DEGREES = 30.0
HARD_EDGE_NORMAL_DOT = cos(radians(HARD_EDGE_ANGLE_DEGREES))
DIAGONAL_DRAG_NORMALIZER = 1.0 / sqrt(2.0)
LENGTH_UNIT_TO_METERS = {
    "KILOMETERS": 1000.0,
    "METERS": 1.0,
    "CENTIMETERS": 0.01,
    "MILLIMETERS": 0.001,
    "MICROMETERS": 0.000001,
    "MILES": 1609.344,
    "FEET": 0.3048,
    "INCHES": 0.0254,
    "THOU": 0.0000254,
}


def _safe_normalized(vector):
    if vector.length < VERT_MATCH_EPSILON:
        return Vector()
    return vector.normalized()


def _edit_mesh_poll_error(context, selection_index):
    obj = context.edit_object
    if obj is None or obj.type != "MESH" or context.mode != "EDIT_MESH":
        return "Enter Mesh Edit Mode"

    if not context.tool_settings.mesh_select_mode[selection_index]:
        return (
            "Switch to Face Select Mode"
            if selection_index == 2
            else "Switch to Edge Select Mode"
        )
    return None


def _smart_call_poll_error(context):
    obj = context.edit_object
    if obj is None or obj.type != "MESH" or context.mode != "EDIT_MESH":
        return "Enter Mesh Edit Mode"

    select_mode = context.tool_settings.mesh_select_mode
    if not (select_mode[1] or select_mode[2]):
        return "Switch to Face or Edge Select Mode"

    bm = bmesh.from_edit_mesh(obj.data)
    if select_mode[2] and any(face.select for face in bm.faces):
        return None
    if select_mode[1] and any(edge.select for edge in bm.edges):
        return None
    return "Select at least one face or edge"


def _face_normal_displacement(normals):
    """Return a unit-distance displacement satisfying each face normal."""
    valid_normals = [
        _safe_normalized(normal)
        for normal in normals
        if normal.length >= VERT_MATCH_EPSILON
    ]
    if not valid_normals:
        return Vector()
    if len(valid_normals) == 1:
        return valid_normals[0].copy()

    if len(valid_normals) == 2:
        normal_1, normal_2 = valid_normals
        denominator = 1.0 + _clamp(normal_1.dot(normal_2), -1.0, 1.0)
        if denominator > VERT_MATCH_EPSILON:
            # Minimum-length intersection of the two unit-offset face planes.
            return (normal_1 + normal_2) / denominator

        # Opposing planes have no finite shared offset. Keep the result bounded
        # instead of allowing an almost singular solve to create long spikes.
        bisector = normal_1 + normal_2
        if bisector.length >= VERT_MATCH_EPSILON:
            return bisector.normalized()
        return normal_1.copy()

    # Solve complex corners in a regularized least-squares sense. The small
    # diagonal term selects a bounded minimum-length result when the normals do
    # not span all three dimensions.
    normal_matrix = Matrix(((0.0, 0.0, 0.0),) * 3)
    target = Vector()
    for normal in valid_normals:
        target += normal
        normal_matrix += Matrix(
            (
                (normal.x * normal.x, normal.x * normal.y, normal.x * normal.z),
                (normal.y * normal.x, normal.y * normal.y, normal.y * normal.z),
                (normal.z * normal.x, normal.z * normal.y, normal.z * normal.z),
            )
        )

    regularization = max(
        normal_matrix[0][0] + normal_matrix[1][1] + normal_matrix[2][2],
        1.0,
    ) * 0.000001
    for axis in range(3):
        normal_matrix[axis][axis] += regularization

    displacement = normal_matrix.inverted_safe() @ target
    if displacement.length < VERT_MATCH_EPSILON:
        displacement = _safe_normalized(target)
    return displacement


def _clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def _trim_number(text):
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _format_distance(value):
    magnitude = abs(value)
    if magnitude >= 100.0:
        return _trim_number(f"{value:.2f}")
    if magnitude >= 1.0:
        return _trim_number(f"{value:.3f}")
    if magnitude >= 0.01:
        return _trim_number(f"{value:.4f}")
    if magnitude >= 0.0001:
        return _trim_number(f"{value:.6f}")
    if magnitude > 0.0:
        return _trim_number(f"{value:.9f}")
    return "0"


class _DistanceDisplay:
    def __init__(self, value):
        self.value = value

    def __format__(self, format_spec):
        del format_spec
        return _format_distance(self.value)


def _get_settings(context):
    window_manager = getattr(context, "window_manager", None)
    if window_manager is None:
        return None
    return getattr(window_manager, "omni_outset", None)


def _shape_key_locked(operator, context):
    from bpy_extras.object_utils import object_report_if_active_shape_key_is_locked

    return object_report_if_active_shape_key_is_locked(context.object, operator)


def _length_unit_size_in_blender_units(context):
    unit_settings = getattr(context.scene, "unit_settings", None)
    if unit_settings is None:
        return 1.0

    length_unit = getattr(unit_settings, "length_unit", "ADAPTIVE")
    unit_meters = LENGTH_UNIT_TO_METERS.get(length_unit)
    if unit_meters is None:
        return 1.0

    scale_length = getattr(unit_settings, "scale_length", 1.0) or 1.0
    if scale_length <= 0.0:
        return 1.0

    return unit_meters / scale_length


def _median_positive(values):
    values = sorted(value for value in values if value > DRAG_REFERENCE_EPSILON)
    if not values:
        return 0.0

    midpoint = len(values) // 2
    if len(values) % 2:
        return values[midpoint]

    return (values[midpoint - 1] + values[midpoint]) * 0.5


def _bounds_reference_length(verts):
    coords = [vert.co for vert in verts]
    if not coords:
        return 0.0

    min_x = min(co.x for co in coords)
    min_y = min(co.y for co in coords)
    min_z = min(co.z for co in coords)
    max_x = max(co.x for co in coords)
    max_y = max(co.y for co in coords)
    max_z = max(co.z for co in coords)
    diagonal = Vector((max_x - min_x, max_y - min_y, max_z - min_z)).length

    if diagonal <= DRAG_REFERENCE_EPSILON:
        return 0.0

    return diagonal * 0.25


def _selection_reference_length(verts, edges, fallback):
    edge_reference = _median_positive(edge.calc_length() for edge in edges)
    if edge_reference > 0.0:
        return edge_reference

    bounds_reference = _bounds_reference_length(verts)
    if bounds_reference > 0.0:
        return bounds_reference

    return fallback


def _drag_sensitivity(context, event, reference_length, multiplier=1.0):
    preferences = _get_settings(context)
    use_adaptive_scale = getattr(preferences, "use_adaptive_drag_scale", True)

    fallback_size = _length_unit_size_in_blender_units(context)
    drag_scale = reference_length if use_adaptive_scale else fallback_size
    if drag_scale <= DRAG_REFERENCE_EPSILON:
        drag_scale = fallback_size

    multiplier = _clamp(
        multiplier,
        MIN_DRAG_SENSITIVITY_MULTIPLIER,
        MAX_DRAG_SENSITIVITY_MULTIPLIER,
    )
    if multiplier == 0.0:
        return 0.0

    sensitivity = BASE_DRAG_UNITS_PER_PIXEL * drag_scale * multiplier
    if event.shift:
        sensitivity *= PRECISION_DRAG_MULTIPLIER

    return _clamp(sensitivity, MIN_DRAG_SENSITIVITY, MAX_DRAG_SENSITIVITY)


def _unified_drag_delta(delta_x, delta_y):
    return (delta_x + delta_y) * DIAGONAL_DRAG_NORMALIZER


def _coord_key(co):
    return (
        round(co.x, COORD_KEY_DIGITS),
        round(co.y, COORD_KEY_DIGITS),
        round(co.z, COORD_KEY_DIGITS),
    )


def _build_old_vert_lookup(verts):
    lookup = {}
    for vert in verts:
        lookup.setdefault(_coord_key(vert.co), []).append(vert)
    return lookup


def _take_matching_old_vert(new_vert, old_vert_lookup):
    key = _coord_key(new_vert.co)
    candidates = old_vert_lookup.get(key, [])

    if candidates:
        if len(candidates) == 1:
            return candidates.pop()

        best_index = None
        best_distance = float("inf")
        for index, old_vert in enumerate(candidates):
            distance = (old_vert.co - new_vert.co).length
            if distance < best_distance:
                best_distance = distance
                best_index = index

        if best_index is not None and best_distance <= VERT_MATCH_EPSILON:
            return candidates.pop(best_index)

    best_key = None
    best_index = None
    best_distance = float("inf")
    for lookup_key, lookup_candidates in old_vert_lookup.items():
        for index, old_vert in enumerate(lookup_candidates):
            distance = (old_vert.co - new_vert.co).length
            if distance < best_distance:
                best_distance = distance
                best_key = lookup_key
                best_index = index

    if best_key is not None and best_distance <= VERT_MATCH_EPSILON:
        return old_vert_lookup[best_key].pop(best_index)

    return None


def _update_edit_mesh(mesh, topology_changed):
    bmesh.update_edit_mesh(
        mesh,
        loop_triangles=topology_changed,
        destructive=topology_changed,
    )


def _update_edit_mesh_fast(mesh):
    bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)


def _stored_drag_sensitivity(context):
    preferences = _get_settings(context)
    return _clamp(
        getattr(
            preferences,
            "drag_sensitivity",
            DEFAULT_DRAG_SENSITIVITY_MULTIPLIER,
        ),
        MIN_DRAG_SENSITIVITY_MULTIPLIER,
        MAX_DRAG_SENSITIVITY_MULTIPLIER,
    )


def _store_drag_sensitivity(context, value):
    preferences = _get_settings(context)
    if preferences is not None:
        preferences.drag_sensitivity = _clamp(
            value,
            MIN_DRAG_SENSITIVITY_MULTIPLIER,
            MAX_DRAG_SENSITIVITY_MULTIPLIER,
        )


def _show_adjust_last_operation_region(context):
    space_data = context.space_data
    if isinstance(space_data, bpy.types.SpaceView3D):
        space_data.show_region_hud = True


def _valid_faces(faces):
    return [face for face in faces if face.is_valid]


def _faces_created_since(bm, original_faces):
    return [
        face
        for face in bm.faces
        if face.is_valid and face not in original_faces
    ]


def _extrusion_edges_from_faces(faces):
    return list(
        {
            edge
            for face in _valid_faces(faces)
            for edge in face.edges
            if edge.is_valid
        }
    )


def _set_edge_smoothing_by_angle(edges):
    for edge in edges:
        if not edge.is_valid:
            continue

        linked_faces = [face for face in edge.link_faces if face.is_valid]
        if len(linked_faces) != 2:
            edge.smooth = True
            continue

        normal_dot = linked_faces[0].normal.dot(linked_faces[1].normal)
        edge.smooth = normal_dot >= HARD_EDGE_NORMAL_DOT


def _align_faces_to_direction(bm, faces, direction):
    direction = _safe_normalized(direction)
    if direction.length < DRAG_REFERENCE_EPSILON:
        return

    faces_to_flip = []
    for face in _valid_faces(faces):
        face.normal_update()
        if face.normal.dot(direction) < 0.0:
            faces_to_flip.append(face)

    if faces_to_flip:
        bmesh.ops.reverse_faces(bm, faces=faces_to_flip)
        bm.normal_update()


def _face_cap_faces(faces, cap_verts):
    cap_vert_set = set(cap_verts)
    return [
        face
        for face in faces
        if face.is_valid and all(vert in cap_vert_set for vert in face.verts)
    ]


def _face_edge_direction(face, edge):
    for loop in face.loops:
        if loop.edge == edge:
            return loop.vert, loop.link_loop_next.vert
    return None


def _align_faces_to_existing_neighbors(bm, faces):
    new_face_set = set(_valid_faces(faces))
    faces_to_flip = set()
    anchored_faces = set()

    for face in new_face_set:
        for edge in face.edges:
            direction = _face_edge_direction(face, edge)
            if direction is None:
                continue

            for neighbor in edge.link_faces:
                if neighbor is face or neighbor in new_face_set or not neighbor.is_valid:
                    continue

                neighbor_direction = _face_edge_direction(neighbor, edge)
                if neighbor_direction is None:
                    continue

                anchored_faces.add(face)
                if direction == neighbor_direction:
                    faces_to_flip.add(face)
                break

            if face in faces_to_flip:
                break

    if faces_to_flip:
        bmesh.ops.reverse_faces(bm, faces=list(faces_to_flip))
        bm.normal_update()

    return anchored_faces


def _orient_connected_faces_from_seeds(bm, faces, seed_faces):
    face_set = set(_valid_faces(faces))
    seeds = [face for face in seed_faces if face in face_set]
    visited = set()

    for seed in seeds + list(face_set):
        if seed in visited:
            continue

        visited.add(seed)
        pending = [seed]
        while pending:
            face = pending.pop()
            for edge in face.edges:
                face_direction = _face_edge_direction(face, edge)
                if face_direction is None:
                    continue

                for neighbor in list(edge.link_faces):
                    if neighbor is face or neighbor not in face_set or neighbor in visited:
                        continue

                    neighbor_direction = _face_edge_direction(neighbor, edge)
                    if neighbor_direction is None:
                        continue
                    if face_direction == neighbor_direction:
                        bmesh.ops.reverse_faces(bm, faces=[neighbor])

                    visited.add(neighbor)
                    pending.append(neighbor)

    bm.normal_update()


def _restore_bmesh_from_backup(bm, backup_bm, mesh):
    if backup_bm is None:
        return

    bm.clear()
    temp_mesh = bpy.data.meshes.new("__omnioutset_restore")
    try:
        backup_bm.to_mesh(temp_mesh)
        bm.from_mesh(temp_mesh)
    finally:
        bpy.data.meshes.remove(temp_mesh)

    _update_edit_mesh(mesh, topology_changed=True)


def _free_backup(operator):
    backup = getattr(operator, "_bm_backup", None)
    if backup is not None:
        backup.free()
        operator._bm_backup = None

    if bpy.app.version < (5, 0, 0):
        # Blender 4.x can crash while clearing undo history if a finished
        # operator still owns a wrapper for the edit-mode BMesh.
        operator.bm = None
        operator.obj = None


def _compute_face_extrude_data(sel_faces):
    sel_face_set = set(sel_faces)

    has_unselected_neighbor = any(
        (not link_face.select)
        for face in sel_faces
        for edge in face.edges
        for link_face in edge.link_faces
    )

    top_verts = list({vert for face in sel_faces for vert in face.verts})

    vert_extrude_vectors = {}
    for vert in top_verts:
        selected_normals = []
        for link_face in vert.link_faces:
            if link_face in sel_face_set:
                selected_normals.append(link_face.normal)
        vert_extrude_vectors[vert] = _face_normal_displacement(selected_normals)

    selected_edges = {edge for face in sel_faces for edge in face.edges}
    boundary_edges = [
        edge
        for edge in selected_edges
        if sum(1 for face in edge.link_faces if face in sel_face_set) == 1
    ]
    boundary_edge_set = set(boundary_edges)

    boundary_verts = list({vert for edge in boundary_edges for vert in edge.verts})

    vert_outward_vectors = {}
    for vert in boundary_verts:
        connected_boundary_edges = [
            edge for edge in vert.link_edges if edge in boundary_edge_set
        ]

        outward_normals = []
        for edge in connected_boundary_edges:
            face = next((f for f in edge.link_faces if f in sel_face_set), None)
            if face is None:
                continue

            for loop in face.loops:
                if loop.edge == edge:
                    edge_vector = loop.link_loop_next.vert.co - loop.vert.co
                    if edge_vector.length >= VERT_MATCH_EPSILON:
                        tangent = edge_vector.normalized()
                        outward = tangent.cross(face.normal)
                        if outward.length >= VERT_MATCH_EPSILON:
                            outward_normals.append(outward.normalized())
                    break

        if len(outward_normals) >= 2:
            normal_1, normal_2 = outward_normals[:2]
            bisector = normal_1 + normal_2
            if bisector.length < VERT_MATCH_EPSILON:
                vert_outward_vectors[vert] = normal_1
            else:
                bisector.normalize()
                cos_theta = max(0.01, normal_1.dot(bisector))
                factor = min(3.0, 1.0 / cos_theta)
                vert_outward_vectors[vert] = bisector * factor
        elif len(outward_normals) == 1:
            vert_outward_vectors[vert] = outward_normals[0]

    return (
        has_unselected_neighbor,
        top_verts,
        vert_extrude_vectors,
        vert_outward_vectors,
    )


def _compute_edge_extrude_data(sel_edges, object_matrix):
    sel_edge_set = set(sel_edges)
    sel_verts = list({vert for edge in sel_edges for vert in edge.verts})
    linear_matrix = object_matrix.to_3x3()
    inverse_linear = linear_matrix.inverted_safe()
    normal_matrix = inverse_linear.transposed()

    vert_outward_vectors = {}
    vert_offset_vectors = {}

    for vert in sel_verts:
        connected_sel_edges = [edge for edge in vert.link_edges if edge in sel_edge_set]
        outward_normals = []
        offset_normals = []

        for edge in connected_sel_edges:
            face = next((item for item in edge.link_faces if item.is_valid), None)
            if face is None:
                continue

            world_face_normal = _safe_normalized(normal_matrix @ face.normal)
            offset_normals.append(world_face_normal)
            for loop in face.loops:
                if loop.edge != edge:
                    continue

                local_edge_vector = loop.link_loop_next.vert.co - loop.vert.co
                world_edge_vector = linear_matrix @ local_edge_vector
                if world_edge_vector.length < VERT_MATCH_EPSILON:
                    break

                outward = world_edge_vector.normalized().cross(world_face_normal)
                if outward.length < VERT_MATCH_EPSILON:
                    break

                outward.normalize()
                edge_center = (edge.verts[0].co + edge.verts[1].co) * 0.5
                toward_face = linear_matrix @ (
                    face.calc_center_median() - edge_center
                )
                if outward.dot(toward_face) > 0.0:
                    outward.negate()
                outward_normals.append(outward)
                break

        if len(outward_normals) == 2:
            normal_1, normal_2 = outward_normals
            denominator = 1.0 + _clamp(normal_1.dot(normal_2), -1.0, 1.0)
            if denominator > VERT_MATCH_EPSILON:
                # Intersection of the two unit-offset edge lines.
                world_displacement = (normal_1 + normal_2) / denominator
                vert_outward_vectors[vert] = inverse_linear @ world_displacement
            else:
                vert_outward_vectors[vert] = inverse_linear @ normal_1
        elif len(outward_normals) > 2:
            world_displacement = _face_normal_displacement(
                outward_normals
            )
            vert_outward_vectors[vert] = inverse_linear @ world_displacement
        elif outward_normals:
            vert_outward_vectors[vert] = inverse_linear @ outward_normals[0]
        else:
            vert_outward_vectors[vert] = Vector()

        if offset_normals:
            world_offset = _safe_normalized(sum(offset_normals, Vector()))
            vert_offset_vectors[vert] = inverse_linear @ world_offset
            if vert_offset_vectors[vert].length < VERT_MATCH_EPSILON:
                vert_offset_vectors[vert] = Vector((0.0, 0.0, 1.0))
        else:
            vert_offset_vectors[vert] = Vector((0.0, 0.0, 1.0))

    return vert_outward_vectors, vert_offset_vectors


def _edge_displacement(
    extrude_direction,
    offset_direction,
    extrude,
    offset,
):
    return (
        (extrude_direction * extrude)
        + (offset_direction * offset)
    )


def _extrude_edges_segmented(
    bm,
    sel_edges,
    object_matrix,
    extrude,
    offset,
    divisions,
    keep_faces_together,
):
    edge_groups = [list(sel_edges)] if keep_faces_together else [
        [edge] for edge in sel_edges
    ]
    prepared_groups = []

    for edge_group in edge_groups:
        extrude_vectors, offset_vectors = _compute_edge_extrude_data(
            edge_group,
            object_matrix,
        )
        motion_by_vert = {
            vert: (
                extrude_vectors.get(vert, Vector()),
                offset_vectors.get(vert, Vector((0.0, 0.0, 1.0))),
            )
            for edge in edge_group
            for vert in edge.verts
        }
        prepared_groups.append((edge_group, motion_by_vert))

    segment_count = max(1, int(divisions))
    original_faces = set(bm.faces)

    for edge_group, motion_by_vert in prepared_groups:
        current_edges = edge_group
        current_motion = motion_by_vert

        for _segment_index in range(segment_count):
            ret = bmesh.ops.extrude_edge_only(bm, edges=current_edges)
            new_verts = [
                vert
                for vert in ret["geom"]
                if isinstance(vert, bmesh.types.BMVert)
            ]
            new_vert_set = set(new_verts)
            current_edges = [
                edge
                for edge in ret["geom"]
                if isinstance(edge, bmesh.types.BMEdge)
                and all(vert in new_vert_set for vert in edge.verts)
            ]
            next_motion = {}

            for new_vert in new_verts:
                old_vert = next(
                    (
                        edge.other_vert(new_vert)
                        for edge in new_vert.link_edges
                        if edge.other_vert(new_vert) in current_motion
                    ),
                    None,
                )
                if old_vert is None:
                    continue

                directions = current_motion[old_vert]
                new_vert.co = old_vert.co + (
                    _edge_displacement(
                        directions[0],
                        directions[1],
                        extrude,
                        offset,
                    )
                    / segment_count
                )
                next_motion[new_vert] = directions

            current_motion = next_motion

    return _faces_created_since(bm, original_faces)


def _execute_face_geometry(
    context,
    extrude_dist,
    outward_offset,
    reporter=None,
    keep_faces_together=True,
):
    obj = context.edit_object
    bm = bmesh.from_edit_mesh(obj.data)
    sel_faces = [face for face in bm.faces if face.select]
    if not sel_faces:
        if reporter is not None:
            reporter({"WARNING"}, "At least one face must be selected!")
        return {"CANCELLED"}

    has_unselected_neighbor = any(
        (not link_face.select)
        for face in sel_faces
        for edge in face.edges
        for link_face in edge.link_faces
    )
    reverse_extrusion_direction = (
        not has_unselected_neighbor and extrude_dist < 0.0
    )
    cap_direction_sign = -1.0 if reverse_extrusion_direction else 1.0
    original_faces = set(bm.faces)
    new_faces = []
    cap_face_groups = []

    if keep_faces_together:
        (
            _has_unselected_neighbor,
            top_verts,
            vert_extrude_vectors,
            vert_outward_vectors,
        ) = _compute_face_extrude_data(sel_faces)
        cap_normal = _safe_normalized(
            sum((face.normal for face in sel_faces), Vector())
        ) * cap_direction_sign
        ret = bmesh.ops.extrude_face_region(bm, geom=sel_faces)
        new_top_verts = [
            vert for vert in ret["geom"] if isinstance(vert, bmesh.types.BMVert)
        ]
        new_faces = _faces_created_since(bm, original_faces)
        cap_faces = _face_cap_faces(new_faces, new_top_verts)
        cap_face_groups.append((cap_faces, cap_normal))

        old_vert_lookup = _build_old_vert_lookup(top_verts)
        for new_vert in new_top_verts:
            old_vert = _take_matching_old_vert(new_vert, old_vert_lookup)
            if old_vert is None:
                continue

            new_vert.co += (
                vert_extrude_vectors.get(old_vert, Vector()) * extrude_dist
            )
            new_vert.co += (
                vert_outward_vectors.get(old_vert, Vector()) * outward_offset
            )
    else:
        for face in sel_faces:
            if not face.is_valid:
                continue

            (
                _has_unselected_neighbor,
                top_verts,
                vert_extrude_vectors,
                vert_outward_vectors,
            ) = _compute_face_extrude_data([face])
            face_normal = face.normal.copy() * cap_direction_sign
            ret = bmesh.ops.extrude_face_region(bm, geom=[face])
            new_top_verts = [
                vert
                for vert in ret["geom"]
                if isinstance(vert, bmesh.types.BMVert)
            ]
            face_new_faces = [
                item
                for item in ret["geom"]
                if isinstance(item, bmesh.types.BMFace)
            ]
            new_faces.extend(face_new_faces)
            cap_faces = _face_cap_faces(face_new_faces, new_top_verts)
            cap_face_groups.append((cap_faces, face_normal))

            old_vert_lookup = _build_old_vert_lookup(top_verts)
            for new_vert in new_top_verts:
                old_vert = _take_matching_old_vert(new_vert, old_vert_lookup)
                if old_vert is None:
                    continue

                new_vert.co += (
                    vert_extrude_vectors.get(old_vert, Vector()) * extrude_dist
                )
                new_vert.co += (
                    vert_outward_vectors.get(old_vert, Vector()) * outward_offset
                )

    extrusion_edges = _extrusion_edges_from_faces(new_faces)

    if has_unselected_neighbor:
        bmesh.ops.delete(bm, geom=sel_faces, context="FACES")
    elif reverse_extrusion_direction:
        bmesh.ops.reverse_faces(bm, faces=sel_faces)

    bm.normal_update()
    cap_seeds = []
    for cap_faces, cap_normal in cap_face_groups:
        _align_faces_to_direction(bm, cap_faces, cap_normal)
        cap_seeds.extend(cap_faces)
    _orient_connected_faces_from_seeds(bm, new_faces, cap_seeds)
    _set_edge_smoothing_by_angle(extrusion_edges)
    _update_edit_mesh(obj.data, topology_changed=True)
    return {"FINISHED"}


def _execute_edge_geometry(
    context,
    extrude,
    offset,
    divisions,
    keep_faces_together,
    reporter=None,
):
    obj = context.edit_object
    bm = bmesh.from_edit_mesh(obj.data)
    sel_edges = [edge for edge in bm.edges if edge.select]
    if not sel_edges:
        if reporter is not None:
            reporter({"WARNING"}, "Please select edges first!")
        return {"CANCELLED"}

    new_faces = _extrude_edges_segmented(
        bm,
        sel_edges,
        obj.matrix_world,
        extrude,
        offset,
        divisions,
        keep_faces_together,
    )
    extrusion_edges = _extrusion_edges_from_faces(new_faces)

    bm.normal_update()
    anchored_faces = _align_faces_to_existing_neighbors(bm, new_faces)
    _orient_connected_faces_from_seeds(bm, new_faces, anchored_faces)
    _set_edge_smoothing_by_angle(extrusion_edges)
    _update_edit_mesh(obj.data, topology_changed=True)
    return {"FINISHED"}


# -------------------------------------------------------------------
# CORE OPERATOR: FACE EXTRUDE
# -------------------------------------------------------------------
class MESH_OT_omni_outset_face(bpy.types.Operator):
    """Per-face normal extrusion with uniform side-wall spacing"""

    bl_idname = "mesh.omni_outset_face"
    bl_label = "Smart Face Extrude"
    bl_description = (
        "Extrude selected faces along their normals with uniform side-wall spacing"
    )
    bl_translation_context = i18n_contexts.operator_default
    bl_options = {"REGISTER", "UNDO", "GRAB_CURSOR", "BLOCKING"}

    extrude_dist: bpy.props.FloatProperty(
        name="Extrude Distance",
        translation_context=i18n_contexts.operator_default,
        default=0.0,
        step=0.01,
        subtype='DISTANCE',
    )
    outward_offset: bpy.props.FloatProperty(
        name="Outward Offset",
        translation_context=i18n_contexts.operator_default,
        default=0.0,
        step=0.01,
        subtype='DISTANCE',
    )
    keep_faces_together: bpy.props.BoolProperty(
        name="Keep Faces Together",
        translation_context=i18n_contexts.operator_default,
        description="Keep connected selected faces joined during extrusion",
        default=True,
    )
    drag_sensitivity: bpy.props.FloatProperty(
        name="Drag Sensitivity",
        translation_context=i18n_contexts.operator_default,
        description="Mouse drag speed multiplier saved for future interactive extrusions",
        default=DEFAULT_DRAG_SENSITIVITY_MULTIPLIER,
        min=MIN_DRAG_SENSITIVITY_MULTIPLIER,
        max=MAX_DRAG_SENSITIVITY_MULTIPLIER,
        precision=2,
    )

    @classmethod
    def poll(cls, context):
        # Selection is checked in invoke. Requiring it here hides Blender's
        # Adjust Last Operation panel after the modal extrusion changes it.
        message = _edit_mesh_poll_error(context, 2)
        if message is not None:
            cls.poll_message_set(message)
            return False
        return True

    def draw(self, context):
        del context
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "extrude_dist")
        layout.prop(self, "outward_offset")
        layout.prop(self, "keep_faces_together")

    def execute(self, context):
        if _shape_key_locked(self, context):
            return {"CANCELLED"}
        _store_drag_sensitivity(context, self.drag_sensitivity)
        return _execute_face_geometry(
            context,
            self.extrude_dist,
            self.outward_offset,
            self.report,
            self.keep_faces_together,
        )

    def invoke(self, context, event):
        if _shape_key_locked(self, context):
            return {"CANCELLED"}
        self.prev_mouse_x = event.mouse_x
        self.prev_mouse_y = event.mouse_y
        self.extrude_dist = 0.0
        self.outward_offset = 0.0
        self.keep_faces_together = True
        self.drag_sensitivity = _stored_drag_sensitivity(context)

        self.obj = context.edit_object
        self.bm = bmesh.from_edit_mesh(self.obj.data)
        self._bm_backup = self.bm.copy()

        sel_faces = [face for face in self.bm.faces if face.select]
        if not sel_faces:
            self.report({"WARNING"}, "At least one face must be selected!")
            _free_backup(self)
            return {"CANCELLED"}

        top_verts = list({vert for face in sel_faces for vert in face.verts})
        selected_edges = list({edge for face in sel_faces for edge in face.edges})
        self._drag_reference_length = _selection_reference_length(
            top_verts,
            selected_edges,
            _length_unit_size_in_blender_units(context),
        )

        result = _execute_face_geometry(
            context,
            self.extrude_dist,
            self.outward_offset,
            self.report,
            self.keep_faces_together,
        )
        if "FINISHED" not in result:
            _restore_bmesh_from_backup(self.bm, self._bm_backup, self.obj.data)
            _free_backup(self)
            return result

        hud.start(self, context, "FACE")
        self._hud_ctrl = bool(event.ctrl)
        self._hud_shift = bool(event.shift)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        self._hud_ctrl = bool(event.ctrl)
        self._hud_shift = bool(event.shift)
        hud.tag_redraw(self)

        if event.type in {"RIGHTMOUSE", "ESC"}:
            hud.stop(self)
            _restore_bmesh_from_backup(self.bm, self._bm_backup, self.obj.data)
            _free_backup(self)
            context.workspace.status_text_set(None)
            return {"CANCELLED"}

        if event.type in {"LEFTMOUSE", "RET", "NUMPAD_ENTER"} and event.value == "PRESS":
            hud.stop(self)
            _store_drag_sensitivity(context, self.drag_sensitivity)
            _free_backup(self)
            context.workspace.status_text_set(None)
            _show_adjust_last_operation_region(context)
            return {"FINISHED"}

        if event.type == "MOUSEMOVE":
            dx = event.mouse_x - self.prev_mouse_x
            dy = event.mouse_y - self.prev_mouse_y
            sensitivity = _drag_sensitivity(
                context,
                event,
                self._drag_reference_length,
                self.drag_sensitivity,
            )

            if event.ctrl:
                self.outward_offset += _unified_drag_delta(dx, dy) * sensitivity
            else:
                self.extrude_dist += _unified_drag_delta(dx, dy) * sensitivity

            self.prev_mouse_x = event.mouse_x
            self.prev_mouse_y = event.mouse_y
            _restore_bmesh_from_backup(self.bm, self._bm_backup, self.obj.data)
            _execute_face_geometry(
                context,
                self.extrude_dist,
                self.outward_offset,
                keep_faces_together=self.keep_faces_together,
            )

            translate = bpy.app.translations.pgettext_iface
            status_msg = translate(
                "OmniOutset Face | Extrude: {0:.3f} (Up/Down) | Outset: {1:.3f} (Ctrl+L/R) | [Shift] Precision | [LMB] Confirm"
            )
            context.workspace.status_text_set(
                status_msg.format(
                    _DistanceDisplay(self.extrude_dist),
                    _DistanceDisplay(self.outward_offset),
                )
            )

        return {"RUNNING_MODAL"}


# -------------------------------------------------------------------
# CORE OPERATOR: EDGE EXTRUDE
# -------------------------------------------------------------------
class MESH_OT_omni_outset_edge(bpy.types.Operator):
    """Interactive equidistant outward extrusion for selected edges"""

    bl_idname = "mesh.omni_outset_edge"
    bl_label = "Equidistant Edge Extrude"
    bl_description = "Extrude selected edges equidistantly outward"
    bl_translation_context = i18n_contexts.operator_default
    bl_options = {"REGISTER", "UNDO", "GRAB_CURSOR", "BLOCKING"}

    extrude: bpy.props.FloatProperty(
        name="Extrude",
        translation_context=i18n_contexts.operator_default,
        description="Extrude selected edges equidistantly outward",
        default=0.0,
        step=0.01,
        subtype='DISTANCE',
    )
    offset: bpy.props.FloatProperty(
        name="Offset",
        translation_context=i18n_contexts.operator_default,
        description="Move new edges along connected face normals",
        default=0.0,
        step=0.01,
        subtype='DISTANCE',
    )
    divisions: bpy.props.IntProperty(
        name="Divisions",
        translation_context=i18n_contexts.operator_default,
        description="Number of extrusion segments",
        default=1,
        min=1,
        soft_max=64,
    )
    keep_faces_together: bpy.props.BoolProperty(
        name="Keep Faces Together",
        translation_context=i18n_contexts.operator_default,
        description="Keep connected selected edges joined during extrusion",
        default=True,
    )
    drag_sensitivity: bpy.props.FloatProperty(
        name="Drag Sensitivity",
        translation_context=i18n_contexts.operator_default,
        description="Mouse drag speed multiplier saved for future interactive extrusions",
        default=DEFAULT_DRAG_SENSITIVITY_MULTIPLIER,
        min=MIN_DRAG_SENSITIVITY_MULTIPLIER,
        max=MAX_DRAG_SENSITIVITY_MULTIPLIER,
        precision=2,
    )

    @classmethod
    def poll(cls, context):
        message = _edit_mesh_poll_error(context, 1)
        if message is not None:
            cls.poll_message_set(message)
            return False
        return True

    def draw(self, context):
        del context
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "extrude")
        layout.prop(self, "offset")
        layout.prop(self, "divisions")
        layout.prop(self, "keep_faces_together")

    def execute(self, context):
        if _shape_key_locked(self, context):
            return {"CANCELLED"}
        _store_drag_sensitivity(context, self.drag_sensitivity)
        return _execute_edge_geometry(
            context,
            self.extrude,
            self.offset,
            self.divisions,
            self.keep_faces_together,
            self.report,
        )

    def invoke(self, context, event):
        if _shape_key_locked(self, context):
            return {"CANCELLED"}
        self.prev_mouse_x = event.mouse_x
        self.prev_mouse_y = event.mouse_y
        self.extrude = 0.0
        self.offset = 0.0
        self.divisions = 1
        self.keep_faces_together = True
        self.drag_sensitivity = _stored_drag_sensitivity(context)

        self.obj = context.edit_object
        self.bm = bmesh.from_edit_mesh(self.obj.data)
        self._bm_backup = self.bm.copy()

        sel_edges = [edge for edge in self.bm.edges if edge.select]
        if not sel_edges:
            self.report({"WARNING"}, "Please select edges first!")
            _free_backup(self)
            return {"CANCELLED"}

        sel_verts = list({vert for edge in sel_edges for vert in edge.verts})
        self._drag_reference_length = _selection_reference_length(
            sel_verts,
            sel_edges,
            _length_unit_size_in_blender_units(context),
        )

        result = _execute_edge_geometry(
            context,
            self.extrude,
            self.offset,
            self.divisions,
            self.keep_faces_together,
            self.report,
        )
        if "FINISHED" not in result:
            _restore_bmesh_from_backup(self.bm, self._bm_backup, self.obj.data)
            _free_backup(self)
            return result

        hud.start(self, context, "EDGE")
        self._hud_ctrl = bool(event.ctrl)
        self._hud_shift = bool(event.shift)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        self._hud_ctrl = bool(event.ctrl)
        self._hud_shift = bool(event.shift)
        hud.tag_redraw(self)

        if event.type in {"RIGHTMOUSE", "ESC"}:
            hud.stop(self)
            _restore_bmesh_from_backup(self.bm, self._bm_backup, self.obj.data)
            _free_backup(self)
            context.workspace.status_text_set(None)
            return {"CANCELLED"}

        if event.type in {"LEFTMOUSE", "RET", "NUMPAD_ENTER"} and event.value == "PRESS":
            hud.stop(self)
            _store_drag_sensitivity(context, self.drag_sensitivity)
            _free_backup(self)
            context.workspace.status_text_set(None)
            _show_adjust_last_operation_region(context)
            return {"FINISHED"}

        if event.type == "MOUSEMOVE":
            dx = event.mouse_x - self.prev_mouse_x
            dy = event.mouse_y - self.prev_mouse_y
            sensitivity = _drag_sensitivity(
                context,
                event,
                self._drag_reference_length,
                self.drag_sensitivity,
            )

            if event.ctrl:
                self.offset += _unified_drag_delta(dx, dy) * sensitivity
            else:
                self.extrude += _unified_drag_delta(dx, dy) * sensitivity

            self.prev_mouse_x = event.mouse_x
            self.prev_mouse_y = event.mouse_y
            _restore_bmesh_from_backup(self.bm, self._bm_backup, self.obj.data)
            _execute_edge_geometry(
                context,
                self.extrude,
                self.offset,
                self.divisions,
                self.keep_faces_together,
            )

            translate = bpy.app.translations.pgettext_iface
            status_msg = translate(
                "OmniOutset Edge | Extrude: {0:.3f} (L/R) | Offset: {1:.3f} (Ctrl+Up/Down) | [Shift] Precision | [LMB] Confirm"
            )
            context.workspace.status_text_set(
                status_msg.format(
                    _DistanceDisplay(self.extrude),
                    _DistanceDisplay(self.offset),
                )
            )

        return {"RUNNING_MODAL"}


# -------------------------------------------------------------------
# SMART ROUTER: face outset, edge outset, otherwise classic extrude
# -------------------------------------------------------------------
class MESH_OT_omni_outset(bpy.types.Operator):
    """Extrude faces along their normals or edges equidistantly outward"""

    bl_idname = "mesh.omni_outset"
    bl_label = "Extrude"
    bl_description = (
        "Run face or edge extrusion for the active mesh selection mode"
    )
    bl_translation_context = i18n_contexts.operator_default
    bl_options = set()

    @classmethod
    def poll(cls, context):
        obj = context.edit_object
        if obj is None or obj.type != "MESH" or context.mode != "EDIT_MESH":
            cls.poll_message_set("Enter Mesh Edit Mode")
            return False
        return True

    def invoke(self, context, _event):
        select_mode = context.tool_settings.mesh_select_mode

        if select_mode[2]:
            result = bpy.ops.mesh.omni_outset_face("INVOKE_DEFAULT")
        elif select_mode[1]:
            result = bpy.ops.mesh.omni_outset_edge("INVOKE_DEFAULT")
        else:
            # Vertex selection has no outset equivalent. Keep classic extrude.
            result = bpy.ops.view3d.edit_mesh_extrude_move_normal("INVOKE_DEFAULT")

        # This dispatcher must stay out of Adjust Last Operation history.
        if "RUNNING_MODAL" in result:
            return {"FINISHED"}
        return result


class OmniOutsetSettings(bpy.types.PropertyGroup):
    use_adaptive_drag_scale: bpy.props.BoolProperty(
        name="Adaptive to Selection Size",
        translation_context=i18n_contexts.default,
        description="Scale mouse dragging by the current selected geometry size",
        default=True,
    )
    drag_sensitivity: bpy.props.FloatProperty(
        name="Drag Sensitivity",
        translation_context=i18n_contexts.default,
        description="Mouse drag speed multiplier saved for future interactive extrusions",
        default=DEFAULT_DRAG_SENSITIVITY_MULTIPLIER,
        min=MIN_DRAG_SENSITIVITY_MULTIPLIER,
        max=MAX_DRAG_SENSITIVITY_MULTIPLIER,
        precision=2,
        options={'HIDDEN'},
    )
    show_hud: bpy.props.BoolProperty(
        name="Show HUD During Drag",
        translation_context=i18n_contexts.default,
        description="Show live controls and values in the 3D View while dragging",
        default=True,
    )
    hud_font_size: bpy.props.IntProperty(
        name="HUD Font Size",
        translation_context=i18n_contexts.default,
        description="Font size of the modal HUD",
        default=hud.DEFAULT_FONT_SIZE,
        min=8,
        max=48,
    )


classes = (
    OmniOutsetSettings,
    MESH_OT_omni_outset_face,
    MESH_OT_omni_outset_edge,
    MESH_OT_omni_outset,
)


def register():
    bpy.types.WindowManager.omni_outset = bpy.props.PointerProperty(type=OmniOutsetSettings)
    bpy.app.translations.register(__name__, translation.translations_dict)


def unregister():
    hud.clear_handlers()
    try:
        bpy.app.translations.unregister(__name__)
    except Exception:
        pass
    pointer = getattr(bpy.types.WindowManager, "omni_outset", None)
    if pointer is not None:
        del bpy.types.WindowManager.omni_outset
