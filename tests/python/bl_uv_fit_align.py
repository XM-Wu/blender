# SPDX-License-Identifier: GPL-2.0-or-later
"""Run with blender --background --factory-startup --python-exit-code 1 --python this_file."""
import bpy
import bmesh
import math
from mathutils import Vector
from bl_operators.uv_tools_fit_align import bounds, snap_bounds, transformed, selected_units, ANCHORS


def close(a, b):
    assert len(a) == len(b) and all(abs(x-y)<1e-5 for x,y in zip(a,b)), (a,b)


points = [Vector((2,3)), Vector((6,3)), Vector((6,5)), Vector((2,5))]
target = (0,0,2,2)
close(bounds(transformed(points,target,'FIT')), target)
close(bounds(transformed(points,target,'FIT',proportional=True)), (0,.5,2,1.5))
close(bounds(transformed(points,target,'FIT',axes='U',proportional=True)), (0,3.5,2,4.5))
for key, anchor in ANCHORS.items():
    rect = bounds(transformed(points,target,'ALIGN',anchor=anchor))
    close((rect[2]-rect[0],rect[3]-rect[1]), (4,2))
    close((rect[0]+4*anchor[0], rect[1]+2*anchor[1]), (2*anchor[0],2*anchor[1]))
close(snap_bounds((-.24,.11,.71,.84),(.25,.25),'LARGER'),(-.25,0,.75,1))
close(snap_bounds((-.24,.11,.71,.84),(.25,.25),'SMALLER'),(0,.25,.5,.75))
close(snap_bounds((-.24,.11,.71,.84),(.25,.25),'CLOSEST'),(-.25,0,.75,.75))
try:
    transformed([Vector((0,0))],target,'FIT')
    raise AssertionError('Degenerate fit should fail')
except ValueError:
    pass

bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
verts=[(.2,.1,0),(.4,.1,0),(.4,.3,0),(.2,.3,0),(.6,.4,0),(1.2,.4,0),(1.2,.8,0),(.6,.8,0)]
mesh=bpy.data.meshes.new('Fit test')
mesh.from_pydata(verts,[],[(0,1,2,3),(4,5,6,7)])
uv=mesh.uv_layers.new()
for loop in mesh.loops:
    uv.data[loop.index].uv=verts[loop.vertex_index][:2]
obj=bpy.data.objects.new('Fit test',mesh)
bpy.context.collection.objects.link(obj)
obj.select_set(True)
bpy.context.view_layer.objects.active=obj
bpy.ops.object.mode_set(mode='EDIT')
screen=bpy.data.workspaces['UV Editing'].screens[0]
area=next(a for a in screen.areas if a.type=='IMAGE_EDITOR')
region=next(r for r in area.regions if r.type=='WINDOW')
with bpy.context.temp_override(screen=screen,area=area,region=region):
    bpy.context.scene.tool_settings.use_uv_select_sync=True
    bpy.ops.uv_tools.select_mode(type='FACE')
    bpy.ops.mesh.select_all(action='SELECT')
    bm=bmesh.from_edit_mesh(mesh)
    layer=bm.loops.layers.uv.active
    originals=[(loop,loop[layer].uv.copy()) for face in bm.faces for loop in face.loops]
    def reset():
        for loop,value in originals: loop[layer].uv=value
        bmesh.update_edit_mesh(mesh,loop_triangles=False,destructive=False)
    settings=bpy.context.scene.uv_tools_fit_align
    settings.target='BOX'; settings.box_min=(0,0); settings.box_max=(2,1)
    settings.grouping='MULTI'
    assert bpy.ops.uv_tools.fit_align(action='FIT')=={'FINISHED'}
    for unit in selected_units(bpy.context,'MULTI')[0]: close(bounds([r[1] for r in unit]),(0,0,2,1))
    reset(); settings.grouping='SINGLE'
    assert bpy.ops.uv_tools.fit_align(action='FIT')=={'FINISHED'}
    close(bounds([r[1] for r in selected_units(bpy.context,'SINGLE')[0][0]]),(0,0,2,1))
    reset(); settings.grouping='VERTEX';settings.target='SELECTION'
    assert bpy.ops.uv_tools.fit_align(action='ALIGN',axes='U',anchor='C')=={'FINISHED'}
    for loop,value in originals: close(loop[layer].uv,(.7,value.y))
    reset();settings.grouping='MULTI';settings.target='TILE';settings.tile=1012
    assert bpy.ops.uv_tools.fit_align(action='FIT')=={'FINISHED'}
    for unit in selected_units(bpy.context,'MULTI')[0]: close(bounds([r[1] for r in unit]),(1,1,2,2))
    for reference in ('PIXEL','GRID'):
        reset();settings.target=reference;settings.snapping='LARGER'
        assert bpy.ops.uv_tools.fit_align(action='FIT')=={'FINISHED'}
    reset();settings.target='TRIM'
    assert bpy.ops.uv_tools.fit_target(operation='ADD_TRIM')=={'FINISHED'}
    assert bpy.ops.uv_tools.fit_align(action='FIT')=={'FINISHED'}
    reset();settings.target='BOX'
    assert bpy.ops.uv_tools.fit_target(operation='BOX')=={'FINISHED'}
    close((*settings.box_min,*settings.box_max),(.2,.1,1.2,.8))
    settings.box_min=(0,0);settings.box_max=(2,1)
    originals[0][0][layer].pin_uv=True
    assert bpy.ops.uv_tools.fit_align(action='FIT')=={'CANCELLED'}
    for loop,value in originals: close(loop[layer].uv,value)
    originals[0][0][layer].pin_uv=False
    for mode,angle in (('ROTATE_CW',-math.pi/2),('ROTATE_CCW',math.pi/2),('ROTATE_180',math.pi)):
        reset()
        assert bpy.ops.uv_tools.transform(mode=mode)=={'FINISHED'}
        for face in bm.faces:
            old=[value for loop,value in originals if loop.face==face]
            rect=bounds(old);center=Vector(((rect[0]+rect[2])/2,(rect[1]+rect[3])/2))
            for loop,value in originals:
                if loop.face==face:
                    delta=value-center;c,s=math.cos(angle),math.sin(angle)
                    close(loop[layer].uv,center+Vector((c*delta.x-s*delta.y,s*delta.x+c*delta.y)))
print('PASS: rotations, 6 targets, 3 groupings, proportional fit, anchors, snapping and atomic pin protection')
