# SPDX-License-Identifier: GPL-2.0-or-later
"""Test Skin Tools brush sampling against an XBone and a posed subdivision cage."""
from types import SimpleNamespace

import bpy
import numpy as np
from bpy_extras.view3d_utils import location_3d_to_region_2d
from mathutils import Vector
from bl_operators import skin_layers as skin


bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.object.xbone_add(enter_editmode=True)
rig = bpy.context.object
edit = rig.data.edit_bones
root = edit.active
root.name = "Body"
for name, x in (("arm_L0_0_jnt", .5), ("arm_R0_0_jnt", -.5), ("Branch.L", .5)):
    bone = edit.new(name)
    bone.head, bone.tail, bone.parent = (x, 0, 0), (x, .2, 0), root
bpy.ops.object.mode_set(mode='OBJECT')
rig.data.bones.active = rig.data.bones["Body"]
bpy.ops.mesh.primitive_grid_add(x_subdivisions=32, y_subdivisions=32, size=2)
obj = bpy.context.object
mesh = obj.data
obj.modifiers.new("Bind", 'ARMATURE').object = rig
assert not obj.vertex_groups
assert bpy.ops.paint.skin_source() == {'FINISHED'}
state = obj.skin_layers
assert state.display == 'LAYER'
assert obj["_skin_preview"] == skin.layer_key(state, skin.current_layer(state)) + "_" + str(state.influence)
assert state.influence == obj.vertex_groups["Body"].skin_channel != 0
assert bpy.ops.paint.skin_flood() == {'FINISHED'}
assert bpy.ops.paint.skin_layer_edit(action='ADD') == {'FINISHED'}
bpy.ops.object.mode_set(mode='WEIGHT_PAINT')
obj.use_mesh_mirror_x = True
rig.pose.bones["Body"].location.x = .25
bpy.context.view_layer.update()
idx = min(range(len(mesh.vertices)), key=lambda i: (mesh.vertices[i].co - Vector((.5, 0, 0))).length)
op = skin.PAINT_OT_skin_layer_stroke
area = next(a for a in bpy.context.screen.areas if a.type == 'VIEW_3D')
region = next(r for r in area.regions if r.type == 'WINDOW')
with bpy.context.temp_override(area=area, region=region):
    c = bpy.context
    # Exercise the operator directly; only replace window-manager modal dispatch.
    ctx = SimpleNamespace(active_object=obj, mode=c.mode, region=c.region, region_data=c.region_data,
                          area=area, evaluated_depsgraph_get=c.evaluated_depsgraph_get,
                          window_manager=SimpleNamespace(modal_handler_add=lambda operator: None))
    dg = c.evaluated_depsgraph_get()
    world = obj.matrix_world @ obj.evaluated_get(dg).data.vertices[idx].co
    xy = location_3d_to_region_2d(region, c.region_data, world)
    state.radius = 10
    event = SimpleNamespace(type='LEFTMOUSE', value='PRESS', mouse_region_x=round(xy.x),
                            mouse_region_y=round(xy.y), shift=False, is_tablet=False)
    def stroke():
        reports = []
        tool = SimpleNamespace(report=lambda kind, msg: reports.append(msg))
        event.type, event.value = 'LEFTMOUSE', 'PRESS'
        result = op.invoke(tool, ctx, event)
        assert result == {'RUNNING_MODAL'}, reports
        event.type = 'INBETWEEN_MOUSEMOVE'
        assert op.modal(tool, ctx, event) == {'RUNNING_MODAL'}
        event.type, event.value = 'LEFTMOUSE', 'RELEASE'
        assert op.modal(tool, ctx, event) == {'FINISHED'}, reports
        key = skin.layer_key(state, skin.current_layer(state)) + "_" + str(state.influence)
        values = skin.read_values(mesh, key)
        assert values[idx] > .5 and np.count_nonzero(values) < len(values)//2
        assert obj["_skin_preview"] == key
        return tool.stroke

    assert bpy.ops.paint.skin_influence(bone="Branch.L") == {'FINISHED'}
    unpaired = stroke()
    assert unpaired.mirror_pair is None
    assert "Branch.R" not in obj.vertex_groups
    assert skin.read_values(mesh, state.prefix + "_r" + str(state.influence))[idx] == 1.0
    movable = rig.pose.bones["Branch.L"]
    movable.location.z = .12
    c.view_layer.update()
    posed = obj.matrix_world @ obj.evaluated_get(dg).data.vertices[idx].co
    assert abs((posed - world).length - .12) < .001
    movable.location.z = 0
    c.view_layer.update()
    assert bpy.ops.paint.skin_layer_edit(action='ADD') == {'FINISHED'}
    assert bpy.ops.paint.skin_influence(bone="arm_L0_0_jnt") == {'FINISHED'}
    brush = stroke()
    opposite = obj.vertex_groups["arm_R0_0_jnt"].skin_channel
    assert int(brush.mirror_active) == opposite
    assert brush.weights[str(opposite)][brush.mirror_pair[idx]] > .5
    assert bpy.ops.paint.skin_layer_edit(action='ADD') == {'FINISHED'}
    sub = obj.modifiers.new("Preview", 'SUBSURF')
    c.view_layer.update()
    assert len(obj.evaluated_get(dg).data.vertices) > len(mesh.vertices)
    brush = stroke()
    assert brush.weights[str(opposite)][brush.mirror_pair[idx]] > .5
    twin = obj.copy()
    c.collection.objects.link(twin)
    original_preview = obj["_skin_preview"]
    skin.unique_storage(twin)
    assert twin.skin_layers.prefix != state.prefix
    assert twin["_skin_preview"] == twin.skin_layers.prefix + original_preview[len(state.prefix):]
    assert obj["_skin_preview"] == original_preview
    bpy.data.objects.remove(twin, do_unlink=True)
    # A real change in the source vertex correspondence must still prevent painting.
    mesh.attributes[state.prefix].data[idx].value = 0
    assert skin.brush_error(obj)
