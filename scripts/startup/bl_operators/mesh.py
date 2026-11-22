# SPDX-FileCopyrightText: 2009-2023 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

from bpy.types import Operator


class MeshSelectNext(Operator):
    """Select the next element (using selection order)"""
    bl_idname = "mesh.select_next_item"
    bl_label = "Select Next Element"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'EDIT_MESH')

    def execute(self, context):
        import bmesh
        from .bmesh import find_adjacent

        obj = context.active_object
        me = obj.data
        bm = bmesh.from_edit_mesh(me)

        if find_adjacent.select_next(bm, self.report):
            bm.select_flush_mode()
            bmesh.update_edit_mesh(me, loop_triangles=False)

        return {'FINISHED'}


class MeshSelectPrev(Operator):
    """Select the previous element (using selection order)"""
    bl_idname = "mesh.select_prev_item"
    bl_label = "Select Previous Element"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'EDIT_MESH')

    def execute(self, context):
        import bmesh
        from .bmesh import find_adjacent

        obj = context.active_object
        me = obj.data
        bm = bmesh.from_edit_mesh(me)

        if find_adjacent.select_prev(bm, self.report):
            bm.select_flush_mode()
            bmesh.update_edit_mesh(me, loop_triangles=False)

        return {'FINISHED'}


class MeshVertEdgeSlide(Operator):
    """Slide an edge loop, or vertices when edge slide is not possible"""
    bl_idname = "mesh.vert_edge_slide"
    bl_label = "Vertex/Edge Slide"
    bl_options = set()

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def invoke(self, _context, _event):
        import bpy

        result = bpy.ops.transform.edge_slide('INVOKE_DEFAULT')
        if 'RUNNING_MODAL' in result or 'FINISHED' in result:
            return {'FINISHED'}
        result = bpy.ops.transform.vert_slide('INVOKE_DEFAULT')
        if 'RUNNING_MODAL' in result:
            return {'FINISHED'}
        return result


class XMEnterPoseMode(Operator):
    """Enter Pose Mode on the active armature"""
    bl_idname = "object.xm_enter_pose_mode"
    bl_label = "XM Enter Pose Mode"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'ARMATURE' and obj.is_editable

    def execute(self, context):
        import bpy
        return bpy.ops.object.mode_set(mode='POSE')


class XMEditModeToggle(Operator):
    """Leave Pose Mode for Object Mode, otherwise toggle Edit Mode"""
    bl_idname = "object.xm_edit_mode_toggle"
    bl_label = "XM Toggle Edit Mode"
    bl_options = set()

    def execute(self, context):
        import bpy
        import datetime
        import json
        import os
        import tempfile

        obj = context.active_object
        record = {
            "time": datetime.datetime.now().isoformat(),
            "mode_before": context.mode,
            "object": obj.name if obj else None,
            "type": obj.type if obj else None,
            "editable": obj.is_editable if obj else None,
            "selected": obj.select_get() if obj else None,
            "area": context.area.type if context.area else None,
            "keyconfig": context.window_manager.keyconfigs.active.name,
        }
        result = {'CANCELLED'}
        try:
            if context.mode == 'POSE':
                result = bpy.ops.object.mode_set(mode='OBJECT')
            elif bpy.ops.object.editmode_toggle.poll():
                result = bpy.ops.object.editmode_toggle('EXEC_DEFAULT')
            else:
                record["reason"] = "editmode_toggle poll failed"
                self.report({'WARNING'}, "Active object cannot enter Edit Mode")
        except RuntimeError as error:
            record["error"] = str(error)
            self.report({'WARNING'}, str(error))
        finally:
            record["mode_after"] = context.mode
            record["result"] = sorted(result)
            # Bounded persistent diagnostics, independent of the console window.
            try:
                path = os.path.join(tempfile.gettempdir(), "xm_edit_mode_toggle.log")
                if os.path.exists(path) and os.path.getsize(path) > 1024 * 1024:
                    os.replace(path, path + ".1")
                with open(path, "a", encoding="utf-8") as log:
                    log.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                pass
        return result


class MeshXMSmartDelete(Operator):
    """Dissolve vertices and edges, or delete faces along a selected border"""
    bl_idname = "mesh.xm_smart_delete"
    bl_label = "Smart Delete"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        import bpy
        import bmesh

        vertex_mode, edge_mode, _ = context.tool_settings.mesh_select_mode
        if vertex_mode:
            return bpy.ops.mesh.dissolve_verts()
        if edge_mode:
            edges = [edge for obj in context.objects_in_mode_unique_data
                     for edge in bmesh.from_edit_mesh(obj.data).edges if edge.select]
            if not edges:
                return {'CANCELLED'}

            # Match Interactive Tools' border test. Each endpoint must have two
            # boundary edges, and selected edges must average two neighbors.
            degrees = {}
            for edge in edges:
                for vert in edge.verts:
                    degrees[vert] = degrees.get(vert, 0) + 1
            neighbors = sum(degree * (degree - 1) for degree in degrees.values())
            if neighbors < 2 * len(edges) or not all(
                    sum(edge.is_boundary for edge in vert.link_edges) >= 2 for vert in degrees):
                return bpy.ops.mesh.dissolve_edges()
            for edge in edges:
                for face in edge.link_faces:
                    face.select = True
        return bpy.ops.mesh.delete(type='FACE')


class MeshApplyAllTransforms(Operator):
    """Apply object location, rotation and scale, then return to mesh editing"""
    bl_idname = "mesh.apply_all_transforms"
    bl_label = "Apply All Transforms"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH' and context.edit_object is not None

    def execute(self, context):
        import bpy

        objects = list(context.objects_in_mode)
        active = context.view_layer.objects.active
        selected = list(context.selected_objects)
        if bpy.ops.object.mode_set(mode='OBJECT') != {'FINISHED'}:
            return {'CANCELLED'}
        try:
            # Do not apply transforms to other selected objects outside edit mode.
            with context.temp_override(selected_objects=objects, selected_editable_objects=objects):
                return bpy.ops.object.transform_apply(
                    'EXEC_DEFAULT', location=True, rotation=True, scale=True,
                    isolate_users=False)
        except RuntimeError as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        finally:
            # Re-enter with exactly the original set of editing objects.
            for obj in context.selected_objects:
                obj.select_set(False)
            for obj in objects:
                obj.select_set(True)
            context.view_layer.objects.active = active
            bpy.ops.object.mode_set(mode='EDIT')
            for obj in selected:
                obj.select_set(True)


classes = (
    MeshSelectNext,
    MeshSelectPrev,
    MeshVertEdgeSlide,
    MeshXMSmartDelete,
    MeshApplyAllTransforms,
    XMEditModeToggle,
    XMEnterPoseMode,
)
