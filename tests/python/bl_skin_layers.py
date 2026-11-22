# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

import os
import tempfile
import bpy
import numpy as np
from bl_operators.skin_layers import read_values, topology_error


def near(actual, expected):
    np.testing.assert_allclose(actual, expected, atol=1e-5)


def check_personal_startup():
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "startup.blend")
        bpy.data.workspaces["Weight Paint"].name = "My Painting Layout"
        names = set(bpy.data.workspaces.keys())
        active = bpy.context.window.workspace.name
        objects = set(bpy.data.objects.keys())
        bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
        bpy.ops.wm.read_homefile(filepath=path, app_template="")
        assert set(bpy.data.workspaces.keys()) == names | {"Weight Paint"}
        assert bpy.context.window.workspace.name == active
        assert set(bpy.data.objects.keys()) == objects

        # A saved, customized Weight Paint workspace takes precedence over the shipped one.
        bpy.data.workspaces["Weight Paint"]["personal_setting"] = 42
        bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
        bpy.ops.wm.read_homefile(filepath=path, app_template="")
        assert set(bpy.data.workspaces.keys()) == names | {"Weight Paint"}
        assert bpy.data.workspaces["Weight Paint"]["personal_setting"] == 42


def check_render_pose(obj, expected):
    coordinates = []

    class SkinTestRender(bpy.types.RenderEngine):
        bl_idname = "SKIN_TEST_RENDER"
        bl_label = "Skin Test Render"

        def render(self, depsgraph):
            assert depsgraph.mode == 'RENDER'
            coordinates.append(obj.evaluated_get(depsgraph).data.vertices[0].co.x)
            self.end_result(self.begin_result(0, 0, 1, 1))

    scene = bpy.context.scene
    previous = scene.render.engine
    bpy.utils.register_class(SkinTestRender)
    try:
        scene.render.engine = SkinTestRender.bl_idname
        bpy.ops.render.render()
        near(coordinates, [expected])
    finally:
        scene.render.engine = previous
        bpy.utils.unregister_class(SkinTestRender)


