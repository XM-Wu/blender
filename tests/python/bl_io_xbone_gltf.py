# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Import a binary glTF with a branched skin, inverse binds and an animated joint attachment."""

import json
import math
from pathlib import Path
import struct
import tempfile

import bpy
import numpy as np
from mathutils import Matrix, Vector

POSITIONS = [(0, 0, 0), (1, 2, 0), (0, 3, 1)]
BASIS = Matrix.Rotation(math.pi / 2, 4, 'X')
TIP_BIND = Matrix.Translation((0, 2, 0)) @ Matrix.Rotation(math.pi / 2, 4, 'Z')


def write_fixture(path, *, skinned):
    buffer = bytearray()
    views, accessors = [], []

    def accessor(rows, shape, ushort=False, bounds=False):
        buffer.extend(bytes(-len(buffer) % 4))
        array = np.array(rows, dtype='<u2' if ushort else '<f4')
        views.append(dict(buffer=0, byteOffset=len(buffer), byteLength=array.nbytes))
        buffer.extend(array.tobytes())
        item = dict(bufferView=len(views) - 1, componentType=5123 if ushort else 5126,
                    type=shape, count=len(rows))
        if bounds:
            components = array.reshape(len(rows), -1)
            item.update(min=components.min(axis=0).tolist(), max=components.max(axis=0).tolist())
        accessors.append(item)
        return len(accessors) - 1

    attributes = {"POSITION": accessor(POSITIONS, 'VEC3', bounds=True)}
    nodes = [dict(name="Mesh", mesh=0)]
    doc = dict(asset={"version": "2.0"}, nodes=nodes, scenes=[{"nodes": [0]}], scene=0,
               meshes=[dict(primitives=[dict(attributes=attributes)])], accessors=accessors, bufferViews=views)
    if skinned:
        nodes[0]['skin'] = 0
        nodes.extend([
            dict(name="Rig", children=[2]),
            dict(name="Root", children=[3, 4], translation=[1, 0, 0]),
            dict(name="Tip", children=[5, 6], translation=[0, 2, 0], rotation=[0, 0, 2**-0.5, 2**-0.5]),
            dict(name="Branch", translation=[0, 0, 1]),
            dict(name="Socket", translation=[1, 0, 0]),
            dict(name="FixedSocket", translation=[0, 0, 0.25]),
        ])
        doc["scenes"][0]["nodes"].append(1)
        attributes["JOINTS_0"] = accessor([(0, 0, 0, 0), (1, 0, 0, 0), (0, 1, 0, 0)], 'VEC4', ushort=True)
        attributes["WEIGHTS_0"] = accessor([(1, 0, 0, 0), (1, 0, 0, 0), (.5, .5, 0, 0)], 'VEC4')
        binds = [Matrix.Identity(4), TIP_BIND, Matrix.Translation((0, 0, 1))]
        ibms = accessor([np.array(m.inverted()).T.flatten() for m in binds], 'MAT4')
        doc["skins"] = [dict(skeleton=2, joints=[2, 3, 4], inverseBindMatrices=ibms)]
        times = accessor([0, 1], 'SCALAR', bounds=True)
        tracks = [
            (2, "translation", accessor([[1, 0, 0], [2, 0, 0]], 'VEC3')),
            (3, "rotation", accessor([[0, 0, 2**-0.5, 2**-0.5], [0, 0, 0, 1]], 'VEC4')),
            (3, "scale", accessor([[1, 1, 1], [2, 1, 0.5]], 'VEC3')),
            (5, "translation", accessor([[1, 0, 0], [2, 0, 0]], 'VEC3')),
        ]
        doc["animations"] = [dict(name="Motion",
                                  channels=[dict(sampler=i, target=dict(node=n, path=p))
                                            for i, (n, p, _) in enumerate(tracks)],
                                  samplers=[dict(input=times, output=a) for _, _, a in tracks])]

    doc["buffers"] = [dict(byteLength=len(buffer))]
    if path.suffix == ".gltf":
        sidecar = path.with_suffix(".bin")
        doc["buffers"][0]["uri"] = sidecar.name
        path.write_text(json.dumps(doc), encoding="utf8")
        sidecar.write_bytes(buffer)
        return
    header = json.dumps(doc).encode()
    header += b' ' * (-len(header) % 4)
    buffer.extend(bytes(-len(buffer) % 4))
    chunks = struct.pack('<I4s', len(header), b'JSON') + header + struct.pack('<I4s', len(buffer), b'BIN\0') + buffer
    path.write_bytes(struct.pack('<4sII', b'glTF', 2, 12 + len(chunks)) + chunks)


