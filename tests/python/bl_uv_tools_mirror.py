# SPDX-License-Identifier: GPL-2.0-or-later
"""Run in factory-startup Blender, background mode."""
import bpy
import bmesh
import numpy as np

# A pair of triangles mirrored in mesh-local space. UVs are intentionally
# asymmetric and the destination UV layer is not the first layer.
for axis, name in enumerate("XYZ"):
    if bpy.context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='DESELECT')
    mesh = bpy.data.meshes.new(name)
    verts = [(1,0,0), (2,.3,0), (1,.5,.6)]
    points = [(sign*x,y,z) for sign in (-1,1) for x,y,z in verts]
    points = [p[-axis:] + p[:-axis] if axis else p for p in points]
    mesh.from_pydata(points, [], [(0,1,2),(5,4,3)])
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    obj.location.x = 3
    mesh.uv_layers.new(name='Base')
    mesh.uv_layers.active = mesh.uv_layers.new(name='Paint')
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.context.scene.tool_settings.use_uv_select_sync=True
    bpy.ops.mesh.select_mode(type='FACE')
    bpy.ops.mesh.select_all(action='SELECT')
    bm=bmesh.from_edit_mesh(mesh)
    bm.verts.index_update()
    active=bm.loops.layers.uv.active
    first=bm.loops.layers.uv.get('Base')
    loops=[loop for face in bm.faces for loop in face.loops]
    uv=[(.05,.1),(.15,.2),(.25,.4),(.60,.80),(.72,.67),(.89,.72)]
    for uv_axis in (0,1):
        for positive in (True,False):
            for l in loops:
                l[active].uv=uv[l.vert.index]
                l[first].uv=(.125,.25)
            assert bpy.ops.uv.copy_mirrored_faces(mesh_axis=('POS_' if positive else 'NEG_')+name,
                                                  uv_axis='XY'[uv_axis])=={'FINISHED'}
            for l in loops:
                idx=l.vert.index
                expected=list(uv[idx])
                if (idx>=3)==positive:
                    expected=list(uv[(idx+3)%6])
                    expected[uv_axis]=1-expected[uv_axis]
                assert np.allclose(l[active].uv,expected),(name,uv_axis,positive,idx)
                assert tuple(l[first].uv)==(.125,.25)
print('UV mirror passed: all four direction buttons, three local axes and secondary UV layer')
