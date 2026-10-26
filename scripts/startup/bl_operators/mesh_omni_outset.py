# SPDX-FileCopyrightText: 2026 OmniOutset Authors
# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Built-in extrusion derived from OmniOutset (ZXY, GPL-3.0-or-later).
# Component geometry, symmetry, divisions and interaction are implemented here
# and in omni_extrude_geometry; this is not Autodesk's proprietary algorithm.

import bmesh
import bpy
from math import cos, radians, sqrt
from mathutils import Matrix, Vector

from . import omni_outset_hud as hud
from . import omni_outset_translation as translation

i18n_contexts = bpy.app.translations.contexts

VERT_MATCH_EPSILON = 0.0001
DRAG_REFERENCE_EPSILON = 0.000000000001
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

    return None


def _extrude_selection_kind(context):
    bm = bmesh.from_edit_mesh(context.edit_object.data)
    mode = context.tool_settings.mesh_select_mode
    faces = any(f.select and not f.hide for f in bm.faces)
    edges = any(e.select and not e.hide for e in bm.edges)
    if mode[2] and faces:
        return 'FACE'
    if mode[1] and edges:
        return 'EDGE'
    # Vertex mode can still describe a complete face or edge selection.
    return 'FACE' if faces else 'EDGE' if edges else 'VERT'


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


def _update_edit_mesh(mesh, topology_changed):
    bmesh.update_edit_mesh(
        mesh,
        loop_triangles=topology_changed,
        destructive=topology_changed,
    )


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


def _execute_face_geometry(context, extrude_dist, outward_offset, reporter=None,
                           keep_faces_together=True, divisions=1, direction='NORMAL',
                           translation=(0, 0, 0), taper=1.0):
    from .omni_extrude_geometry import extrude
    if not extrude(context.edit_object, 'FACE', extrude_dist, outward_offset,
                   divisions, keep_faces_together, direction, translation, taper):
        if reporter:
            reporter({'WARNING'}, "At least one face must be selected!")
        return {'CANCELLED'}
    return {'FINISHED'}


def _execute_edge_geometry(context, extrude, offset, divisions, keep_faces_together,
                           reporter=None, direction='OUTWARD', translation=(0, 0, 0), taper=1.0):
    from .omni_extrude_geometry import extrude as extrude_geometry
    if not extrude_geometry(context.edit_object, 'EDGE', extrude, offset,
                            divisions, keep_faces_together, direction, translation, taper):
        if reporter:
            reporter({'WARNING'}, "Please select edges first!")
        return {'CANCELLED'}
    return {'FINISHED'}


