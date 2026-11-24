# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Check type-scoped cleanup and the references that must keep scene empties alive."""

import bpy

clear = bpy.ops.object.clear_unused


def test_data():
    cube = bpy.context.active_object
    bpy.data.meshes.new("UnusedMesh")
    bpy.data.meshes.new("ReservedMesh").use_fake_user = True
    mat = bpy.data.materials.new("UnusedMaterial")
    bpy.data.materials.new("AssetMaterial").asset_mark()
    used_mat = bpy.data.materials.new("UsedMaterial")
    cube.data.materials.append(used_mat)
    image = bpy.data.images.new("UsedImage", 8, 8)
    used_mat.node_tree.nodes.new("ShaderNodeTexImage").image = image
    dirty = bpy.data.images.new("PaintInProgress", 8, 8)
    dirty.pixels[0] = 0.25
    assert dirty.is_dirty
    standalone = bpy.data.images.new("UnusedImage", 8, 8)
    released = bpy.data.images.new("TextureImage", 8, 8)
    tex = bpy.data.textures.new("UnusedTexture", 'IMAGE')
    tex.image = released
    protected = bpy.data.textures.new("ReservedTexture", 'IMAGE')
    protected.use_fake_user = True
    protected.image = bpy.data.images.new("ReservedTextureImage", 8, 8)

    assert clear(type='MESH') == {'FINISHED'}
    assert "UnusedMesh" not in bpy.data.meshes
    assert bpy.data.meshes.get("ReservedMesh") and cube.data
    assert mat.name == "UnusedMaterial" and standalone.name == "UnusedImage"
    assert clear(type='IMAGE_TEXTURE') == {'FINISHED'}
    assert "UnusedTexture" not in bpy.data.textures
    assert not {"UnusedImage", "TextureImage"}.intersection(bpy.data.images.keys())
    assert bpy.data.images.get("PaintInProgress")
    assert bpy.data.images.get("ReservedTextureImage") and bpy.data.images.get("UsedImage")
    assert mat.name == "UnusedMaterial"
    assert clear(type='MATERIAL') == {'FINISHED'}
    assert "UnusedMaterial" not in bpy.data.materials
    assert bpy.data.materials.get("AssetMaterial") and cube.data.materials[-1] == used_mat
    assert clear(type='MATERIAL') == {'CANCELLED'}


def test_empties():
    scene = bpy.context.scene
    regular = bpy.context.collection
    cube = bpy.data.objects['Cube']

    def empty(name, col=regular, parent=None):
        ob = bpy.data.objects.new(name, None)
        col.objects.link(ob)
        ob.parent = parent
        return ob

    root = empty("DiscardRoot")
    mid = empty("DiscardMid", parent=root)
    empty("DiscardLeaf", parent=mid)
    empty("DiscardMaster", scene.collection)
    mixed = empty("KeepAncestor")
    cube.parent = mixed
    empty("DiscardSibling", parent=mixed)
    other = bpy.data.scenes.new("Other")
    other.collection.objects.link(empty("SharedObject"))
    empty("OtherOnly", other.collection)
    cross_scene_parent = empty("ParentOfForeignChild")
    empty("ForeignChild", other.collection, cross_scene_parent)
    shared_coll = bpy.data.collections.new("SharedCollection")
    scene.collection.children.link(shared_coll)
    other.collection.children.link(shared_coll)
    empty("SharedByCollection", shared_coll)

    cube.constraints.new('COPY_LOCATION').target = empty("ConstraintTarget")
    cube.modifiers.new("Mirror", 'MIRROR').mirror_object = empty("ModifierTarget")
    driver = cube.driver_add("hide_render").driver
    driver.variables.new().targets[0].id = empty("DriverTarget")
    cube["rigging_guide"] = empty("GuideReference")
    tree = bpy.data.node_groups.new("Nodes", 'GeometryNodeTree')
    for direction in ('INPUT', 'OUTPUT'):
        tree.interface.new_socket(name="Geometry", in_out=direction, socket_type='NodeSocketGeometry')
    tree.links.new(tree.nodes.new('NodeGroupInput').outputs["Geometry"],
                   tree.nodes.new('NodeGroupOutput').inputs["Geometry"])
    cube.modifiers.new("Nodes", 'NODES').node_group = tree
    tree.nodes.new('GeometryNodeObjectInfo').inputs['Object'].default_value = empty("GeometryNodesTarget")
    regular["ref"] = empty("CollectionProperty", regular)
    scene["ref"] = empty("SceneProperty", scene.collection)
    scene["driven_value"] = 0.0
    scene_driver = scene.driver_add('["driven_value"]').driver
    scene_driver.variables.new().targets[0].id = empty("SceneDriver", scene.collection)
    empty("GuideMetadata")["guide_component"] = "arm"
    empty("Picture").empty_display_type = 'IMAGE'
    empty("Animated").keyframe_insert(data_path="location", frame=1)
    instance = empty("Instancer")
    instance.instance_type = 'COLLECTION'
    # This collection is referenced solely as instance content, and lives in this scene.
    instanced = bpy.data.collections.new("InstanceContents")
    scene.collection.children.link(instanced)
    instance.instance_collection = instanced
    child = bpy.data.collections.new("InstanceSubCollection")
    instanced.children.link(child)
    empty("InstanceMember", child)
    bpy.ops.object.effector_add(type='WIND')
    bpy.context.object.name = "ForceField"

    bpy.context.view_layer.update()
    before = set(bpy.data.objects.keys())
    assert clear(type='EMPTY') == {'FINISHED'}
    removed = before - set(bpy.data.objects.keys())
    assert removed == {"DiscardRoot", "DiscardMid", "DiscardLeaf", "DiscardMaster", "DiscardSibling"}, removed
    assert cube.constraints[0].target.name == "ConstraintTarget" and cube.parent == mixed
    assert clear(type='EMPTY') == {'CANCELLED'}


if __name__ == "__main__":
    test_data()
    test_empties()
    print("PASS scoped mesh/material/image/texture cleanup, nested empties and reference preservation")
