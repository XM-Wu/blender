# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

import math
import sys
import tempfile
from pathlib import Path

import bpy
from mathutils import Vector


def near(a, b):
    assert (Vector(a) - Vector(b)).length < 0.0001, (a, b)


def check_authored_frames(directory):
    # An explicit FBX Root, an X-aimed child, an off-axis branch and a twisted leaf.
    nodes = [
        ("Root", "Root", 0, (3, 4, 5), (0, 0, 35)),
        ("First", "LimbNode", 1, (5, 0, 0), (23, 0, 0)),
        ("Branch", "LimbNode", 1, (0, 4, 0), (0, 0, 90)),
    ]
    objects = []
    connections = []
    for i, (name, kind, parent, loc, pre) in enumerate(nodes, start=1):
        objects.append(f'''Model: {i}, "Model::{name}", "LimbNode" {{
            Properties70: {{
                P: "Lcl Translation","Lcl Translation","","A",{loc[0]},{loc[1]},{loc[2]}
                P: "RotationActive","bool","","",1
                P: "PreRotation","Vector3D","Vector","",{pre[0]},{pre[1]},{pre[2]}
            }}
        }}
        NodeAttribute: {10+i}, "NodeAttribute::{name}", "{kind}" {{
            TypeFlags: "Skeleton"
        }}''')
        connections.extend([f'C: "OO",{i},{parent}', f'C: "OO",{10+i},{i}'])
    objects.extend(['Model: 21, "Model::OtherRig", "Null" {}',
                    'Model: 22, "Model::Lone", "LimbNode" {}',
                    'NodeAttribute: 23, "NodeAttribute::Lone", "Root" { TypeFlags: "Skeleton" }'])
    connections.extend(['C: "OO",21,0', 'C: "OO",22,21', 'C: "OO",23,22'])
    path = directory / "joint-frames.fbx"
    path.write_text("; FBX 7.4.0 project file\nFBXHeaderExtension: { FBXVersion: 7400 }\nObjects: {\n" +
                    "\n".join(objects) + "\n}\nConnections: {\n" + "\n".join(connections) + "\n}\n",
                    encoding="ascii")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.wm.xbone_fbx_import(filepath=str(path))
    rigs = [o for o in bpy.data.objects if o.type == 'ARMATURE']
    assert len(rigs) == 2
    lone, = [o for o in rigs if "Lone" in o.data.bones]
    assert len(lone.data.bones) == 1 and lone.data.bones["Lone"].xbone_joint
    rig, = [o for o in rigs if "Root" in o.data.bones]
    bones = rig.data.bones
    assert set(bones.keys()) == {"Root", "First", "Branch"}
    assert bones["First"].parent == bones["Branch"].parent == bones["Root"]
    for name, axis in (("First", (1, 0, 0)), ("Branch", (0, 1, 0))):
        child = bones[name]
        near(child.xbone_joint.position.normalized(), axis)
    leaf_up = bones["First"].xbone_joint.orientation @ Vector((0, 1, 0))
    near(leaf_up, (0, math.cos(math.radians(23)), math.sin(math.radians(23))))


def snapshot(rig, mesh):
    result = []
    for frame in (1, 8, 16, 24):
        bpy.context.scene.frame_set(frame)
        graph = bpy.context.evaluated_depsgraph_get()
        pose = rig.evaluated_get(graph).pose
        vertices = mesh.evaluated_get(graph).data.vertices
        result.append(([row.copy() for b in pose.bones for row in b.matrix],
                       [v.co.copy() for v in vertices]))
    return result


def compare(actual, expected):
    for a, b in zip(actual, expected, strict=True):
        for av, bv in zip(a, b, strict=True):
            for x, y in zip(av, bv, strict=True):
                near(x, y)


def check_skin(path, directory):
    def imported(xbone):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        op = bpy.ops.wm.xbone_fbx_import if xbone else bpy.ops.wm.fbx_import
        assert op(filepath=str(path)) == {'FINISHED'}
        rig, = [o for o in bpy.data.objects if o.type == 'ARMATURE']
        mesh, = [o for o in bpy.data.objects if o.type == 'MESH']
        assert rig.data.is_xbone == xbone
        assert rig.animation_data.action and mesh.vertex_groups
        assert any(m.type == 'ARMATURE' and m.object == rig for m in mesh.modifiers)
        return rig, mesh

    rig, mesh = imported(False)
    reference = snapshot(rig, mesh)
    assert any((a-b).length > 0.0001 for frame in reference[1:]
               for a, b in zip(frame[1], reference[0][1]))
    rig, mesh = imported(True)
    for bone in rig.data.bones:
        assert bone.xbone_joint
        if bone.children:
            # This Maya fixture was authored with local +X as the primary axis.
            near(bone.matrix_local.to_3x3().col[0],
                 (bone.children[0].head_local - bone.head_local).normalized())
    compare(snapshot(rig, mesh), reference)
    names = rig.name, mesh.name
    blend = str(directory / "import.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    bpy.ops.wm.open_mainfile(filepath=blend)
    rig, mesh = (bpy.data.objects[n] for n in names)
    assert rig.data.is_xbone
    compare(snapshot(rig, mesh), reference)


def check_export_leaf_policy(directory):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.object.armature_add()
    bpy.context.object.data.bones[0].name = "Legacy"
    bpy.ops.object.xbone_add(enter_editmode=True)
    rig = bpy.context.edit_object
    rig.data.edit_bones.active.name = "Root"
    bpy.context.scene.cursor.location = (2, 1, 3)
    bpy.ops.armature.xbone_joint_add()
    leaf = rig.data.edit_bones.active
    leaf.name = "Authored_end"
    leaf.joint_orientation = (0.5, 0.5, 0.5, 0.5)
    bpy.ops.object.mode_set(mode='OBJECT')
    rest = rig.data.bones["Authored_end"].matrix_local.copy()
    path = str(directory / "mixed.fbx")
    assert bpy.ops.export_scene.fbx(filepath=path, add_leaf_bones=True) == {'FINISHED'}
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.wm.xbone_fbx_import(filepath=path)
    rigs = [obj.data for obj in bpy.data.objects if obj.type == 'ARMATURE']
    legacy, = [arm for arm in rigs if "Legacy" in arm.bones]
    xbone, = [arm for arm in rigs if "Authored_end" in arm.bones]
    assert set(legacy.bones.keys()) == {"Legacy", "Legacy_end"}
    assert set(xbone.bones.keys()) == {"Root", "Authored_end"}
    for got, want in zip(xbone.bones["Authored_end"].matrix_local, rest):
        near(got, want)


with tempfile.TemporaryDirectory() as tmp:
    directory = Path(tmp)
    check_export_leaf_policy(directory)
    check_authored_frames(directory)
    check_skin(Path(sys.argv[-1]) / "ufbx_maya_game_sausage_75b.fbx", directory)
print("XBone FBX joint axes, hierarchy, animation, skin and reload passed")
