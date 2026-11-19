# SPDX-FileCopyrightText: 2026 Blender Authors
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Texel Pack island constraints in the UV editor sidebar: direct editing of the constraints of the
selected islands, group and stack lists, and an overlay of group and stack bounds.

Constraints are the `uvpack_*` face attributes read by `uv.texel_pack`, written through the
`uv.texel_pack_constraint_*` operators so every edit affects whole islands.
"""

import bpy
import blf
import gpu
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    PointerProperty,
    IntProperty,
    StringProperty,
)
from gpu_extras.batch import batch_for_shader
from bpy.app.handlers import persistent
from types import SimpleNamespace

ATTRIBUTES = {
    'ROTATION': 'uvpack_rotation',
    'FLIP': 'uvpack_flip',
    'SCALE': 'uvpack_scale',
    'DENSITY': 'uvpack_density',
    'GROUP': 'uvpack_group',
    'STACK': 'uvpack_stack',
    'LOCK': 'uvpack_lock',
}

_state_cache = {}
_handles = []


def group_name(scene, index, kind='GROUP'):
    return scene.get('uvpack_' + kind.lower() + '_names', {}).get(str(index), '{} {}'.format(kind.title(), index))


def group_entries(context, all_meshes=False, kind='GROUP'):
    """Scene-wide outside edit mode; all faces of participating meshes in edit mode."""
    editing = context.mode == 'EDIT_MESH' and not all_meshes
    objects = edit_meshes(context) if editing else context.scene.objects
    entries, seen = {}, set()
    for ob in objects:
        if ob.type != 'MESH' or ob.data.as_pointer() in seen:
            continue
        mesh = ob.data
        seen.add(mesh.as_pointer())
        if mesh.is_editmode:
            info = mesh.texel_pack_info()
            counts = [(int(info[i+2]), int(info[i+3])) for i in range(0, len(info), _INFO_STRIDE)
                      if info[i] == 4.0 and int(info[i+1]) == _KINDS.index(kind)]
        else:
            attribute = mesh.attributes.get(ATTRIBUTES[kind])
            counts_map = {}
            if attribute and attribute.domain == 'FACE' and attribute.data_type == 'INT':
                for item in attribute.data:
                    if item.value > 0:
                        counts_map[item.value] = counts_map.get(item.value, 0) + 1
            counts = counts_map.items()
        for index, count in counts:
            entry = entries.setdefault(index, {'faces': 0, 'selected': False})
            entry['faces'] += count
    if not editing and kind != 'GROUP':
        for key in context.scene.get('uvpack_' + kind.lower() + '_names', {}):
            entries.setdefault(int(key), {'faces': 0, 'selected': False})
    return entries


def cleanup_empty_groups(scene, kind='GROUP'):
    key = 'uvpack_' + kind.lower() + '_names'
    names = dict(scene.get(key, {}))
    if not names:
        return
    context = SimpleNamespace(mode='OBJECT', scene=scene)
    live = group_entries(context, all_meshes=True, kind=kind)
    # Stack lists historically retain empty names; use positive counts for both kinds.
    cleaned = {key: value for key, value in names.items()
               if int(key) in live and live[int(key)]['faces'] > 0}
    if cleaned != names:
        scene[key] = cleaned


_cleaning = False


@persistent
def _cleanup_groups(*_):
    global _cleaning
    if _cleaning:
        return
    _cleaning = True
    try:
        for scene in bpy.data.scenes:
            if scene.is_editable:
                cleanup_empty_groups(scene)
    finally:
        _cleaning = False


_target_items = []


def _assignment_targets(self, context):
    global _target_items
    _target_items = [(str(index), group_name(context.scene, index, self.kind), '')
                     for index, entry in sorted(group_entries(context, all_meshes=True, kind=self.kind).items())
                     if entry['faces'] > 0]
    return _target_items


def write_mesh_membership(mesh, kind, value, matching=None):
    """Explicit operator mutation only; includes hidden faces when deleting a group."""
    if mesh.is_editmode:
        import bmesh
        bm = bmesh.from_edit_mesh(mesh)
        layer = bm.faces.layers.int.get(ATTRIBUTES[kind])
        if layer is None:
            if matching is not None:
                return
            layer = bm.faces.layers.int.new(ATTRIBUTES[kind])
        for face in bm.faces:
            if matching is None or face[layer] == matching:
                face[layer] = value
        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    else:
        attr = mesh.attributes.get(ATTRIBUTES[kind])
        if attr is None:
            if matching is not None:
                return
            attr = mesh.attributes.new(ATTRIBUTES[kind], 'INT', 'FACE')
        if attr.domain != 'FACE' or attr.data_type != 'INT':
            raise ValueError('Invalid group attribute on ' + mesh.name)
        for item in attr.data:
            if matching is None or item.value == matching:
                item.value = value
        mesh.update()


class UVTOOLS_OT_stack_detect(bpy.types.Operator):
    bl_idname = 'uv_tools.stack_detect'
    bl_label = 'Detect and Stack Groups'
    bl_description = 'Find every matching set of selected unassigned UV islands and stack each set separately'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH' and bpy.ops.uv.texel_pack_stack_detect.poll()

    def execute(self, context):
        settings = context.scene.uv_tools_texel_pack
        first_id = max(max(group_entries(context, all_meshes=True, kind='STACK'), default=0) + 1,
                       context.scene.get('uvpack_stack_next_id', 1))
        result = bpy.ops.uv.texel_pack_stack_detect(
            first_id=first_id, resolution_x=settings.resolution_x, resolution_y=settings.resolution_y,
            mirror=settings.stack_mirrored, tolerance=settings.stack_tolerance)
        if result == {'FINISHED'}:
            entries = group_entries(context, all_meshes=True, kind='STACK')
            context.scene['uvpack_stack_next_id'] = max(entries, default=first_id) + 1
            settings.show_stacks = True
            invalidate()
        return result


class UVTOOLS_OT_group_membership(bpy.types.Operator):
    bl_idname = 'uv_tools.group_membership'
    bl_label = 'Assign UV Group / Stack'
    bl_description = 'Assign selected UV islands in Edit Mode or all faces of selected meshes in Object Mode'
    bl_options = {'REGISTER', 'UNDO'}
    kind: EnumProperty(items=(('GROUP','Group',''),('STACK','Stack','')))
    operation: EnumProperty(items=(('ASSIGN','Assign',''),('REMOVE','Remove from Group',''),('NEW','New from Selection','')),
                            options={'SKIP_SAVE'})
    group: IntProperty(min=0)
    choose: BoolProperty(default=False, options={'SKIP_SAVE'})
    target: EnumProperty(name='Target', items=_assignment_targets)

    @classmethod
    def poll(cls, context):
        return context.mode in {'OBJECT','EDIT_MESH'}

    def invoke(self, context, event):
        if self.choose:
            if not _assignment_targets(self, context):
                self.report({'WARNING'}, 'No existing groups; use New from Selection')
                return {'CANCELLED'}
            return context.window_manager.invoke_props_dialog(self)
        return self.execute(context)

    def draw(self, context):
        self.layout.prop(self, 'target')

    def execute(self, context):
        value = int(self.target) if self.choose and self.target else self.group
        if self.operation == 'NEW':
            counter = 'uvpack_' + self.kind.lower() + '_next_id'
            value = max(max(group_entries(context, all_meshes=True, kind=self.kind), default=0)+1,
                        context.scene.get(counter, 1))
        elif self.operation == 'REMOVE':
            value = 0
        if self.operation == 'ASSIGN' and value <= 0:
            self.report({'WARNING'}, 'Choose a target group')
            return {'CANCELLED'}
        if context.mode == 'EDIT_MESH':
            result = bpy.ops.uv.texel_pack_constraint_set(kind=self.kind, **{self.kind.lower(): value})
            if result != {'FINISHED'}:
                return result
        else:
            meshes = {ob.data for ob in context.selected_objects if ob.type == 'MESH' and len(ob.data.polygons)}
            if not meshes:
                self.report({'WARNING'}, 'Select meshes to assign')
                return {'CANCELLED'}
            if any(not mesh.is_editable for mesh in meshes):
                self.report({'WARNING'}, 'Selection contains non-editable mesh data')
                return {'CANCELLED'}
            for mesh in meshes:
                attr = mesh.attributes.get(ATTRIBUTES[self.kind])
                if attr and (attr.domain != 'FACE' or attr.data_type != 'INT'):
                    self.report({'ERROR'}, 'Invalid group attribute on ' + mesh.name)
                    return {'CANCELLED'}
            for mesh in meshes:
                write_mesh_membership(mesh, self.kind, value)
        if self.operation == 'NEW':
            key = 'uvpack_' + self.kind.lower() + '_names'
            names = dict(context.scene.get(key, {}))
            names[str(value)] = '{} {}'.format(self.kind.title(), value)
            context.scene[key] = names
            context.scene[counter] = value + 1
        cleanup_empty_groups(context.scene, self.kind)
        invalidate()
        return {'FINISHED'}


class UVTOOLS_OT_group_delete(bpy.types.Operator):
    bl_idname = 'uv_tools.group_delete'
    bl_label = 'Delete UV Group / Stack'
    bl_description = 'Delete this group and clear its membership across the scene, including hidden meshes; keep geometry and UVs'
    bl_options = {'REGISTER', 'UNDO'}
    kind: EnumProperty(items=(('GROUP','Group',''),('STACK','Stack','')))
    group: IntProperty(min=1)

    def execute(self, context):
        def contains(mesh):
            if mesh.is_editmode:
                info = mesh.texel_pack_info()
                return any(info[i] == 4.0 and int(info[i+1]) == _KINDS.index(self.kind)
                           and int(info[i+2]) == self.group for i in range(0, len(info), _INFO_STRIDE))
            attr = mesh.attributes.get(ATTRIBUTES[self.kind])
            return (attr is not None and attr.domain == 'FACE' and attr.data_type == 'INT'
                    and any(item.value == self.group for item in attr.data))
        meshes = {ob.data for ob in context.scene.objects if ob.type == 'MESH' and contains(ob.data)}
        if any(not mesh.is_editable for mesh in meshes):
            self.report({'WARNING'}, 'Scene contains non-editable group data')
            return {'CANCELLED'}
        for mesh in meshes:
            write_mesh_membership(mesh, self.kind, 0, matching=self.group)
        key = 'uvpack_' + self.kind.lower() + '_names'
        names = dict(context.scene.get(key, {}))
        names.pop(str(self.group), None)
        context.scene[key] = names
        invalidate()
        return {'FINISHED'}


def remove_unused_group_material_slots(mesh):
    """Compact face-referenced slots, preserving object-linked overrides on shared meshes."""
    if mesh.is_editmode:
        import bmesh
        bm = bmesh.from_edit_mesh(mesh)
        indices = [face.material_index for face in bm.faces]
    else:
        indices = [face.material_index for face in mesh.polygons]
    used = set(indices)
    kept = [index for index in range(len(mesh.materials)) if index in used]
    removed = len(mesh.materials) - len(kept)
    if not removed:
        return 0
    remap = {old: new for new, old in enumerate(kept)}
    users = []
    for obj in bpy.data.objects:
        if obj.type == 'MESH' and obj.data == mesh:
            users.append((obj, obj.active_material_index,
                          [(obj.material_slots[i].link, obj.material_slots[i].material) for i in kept]))
    # IDMaterials.pop remaps Mesh indices, but edit BMesh indices need the same
    # explicit old-to-new mapping. Snapshotting also avoids applying the shift twice.
    for index in reversed(range(len(mesh.materials))):
        if index not in used:
            mesh.materials.pop(index=index)
    faces = bm.faces if mesh.is_editmode else mesh.polygons
    for face, old in zip(faces, indices):
        face.material_index = remap.get(old, 0)
    # Mesh-level slot removal resizes each object's slots; restore the surviving
    # per-object assignments at their new positions rather than changing their shaders.
    for obj, active, slots in users:
        for i, (link, material) in enumerate(slots):
            obj.material_slots[i].link = link
            if link == 'OBJECT':
                obj.material_slots[i].material = material
        obj.active_material_index = remap.get(active, 0)
    return removed


class UVTOOLS_OT_group_material(bpy.types.Operator):
    bl_idname = 'uv_tools.group_material'
    bl_label = 'Assign Material by Group'
    bl_description = 'Assign group-named materials, reusing existing materials or replacing them with new ones'
    bl_options = {'REGISTER', 'UNDO'}
    group: IntProperty(name='Group', default=0, min=0, options={'SKIP_SAVE'})
    material_mode: EnumProperty(name='Material', default='REUSE', options={'SKIP_SAVE'}, items=(
        ('REUSE', 'Reuse Existing', 'Keep the existing same-name material and its shader; create only if missing'),
        ('REPLACE', 'New & Replace Old', 'Create a fresh material, redirect all users of the same-name material, and delete the old one')))

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        self.layout.prop(self, 'material_mode', expand=True)
        if self.material_mode == 'REPLACE':
            self.layout.label(text='Existing shaders are replaced for all their users.', icon='INFO')
        else:
            self.layout.label(text='Existing shaders are kept; missing materials are created.')

    @classmethod
    def poll(cls, context):
        return context.mode in {'OBJECT', 'EDIT_MESH'}

    def execute(self, context):
        import bmesh
        ids = {self.group} if self.group else set(group_entries(context))
        plans = []
        owners = []
        used = set()
        seen = set()
        # Preflight every member before creating any datablocks or changing faces.
        for obj in context.scene.objects:
            if obj.type != 'MESH' or obj.data.as_pointer() in seen:
                continue
            mesh = obj.data
            seen.add(mesh.as_pointer())
            if mesh.is_editmode:
                bm = bmesh.from_edit_mesh(mesh)
                owners.append(bm)  # Face references require a live BMesh wrapper.
                layer = bm.faces.layers.int.get('uvpack_group')
                members = [(face, face[layer]) for face in bm.faces if face[layer] in ids] if layer else []
            else:
                attr = mesh.attributes.get('uvpack_group')
                members = ([(face, attr.data[face.index].value) for face in mesh.polygons
                            if attr.data[face.index].value in ids]
                           if attr and attr.domain == 'FACE' and attr.data_type == 'INT' else [])
            if not members:
                continue
            if not mesh.is_editable:
                self.report({'ERROR'}, 'Group contains non-editable mesh: ' + mesh.name)
                return {'CANCELLED'}
            used.update(index for _, index in members)
            plans.append((mesh, members, len(bm.faces) if mesh.is_editmode else len(mesh.polygons)))
        if not used:
            self.report({'WARNING'}, 'No group faces to assign materials to')
            return {'CANCELLED'}
        names = {index: group_name(context.scene, index) for index in used}
        existing = {name: bpy.data.materials.get(name) for name in set(names.values())}
        if self.material_mode == 'REPLACE' and any(mat and not mat.is_editable for mat in existing.values()):
            self.report({'ERROR'}, 'Cannot replace linked non-editable materials; choose Reuse Existing')
            return {'CANCELLED'}
        # Remember only numbered variants actually assigned to these group faces.
        # Never purge other unused materials merely because their names look similar.
        previous_variants = set()
        for mesh, members, _ in plans:
            for face, index in members:
                if face.material_index < len(mesh.materials):
                    old = mesh.materials[face.material_index]
                    prefix = names[index] + '.'
                    if old and old.name.startswith(prefix) and old.name[len(prefix):].isdigit():
                        previous_variants.add(old)
        materials = {}
        by_name = {}
        created = reused = deleted = 0
        for name in sorted(existing):
            old = existing[name]
            if old and self.material_mode == 'REUSE':
                material = old
                reused += 1
            else:
                material = bpy.data.materials.new(name)
                material.use_nodes = True
                created += 1
                if old:
                    old.user_remap(material)
                    bpy.data.materials.remove(old)
                    deleted += 1
                material.name = name
            by_name[name] = material
        materials = {index: by_name[name] for index, name in names.items()}
        faces = 0
        removed_slots = 0
        for mesh, members, face_count in plans:
            slots = {}
            # Without an empty slot, previously unassigned (index 0) faces would
            # inadvertently acquire the first newly appended group material.
            if not mesh.materials and len(members) < face_count:
                mesh.materials.append(None)
            for index in sorted({index for _, index in members}):
                material = materials[index]
                users = [ob for ob in bpy.data.objects if ob.type == 'MESH' and ob.data == mesh]
                slot = next((i for i, existing_material in enumerate(mesh.materials)
                             if existing_material == material and
                             all(ob.material_slots[i].material == material for ob in users)), None)
                if slot is None:
                    slot = len(mesh.materials)
                    mesh.materials.append(material)
                slots[index] = slot
            for face, index in members:
                face.material_index = slots[index]
                faces += 1
            removed_slots += remove_unused_group_material_slots(mesh)
            if mesh.is_editmode:
                bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
            else:
                mesh.update()
        invalidate()
        if self.material_mode == 'REPLACE':
            for material in previous_variants:
                try:
                    if material.is_editable and material.users == 0 and material not in by_name.values():
                        bpy.data.materials.remove(material)
                        deleted += 1
                except ReferenceError:
                    pass  # It was the canonical material of another processed group.
        self.report({'INFO'}, 'Created {}, reused {}, replaced/removed {} materials; assigned {} faces; removed {} unused slots'.format(
            created, reused, deleted, faces, removed_slots))
        return {'FINISHED'}


class UVTOOLS_OT_group_rename(bpy.types.Operator):
    bl_idname = 'uv_tools.group_rename'
    bl_label = 'Rename UV Group / Stack'
    bl_options = {'REGISTER', 'UNDO'}
    kind: EnumProperty(items=(('GROUP', 'Group', ''), ('STACK', 'Stack', '')))
    group: IntProperty(min=1)
    name: StringProperty(name='Name')

    def invoke(self, context, event):
        self.name = group_name(context.scene, self.group, self.kind)
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        name = self.name.strip()
        if not name:
            self.report({'ERROR'}, 'Group name cannot be empty')
            return {'CANCELLED'}
        names = dict(context.scene.get('uvpack_' + self.kind.lower() + '_names', {}))
        names[str(self.group)] = name
        context.scene['uvpack_' + self.kind.lower() + '_names'] = names
        invalidate()
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'IMAGE_EDITOR':
                    area.tag_redraw()
        return {'FINISHED'}


def group_objects(context, index, kind='GROUP'):
    """All object instances in the current view layer, including hidden ones."""
    result = []
    for obj in context.view_layer.objects:
        if obj.type != 'MESH':
            continue
        attr = obj.data.attributes.get(ATTRIBUTES[kind])
        if attr and attr.domain == 'FACE' and attr.data_type == 'INT':
            if any(value.value == index for value in attr.data):
                result.append(obj)
    return result


def group_hidden(context, index, kind='GROUP'):
    if context.mode == 'EDIT_MESH':
        total = hidden = 0
        for obj in edit_meshes(context):
            info = obj.data.texel_pack_info()
            for i in range(0, len(info), _INFO_STRIDE):
                if info[i] == 4.0 and int(info[i+1]) == _KINDS.index(kind) and int(info[i+2]) == index:
                    total += int(info[i+3])
                    hidden += int(info[i+4])
        return total > 0 and hidden == total
    objects = group_objects(context, index, kind)
    return bool(objects) and all(obj.hide_get() for obj in objects)


class UVTOOLS_OT_group_select_objects(bpy.types.Operator):
    bl_idname = 'uv_tools.group_select_objects'
    bl_label = 'Select Group Meshes'
    bl_description = 'Reveal and select selectable meshes containing this group in the current view layer'
    bl_options = {'REGISTER', 'UNDO'}
    kind: EnumProperty(items=(('GROUP', 'Group', ''), ('STACK', 'Stack', '')))
    group: IntProperty(min=1)

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        objects = group_objects(context, self.group, self.kind)
        targets = []
        for obj in objects:
            if not obj.hide_select:
                obj.hide_set(False)
                if obj.visible_get():
                    targets.append(obj)
        if not targets:
            self.report({'WARNING'}, 'No selectable group meshes in this view layer')
            return {'CANCELLED'}
        for obj in context.view_layer.objects:
            obj.select_set(obj in targets)
        context.view_layer.objects.active = targets[0]
        invalidate()
        return {'FINISHED'}


class UVTOOLS_OT_group_visibility(bpy.types.Operator):
    bl_idname = 'uv_tools.group_visibility'
    bl_label = 'Toggle Group Visibility'
    bl_description = 'Hide or reveal group faces in Edit Mode, or entire member meshes in Object Mode'
    bl_options = {'REGISTER', 'UNDO'}
    kind: EnumProperty(items=(('GROUP', 'Group', ''), ('STACK', 'Stack', '')))
    group: IntProperty(min=1)

    @classmethod
    def poll(cls, context):
        return context.mode in {'OBJECT', 'EDIT_MESH'}

    def execute(self, context):
        hide = not group_hidden(context, self.group, self.kind)
        if context.mode == 'EDIT_MESH':
            # BMesh wrappers are created only by this explicit operator, never by drawing.
            import bmesh
            for obj in edit_meshes(context):
                bm = bmesh.from_edit_mesh(obj.data)
                layer = bm.faces.layers.int.get(ATTRIBUTES[self.kind])
                if layer is None:
                    continue
                for face in bm.faces:
                    if face[layer] == self.group:
                        face.hide_set(hide)
                bm.select_flush_mode()
                bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        else:
            for obj in group_objects(context, self.group, self.kind):
                obj.hide_set(hide)
        invalidate()
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
        return {'FINISHED'}


def _revision():
    from . import uv_tools
    return uv_tools._revision


def invalidate():
    from . import uv_tools
    uv_tools._invalidate()


def edit_meshes(context):
    """Meshes in edit mode, one object per mesh (also without a screen context)."""
    objects = getattr(context, "objects_in_mode_unique_data", None)
    if objects is None:
        objects = [ob for ob in context.view_layer.objects if ob.mode == 'EDIT']
    result, meshes = [], set()
    for ob in objects:
        if ob.type == 'MESH' and ob.mode == 'EDIT' and ob.data not in meshes:
            meshes.add(ob.data)
            result.append(ob)
    return result


# Kind indices of the records of `Mesh.texel_pack_info()`.
_KINDS = ('ROTATION', 'FLIP', 'SCALE', 'DENSITY', 'GROUP', 'STACK', 'LOCK')
_INFO_STRIDE = 9


def island_state(context):
    """
    Constraint values of the selected faces, and the faces and UV bounds of every group and
    stack among the visible faces. Cached until the next depsgraph update, undo or redo.

    Read through `Mesh.texel_pack_info()` rather than `bmesh`: creating Python BMesh elements
    adds a pointer layer to the BMesh, reallocating its data, which crashes a UV transform
    running while the sidebar or overlay redraws.
    """
    objects = edit_meshes(context)
    key = (_revision(), tuple(ob.name for ob in objects), context.scene.tool_settings.use_uv_select_sync)
    state = _state_cache.get('state')
    if state is not None and state['key'] == key:
        return state

    values = {kind: {} for kind in ATTRIBUTES}
    lists = {'GROUP': {}, 'STACK': {}}
    max_ids = {'GROUP': 0, 'STACK': 0}
    selected_faces = 0
    for ob in objects:
        info = ob.data.texel_pack_info()
        for i in range(0, len(info), _INFO_STRIDE):
            record = info[i:i + _INFO_STRIDE]
            kind = _KINDS[int(record[1])] if record[0] != 3.0 else None
            if record[0] == 0.0:
                value = round(record[2], 4) if kind == 'DENSITY' else int(record[2])
                counts = values[kind]
                counts[value] = counts.get(value, 0) + int(record[3])
            elif record[0] == 1.0:
                entry = lists[kind].setdefault(int(record[2]), {'faces': 0, 'selected': False, 'bounds': None})
                entry['faces'] += int(record[3])
                entry['selected'] |= record[4] != 0.0
                b = entry['bounds']
                entry['bounds'] = list(record[5:9]) if b is None else [
                    min(b[0], record[5]), min(b[1], record[6]), max(b[2], record[7]), max(b[3], record[8])]
            elif record[0] == 2.0:
                max_ids[kind] = max(max_ids[kind], int(record[2]))
            elif record[0] == 3.0:
                selected_faces += int(record[1])

    state = {'key': key, 'selected_faces': selected_faces, 'values': values, 'lists': lists,
             'max_ids': max_ids}
    _state_cache['state'] = state
    return state


def selected_value(kind, default):
    """Most common value among the selected faces, and whether they disagree."""
    counts = island_state(bpy.context)['values'][kind]
    if not counts:
        return default, False
    value = max(sorted(counts), key=lambda k: counts[k])
    return value, len(counts) > 1


def _set_constraint(kind, **value):
    if bpy.ops.uv.texel_pack_constraint_set.poll():
        bpy.ops.uv.texel_pack_constraint_set('EXEC_DEFAULT', kind=kind, **value)
    invalidate()


_ROTATION_ITEMS = (
    ('INHERIT', 'Default', 'Use the packing setting', 0),
    ('NONE', 'None', 'Keep the current orientation', 1),
    ('CARDINAL', '90°', 'Rotate by multiples of 90 degrees', 2),
    ('FREE', 'Free', 'Rotate to the smallest bounding box first', 3),
)
_FLIP_ITEMS = (
    ('INHERIT', 'Default', 'Use the packing setting', 0),
    ('FORBID', 'Forbid', 'Never mirror', 1),
    ('ALLOW', 'Allow', 'May be mirrored', 2),
)


class UVToolsIslandConstraints(bpy.types.PropertyGroup):
    """Constraints of the selected UV islands, edited in place."""
    rotation: EnumProperty(
        name='Rotation', items=_ROTATION_ITEMS,
        get=lambda self: selected_value('ROTATION', 0)[0],
        set=lambda self, value: _set_constraint('ROTATION', rotation=_ROTATION_ITEMS[value][0]))
    flip: EnumProperty(
        name='Flip', items=_FLIP_ITEMS, description='Mirroring changes tangent space handedness',
        get=lambda self: selected_value('FLIP', 0)[0],
        set=lambda self, value: _set_constraint('FLIP', flip=_FLIP_ITEMS[value][0]))
    keep_scale: BoolProperty(
        name='Keep Scale', description='Keep the current UV scale instead of following the texel density',
        get=lambda self: bool(selected_value('SCALE', 0)[0]),
        set=lambda self, value: _set_constraint('SCALE', keep_scale=value))
    density: FloatProperty(
        name='Density', description='Texel density multiplier',
        min=0.01, max=100.0, soft_min=0.1, soft_max=10.0,
        get=lambda self: float(selected_value('DENSITY', 1.0)[0]),
        set=lambda self, value: _set_constraint('DENSITY', density=value))
    lock: BoolProperty(
        name='Lock', description='Keep the islands in place, packing around them',
        get=lambda self: bool(selected_value('LOCK', 0)[0]),
        set=lambda self, value: _set_constraint('LOCK', lock=value))


class UVTOOLS_OT_texel_pack_new_id(bpy.types.Operator):
    bl_idname = 'uv_tools.texel_pack_new_id'
    bl_label = 'New from Selection'
    bl_description = 'Put the selected UV islands into a new group or stack'
    bl_options = {'REGISTER', 'UNDO'}

    kind: EnumProperty(items=(('GROUP', 'Group', ''), ('STACK', 'Stack', '')))

    @classmethod
    def poll(cls, context):
        return bpy.ops.uv.texel_pack_constraint_set.poll()

    def execute(self, context):
        invalidate()
        new_id = max(group_entries(context, all_meshes=True, kind=self.kind), default=0) + 1
        value = {'group': new_id} if self.kind == 'GROUP' else {'stack': new_id}
        result = bpy.ops.uv.texel_pack_constraint_set('EXEC_DEFAULT', kind=self.kind, **value)
        invalidate()
        if result != {'FINISHED'}:
            self.report({'WARNING'}, 'No selected UV islands')
            return {'CANCELLED'}
        if self.kind in {'GROUP', 'STACK'}:
            names = dict(context.scene.get('uvpack_' + self.kind.lower() + '_names', {}))
            names[str(new_id)] = '{} {}'.format(self.kind.title(), new_id)
            context.scene['uvpack_' + self.kind.lower() + '_names'] = names
        self.report({'INFO'}, '{:s} {:d}'.format(self.kind.title(), new_id))
        return {'FINISHED'}


# Theme bone color sets in an order where neighboring ids get clearly different hues.
_COLOR_SET_ORDER = (4, 3, 9, 6, 2, 8, 1, 11, 12, 5, 7, 10, 13, 14, 15)


def _color_set(index, kind):
    # Stacks start further along the palette, so group 1 and stack 1 differ.
    offset = 0 if kind == 'GROUP' else 2
    # Theme slots 16-20 are uninitialized black by default. Also skip any
    # deliberately dark custom slots so boundaries stay visible on the canvas.
    sets = bpy.context.preferences.themes[0].bone_color_sets
    colors = tuple(i for i in _COLOR_SET_ORDER if max(sets[i-1].active) > 0.15)
    colors = colors or _COLOR_SET_ORDER
    return colors[(index - 1 + offset) % len(colors)]


def id_color(index, kind='GROUP'):
    """Color of a group or stack id, matching #id_icon."""
    sets = bpy.context.preferences.themes[0].bone_color_sets
    return tuple(sets[_color_set(index, kind) - 1].active)


