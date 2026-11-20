# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

import os
import tempfile

import bpy
import numpy as np

from bl_operators import skin_layers as skin
from bl_operators.skin_layers_tools import mirror_geometry


def setup_mesh():
    bpy.ops.object.armature_add()
    rig = bpy.context.object
    bpy.ops.object.mode_set(mode='EDIT')
    left = rig.data.edit_bones[0]
    left.name, left.head, left.tail = "Arm.L", (1, 0, 0), (1, 1, 0)
    right = rig.data.edit_bones.new("Arm.R")
    right.head, right.tail = (-1, 0, 0), (-1, 1, 0)
    bpy.ops.object.mode_set(mode='OBJECT')
    mesh = bpy.data.meshes.new("Skin V2")
    mesh.from_pydata([(1, 0, 0), (-1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    obj = bpy.data.objects.new("Skin V2", mesh)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    rig.select_set(False)
    obj.modifiers.new("Rig", 'ARMATURE').object = rig
    obj.vertex_groups.new(name="Arm.L").add([0, 1, 2], 1, 'REPLACE')
    assert bpy.ops.paint.skin_source() == {'FINISHED'}
    return obj, rig


def run():
    obj, rig = setup_mesh()
    state = obj.skin_layers
    edit = bpy.ops.paint.skin_layer_edit
    pick = bpy.ops.paint.skin_influence
    flood = bpy.ops.paint.skin_flood

    def layer():
        return skin.current_layer(state)

    def select(key):
        state.index = skin.layer_index(state, key)

    def result(name):
        return skin.read_values(obj.data, state.prefix + "_r" + str(obj.vertex_groups[name].skin_channel))

    edit(action='ADD')
    pick(bone='Arm.R')
    right = state.influence
    left = obj.vertex_groups['Arm.L'].skin_channel
    assert rig.data.bones.active.name == 'Arm.R' and rig.pose.bones['Arm.R'].select
    assert bpy.context.active_object == obj
    state.value, state.strength = 1, 1
    flood()
    assert skin.used_channels(obj, layer()) == {right}
    edit(action='CHILD')
    child, group = layer().key, layer().parent_key
    assert len(skin.children(state, group)) == 2
    pick(bone='Arm.L')
    state.value = 0.5
    flood()
    select(group)
    layer().opacity = 0.5
    np.testing.assert_allclose(result('Arm.L'), 0.75)
    np.testing.assert_allclose(result('Arm.R'), 0.25)
    assert skin.used_channels(obj, layer()) == {right, left}
    state.value = 0.5
    flood()  # The group brush writes its shared mask.
    np.testing.assert_allclose(result('Arm.L'), 0.875)
    np.testing.assert_allclose(result('Arm.R'), 0.125)
    select(child)
    state.value = 0
    flood()
    assert not skin.used_channels(obj, layer())
    np.testing.assert_allclose(result('Arm.R'), 0.25)
    select(group)
    assert skin.used_channels(obj, layer()) == {right}
    edit(action='DUPLICATE')
    duplicate = layer().key
    assert len(skin.children(state, duplicate)) == 2
    edit(action='DELETE')
    assert duplicate not in [v.parent_key for v in state.layers]
    select(child)
    edit(action='CHILD')
    state.value = 0.5
    flood()
    expected = result('Arm.L').copy()
    # Full recomposition must agree with the stroke's cached traversal.
    skin.composite(obj)
    np.testing.assert_allclose(result('Arm.L'), expected)
    select(group)
    edit(action='MERGE')
    assert len(state.layers) == 1
    np.testing.assert_allclose(result('Arm.L'), expected)

    edit(action='ADD')
    state.value = 1
    obj.data.use_paint_mask_vertex = True
    for v in obj.data.vertices:
        v.select = v.index == 0
    pick(bone='Arm.L')
    flood()
    obj.data.use_paint_mask_vertex = False
    assert list(mirror_geometry(obj.data)[1]) == [1, 0, 2]
    assert bpy.ops.paint.skin_mirror(direction='POSITIVE') == {'FINISHED'}
    prefix = skin.layer_key(state, layer()) + "_"
    np.testing.assert_allclose(skin.read_values(obj.data, prefix + str(right)), [0, 1, 0])
    # The header X switch applies to strokes only. Manual mirroring also works with X off.
    obj.use_mesh_mirror_x = True
    assert skin.SkinBrush(obj, stroke=True).mirror_pair is not None
    assert skin.SkinBrush(obj).mirror_pair is None
    obj.use_mesh_mirror_x = False
    assert skin.SkinBrush(obj, stroke=True).mirror_pair is None

    # An existing destination distribution demonstrates the difference between the two scopes.
    # Current Bone keeps unrelated weights when there is room and only reduces overflow.
    lw, rw = prefix + str(left), prefix + str(right)
    skin.write_values(obj.data, lw, [0.4, 0.3, 0.2])
    skin.write_values(obj.data, rw, [0.1, 0.05, 0.2])
    skin.composite(obj)
    assert bpy.ops.paint.skin_mirror(scope='CURRENT', direction='NEGATIVE') == {'FINISHED'}
    np.testing.assert_allclose(skin.read_values(obj.data, rw), [0.1, 0.4, 0.2])
    np.testing.assert_allclose(skin.read_values(obj.data, lw), [0.4, 0.3, 0.2])
    pick(bone='Arm.R')
    skin.write_values(obj.data, rw, [0.7, 0.6, 0.2])
    assert bpy.ops.paint.skin_mirror(scope='CURRENT') == {'FINISHED'}
    np.testing.assert_allclose(skin.read_values(obj.data, lw), [0.6, 0.3, 0.2])
    np.testing.assert_allclose(skin.read_values(obj.data, rw), [0.4, 0.6, 0.2])
    assert bpy.ops.paint.skin_mirror(scope='ALL', direction='NEGATIVE') == {'FINISHED'}
    np.testing.assert_allclose(skin.read_values(obj.data, rw), [0.3, 0.6, 0.2])
    pick(bone='Arm.L')
    state.target = 'MASK'
    state.value = 0.35
    skin.SkinBrush(obj).paint([0], 1, 'REPLACE')
    bpy.ops.paint.skin_mirror(direction='POSITIVE')
    np.testing.assert_allclose(skin.read_values(obj.data, prefix + "mask"), [0.35, 0.35, 1])
    state.target = 'WEIGHT'
    assert bpy.ops.paint.skin_closest(scope='ALL') == {'FINISHED'}
    assert skin.read_values(obj.data, prefix + str(right))[1] == 1
    assert skin.read_values(obj.data, prefix + str(left))[0] == 1
    rig.pose.bones['Arm.L'].rotation_mode = 'XYZ'
    rig.pose.bones['Arm.L'].rotation_euler.z = 0.6
    from bl_ui.space_weight_layers import VIEW3D_PT_weight_paint_rig
    bpy.ops.object.mode_set(mode='WEIGHT_PAINT')
    assert VIEW3D_PT_weight_paint_rig.poll(bpy.context)
    rig.data.pose_position = 'REST'  # An existing V2 file can have rest preview enabled.
    assert bpy.ops.paint.skin_rest_pose() == {'FINISHED'}
    assert rig.data.pose_position == 'POSE'
    np.testing.assert_allclose(rig.pose.bones['Arm.L'].matrix_basis, np.eye(4), atol=1e-6)
    source = (state.index, state.influence, state.target, state.display, obj["_skin_source"])

    # Two meshes can share a rig. The return target must survive renaming and a file reload.
    sibling = obj.copy()
    bpy.context.collection.objects.link(sibling)
    assert bpy.ops.paint.skin_switch_pose() == {'FINISHED'}
    assert bpy.context.mode == 'POSE' and bpy.context.active_object == rig
    assert not VIEW3D_PT_weight_paint_rig.poll(bpy.context)
    assert not bpy.ops.paint.skin_rest_pose.poll()
    rig.pose.bones['Arm.L'].rotation_euler.z = 0.4
    obj.name = "Renamed Skin V2"
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "posing.blend")
        bpy.ops.wm.save_as_mainfile(filepath=path)
        bpy.ops.wm.open_mainfile(filepath=path)
        obj = bpy.data.objects["Renamed Skin V2"]
        rig = skin.rig_modifier(obj).object
        state = obj.skin_layers
        assert bpy.ops.paint.skin_switch_pose() == {'FINISHED'}
        assert bpy.context.active_object == obj and bpy.context.mode == 'PAINT_WEIGHT'
        assert source == (state.index, state.influence, state.target, state.display, obj["_skin_source"])
        np.testing.assert_allclose(rig.pose.bones['Arm.L'].rotation_euler.z, 0.4)
    obj.name = "Skin V2"
    bpy.ops.paint.skin_source(layered=False)
    assert bpy.ops.paint.skin_switch_pose() == {'FINISHED'}
    assert bpy.ops.paint.skin_switch_pose() == {'FINISHED'}
    assert bpy.context.active_object == obj and "_skin_source" not in obj
    bpy.ops.paint.skin_source()
    edit(action='CHILD')
    leaf, parent = layer().key, layer().parent_key
    assert bpy.ops.paint.skin_reparent(parent_key="") == {'FINISHED'}
    assert layer().parent_key == ""
    assert bpy.ops.paint.skin_reparent(parent_key=parent) == {'FINISHED'}
    assert layer().key == leaf
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "nested.blend")
        bpy.ops.wm.save_as_mainfile(filepath=path)
        bpy.ops.wm.open_mainfile(filepath=path)
        state = bpy.data.objects["Skin V2"].skin_layers
        assert skin.current_layer(state).parent_key
        assert len(skin.children(state, skin.current_layer(state).parent_key)) == 2
    print("PASS nested masks, grouped incremental strokes, influence membership, merge, mirror, closest joint, rest pose and reload")


if __name__ == "__main__":
    run()