class _ExtrudeInteraction:
    # Shared by face and edge operators so cancel, numeric entry and redo agree.
    def _geometry(self, context):
        if self._kind == 'FACE':
            return _execute_face_geometry(
                context, self.extrude_dist, self.outward_offset, self.report,
                self.keep_faces_together, self.divisions, self.direction,
                self.translation, self.taper)
        return _execute_edge_geometry(
            context, self.extrude, self.offset, self.divisions,
            self.keep_faces_together, self.report, self.direction, self.translation, self.taper)

    def execute(self, context):
        if _shape_key_locked(self, context):
            return {'CANCELLED'}
        return self._geometry(context)

    def draw(self, _context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, 'extrude_dist' if self._kind == 'FACE' else 'extrude')
        layout.prop(self, 'outward_offset' if self._kind == 'FACE' else 'offset')
        layout.prop(self, 'direction')
        layout.prop(self, 'translation')
        layout.prop(self, 'taper')
        layout.prop(self, 'divisions')
        layout.prop(self, 'keep_faces_together')

    def invoke(self, context, event):
        if _shape_key_locked(self, context):
            return {'CANCELLED'}
        self.obj = context.edit_object
        self.bm = bmesh.from_edit_mesh(self.obj.data)
        elements = self.bm.faces if self._kind == 'FACE' else self.bm.edges
        selected = [elem for elem in elements if elem.select and not elem.hide]
        if not selected:
            self.report({'WARNING'}, "Select at least one face or edge")
            return {'CANCELLED'}
        self._bm_backup = self.bm.copy()
        self.prev_mouse_x, self.prev_mouse_y = event.mouse_x, event.mouse_y
        self._numeric = ''
        self._numeric_target = None
        self.drag_sensitivity = _stored_drag_sensitivity(context)
        verts = {v for elem in selected for v in elem.verts}
        edges = {e for elem in selected for e in elem.edges} if self._kind == 'FACE' else selected
        self._drag_reference_length = _selection_reference_length(
            verts, edges, _length_unit_size_in_blender_units(context))
        self._hud_ctrl, self._hud_shift = bool(event.ctrl), bool(event.shift)
        self._geometry(context)
        context.window_manager.modal_handler_add(self)
        self._status(context)
        return {'RUNNING_MODAL'}

    def _status(self, context):
        pass

    def _finish(self, context, cancel=False):
        hud.stop(self)
        if cancel:
            _restore_bmesh_from_backup(self.bm, self._bm_backup, self.obj.data)
        _free_backup(self)
        context.workspace.status_text_set(None)

    def cancel(self, context):
        self._finish(context, cancel=True)

    def modal(self, context, event):
        self._hud_ctrl, self._hud_shift = bool(event.ctrl), bool(event.shift)
        if event.type in {'RIGHTMOUSE', 'ESC'}:
            self._finish(context, cancel=True)
            return {'CANCELLED'}
        if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            self._finish(context)
            _show_adjust_last_operation_region(context)
            return {'FINISHED'}
        changed = False
        target = ('outward_offset' if self._kind == 'FACE' else 'offset') if event.ctrl else (
            'extrude_dist' if self._kind == 'FACE' else 'extrude')
        if event.value == 'PRESS':
            if event.type in {'X', 'Y', 'Z'}:
                self.direction = ('NORMAL' if self._kind == 'FACE' else 'OUTWARD') if (
                    self.direction == event.type) else event.type
                changed = True
            elif event.type == 'K':
                self.keep_faces_together = not self.keep_faces_together
                changed = True
            elif event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
                self.divisions = max(1, min(256, self.divisions + (
                    1 if event.type == 'WHEELUPMOUSE' else -1)))
                changed = True
            else:
                char = event.ascii
                if (char and char in '0123456789.-') or event.type == 'BACK_SPACE':
                    if self._numeric_target != target:
                        self._numeric = ''
                        self._numeric_target = target
                    self._numeric = self._numeric[:-1] if event.type == 'BACK_SPACE' else self._numeric + char
                    try:
                        value = float(self._numeric or '0')
                    except ValueError:
                        pass
                    else:
                        # Numeric distances use the scene's displayed length unit.
                        setattr(self, target, value * _length_unit_size_in_blender_units(context))
                        changed = True
        if event.type == 'MOUSEMOVE':
            dx, dy = event.mouse_x - self.prev_mouse_x, event.mouse_y - self.prev_mouse_y
            self.prev_mouse_x, self.prev_mouse_y = event.mouse_x, event.mouse_y
            if not self._numeric or self._numeric_target != target:
                sensitivity = _drag_sensitivity(context, event, self._drag_reference_length,
                                                self.drag_sensitivity)
                setattr(self, target, getattr(self, target) + _unified_drag_delta(dx, dy) * sensitivity)
                changed = True
        if changed:
            _restore_bmesh_from_backup(self.bm, self._bm_backup, self.obj.data)
            self._geometry(context)
            self._status(context)
        return {'RUNNING_MODAL'}


