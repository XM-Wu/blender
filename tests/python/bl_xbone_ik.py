# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""XBone IK must target the joint pivot independently of compatibility tail size."""

import bpy
from mathutils import Vector


def solve(solver, size, include_owner=True, joint_rig=True, straight=False, subchain=False):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if joint_rig:
        bpy.ops.object.xbone_add(enter_editmode=True)
    else:
        bpy.ops.object.armature_add(enter_editmode=True)
    rig = bpy.context.edit_object
    edit = rig.data.edit_bones
    root = edit.active
    root.name = "Root"
    points = [(0, 0, 0), (1, .4, 0), (2, .4, .2)]
    if straight:
        points = [(0, 0, 0), (1, 0, 0), (2, 0, 0)]
    parent = None
    for name, point in zip(("Root", "Mid", "Tip"), points):
        joint = root if name == "Root" else edit.new(name)
        joint.parent = parent
        joint.head = point
        joint.tail = Vector(point) + Vector((0, size, 0))
        parent = joint
    if subchain:
        master = edit.new("Master")
        master.head = (-1, -.2, .4)
        master.tail = (-1, .8, .4)
        root.parent = master
        rig.location = (4, 1, -1)
        rig.scale = (.5, .5, .5)
        rig.rotation_euler = (.1, 0, .3)
    bpy.ops.object.mode_set(mode='OBJECT')
    rig.pose.ik_solver = solver
    target = bpy.data.objects.new("IK target", None)
    bpy.context.collection.objects.link(target)
    target.location = (1.2, 1.0, .3) if include_owner else (.4, 1.0, 0)
    tip = rig.pose.bones["Tip"]
    con = tip.constraints.new('IK')
    if straight:
        target.location = (1.5, 0, 0)
        pole = bpy.data.objects.new("Pole", None)
        bpy.context.collection.objects.link(pole)
        pole.location = (0, 0, 2)
        pole.location = rig.matrix_world @ pole.location
        con.pole_target = pole
    target.location = rig.matrix_world @ target.location
    con.target = target
    con.chain_count = 3
    con.use_tail = include_owner
    con.use_stretch = False
    bpy.context.scene.frame_set(2)
    bpy.context.view_layer.update()
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    pose = evaluated.pose.bones
    effector = pose["Tip"] if include_owner else pose["Mid"]
    endpoint = effector.head if joint_rig else effector.tail
    distance = (evaluated.matrix_world @ endpoint - target.location).length
    print(solver, "XBone" if joint_rig else "Bone", size, "position error", distance)
    assert distance < .015, (solver, size, endpoint[:], target.location[:])
    if straight:
        assert pose["Mid"].head.z > .3, pose["Mid"].head[:]
    if joint_rig:
        assert (effector.tail - effector.head).length > size * .99
        for i, (parent, child) in enumerate((("Root", "Mid"), ("Mid", "Tip"))):
            expected = (Vector(points[i + 1]) - Vector(points[i])).length
            assert abs((pose[child].head - pose[parent].head).length - expected) < .001
    return [pose[n].head.copy() for n in ("Root", "Mid", "Tip")]


for method in ('LEGACY', 'ITASC'):
    small = solve(method, .25)
    large = solve(method, 2.0)
    for a, b in zip(small, large):
        assert (a-b).length < .002, (method, a, b)
    solve(method, .8, include_owner=False)
    solve(method, .8, joint_rig=False)
    solve(method, .8, straight=True, subchain=True)
print("XBone pivot IK and ordinary bone tip IK passed")
