# SPDX-FileCopyrightText: 2026 Blender Authors
# SPDX-License-Identifier: GPL-2.0-or-later

import bpy
from bl_operators import workplane


scene = bpy.context.scene
original_name = scene.name
scene.cursor.location = (2, 3, 4)
scene.tool_settings.transform_pivot_point = 'ACTIVE_ELEMENT'
area = next(area for area in bpy.context.screen.areas if area.type == 'VIEW_3D')
with bpy.context.temp_override(area=area):
    bpy.ops.mesh.primitive_plane_add()
    bpy.ops.object.mode_set(mode='EDIT')
    assert bpy.ops.view3d.workplane_set() == {'FINISHED'}
    scene.name = 'Renamed Scene'
    other = bpy.data.scenes.new(original_name)
    workplane._undo_post(None)
    assert scene.use_workplane and not other.use_workplane
    assert bpy.ops.view3d.workplane_align_view.poll()
    assert bpy.ops.view3d.workplane_restore() == {'FINISHED'}
    assert not scene.use_workplane
    assert tuple(scene.cursor.location) == (2, 3, 4)
    assert scene.tool_settings.transform_pivot_point == 'ACTIVE_ELEMENT'
    assert not bpy.ops.view3d.workplane_align_view.poll()
