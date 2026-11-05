# SPDX-FileCopyrightText: 2026 Blender Authors
# SPDX-License-Identifier: GPL-2.0-or-later

import bpy
from bpy.app.handlers import persistent
from mathutils import Matrix, Vector

_state = None


def _redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _restore():
    global _state
    if _state:
        state, _state = _state, None
        try:
            scene = state["scene"]
            scene.use_workplane = False
            scene.cursor.matrix = state["cursor"]
            scene.cursor.rotation_mode = state["cursor_mode"]
            scene.transform_orientation_slots[0].type = state["orientation"]
            scene.tool_settings.transform_pivot_point = state["pivot"]
            for region, rotation, location, perspective, distance in state.get("views", []):
                try:
                    region.view_rotation = rotation
                    region.view_location = location
                    region.view_perspective = perspective
                    region.view_distance = distance
                except ReferenceError:
                    pass
        except (ReferenceError, TypeError):
            pass
    _redraw()


@persistent
def _load_pre(_dummy):
    _restore()


@persistent
def _undo_post(_dummy):
    if _state:
        scene = bpy.data.scenes.get(_state["scene_name"])
        if scene is None:
            _restore()
            return
        _state["scene"] = scene
        scene.cursor.matrix = _state["matrix"]
        scene.use_workplane = True
        scene.transform_orientation_slots[0].type = 'CURSOR'
        scene.tool_settings.transform_pivot_point = 'CURSOR'
        _redraw()


class VIEW3D_OT_workplane_set(bpy.types.Operator):
    """Use the active selected face as a temporary XY workplane"""
    bl_idname = "view3d.workplane_set"
    bl_label = "Set Workplane from Face"

    @classmethod
    def poll(cls, context):
        return context.area and context.area.type == 'VIEW_3D' and context.mode == 'EDIT_MESH'

    def execute(self, context):
        import bmesh
        global _state
        obj = context.edit_object
        bm = bmesh.from_edit_mesh(obj.data)
        face = bm.faces.active
        if face is None or not face.select:
            faces = [f for f in bm.faces if f.select and not f.hide]
            face = faces[0] if len(faces) == 1 else None
        if face is None or face.hide:
            self.report({'WARNING'}, "Select an active face to define the workplane")
            return {'CANCELLED'}
        world = obj.matrix_world
        points = [world @ v.co for v in face.verts]
        origin = sum(points, Vector()) / len(points)
        # World-space polygon normal also handles non-uniform and negative scale.
        normal = Vector()
        for i, point in enumerate(points):
            normal += (point - origin).cross(points[(i + 1) % len(points)] - origin)
        if normal.length < 1e-10:
            self.report({'WARNING'}, "Cannot use a degenerate face")
            return {'CANCELLED'}
        z = normal.normalized()
        edges = [points[(i + 1) % len(points)] - p for i, p in enumerate(points)]
        x = max((e - z * e.dot(z) for e in edges), key=lambda e: e.length_squared)
        x.normalize()
        matrix = Matrix((x, z.cross(x), z)).transposed().to_4x4()
        matrix.translation = origin
        if _state and _state["scene"] != context.scene:
            _restore()
        scene = context.scene
        if _state is None:
            _state = dict(scene=scene, scene_name=scene.name, cursor=scene.cursor.matrix.copy(),
                          cursor_mode=scene.cursor.rotation_mode,
                          orientation=scene.transform_orientation_slots[0].type,
                          pivot=scene.tool_settings.transform_pivot_point)
        previous = _state.get("matrix", Matrix.Identity(4)).to_quaternion()
        delta = matrix.to_quaternion() @ previous.inverted()
        views = _state.setdefault("views", [])
        for window in context.window_manager.windows:
            if window.scene != scene:
                continue
            for area in window.screen.areas:
                if area.type != 'VIEW_3D':
                    continue
                space = area.spaces.active
                regions = list(space.region_quadviews) or [space.region_3d]
                for region in regions:
                    if region is None or region.view_perspective == 'CAMERA':
                        continue
                    if not any(item[0] == region for item in views):
                        views.append((region, region.view_rotation.copy(),
                                      region.view_location.copy(), region.view_perspective,
                                      region.view_distance))
                    region.view_rotation = delta @ region.view_rotation
                    region.view_location = origin
        _state.update(matrix=matrix)
        scene.cursor.matrix = matrix
        scene.use_workplane = True
        scene.transform_orientation_slots[0].type = 'CURSOR'
        scene.tool_settings.transform_pivot_point = 'CURSOR'
        _redraw()
        return {'FINISHED'}


class VIEW3D_OT_workplane_restore(bpy.types.Operator):
    bl_idname = "view3d.workplane_restore"
    bl_label = "Restore Defaults"
    bl_description = "Exit the temporary reference frame and restore the original viewport settings"

    def execute(self, context):
        _restore()
        return {'FINISHED'}


class VIEW3D_OT_workplane_align_view(bpy.types.Operator):
    bl_idname = "view3d.workplane_align_view"
    bl_label = "View Workplane from Above"

    def execute(self, context):
        if not _state or context.space_data.type != 'VIEW_3D':
            return {'CANCELLED'}
        views = _state.setdefault("views", [])
        region = context.space_data.region_3d
        if not any(item[0] == region for item in views):
            views.append((region, region.view_rotation.copy(), region.view_location.copy(),
                          region.view_perspective, region.view_distance))
        window_region = next((r for r in context.area.regions if r.type == 'WINDOW'), None)
        if window_region is None:
            return {'CANCELLED'}
        with context.temp_override(region=window_region):
            bpy.ops.view3d.view_axis(type='TOP', align_active=False)
        region.view_location = _state["matrix"].translation
        return {'FINISHED'}


class VIEW3D_PT_workplane(bpy.types.Panel):
    bl_label = "Temporary World Coordinates"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "View"

    def draw(self, context):
        layout = self.layout
        layout.operator("view3d.workplane_set")
        if _state and _state["scene"] == context.scene:
            layout.label(text="Viewport reference frame active")
            layout.label(text="New objects follow this plane")
            layout.operator("view3d.workplane_align_view")
            layout.operator("view3d.workplane_restore")


def register():
    bpy.app.handlers.load_pre.append(_load_pre)
    bpy.app.handlers.undo_post.append(_undo_post)
    bpy.app.handlers.redo_post.append(_undo_post)


def unregister():
    _restore()
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _undo_post in handlers:
            handlers.remove(_undo_post)
    if _load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_load_pre)


classes = (VIEW3D_OT_workplane_set, VIEW3D_OT_workplane_restore,
           VIEW3D_OT_workplane_align_view, VIEW3D_PT_workplane)