def id_icon(index, kind='GROUP'):
    return 'COLORSET_{:02d}_VEC'.format(_color_set(index, kind))


def _overlay_entries(context):
    settings = context.scene.uv_tools_texel_pack
    if context.mode != 'EDIT_MESH' or context.area.ui_type != 'UV':
        return []
    if not context.space_data.overlay.show_overlays:
        return []
    lists = island_state(context)['lists']
    entries = []
    if settings.show_groups:
        entries += [('G', index, entry) for index, entry in sorted(lists['GROUP'].items())]
    if settings.show_stacks:
        entries += [('S', index, entry) for index, entry in sorted(lists['STACK'].items())]
    return entries


def _draw_bounds():
    context = bpy.context
    entries = _overlay_entries(context)
    if not entries:
        return
    view2d = context.region.view2d
    # Pad by a few pixels so boxes don't cover island edges.
    x0, y0 = view2d.region_to_view(0, 0)
    x1, y1 = view2d.region_to_view(5, 5)
    pad_u, pad_v = abs(x1 - x0), abs(y1 - y0)
    fill = gpu.shader.from_builtin('UNIFORM_COLOR')
    line = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    blend = gpu.state.blend_get()
    gpu.state.blend_set('ALPHA')
    try:
        for prefix, index, entry in entries:
            u0, v0, u1, v1 = entry['bounds']
            # Stacks are drawn slightly outside groups so both stay readable.
            grow = 1.0 if prefix == 'G' else 1.8
            u0, v0, u1, v1 = u0 - pad_u * grow, v0 - pad_v * grow, u1 + pad_u * grow, v1 + pad_v * grow
            color = id_color(index, 'GROUP' if prefix == 'G' else 'STACK')
            corners = ((u0, v0), (u1, v0), (u1, v1), (u0, v1))
            # Preserve the original stack tint; groups remain outline-only.
            if prefix == 'S':
                fill.bind()
                fill.uniform_float('color', (*color, 0.05))
                batch_for_shader(fill, 'TRI_FAN', {'pos': corners}).draw(fill)
            line.bind()
            line.uniform_float('viewportSize', (context.region.width, context.region.height))
            line.uniform_float('lineWidth', (2.0 if entry['selected'] else 1.2) * context.preferences.system.ui_scale)
            line.uniform_bool('lineSmooth', True)
            line.uniform_float('color', (*color, 0.9))
            batch_for_shader(line, 'LINE_LOOP', {'pos': corners}).draw(line)
    finally:
        gpu.state.blend_set(blend)


