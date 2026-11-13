# SPDX-License-Identifier: GPL-2.0-or-later
import bpy
import bmesh
from bl_operators.uv_tools_texel_pack import group_entries, group_name, _cleanup_groups

bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
bpy.ops.mesh.primitive_grid_add(x_subdivisions=2, y_subdivisions=1)
a = bpy.context.object
bpy.ops.mesh.primitive_cube_add()
b = bpy.context.object
bpy.ops.object.select_all(action='DESELECT')
a.select_set(True)
bpy.context.view_layer.objects.active = a
assert bpy.ops.uv_tools.group_membership(operation='NEW') == {'FINISHED'}
first = next(iter(group_entries(bpy.context)))
assert bpy.ops.uv_tools.group_rename(group=first, name='First') == {'FINISHED'}
a.select_set(False); b.select_set(True)
assert bpy.ops.uv_tools.group_membership(choose=True,target=str(first)) == {'FINISHED'}
assert group_entries(bpy.context)[first]['faces'] == 8
assert bpy.ops.uv_tools.group_membership(operation='NEW') == {'FINISHED'}
second = max(group_entries(bpy.context))
assert first != second
assert str(first) in bpy.context.scene['uvpack_group_names'], 'Other mesh still owns first group'
b.hide_set(True)
a.select_set(True)
assert bpy.ops.uv_tools.group_membership(group=second) == {'FINISHED'}
assert first not in group_entries(bpy.context)
assert str(first) not in bpy.context.scene['uvpack_group_names']
assert group_entries(bpy.context)[second]['faces'] == 8
# Delete includes hidden objects, without deleting either mesh or changing vertices/UVs.
before = [(o.name, len(o.data.vertices), len(o.data.polygons)) for o in (a,b)]
assert bpy.ops.uv_tools.group_delete(group=second) == {'FINISHED'}
assert not group_entries(bpy.context)
assert before == [(o.name, len(o.data.vertices), len(o.data.polygons)) for o in (a,b)]
assert all(v.value == 0 for o in (a,b) for v in o.data.attributes['uvpack_group'].data)
b.hide_set(False); b.select_set(False); a.select_set(True)
bpy.context.view_layer.objects.active = a
bpy.ops.object.mode_set(mode='EDIT')
screen = bpy.data.workspaces['UV Editing'].screens[0]
area = next(a for a in screen.areas if a.type == 'IMAGE_EDITOR')
with bpy.context.temp_override(screen=screen,area=area):
    bpy.context.scene.tool_settings.use_uv_select_sync = True
    bpy.ops.uv_tools.select_mode(type='FACE')
    bpy.ops.mesh.select_all(action='DESELECT')
    bm = bmesh.from_edit_mesh(a.data)
    bm.faces.ensure_lookup_table()
    bm.faces[0].select_set(True)
    bmesh.update_edit_mesh(a.data)
    assert bpy.ops.uv_tools.group_membership(group=7) == {'FINISHED'}
    layer = bm.faces.layers.int['uvpack_group']
    assert all(face[layer] == 7 for face in bm.faces), 'Must assign the whole UV island'
    bpy.ops.uv_tools.group_rename(group=7,name='Island')
    assert bpy.ops.uv_tools.group_membership(operation='REMOVE') == {'FINISHED'}
    assert 7 not in group_entries(bpy.context)
    assert '7' not in bpy.context.scene['uvpack_group_names']
bpy.ops.object.mode_set(mode='OBJECT')
# External topology changes must also prune stale definitions.
bpy.ops.uv_tools.group_membership(operation='NEW')
index = next(iter(group_entries(bpy.context)))
a.data.clear_geometry()
bpy.context.view_layer.update()
_cleanup_groups()
assert str(index) not in bpy.context.scene['uvpack_group_names']
print('PASS: mesh/island assignment, reassignment, hidden members, delete and empty-group cleanup')
