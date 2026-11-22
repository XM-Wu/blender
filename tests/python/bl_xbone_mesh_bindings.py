# SPDX-License-Identifier: GPL-2.0-or-later
"""Rigging editor batch bindings, including ambiguous selection and visibility."""
from types import SimpleNamespace

import bpy
from bl_operators import skin_layers as skin
from xbone_rigging import mesh_bindings as bindings


bpy.ops.wm.read_factory_settings(use_empty=True)
c = bpy.context
bpy.ops.object.xbone_add()
rig = c.object
bpy.ops.object.xbone_add()
other = c.object
mesh_data = bpy.data.meshes.new("Shared mesh")
mesh_data.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])


def mesh(name, collection):
    obj = bpy.data.objects.new(name, mesh_data)
    collection.objects.link(obj)
    return obj


def select(active, *others):
    for obj in c.selected_objects:
        obj.select_set(False)
    for obj in (active, *others):
        obj.select_set(True)
    c.view_layer.objects.active = active


def add(obj, target):
    modifier = obj.modifiers.new("Renamed modifier", 'ARMATURE')
    modifier.object = target
    return modifier


first = mesh("Coat", c.scene.collection)
second = mesh("Trim", c.scene.collection)
hidden = mesh("Lining", c.scene.collection)
untouched = mesh("Unbound", c.scene.collection)
elsewhere = mesh("Other scene", bpy.data.scenes.new("Other").collection)
add(first, other)
add(hidden, rig)
add(elsewhere, rig)
first.modifiers.new("Keep", 'SOLIDIFY')
hidden.hide_set(True)
ops = bpy.ops.xbone
area = next(a for a in c.screen.areas if a.type == 'PROPERTIES')
area.type = 'RIGGING'
with c.temp_override(area=area, region=next(r for r in area.regions if r.type == 'WINDOW')):
    select(first, rig, other)
    assert not ops.add_mesh_modifiers.poll()
    select(first, rig, second)
    assert bindings.current_rig(c) == rig
    assert ops.add_mesh_modifiers() == {'FINISHED'}
    assert ops.add_mesh_modifiers() == {'CANCELLED'}
    assert len(bindings.modifiers_for(first, rig)) == len(bindings.modifiers_for(second, rig)) == 1
    assert not untouched.modifiers and first.parent is None and second.parent is None
    add(first, rig)
    assert set(bindings.bound_meshes(c.scene, rig)) == {first, second, hidden}
    listing = SimpleNamespace(filter_name="", bitflag_filter_item=1)
    flags, _ = bindings.RIGGING_UL_bound_meshes.filter_items(listing, c, c.scene, "objects")
    assert {o for o, show in zip(c.scene.objects, flags) if show} == {first, second, hidden}
    select(rig)
    bpy.ops.object.mode_set(mode='POSE')
    assert ops.select_bound_meshes() == {'FINISHED'}
    assert c.mode == 'OBJECT' and set(c.selected_objects) == {first, second}
    assert ops.select_bound_meshes(mesh_name=first.name) == {'FINISHED'}
    assert c.active_object == first and c.selected_objects == [first]
    assert bindings.current_rig(c) == rig
    assert ops.clear_mesh_modifiers() == {'FINISHED'}
    assert not bindings.bound_meshes(c.scene, rig) and hidden.hide_get()
    assert len(bindings.modifiers_for(elsewhere, rig)) == len(bindings.modifiers_for(first, other)) == 1
    assert first.modifiers.get("Keep")

    # A bound Skin Tools stack can be resumed after removing and adding its modifier.
    select(second, rig)
    assert ops.add_mesh_modifiers() == {'FINISHED'}
    second.vertex_groups.new(name=rig.data.bones[0].name).add([0, 1, 2], 1, 'REPLACE')
    assert bpy.ops.paint.skin_source() == {'FINISHED'}
    layer = skin.current_layer(second.skin_layers)
    key = skin.layer_key(second.skin_layers, layer) + "_" + str(second.skin_layers.influence)
    assert all(skin.read_values(mesh_data, key) == 1)
    assert ops.clear_mesh_modifiers() == {'FINISHED'}
    assert "_skin_source" not in second and key in mesh_data.attributes
    assert ops.add_mesh_modifiers() == {'FINISHED'}
    assert bpy.ops.paint.skin_source() == {'FINISHED'}
    assert skin.brush_error(second) is None and all(skin.read_values(mesh_data, key) == 1)
print("PASS: XBone batch add, live list, selection, scoped removal and layered rebind")
