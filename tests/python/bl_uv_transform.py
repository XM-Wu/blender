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

# A pyramid fan with a split base edge. The apex is an interior UV, and
# selecting its four base corners leaves the intermediate boundary UV free.
bpy.ops.object.mode_set(mode='OBJECT')
mesh = bpy.data.meshes.new('Pyramid Rect')
base = [(-2, -1), (0, -1), (2, -1), (2, 1), (-2, 1)]
mesh.from_pydata([(x, y, 0) for x, y in base] + [(0, 0, 2)], [],
                 [(i, (i + 1) % 5, 5) for i in range(5)])
pyramid = bpy.data.objects.new('Pyramid Rect', mesh)
bpy.context.collection.objects.link(pyramid)
bpy.ops.object.select_all(action='DESELECT')
pyramid.select_set(True)
bpy.context.view_layer.objects.active = pyramid
bpy.ops.object.mode_set(mode='EDIT')
bm = bmesh.from_edit_mesh(mesh)
bm.verts.index_update()
layer = bm.loops.layers.uv.verify()
loops = [loop for face in bm.faces for loop in face.loops]
initial = [(2, 1), (3.9, 1.3), (6.2, 1.4), (6.5, 3.1), (2.2, 3.5), (4.8, 2.1)]

def reset_uvs():
    for loop in loops:
        loop[layer].uv = initial[loop.vert.index]

def positions():
    return {l.vert.index: l[layer].uv.copy() for l in loops}

def twice_area():
    pts = positions()
    return sum((pts[(i + 1) % 5] - pts[i]).cross(pts[5] - pts[i]) for i in range(5))

def assert_rect(vertical):
    pts = positions()
    for a, b in ((0, 1), (1, 2), (2, 3), (3, 4), (4, 0)):
        delta = pts[b] - pts[a]
        assert min(abs(delta.x), abs(delta.y)) < 1e-5
        assert (pts[b] - pts[a]).cross(pts[5] - pts[a]) > 0
    assert ((max(p.y for p in pts.values()) - min(p.y for p in pts.values())) >
            (max(p.x for p in pts.values()) - min(p.x for p in pts.values()))) == vertical

with bpy.context.temp_override(screen=screen, area=area, region=region):
    settings = bpy.context.scene.tool_settings
    settings.use_uv_select_sync = True
    bpy.ops.uv_tools.select_mode(type='FACE')
    bpy.ops.mesh.select_all(action='SELECT')
    reset_uvs()
    original_area = twice_area()
    assert bpy.ops.uv_tools.transform(mode='RECT_H') == {'FINISHED'}
    assert_rect(False)
    assert abs(twice_area() - original_area) < 1e-5
    for axis in ('U', 'V'):
        before = positions()
        assert bpy.ops.uv_tools.transform(mode='FLIP_' + axis) == {'FINISHED'}
        assert twice_area() < 0
        unchanged_axis = 1 if axis == 'U' else 0
        for index, point in positions().items():
            assert abs(point[unchanged_axis] - before[index][unchanged_axis]) < 1e-5
        assert bpy.ops.uv_tools.transform(mode='FLIP_' + axis) == {'FINISHED'}
        assert all((before[i] - p).length < 1e-5 for i, p in positions().items())
    settings.use_uv_select_sync = False
    bpy.ops.uv_tools.select_mode(type='VERTEX')
    reset_uvs()
    for loop in loops:
        loop.uv_select_vert = loop.vert.index in {0, 2, 3, 4}
    assert bpy.ops.uv_tools.transform(mode='RECT_V') == {'FINISHED'}
    assert_rect(True)
    reset_uvs()
    for loop in loops:
        loop.uv_select_vert = loop.vert.index in {0, 1, 2}
    assert bpy.ops.uv_tools.transform(mode='RECT_H') == {'FINISHED'}
    pts = positions()
    assert max(pts[i].y for i in (0, 1, 2)) - min(pts[i].y for i in (0, 1, 2)) < 1e-5
    assert all((pts[i] - Vector(initial[i])).length < 1e-6 for i in (3, 4, 5))
    bpy.ops.uv_tools.select_mode(type='EDGE')
    reset_uvs()
    for loop in loops:
        loop.uv_select_edge = all(v.index in {0, 1, 2} for v in loop.edge.verts)
    assert bpy.ops.uv_tools.transform(mode='RECT_V') == {'FINISHED'}
    pts = positions()
    assert max(pts[i].x for i in (0, 1, 2)) - min(pts[i].x for i in (0, 1, 2)) < 1e-5
    reset_uvs()
    for loop in loops:
        loop.uv_select_edge = all(v.index < 5 for v in loop.edge.verts)
    next(l for l in loops if l.vert.index == 0)[layer].pin_uv = True
    before = positions()
    assert bpy.ops.uv_tools.transform(mode='RECT_H') == {'CANCELLED'}
    assert before == positions()
    for loop in loops:
        loop[layer].pin_uv = False
    assert bpy.ops.uv_tools.transform(mode='RECT_H') == {'FINISHED'}
    assert_rect(False)
print('PASS: pyramid Rect, four corners, point and edge chains, boundary loop, pins and U/V flips')

# A cut-off triangular pyramid side can only supply three distinct UV corners.
# A ring with two boundary cycles requires a seam before it can fill a rectangle.
from collections import defaultdict
from bl_operators.uv_tools_transform import disk_rectangle

def solve_patch(coords, polygons):
    patch = bmesh.new()
    try:
        verts = [patch.verts.new((*point, 0)) for point in coords]
        for polygon in polygons:
            patch.faces.new([verts[i] for i in polygon])
        patch.verts.index_update()
        uv = patch.loops.layers.uv.new()
        for face in patch.faces:
            for loop in face.loops:
                loop[uv].uv = coords[loop.vert.index]
        triangles = defaultdict(list)
        for tri in patch.calc_loop_triangles():
            triangles[tri[0].face].append(tri)
        nodes, solved = disk_rectangle(list(patch.faces), uv, triangles)
        return {ls[0].vert.index: solved[i] for i, ls in nodes.items()}
    finally:
        patch.free()

tri = solve_patch([(0, 0), (2, .2), (.3, 1)], [(0, 1, 2)])
assert abs((tri[0] - tri[2]).dot(tri[1] - tri[2])) < 1e-6
assert abs((tri[1] - tri[0]).cross(tri[2] - tri[0]) - 1.94) < 1e-5
try:
    solve_patch([(-2,-2), (2,-2), (2,2), (-2,2), (-1,-1), (1,-1), (1,1), (-1,1)],
                [(i, (i+1)%4, (i+1)%4+4, i+4) for i in range(4)])
except ValueError as error:
    assert 'boundary' in str(error)
else:
    raise AssertionError('Rect accepted a patch with a hole')
