# SPDX-License-Identifier: GPL-2.0-or-later
import bpy
import bmesh
import math
from mathutils import Vector
from bl_operators.uv_tools_transform import geometry_axis_direction, TARGET_ANGLES

bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
mesh=bpy.data.meshes.new('Orientation reference')
mesh.from_pydata([(0,0,0),(2,0,0),(2,1,0),(0,1,0)],[],[(0,1,2,3)])
uv=mesh.uv_layers.new()
angle=.37
for loop in mesh.loops:
    x,y,_=mesh.vertices[loop.vertex_index].co
    uv.data[loop.index].uv=(x*math.cos(angle)-y*math.sin(angle),x*math.sin(angle)+y*math.cos(angle))
obj=bpy.data.objects.new('Orientation reference',mesh)
bpy.context.collection.objects.link(obj);obj.select_set(True)
bpy.context.view_layer.objects.active=obj
bpy.ops.object.mode_set(mode='EDIT')
screen=bpy.data.workspaces['UV Editing'].screens[0]
area=next(a for a in screen.areas if a.type=='IMAGE_EDITOR')
with bpy.context.temp_override(screen=screen,area=area):
    bm=bmesh.from_edit_mesh(mesh);bm.faces.ensure_lookup_table()
    layer=bm.loops.layers.uv.active
    loops=list(bm.faces[0].loops)
    original=[l[layer].uv.copy() for l in loops]
    def reset():
        for l,p in zip(loops,original):l[layer].uv=p
        bmesh.update_edit_mesh(mesh,loop_triangles=False,destructive=False)
    def rigid():
        for i in range(4):
            for j in range(4):
                assert abs((loops[i][layer].uv-loops[j][layer].uv).length-(original[i]-original[j]).length)<1e-5
    settings=bpy.context.scene.uv_tools_orientation
    settings.reference='EDGES'
    for sync in (False,True):
        bpy.context.scene.tool_settings.use_uv_select_sync=sync
        bpy.ops.uv_tools.select_mode(type='EDGE')
        bpy.ops.mesh.select_all(action='SELECT' if not sync else 'DESELECT')
        if sync:
            loops[0].edge.select_set(True);bm.select_flush_mode();bm.uv_select_sync_valid=False
        else:
            bpy.ops.uv.select_all(action='DESELECT')
            loops[0].uv_select_edge=True
            loops[0].uv_select_vert=True;loops[1].uv_select_vert=True
        for mode in ('ORIENT_H','ORIENT_V'):
            reset()
            assert bpy.ops.uv_tools.transform(mode=mode)=={'FINISHED'}
            direction=loops[1][layer].uv-loops[0][layer].uv
            assert abs(direction.y if mode=='ORIENT_H' else direction.x)<1e-5
            rigid()
    bpy.context.scene.tool_settings.use_uv_select_sync=True
    bpy.ops.uv_tools.select_mode(type='FACE');bpy.ops.mesh.select_all(action='SELECT')
    settings.reference='GEOMETRY'
    settings.axis='Z';settings.fallback_axis='Y'
    for target,theta in TARGET_ANGLES.items():
        reset();settings.target=target
        assert bpy.ops.uv_tools.transform(mode='ORIENT_3D')=={'FINISHED'}
        direction=geometry_axis_direction(bm.calc_loop_triangles(),layer,'Y')
        assert direction.dot(Vector((math.cos(theta),math.sin(theta))))>.99999
        rigid()
    settings.space='WORLD';settings.axis='X';settings.target='POS_V'
    obj.rotation_euler.z=math.pi/2;bpy.context.view_layer.update();reset()
    assert bpy.ops.uv_tools.transform(mode='ORIENT_3D')=={'FINISHED'}
    direction=geometry_axis_direction(bm.calc_loop_triangles(),layer,'X',obj.matrix_world)
    assert direction.y>.99999
    rigid()
    settings.space='LOCAL';settings.axis='Z';settings.fallback_axis='Z'
    reset()
    assert bpy.ops.uv_tools.transform(mode='ORIENT_3D')=={'CANCELLED'}
    assert [tuple(l[layer].uv) for l in loops]==[tuple(p) for p in original]
    settings.axis='X';settings.fallback_axis='Y';settings.target='POS_U'
    loops[0][layer].pin_uv=True
    assert bpy.ops.uv_tools.transform(mode='ORIENT_3D')=={'CANCELLED'}
    assert [tuple(l[layer].uv) for l in loops]==[tuple(p) for p in original]
print('PASS: selected-edge H/V with sync on/off, four signed targets, fallback, world space, rigid shape and pin protection')