# Three disconnected triangles at different depths under the same cursor.
with bpy.context.temp_override(area=area, region=region):
    from bpy_extras.view3d_utils import region_2d_to_location_3d, region_2d_to_vector_3d
    bpy.ops.object.mode_set(mode='OBJECT')
    rig.data.pose_position = 'REST'
    depth_mesh = bpy.data.meshes.new("Brush area test")
    shells = bpy.data.objects.new("Brush area test", depth_mesh)
    scene = bpy.context.scene
    scene.collection.objects.link(shells)
    shells.scale = (2, .65, 1.3)
    bpy.context.view_layer.update()
    view = bpy.context.region_data
    origin = Vector((0, 0, 0))
    mouse = location_3d_to_region_2d(region, view, origin)
    side = region_2d_to_location_3d(region, view, mouse + Vector((40, 0)), origin) - origin
    up = region_2d_to_location_3d(region, view, mouse + Vector((0, 40)), origin) - origin
    depth = region_2d_to_vector_3d(region, view, mouse) * side.length
    points = [shells.matrix_world.inverted() @ (origin + side*x + up*y + depth*z)
              for z in (0, .35, 2.) for x, y in ((-.2, -.2), (.2, -.2), (0, .2))]
    depth_mesh.from_pydata(points, [], [(0, 1, 2), (3, 4, 5), (6, 7, 8)])
    obj.select_set(False)
    bpy.context.view_layer.objects.active = shells
    shells.select_set(True)
    shells.modifiers.new("Bind", 'ARMATURE').object = rig
    bpy.ops.paint.skin_source()
    bpy.ops.object.mode_set(mode='WEIGHT_PAINT')
    state = shells.skin_layers
    state.radius = 40
    assert state.brush_area == 'SURFACE'
    depth_mesh.use_paint_mask_vertex = True
    for v in depth_mesh.vertices:
        v.select = v.index != 4
    key = skin.layer_key(state, skin.current_layer(state)) + "_" + str(state.influence)
    event = SimpleNamespace(mouse_region_x=round(mouse.x), mouse_region_y=round(mouse.y),
                            shift=False, is_tablet=False)
    for area_mode, expected in (('SURFACE', [0, 1, 2]), ('VOLUME', [0, 1, 2, 3, 5])):
        state.brush_area = area_mode
        bpy.context.view_layer.update()
        brush = skin.SkinBrush(shells, stroke=True)
        assert brush.dab(bpy.context, event)
        weights = skin.read_values(depth_mesh, key)
        assert np.flatnonzero(weights).tolist() == expected, (area_mode, weights)
        if area_mode == 'VOLUME':
            assert 0 < weights[3] < weights[0]
        brush.cancel()
        state.brush = 'HARD'
        state.strength, state.value = .025, .375
        event.is_tablet, event.pressure = True, .05
        for target in ('WEIGHT', 'MASK'):
            state.target = target
            field = key if target == 'WEIGHT' else skin.layer_key(state, skin.current_layer(state)) + "_mask"
            untouched = skin.read_values(depth_mesh, field).copy()
            hard = skin.SkinBrush(shells, stroke=True)
            for target_value in (.375, .375, 0.):
                state.value = target_value
                assert hard.dab(bpy.context, event)
                untouched[expected] = target_value
                np.testing.assert_array_equal(skin.read_values(depth_mesh, field), untouched)
            hard.cancel()
        state.brush, state.target = 'REPLACE', 'WEIGHT'
        state.value, state.strength, event.is_tablet = 1., 1., False
print("XBone brush passed: empty bind, direct and mirrored dabs, unpaired bone, posed subdivision cage")