def near(actual, expected):
    np.testing.assert_allclose(actual, expected, atol=3e-5)


def check_pose():
    mesh = bpy.data.objects["Mesh"]
    rig = mesh.modifiers[0].object
    assert set(mesh.vertex_groups.keys()) >= {"Root", "Tip", "Branch"}
    assert rig.data.bones["Branch"].parent.name == rig.data.bones["Tip"].parent.name == "Root"
    for t in (0, 1):
        bpy.context.scene.frame_set(t * bpy.context.scene.render.fps)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = mesh.evaluated_get(depsgraph)
        root = Matrix.Translation((1 + t, 0, 0))
        tip = root @ (TIP_BIND if not t else Matrix.Translation((0, 2, 0)) @ Matrix.Diagonal((2, 1, 0.5, 1)))
        for vertex, pos, w in zip(evaluated.data.vertices, POSITIONS, [0, 1, 0.5]):
            p = Vector(pos)
            expected = (1 - w) * (root @ p) + w * (tip @ TIP_BIND.inverted() @ p)
            near(evaluated.matrix_world @ vertex.co, BASIS @ expected)
        socket = bpy.data.objects["Socket"].evaluated_get(depsgraph)
        near(socket.matrix_world.translation, BASIS @ tip @ Vector((1 + t, 0, 0)))
        fixed = bpy.data.objects["FixedSocket"].evaluated_get(depsgraph)
        near(fixed.matrix_world.translation, BASIS @ tip @ Vector((0, 0, 0.25)))


def run():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.glb"
        write_fixture(path, skinned=True)
        for xbone in (True, False):
            bpy.ops.wm.read_factory_settings(use_empty=True)
            opts = {"use_xbone": True} if xbone else {}
            assert bpy.ops.import_scene.gltf(filepath=str(path), **opts) == {'FINISHED'}
            arm = bpy.data.objects["Rig"].data
            assert arm.is_xbone == xbone
            assert all((b.xbone_joint is not None) == xbone for b in arm.bones)
            if xbone:
                near(arm.bones["Root"].matrix_local, Matrix.Identity(4))
                near(arm.bones["Tip"].matrix_local, BASIS @ TIP_BIND @ BASIS.inverted())
                assert all(pb.custom_shape is None for pb in bpy.data.objects["Rig"].pose.bones)
                assert bpy.data.objects["Socket"].parent_bone_head_tail_factor == 0
            check_pose()
            if xbone:
                blend = str(Path(tmp) / "rig.blend")
                bpy.ops.wm.save_as_mainfile(filepath=blend)
                bpy.ops.wm.open_mainfile(filepath=blend)
                assert bpy.data.objects["Rig"].data.is_xbone
                check_pose()

        bpy.ops.wm.read_factory_settings(use_empty=True)
        external = Path(tmp) / "external.gltf"
        write_fixture(external, skinned=True)
        assert bpy.ops.import_scene.gltf(directory=tmp, files=[{"name": path.name}, {"name": external.name}],
                                         use_xbone=True, guess_original_bind_pose=False,
                                         bone_heuristic='FORTUNE') == {'FINISHED'}
        assert len(bpy.data.armatures) == 2
        for arm in bpy.data.armatures:
            assert arm.is_xbone
            near(arm.bones["Root"].head_local, (1, 0, 0))

        bpy.ops.wm.read_factory_settings(use_empty=True)
        write_fixture(path, skinned=False)
        assert bpy.ops.import_scene.gltf(filepath=str(path), use_xbone=True) == {'FINISHED'}
        assert bpy.data.objects["Mesh"].type == 'MESH' and not bpy.data.armatures
    print("PASS GLB: XBone bind frames, branching, animation, skin, attachment, reload, native and mesh-only imports")


if __name__ == "__main__":
    run()