def run():
    workspace = bpy.data.workspaces["Weight Paint"]
    assert workspace.object_mode == 'WEIGHT_PAINT'
    areas = {a.type: a for a in workspace.screens[0].areas}
    assert set(areas) == {'VIEW_3D', 'OUTLINER', 'WEIGHT_LAYERS'}
    assert areas['VIEW_3D'].x < areas['WEIGHT_LAYERS'].x
    assert areas['OUTLINER'].y > areas['WEIGHT_LAYERS'].y
    assert isinstance(areas['WEIGHT_LAYERS'].spaces.active, bpy.types.SpaceWeightLayers)
    assert next(r for r in areas['WEIGHT_LAYERS'].regions if r.type == 'HEADER').alignment == 'BOTTOM'

    arm = bpy.data.armatures.new("Rig")
    rig = bpy.data.objects.new("Rig", arm)
    scene = bpy.context.scene
    scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    for name, x in [('Clavicle', 0), ('Arm', 1)]:
        bone = arm.edit_bones.new(name)
        bone.head = (x, 0, 0)
        bone.tail = (x, 1, 0)
    bpy.ops.object.mode_set(mode='OBJECT')
    rig.pose.bones['Arm'].location.x = 2

    mesh = bpy.data.meshes.new("Shoulder")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    obj = bpy.data.objects.new("Shoulder", mesh)
    scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    rig.select_set(False)
    obj.select_set(True)
    mod = obj.modifiers.new("Armature", 'ARMATURE')
    mod.object = rig
    clavicle = obj.vertex_groups.new(name="Clavicle")
    clavicle.add([0, 1, 2], 1, 'REPLACE')
    arm_group = obj.vertex_groups.new(name="Arm")
    cloth = obj.vertex_groups.new(name="Cloth Pin")
    cloth.add([0], 0.7, 'REPLACE')

    def posed_x():
        bpy.context.view_layer.update()
        return obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).data.vertices[0].co.x

    near(posed_x(), 0)
    assert bpy.ops.paint.skin_source() == {'FINISHED'}
    assert topology_error(obj) is None
    near(posed_x(), 0)
    state = obj.skin_layers
    assert bpy.ops.paint.skin_layer_edit(action='ADD') == {'FINISHED'}
    assert bpy.ops.paint.skin_influence(bone="Arm") == {'FINISHED'}
    state.value, state.strength = 0.6, 1.0
    assert bpy.ops.paint.skin_flood() == {'FINISHED'}
    near(posed_x(), 1.2)
    state.target = 'MASK'
    state.value = 0.5
    bpy.ops.paint.skin_flood()
    state.layers[1].opacity = 0.5
    near(posed_x(), 0.3)
    near(read_values(mesh, state.prefix + "_r" + str(clavicle.skin_channel)), [0.85] * 3)
    near(clavicle.weight(0), 1)
    state.layers[1].enabled = False
    near(posed_x(), 0)
    state.layers[1].enabled = True

    bpy.ops.paint.skin_source(layered=False)
    near(posed_x(), 0)
    arm_group.add([0], 1, 'REPLACE')
    clavicle.remove([0])
    near(posed_x(), 2)
    bpy.ops.paint.skin_source()
    near(posed_x(), 0.3)

    bpy.ops.paint.skin_layer_edit(action='DUPLICATE')
    assert len(state.layers) == 3
    bpy.ops.paint.skin_layer_edit(action='DOWN')
    bpy.ops.paint.skin_layer_edit(action='DELETE')
    near(posed_x(), 0.3)
    # Linked duplicates start with the same attributes and separate on the first write.
    twin = obj.copy()
    scene.collection.objects.link(twin)
    bpy.context.view_layer.objects.active = twin
    twin.skin_layers.index = 0
    for action in ('DOWN', 'MERGE'):
        assert bpy.ops.paint.skin_layer_edit(action=action) == {'CANCELLED'}
        assert twin.skin_layers.prefix == state.prefix
    twin.skin_layers.layers[1].opacity = 0
    assert twin.skin_layers.prefix != state.prefix
    assert twin["_skin_preview"].startswith(twin.skin_layers.prefix + "_")
    near(posed_x(), 0.3)
    bpy.data.objects.remove(twin, do_unlink=True)
    bpy.context.view_layer.objects.active = obj
    arm.bones['Arm'].name = 'Upper Arm'
    near(posed_x(), 0.3)
    check_render_pose(obj, 0.3)
    mod.show_in_editmode = True
    bpy.ops.object.mode_set(mode='EDIT')
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    edit_result = evaluated.to_mesh()
    try:
        near(edit_result.vertices[0].co.x, 0.3)
    finally:
        evaluated.to_mesh_clear()
    bpy.ops.object.mode_set(mode='OBJECT')

    bpy.ops.paint.skin_layer_edit(action='ADD')
    state.target = 'WEIGHT'
    key = state.prefix + "_" + state.layers[state.index].key + "_" + str(state.influence)
    mesh.use_paint_mask_vertex = True
    for v in mesh.vertices:
        v.select = v.index == 0
    state.brush, state.value = 'ADD', 0.8
    bpy.ops.paint.skin_flood()
    near(read_values(mesh, key), [0.8, 0, 0])
    state.brush, state.value = 'SCALE', 0.5
    bpy.ops.paint.skin_flood()
    near(read_values(mesh, key), [0.4, 0, 0])
    mesh.use_paint_mask_vertex = False
    state.brush = 'SMOOTH'
    bpy.ops.paint.skin_flood()
    near(read_values(mesh, key), [0, 0.2, 0.2])
    bpy.ops.paint.skin_layer_edit(action='DELETE')
    near(posed_x(), 0.3)

    edited = obj.copy()
    edited.data = mesh.copy()
    scene.collection.objects.link(edited)
    edited.data.vertices.add(1)
    edited.data.update()
    bpy.context.view_layer.update()
    assert topology_error(edited)
    near(edited.evaluated_get(bpy.context.evaluated_depsgraph_get()).data.vertices[0].co.x, 2)
    bpy.data.objects.remove(edited, do_unlink=True)
    assert bpy.ops.paint.skin_bake() == {'FINISHED'}
    near(obj.vertex_groups['Upper Arm'].weight(0), 0.15)
    near(obj.vertex_groups['Cloth Pin'].weight(0), 0.7)

    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "layered.blend")
        bpy.ops.wm.save_as_mainfile(filepath=path)
        bpy.ops.wm.open_mainfile(filepath=path)
        obj = bpy.data.objects["Shoulder"]
        assert len(obj.skin_layers.layers) == 2
        near(posed_x(), 0.3)
    print("PASS layered skin weights, masks, pose coordinates, independent sources, linked duplicates, rename, bake, reload")


if __name__ == "__main__":
    check_personal_startup()
    run()
