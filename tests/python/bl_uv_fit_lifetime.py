# SPDX-License-Identifier: GPL-2.0-or-later
"""No externally retained BMesh handles: reproduces real UI Fit invocation lifetime."""
import bpy
import gc
from bl_operators.uv_tools_fit_align import selected_units, bounds

bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
objects=[]
for i in range(3):
    bpy.ops.mesh.primitive_plane_add(location=(i*2,0,0))
    objects.append(bpy.context.object)
for obj in objects: obj.select_set(True)
bpy.context.view_layer.objects.active=objects[0]
bpy.ops.object.mode_set(mode='EDIT')
screen=bpy.data.workspaces['UV Editing'].screens[0]
area=next(a for a in screen.areas if a.type=='IMAGE_EDITOR')
with bpy.context.temp_override(screen=screen,area=area):
    bpy.context.scene.tool_settings.use_uv_select_sync=True
    bpy.ops.uv_tools.select_mode(type='FACE')
    bpy.ops.mesh.select_all(action='SELECT')
    settings=bpy.context.scene.uv_tools_fit_align
    settings.target='BOX';settings.box_min=(2,3);settings.box_max=(4,5)
    for grouping in ('MULTI','SINGLE','VERTEX'):
        units,_=selected_units(bpy.context,grouping)
        gc.collect()
        assert all(loop.is_valid for unit in units for refs,_ in unit for loop,layer in refs), grouping
        del units
        gc.collect()
    for grouping in ('MULTI','SINGLE'):
        settings.grouping=grouping
        assert bpy.ops.uv_tools.fit_align(action='FIT')=={'FINISHED'}
        gc.collect()
        units,_=selected_units(bpy.context,'MULTI')
        for unit in units:
            assert all(abs(a-b)<1e-6 for a,b in zip(bounds([r[1] for r in unit]),(2,3,4,5)))
        del units
        gc.collect()
    for mode in ('ORIENT_H','ROTATE_CW','RECT_H'):
        assert bpy.ops.uv_tools.transform(mode=mode)=={'FINISHED'}
        gc.collect()
print('PASS: Fit and Transform keep BMesh owners alive across collection, planning and writes')
