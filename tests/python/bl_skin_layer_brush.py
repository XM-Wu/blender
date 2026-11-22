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
    weight = unpaired.weights[unpaired.active][idx]
    assert abs(skin.read_values(mesh, state.prefix + "_r" + str(state.influence))[idx] - weight) < 1e-6
    movable = rig.pose.bones["Branch.L"]
    movable.location.z = .12
    c.view_layer.update()
    posed = obj.matrix_world @ obj.evaluated_get(dg).data.vertices[idx].co
    assert abs((posed - world).length - .12*weight) < .001
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
print("XBone brush passed: empty bind, direct and mirrored dabs, unpaired bone, posed subdivision cage")
