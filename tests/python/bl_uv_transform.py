# SPDX-License-Identifier: GPL-2.0-or-later
import math
import bpy
import bmesh
from mathutils import Vector
from bl_operators.uv_tools_transform import orient, rectangle
from bl_operators.uv_tools_texel_pack import group_entries, group_name, id_color, id_icon

for stack_id in range(1, 257):
    assert max(id_color(stack_id, 'STACK')) > 0.15
    assert int(id_icon(stack_id, 'STACK').split('_')[1]) <= 15

points = [Vector((x*math.cos(.37)-y*math.sin(.37), x*math.sin(.37)+y*math.cos(.37)))
          for x,y in ((0,0),(4,0),(4,1),(0,1))]
for vertical in (False, True):
    result = orient(points, vertical)
    w=max(p.x for p in result)-min(p.x for p in result)
    h=max(p.y for p in result)-min(p.y for p in result)
    assert abs(w*h-4) < 1e-5, (w,h)
    assert (h>w) == vertical

bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
bpy.ops.mesh.primitive_grid_add(x_subdivisions=4, y_subdivisions=2)
obj=bpy.context.object
bpy.ops.object.mode_set(mode='EDIT')
bm=bmesh.from_edit_mesh(obj.data)
bm.verts.index_update()
layer=bm.loops.layers.uv.active
nodes, result=rectangle(list(bm.faces),layer)
assert len(result)==15
for face in bm.faces:
    ids=[next(n for n, ls in nodes.items() if l in ls) for l in face.loops]
    for a,b in zip(ids,ids[1:]+ids[:1]):
        d=result[a]-result[b]
        assert abs(d.x)<1e-6 or abs(d.y)<1e-6
screen=bpy.data.workspaces['UV Editing'].screens[0]
area=next(a for a in screen.areas if a.type=='IMAGE_EDITOR')
region=next(r for r in area.regions if r.type=='WINDOW')
with bpy.context.temp_override(screen=screen,area=area,region=region):
    bpy.context.scene.tool_settings.use_uv_select_sync=True
    bpy.ops.uv_tools.select_mode(type='FACE')
    bpy.ops.mesh.select_all(action='SELECT')
    for mode in ('ORIENT_H','ORIENT_V','RECT_H','RECT_V'):
        assert bpy.ops.uv_tools.transform(mode=mode)=={'FINISHED'},mode
    before = [tuple(l[layer].uv) for f in bm.faces for l in f.loops]
    assert bpy.ops.uv_tools.transform(mode='STRAIGHT') == {'CANCELLED'}
    assert before == [tuple(l[layer].uv) for f in bm.faces for l in f.loops]
    bpy.ops.uv_tools.select_mode(type='EDGE')
    bpy.ops.mesh.select_all(action='DESELECT')
    for edge in bm.edges:
        if all(v.co.y < -0.99 for v in edge.verts): edge.select_set(True)
    bm.select_flush_mode()
    bmesh.update_edit_mesh(obj.data)
    for mode in ('STRAIGHT','STRAIGHT_H','STRAIGHT_V'):
        assert bpy.ops.uv_tools.transform(mode=mode)=={'FINISHED'},mode
bpy.ops.object.mode_set(mode='OBJECT')
attr=obj.data.attributes.new('uvpack_stack','INT','FACE')
for item in attr.data: item.value=3
bpy.ops.mesh.primitive_cube_add()
other=bpy.context.object
attr=other.data.attributes.new('uvpack_stack','INT','FACE')
for item in attr.data: item.value=8
assert set(group_entries(bpy.context,kind='STACK'))=={3,8}
assert bpy.ops.uv_tools.group_rename(kind='STACK',group=3,name='Hair Stack')=={'FINISHED'}
assert group_name(bpy.context.scene,3,'STACK')=='Hair Stack'
assert bpy.ops.uv_tools.group_select_objects(kind='STACK',group=3)=={'FINISHED'}
assert bpy.context.selected_objects==[obj]
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.select_all(action='SELECT')
assert bpy.ops.uv_tools.texel_pack_new_id(kind='STACK')=={'FINISHED'}
assert set(group_entries(bpy.context,kind='STACK'))=={9}
invalid = bmesh.new()
verts = [invalid.verts.new(co) for co in ((0,0,0),(1,0,0),(0,1,0))]
invalid.faces.new(verts)
uv = invalid.loops.layers.uv.new()
try:
    rectangle(list(invalid.faces), uv)
    raise AssertionError('Triangle island accepted as a regular quad grid')
except ValueError:
    pass
invalid.free()
print('PASS: all seven transforms, rectangular grid, orientation and scene-wide stack management')
