# SPDX-License-Identifier: GPL-2.0-or-later
"""Interchange with a Maya/ngSkinTools 2.4 export and all three transfer modes."""
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import bpy
import numpy as np
from bl_operators import skin_layers as skin


def make_mesh(name, points, faces, uvs):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(points, [], faces)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    mesh.uv_layers.new(name="map1").uv.foreach_set("vector", [c for loop in mesh.loops for c in uvs[loop.vertex_index]])
    obj.modifiers.new("Bind", 'ARMATURE').object = rig
    bpy.context.view_layer.objects.active = obj
    bpy.context.view_layer.update()
    return obj


def check(obj, expected, expected_mask):
    state = obj.skin_layers
    group, leaf, disabled = list(state.layers)[-3:]
    assert group.name == "Group" and group.is_group
    assert leaf.name == "Arm weights" and leaf.parent_key == disabled.parent_key == group.key
    assert not disabled.enabled and leaf.opacity == .75
    prefix = skin.layer_key(state, leaf) + "_"
    channel = obj.vertex_groups["Arm"].skin_channel
    np.testing.assert_allclose(skin.read_values(obj.data, prefix + str(channel)), expected, atol=1e-5)
    np.testing.assert_allclose(skin.read_values(obj.data, prefix + "mask"), expected_mask, atol=1e-5)
    return leaf


bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.object.xbone_add(enter_editmode=True)
rig = bpy.context.object
root = rig.data.edit_bones.active
root.name = "Root"
arm = rig.data.edit_bones.new("Arm")
arm.head, arm.tail, arm.parent = (.2, -.4, .3), (.4, -.4, .3), root
bpy.ops.object.mode_set(mode='OBJECT')
source = make_mesh("Source", [(-.5, -.5, 0), (.5, -.5, 0), (-.5, .5, 0), (.5, .5, 0)],
                   [(0, 1, 2), (2, 1, 3)], [(0, 0), (1, 0), (0, 1), (1, 1)])
fixture = str(Path(__file__).with_name("skin_ngst2_quad.json"))
op = bpy.ops.paint.skin_import_layers
assert op(filepath=fixture, mode='vertexId') == {'FINISHED'}
check(source, [0, .3, .7, 1], [.1, .5, .7, .8])
with tempfile.TemporaryDirectory() as temp:
    path = str(Path(temp) / "roundtrip.json")
    assert bpy.ops.paint.skin_export_layers(filepath=path) == {'FINISHED'}
    data = json.loads(Path(path).read_text())
    assert data["mesh"]["vertPositions"] == json.loads(Path(fixture).read_text())["mesh"]["vertPositions"]
    assert any(i["path"] == "|Root|Arm" for i in data["influences"])
    assert op(filepath=path, mode='vertexId') == {'FINISHED'}
    assert len(source.skin_layers.layers) == 10  # existing five + imported five
    check(source, [0, .3, .7, 1], [.1, .5, .7, .8])
    dest = make_mesh("Destination", [(-.25, -.25, .3), (.5, -.5, 0), (-.5, .5, 0)], [(0, 1, 2)],
                     [(.75, .75), (0, 1), (1, 0)])
    assert op(filepath=path, mode='closestPoint') == {'FINISHED'}
    check(dest, [.25, .3, .7], [.35, .5, .7])
    assert op(filepath=path, mode='uvSpace') == {'FINISHED'}
    check(dest, [.75, .7, .3], [.70, .7, .5])
    assert op(filepath=fixture, mode='uvSpace', source_uv_mesh=source.name) == {'FINISHED'}
    check(dest, [.75, .7, .3], [.70, .7, .5])
    n = len(dest.skin_layers.layers)
    for invalid in (dict(filepath=path, mode='vertexId'), dict(filepath=fixture, mode='uvSpace')):
        try:
            result = op(**invalid)
        except RuntimeError:
            result = {'CANCELLED'}
        assert result == {'CANCELLED'} and len(dest.skin_layers.layers) == n
    data["layers"][-1]["parentId"] = 999
    Path(path).write_text(json.dumps(data))
    try:
        result = op(filepath=path, mode='closestPoint')
    except RuntimeError:
        result = {'CANCELLED'}
    assert result == {'CANCELLED'} and len(dest.skin_layers.layers) == n
    # Stacked UV islands: pick the spatially corresponding island even though
    # both source triangles have exactly the same UV coordinates.
    from bl_operators import skin_layers_io as io
    stacked = {"mesh": {"triangles": [0, 1, 2, 3, 4, 5],
                        "vertPositions": [0, 0, 0, 100, 0, 0, 0, 0, -100, 0, 100, 0, 100, 100, 0, 0, 100, -100]},
               "xblender": {"triangleUVs": [0, 0, 1, 0, 0, 1] * 2}}
    upper = make_mesh("Upper island", [(0, 0, 1), (1, 0, 1), (0, 1, 1)], [(0, 1, 2)], [(0, 0), (1, 0), (0, 1)])
    count, mapping = io.vertex_map(stacked, upper, 'uvSpace', 100, None)
    np.testing.assert_allclose(io.transfer([0, 0, 0, 1, 1, 1], count, mapping), 1)
    # A rejected mesh write cannot leave half the layers or initialize an empty target.
    plan = io.plan_import(json.loads(Path(fixture).read_text()), upper, 'closestPoint', 100, None)
    real_write = skin.write_values

    def interrupted(mesh, key, values):
        if key.endswith("_mask") and len(upper.skin_layers.layers) == 3:
            raise RuntimeError("test interrupted write")
        real_write(mesh, key, values)
    attr_names = set(upper.data.attributes.keys())
    with patch.object(skin, 'write_values', interrupted):
        try:
            io.append_layers(bpy.context, upper, plan)
        except RuntimeError as error:
            assert str(error) == "test interrupted write"
        else:
            assert False, "Expected an interrupted import"
    assert not upper.skin_layers.prefix and not upper.skin_layers.layers and not upper.vertex_groups
    assert attr_names == set(upper.data.attributes.keys())
print("ngSkinTools layer IO: Maya fixture, hierarchy, masks, ID, 3D/UV barycentric matching and failed import passed")
