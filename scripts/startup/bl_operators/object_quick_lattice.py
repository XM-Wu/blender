# SPDX-FileCopyrightText: 2026 Blender Authors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Shared, non-destructive lattice rigs for objects and multi-object mesh selections."""
import bpy
import bmesh
from bpy.props import IntVectorProperty
from mathutils import Vector


def selected_indices(bm, modes):
    bm.verts.index_update()
    vertices = set()
    if modes[0]:
        vertices.update(v for v in bm.verts if v.select and not v.hide)
    if modes[1]:
        vertices.update(v for edge in bm.edges if edge.select and not edge.hide for v in edge.verts if not v.hide)
    if modes[2]:
        vertices.update(v for face in bm.faces if face.select and not face.hide for v in face.verts if not v.hide)
    return sorted(v.index for v in vertices)


def collect_targets(context):
    editing = context.mode == 'EDIT_MESH'
    objects = context.objects_in_mode if editing else context.selected_editable_objects
    depsgraph = context.evaluated_depsgraph_get()
    targets, points = [], []
    for obj in objects:
        if obj.type != 'MESH' or not obj.is_editable:
            continue
        if editing:
            bm = bmesh.from_edit_mesh(obj.data)
            bm.verts.ensure_lookup_table()
            indices = selected_indices(bm, context.tool_settings.mesh_select_mode)
            if not indices:
                continue
            points.extend(obj.matrix_world @ bm.verts[i].co for i in indices)
        else:
            indices = None
            evaluated = obj.evaluated_get(depsgraph)
            if not evaluated.data.vertices:
                continue
            points.extend(evaluated.matrix_world @ Vector(corner) for corner in evaluated.bound_box)
        targets.append((obj, indices))
    return targets, points


class OBJECT_OT_quick_lattice(bpy.types.Operator):
    bl_idname = 'object.quick_lattice'
    bl_label = 'Quick Lattice'
    bl_description = 'Fit a shared lattice to selected meshes or mesh elements and edit its native control points'
    bl_options = {'REGISTER', 'UNDO'}

    resolution: IntVectorProperty(name='Resolution', size=3, default=(2, 2, 2), min=2, max=6)

    @classmethod
    def poll(cls, context):
        if context.mode not in {'OBJECT', 'EDIT_MESH'}:
            return False
        objects = context.objects_in_mode if context.mode == 'EDIT_MESH' else context.selected_editable_objects
        return any(obj.type == 'MESH' and obj.is_editable for obj in objects)

    def execute(self, context):
        targets, points = collect_targets(context)
        if not targets or not points:
            self.report({'WARNING'}, 'Select mesh objects or visible vertices, edges or faces first')
            return {'CANCELLED'}
        editing = context.mode == 'EDIT_MESH'
        lower = Vector(tuple(min(p[axis] for p in points) for axis in range(3)))
        upper = Vector(tuple(max(p[axis] for p in points) for axis in range(3)))
        center = (lower + upper) * .5
        size = upper - lower
        # Planar, linear and single-vertex selections must still yield an invertible cage.
        thickness = max(max(size) * .001, .001)
        size = Vector(tuple(max(v, thickness) for v in size))
        if editing:
            bpy.ops.object.mode_set(mode='OBJECT')

        # Reuse Blender's native creation, resolution initialization and modifier setup.
        # Only the fit differs: edit-mode targets use selected vertices, not whole objects.
        for obj in context.selected_objects:
            obj.select_set(False)
        for obj, _ in targets:
            obj.select_set(True)
        context.view_layer.objects.active = targets[0][0]
        bpy.ops.object.lattice_add_to_selected(
            fit_to_selected=False, add_modifiers=True, enter_editmode=False,
            location=tuple(center), rotation=(0.0, 0.0, 0.0), align='WORLD', radius=1.0,
            resolution_u=self.resolution[0], resolution_v=self.resolution[1], resolution_w=self.resolution[2])
        lattice = context.view_layer.objects.active
        lattice.name = 'Quick Lattice'
        lattice_data = lattice.data
        lattice_data.name = 'Quick Lattice'
        lattice.location = center
        lattice.rotation_euler = (0.0, 0.0, 0.0)
        extent = [max(p.co_deform[i] for p in lattice_data.points) -
                  min(p.co_deform[i] for p in lattice_data.points) for i in range(3)]
        lattice.scale = Vector(tuple(size[i] / extent[i] for i in range(3)))
        lattice.display_type = 'WIRE'
        lattice.show_in_front = True
        context.view_layer.update()

        for obj, indices in targets:
            group = None
            if indices is not None:
                # Deform weights live on mesh data; keep unselected linked instances untouched.
                if obj.data.users > 1:
                    obj.data = obj.data.copy()
                group = obj.vertex_groups.new(name='Quick Lattice')
                group.add(indices, 1.0, 'REPLACE')
            modifier = next(m for m in obj.modifiers if m.type == 'LATTICE' and m.object == lattice)
            modifier.name = 'Quick Lattice'
            modifier.show_in_editmode = True
            modifier.show_on_cage = True
            if group is not None:
                modifier.vertex_group = group.name
                # Selected edit vertices are measured before the existing modifier stack.
                obj.modifiers.move(len(obj.modifiers) - 1, 0)

        for obj in context.selected_objects:
            obj.select_set(False)
        lattice.select_set(True)
        context.view_layer.objects.active = lattice
        context.view_layer.update()
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.lattice.select_all(action='SELECT')
        self.report({'INFO'}, 'Created lattice for %d meshes' % len(targets))
        return {'FINISHED'}