def _draw_labels():
    context = bpy.context
    entries = _overlay_entries(context)
    if not entries:
        return
    view2d = context.region.view2d
    scale = context.preferences.system.ui_scale
    blf.size(0, int(13 * scale))
    blf.enable(0, blf.SHADOW)
    blf.shadow(0, 5, 0.0, 0.0, 0.0, 0.9)
    blf.shadow_offset(0, 1, -1)
    for prefix, index, entry in entries:
        u0, v0, u1, v1 = entry['bounds']
        label = '{} [{}{}]'.format(group_name(context.scene, index, 'GROUP' if prefix == 'G' else 'STACK'), prefix, index)
        if prefix == 'S' and group_name(context.scene, index, 'STACK') == 'Stack {}'.format(index):
            label = 'S{}'.format(index)
        if prefix == 'G':
            x, y = view2d.view_to_region(u0, v1, clip=False)
            x, y = x + 4 * scale, y + 6 * scale
        else:
            x, y = view2d.view_to_region(u1, v1, clip=False)
            x, y = x - blf.dimensions(0, label)[0] - 8 * scale, y + 6 * scale
        blf.color(0, *id_color(index, 'GROUP' if prefix == 'G' else 'STACK'), 1.0)
        blf.position(0, x, y, 0)
        blf.draw(0, label)
    blf.disable(0, blf.SHADOW)


