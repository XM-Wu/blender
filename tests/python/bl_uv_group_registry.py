# SPDX-License-Identifier: GPL-2.0-or-later
"""blender --background --factory-startup --python-exit-code 1 --python this_file"""
import bpy
import bmesh
import tempfile
from pathlib import Path
from bl_operators.uv_tools_texel_pack import group_entries, group_name, group_hidden, island_state, invalidate

bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)


def mesh_object(name, group):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    mesh.uv_layers.new()
    mesh.attributes.new('uvpack_group', 'INT', 'FACE').data[0].value = group
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj


a = mesh_object('First', 7)
b = mesh_object('Second', 21)
c = mesh_object('Third', 7)
b.select_set(True)
assert bpy.ops.uv_tools.group_select_objects(group=7) == {'FINISHED'}
assert set(bpy.context.selected_objects) == {a, c}
assert bpy.ops.uv_tools.group_visibility(group=7) == {'FINISHED'}
assert a.hide_get() and c.hide_get() and not b.hide_get()
assert group_hidden(bpy.context, 7)
assert 7 in group_entries(bpy.context), 'Hidden group must remain available to reveal'
assert bpy.ops.uv_tools.group_visibility(group=7) == {'FINISHED'}
assert not a.hide_get() and not c.hide_get()
assert not group_hidden(bpy.context, 7)
assert bpy.ops.uv_tools.group_select_objects(group=7) == {'FINISHED'}
assert set(bpy.context.selected_objects) == {a, c}
assert {i: e['faces'] for i, e in group_entries(bpy.context).items()} == {7: 2, 21: 1}
assert bpy.ops.uv_tools.group_rename(group=7, name='Hair') == {'FINISHED'}
assert group_name(bpy.context.scene, 7) == 'Hair'
a.select_set(True)
c.select_set(True)
bpy.context.view_layer.objects.active = a
bpy.ops.object.mode_set(mode='EDIT')
assert {i: e['faces'] for i, e in group_entries(bpy.context).items()} == {7: 2}
assert bpy.ops.uv_tools.group_visibility(group=7) == {'FINISHED'}
assert group_hidden(bpy.context, 7)
assert bpy.ops.uv_tools.group_visibility(group=7) == {'FINISHED'}
assert not group_hidden(bpy.context, 7)
assert bpy.ops.uv_tools.group_visibility(group=7) == {'FINISHED'}
invalidate()
assert group_entries(bpy.context)[7]['faces'] == 2
assert not island_state(bpy.context)['lists']['GROUP'], 'Hidden UVs must not produce bounds'
assert max(group_entries(bpy.context, all_meshes=True)) == 21
bpy.ops.object.mode_set(mode='OBJECT')
with tempfile.TemporaryDirectory() as directory:
    path = str(Path(directory) / 'groups.blend')
    bpy.ops.wm.save_as_mainfile(filepath=path)
    bpy.ops.wm.open_mainfile(filepath=path)
    assert group_name(bpy.context.scene, 7) == 'Hair'
    assert group_entries(bpy.context)[7]['faces'] == 2
print('PASS: scene/edit scope, multi-mesh membership, hidden faces, bounds, names and persistence')