def is_quick_lattice_group(name):
    return name == 'Quick Lattice' or (name.startswith('Quick Lattice.') and name[14:].isdigit())


def group_still_used(obj, name):
    owners = list(obj.modifiers) + list(obj.constraints) + list(obj.particle_systems)
    for modifier in obj.modifiers:
        # Geometry Nodes may read a group by name through arbitrary node graphs.
        if modifier.type == 'NODES':
            return True
        if modifier.type == 'ARMATURE' and modifier.object and name in modifier.object.data.bones:
            return True
        for attr in ('settings', 'collision_settings'):
            owner = getattr(modifier, attr, None)
            if owner is not None:
                owners.append(owner)
    keys = getattr(obj.data, 'shape_keys', None)
    if keys:
        owners.extend(keys.key_blocks)
    for owner in owners:
        for prop in owner.bl_rna.properties:
            if prop.type == 'STRING' and ('vertex_group' in prop.identifier or prop.identifier == 'subtarget'):
                if getattr(owner, prop.identifier) == name:
                    return True
    return False


class OBJECT_OT_lattice_apply_all(bpy.types.Operator):
    bl_idname = 'object.lattice_apply_all'
    bl_label = 'Apply All'
    bl_description = 'Apply every modifier referencing this lattice, then delete the lattice'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.object
        return (obj is not None and obj.type == 'LATTICE' and obj.is_editable
                and context.mode in {'OBJECT', 'EDIT_LATTICE'})

    def execute(self, context):
        lattice = context.object
        targets = []
        for obj in bpy.data.objects:
            modifiers = [m for m in obj.modifiers
                         if m.type in {'LATTICE', 'GREASE_PENCIL_LATTICE'} and m.object == lattice]
            if modifiers:
                if not obj.is_editable or obj.mode != 'OBJECT':
                    self.report({'ERROR'}, 'Cannot apply modifiers on non-editable object: ' + obj.name)
                    return {'CANCELLED'}
                if getattr(obj.data, 'shape_keys', None):
                    self.report({'ERROR'}, 'Native modifier Apply does not support shape keys: ' + obj.name)
                    return {'CANCELLED'}
                targets.append((obj, [m.name for m in modifiers]))

        editing = context.mode == 'EDIT_LATTICE'
        selected = list(context.selected_objects)
        if editing:
            bpy.ops.object.mode_set(mode='OBJECT')
        stages = []
        try:
            # Evaluate on copies first. A failure must not leave half the targets applied.
            for obj, names in targets:
                stage = obj.copy()
                stage.data = obj.data.copy()
                stages.append(stage)
                context.scene.collection.objects.link(stage)
                stage.hide_viewport = False
                stage.hide_set(False)
                for name in names:
                    stage.modifiers[name].show_viewport = True
                    with context.temp_override(object=stage, active_object=stage,
                                               selected_objects=[stage], selected_editable_objects=[stage]):
                        result = bpy.ops.object.modifier_apply(modifier=name)
                    if result != {'FINISHED'}:
                        raise RuntimeError('Could not apply ' + obj.name + ': ' + name)
                if stage.type != obj.type:
                    raise RuntimeError('Applying the modifier changed object type: ' + obj.name)
        except (RuntimeError, TypeError, ValueError) as error:
            for stage in stages:
                data = stage.data
                bpy.data.objects.remove(stage, do_unlink=True)
                if data.users == 0:
                    bpy.data.batch_remove(ids=[data])
            for obj in context.selected_objects:
                obj.select_set(False)
            for obj in selected:
                obj.select_set(True)
            context.view_layer.objects.active = lattice
            if editing:
                bpy.ops.object.mode_set(mode='EDIT')
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}

        count = 0
        for (obj, names), stage in zip(targets, stages):
            groups = {obj.modifiers[name].vertex_group for name in names
                      if is_quick_lattice_group(obj.modifiers[name].vertex_group)}
            obj.data = stage.data
            for name in names:
                obj.modifiers.remove(obj.modifiers[name])
                count += 1
            bpy.data.objects.remove(stage, do_unlink=True)
            for name in groups:
                group = obj.vertex_groups.get(name)
                if group is not None and not group_still_used(obj, name):
                    obj.vertex_groups.remove(group)
        for child in list(lattice.children):
            matrix = child.matrix_world.copy()
            child.parent = None
            child.matrix_world = matrix
        data = lattice.data
        bpy.data.objects.remove(lattice, do_unlink=True)
        if data.users == 0:
            bpy.data.lattices.remove(data)
        for obj in context.selected_objects:
            obj.select_set(False)
        visible = [obj for obj, _ in targets if obj.name in context.view_layer.objects and obj.visible_get()]
        for obj in visible:
            obj.select_set(True)
        context.view_layer.objects.active = visible[0] if visible else None
        context.view_layer.update()
        self.report({'INFO'}, 'Applied %d lattice modifiers on %d objects' % (count, len(targets)))
        return {'FINISHED'}


classes = (OBJECT_OT_quick_lattice, OBJECT_OT_lattice_apply_all)