def register():
    bpy.types.Scene.uv_tools_island = PointerProperty(type=UVToolsIslandConstraints)
    for handlers in (bpy.app.handlers.depsgraph_update_post, bpy.app.handlers.load_post):
        handlers.append(_cleanup_groups)
    if not bpy.app.background:
        space = bpy.types.SpaceImageEditor
        _handles.append(space.draw_handler_add(_draw_bounds, (), 'WINDOW', 'POST_VIEW'))
        _handles.append(space.draw_handler_add(_draw_labels, (), 'WINDOW', 'POST_PIXEL'))


def unregister():
    for handlers in (bpy.app.handlers.depsgraph_update_post, bpy.app.handlers.load_post):
        if _cleanup_groups in handlers:
            handlers.remove(_cleanup_groups)
    for handle in _handles:
        bpy.types.SpaceImageEditor.draw_handler_remove(handle, 'WINDOW')
    _handles.clear()
    _state_cache.clear()
    del bpy.types.Scene.uv_tools_island


classes = (UVToolsIslandConstraints, UVTOOLS_OT_texel_pack_new_id, UVTOOLS_OT_group_rename,
           UVTOOLS_OT_group_select_objects, UVTOOLS_OT_group_visibility,
           UVTOOLS_OT_group_membership, UVTOOLS_OT_group_delete, UVTOOLS_OT_group_material,
           UVTOOLS_OT_stack_detect)