# -------------------------------------------------------------------
# CORE OPERATOR: FACE EXTRUDE
# -------------------------------------------------------------------
class MESH_OT_omni_outset_face(_ExtrudeInteraction, bpy.types.Operator):
    """Per-face normal extrusion with uniform side-wall spacing"""

    _kind = 'FACE'
    bl_idname = "mesh.omni_outset_face"
    bl_label = "Smart Face Extrude"
    bl_description = (
        "Extrude selected faces along their normals with uniform side-wall spacing"
    )
    bl_translation_context = i18n_contexts.operator_default
    bl_options = {"REGISTER", "UNDO", "GRAB_CURSOR", "BLOCKING"}

    extrude_dist: bpy.props.FloatProperty(
        name="Thickness",
        translation_context=i18n_contexts.operator_default,
        default=0.0,
        step=0.01,
        subtype='DISTANCE',
    )
    outward_offset: bpy.props.FloatProperty(
        name="Offset",
        description="Inset the extruded border; negative values expand it",
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

    direction: bpy.props.EnumProperty(
        name="Direction",
        items=(('NORMAL', "Normal", "Along surface normals"),
               ('OUTWARD', "Outward", "Extend boundary edges in the surface plane"),
               ('X', "World X", "Extrude along world X"),
               ('Y', "World Y", "Extrude along world Y"),
               ('Z', "World Z", "Extrude along world Z")),
        default='NORMAL',
    )
    translation: bpy.props.FloatVectorProperty(
        name="Translation", description="Additional world-space displacement",
        subtype='TRANSLATION', unit='LENGTH', size=3,
    )
    taper: bpy.props.FloatProperty(
        name="End Scale", description="Scale the end of the extrusion; 1 keeps its size",
        default=1.0, min=0.0, soft_max=3.0,
    )
    divisions: bpy.props.IntProperty(
        name="Divisions", default=1, min=1, max=256, soft_max=64,
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


# -------------------------------------------------------------------
# CORE OPERATOR: EDGE EXTRUDE
# -------------------------------------------------------------------
class MESH_OT_omni_outset_edge(_ExtrudeInteraction, bpy.types.Operator):
    """Interactive equidistant outward extrusion for selected edges"""

    _kind = 'EDGE'
    bl_idname = "mesh.omni_outset_edge"
    bl_label = "Smart Edge Extrude"
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
        name="Normal Offset",
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
        max=256,
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

    direction: bpy.props.EnumProperty(
        name="Direction",
        items=(('NORMAL', "Normal", "Along surface normals"),
               ('OUTWARD', "Outward", "Extend boundary edges in the surface plane"),
               ('X', "World X", "Extrude along world X"),
               ('Y', "World Y", "Extrude along world Y"),
               ('Z', "World Z", "Extrude along world Z")),
        default='OUTWARD',
    )
    translation: bpy.props.FloatVectorProperty(
        name="Translation", description="Additional world-space displacement",
        subtype='TRANSLATION', unit='LENGTH', size=3,
    )
    taper: bpy.props.FloatProperty(
        name="End Scale", description="Scale the end of the extrusion; 1 keeps its size",
        default=1.0, min=0.0, soft_max=3.0,
    )

    @classmethod
    def poll(cls, context):
        message = _edit_mesh_poll_error(context, 1)
        if message is not None:
            cls.poll_message_set(message)
            return False
        return True


class MESH_OT_omni_outset_prepare(bpy.types.Operator):
    """Create the extrusion before Blender's native transform manipulator runs"""
    bl_idname = "mesh.omni_outset_prepare"
    bl_label = "Extrude"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        if _shape_key_locked(self, context):
            return {'CANCELLED'}
        kind = _extrude_selection_kind(context)
        if kind == 'FACE':
            return _execute_face_geometry(context, 0, 0, self.report)
        if kind == 'EDGE':
            return _execute_edge_geometry(context, 0, 0, 1, True, self.report)
        return bpy.ops.mesh.extrude_context('EXEC_DEFAULT')


class MESH_OT_omni_outset_move(bpy.types.Macro):
    bl_idname = "mesh.omni_outset_move"
    bl_label = "Extrude"
    bl_description = "Extrude with symmetry using the native transform handles"
    bl_options = {'REGISTER', 'UNDO'}


def _define_extrude_macro():
    MESH_OT_omni_outset_move.define('MESH_OT_omni_outset_prepare')
    transform = MESH_OT_omni_outset_move.define('TRANSFORM_OT_translate')
    transform.properties.use_proportional_edit = False
    transform.properties.mirror = True


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
        kind = _extrude_selection_kind(context)

        if kind == 'FACE':
            result = bpy.ops.mesh.omni_outset_face("INVOKE_DEFAULT")
        elif kind == 'EDGE':
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
        default=False,
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
    MESH_OT_omni_outset_prepare,
)


def register():
    bpy.utils.register_class(MESH_OT_omni_outset_move)
    _define_extrude_macro()
    bpy.types.WindowManager.omni_outset = bpy.props.PointerProperty(type=OmniOutsetSettings)
    bpy.app.translations.register(__name__, translation.translations_dict)


def unregister():
    bpy.utils.unregister_class(MESH_OT_omni_outset_move)
    hud.clear_handlers()
    try:
        bpy.app.translations.unregister(__name__)
    except Exception:
        pass
    pointer = getattr(bpy.types.WindowManager, "omni_outset", None)
    if pointer is not None:
        del bpy.types.WindowManager.omni_outset
