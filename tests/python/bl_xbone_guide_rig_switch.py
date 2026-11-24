# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""A guide round trip edits only the guide, including when another rig uses that guide."""

import os
import tempfile

import bpy
import numpy as np
from mathutils import Vector


def snapshot(rig, mesh):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = rig.evaluated_get(depsgraph)
    skin = mesh.evaluated_get(depsgraph)
    values = {}
    for pb in evaluated.pose.bones:
        values[('pose', pb.name)] = np.array(pb.matrix)
        values[('rest', pb.name)] = np.array(pb.bone.matrix_local)
        values[('basis', pb.name)] = np.array(rig.pose.bones[pb.name].matrix_basis)
    values['vertices'] = np.array([skin.matrix_world @ v.co for v in skin.data.vertices])
    values['native'] = np.array([[g.weight for g in v.groups] for v in mesh.data.vertices])
    for attr in mesh.data.attributes:
        if attr.name.startswith(mesh["_skin_source"]):
            values[attr.name] = np.array([v.value for v in attr.data])
    for fc in rig.animation_data.action.layers[0].strips[0].channelbag(rig.animation_data.action_slot).fcurves:
        values[('keys', fc.data_path, fc.array_index)] = np.array([k.co[:] for k in fc.keyframe_points])
    return values


def unchanged(before, after):
    assert before.keys() == after.keys()
    for key, value in before.items():
        np.testing.assert_allclose(value, after[key], atol=1e-6, err_msg=str(key))


def run():
    ctx = bpy.context
    assert bpy.ops.xbone.new_guide() == {'FINISHED'}
    guide = ctx.active_object
    assert not bpy.ops.xbone.return_to_rig.poll()
    joint = guide.data.edit_bones.new("Hand")
    joint.head, joint.tail = (0, 1, 0), (0, 1.4, 0)
    joint.parent = guide.data.edit_bones["Root"]
    assert bpy.ops.xbone.build_rig() == {'FINISHED'}
    rig = ctx.active_object
    bpy.ops.object.mode_set(mode='OBJECT')
    mesh = bpy.data.objects['Cube']
    ctx.view_layer.objects.active = mesh
    rig.select_set(False)
    mesh.select_set(True)
    mesh.modifiers.new("Bind", 'ARMATURE').object = rig
    for name, weight in (("Root", 0.25), ("Hand", 0.75)):
        mesh.vertex_groups.new(name=name).add(list(range(len(mesh.data.vertices))), weight, 'REPLACE')
    assert bpy.ops.paint.skin_source() == {'FINISHED'}
    assert bpy.ops.paint.skin_layer_edit(action='ADD') == {'FINISHED'}
    assert bpy.ops.paint.skin_influence(bone="Hand") == {'FINISHED'}
    mesh.skin_layers.value = 0.6
    assert bpy.ops.paint.skin_flood() == {'FINISHED'}
    mesh.select_set(False)
    ctx.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode='POSE')
    hand = rig.pose.bones["FK.Hand"]
    hand.rotation_mode = 'XYZ'
    for frame, angle in ((1, 0.2), (15, 0.7)):
        hand.rotation_euler.z = angle
        hand.keyframe_insert(data_path="rotation_euler", index=2, frame=frame)
    anim = rig.animation_data
    action = anim.action
    track = anim.nla_tracks.new()
    track.mute = True
    nla_action = action.copy()
    track.strips.new("Backup action", 20, nla_action)
    ctx.scene.frame_set(8)
    rig.pose.bones["FK.Root"].location.x = 0.37
    ctx.view_layer.update()
    guide.hide_select = True
    assert bpy.ops.xbone.edit_source_guide() == {'CANCELLED'}
    assert ctx.active_object == rig and ctx.mode == 'POSE' and guide.hide_get()
    guide.hide_select = False
    before = snapshot(rig, mesh)
    arm_uid = rig.data.session_uid
    # A second rig shares the guide. Use the exact one chosen by the artist.
    other = rig.copy()
    ctx.collection.objects.link(other)
    other.hide_set(True)

    assert bpy.ops.xbone.edit_source_guide() == {'FINISHED'}
    assert ctx.mode == 'EDIT_ARMATURE' and ctx.active_object == guide
    edit = guide.data.edit_bones
    edit["Hand"].translate(Vector((0.5, 0, 0)))
    position = edit["Hand"].head.copy()
    extra = edit.new("Pending")
    extra.head, extra.tail = (1, 1.5, 0), (1, 1.7, 0)
    extra.parent = edit["Hand"]
    guide.name = "Pending Guide"
    assert bpy.ops.xbone.return_to_rig() == {'FINISHED'}
    assert ctx.mode == 'POSE' and ctx.active_object == rig and not rig.hide_get() and guide.hide_get()
    assert mesh.modifiers["Bind"].object == rig and rig.data.session_uid == arm_uid
    assert rig.animation_data.action == action and rig.animation_data.nla_tracks[0].strips[0].action == nla_action
    assert ctx.scene.frame_current == 8 and "Pending" not in rig.data.bones
    unchanged(before, snapshot(rig, mesh))
    assert bpy.ops.xbone.edit_source_guide() == {'FINISHED'}
    assert "Pending" in guide.data.edit_bones
    np.testing.assert_allclose(guide.data.edit_bones["Hand"].head, position)

    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "pending.blend")
        name = rig.name
        bpy.ops.wm.save_as_mainfile(filepath=path)
        bpy.ops.wm.open_mainfile(filepath=path)
        rig = bpy.data.objects[name]
        guide = rig["rigging_guide"]
        mesh = bpy.data.objects["Cube"]
        assert bpy.ops.xbone.return_to_rig() == {'FINISHED'} and ctx.active_object == rig
        assert mesh.modifiers["Bind"].object == rig
        unchanged(before, snapshot(rig, mesh))
        assert bpy.ops.xbone.edit_source_guide() == {'FINISHED'}
        del guide["rigging_target"]
        assert not bpy.ops.xbone.return_to_rig.poll()  # Two possible rigs; never guess.
        guide["rigging_target"] = rig
        assert bpy.ops.xbone.build_rig() == {'FINISHED'} and ctx.active_object == rig
        assert "Pending" in rig.data.bones
        np.testing.assert_allclose(rig.data.bones["Hand"].head_local, position, atol=1e-6)
        assert mesh.modifiers["Bind"].object == rig


if __name__ == "__main__":
    run()
    print("PASS pending guide edits, exact rig, skin layers, animation, pose, file reload and explicit rebuild")
