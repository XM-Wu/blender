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


classes = (
    MeshSelectNext,
    MeshSelectPrev,
    MeshVertEdgeSlide,
)
