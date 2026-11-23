# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise XBone operators with the Rigging editor's own window-region context."""
import tempfile
from pathlib import Path

import bpy
from mathutils import Vector
import xbone_rigging as ui


def window_region(area):
    return next(r for r in area.regions if r.type == 'WINDOW')


def check_workspace(workspace):
    screen, = workspace.screens
    assert len(screen.areas) == 5
    front, right = sorted((a for a in screen.areas if a.type == 'VIEW_3D'), key=lambda a: -a.y)
    areas = {a.type: a for a in screen.areas}
    rig, tree, props = (areas[k] for k in ('RIGGING', 'OUTLINER', 'PROPERTIES'))
    assert front.x == right.x < rig.x < tree.x == props.x
    assert front.width == right.width and tree.width == props.width
    assert right.y == rig.y == props.y < front.y
    assert abs(front.y + front.height - rig.y - rig.height) <= 2
    width = front.width + rig.width + tree.width
    for size, ratio in zip((front.width, rig.width, tree.width), (.634, .188, .178)):
        assert abs(size / width - ratio) < .015
    assert abs(front.height / (front.height + right.height) - .5) < .015
    assert abs(tree.height / (tree.height + props.height) - .37) < .015
    for view, axis in ((front, (0, -1, 0)), (right, (1, 0, 0))):
        space = view.spaces.active
        region = space.region_3d
        assert region.view_perspective == 'ORTHO' and space.show_region_ui
        assert (region.view_rotation @ Vector((0, 0, 1)) - Vector(axis)).length < .0001
    assert tree.spaces.active.display_mode == 'VIEW_LAYER'


bpy.ops.wm.read_factory_settings(use_empty=True)
workspace = bpy.data.workspaces["Rigging"]
check_workspace(workspace)
assert ui.ensure_rigging_workspace() == workspace
# Opening the preset in an existing file reuses a previously renamed tab.
workspace.name = "Character Setup"
assert ui.ensure_rigging_workspace() == workspace
workspace.name = "Rigging"
viewport = next(a for a in bpy.context.screen.areas if a.type == 'VIEW_3D')
area = next(a for a in bpy.context.screen.areas if a.type == 'PROPERTIES')
area.ui_type = 'RIGGING'
assert isinstance(area.spaces.active, bpy.types.SpaceRigging)
assert not hasattr(bpy.types, "XBONE_PT_rigging")
assert bpy.types.RIGGING_PT_guide.bl_region_type == 'WINDOW'
with bpy.context.temp_override(area=area, region=window_region(area)):
    assert ui.RIGGING_PT_templates.poll(bpy.context)
    assert bpy.ops.xbone.biped_guide(template='xbiped') == {'FINISHED'}
    guide = bpy.context.active_object
    assert ui.RIGGING_PT_guide.poll(bpy.context)
    bpy.ops.object.mode_set(mode='EDIT')
    guide.data.edit_bones.active = guide.data.edit_bones["leg_L0"]
    assert bpy.context.active_bone.name == "leg_L0"
    assert bpy.ops.xbone.symmetrize_guide(side='L') == {'FINISHED'}
    assert bpy.ops.xbone.build_rig() == {'FINISHED'}
    rig = bpy.context.active_object
    assert ui.RIGGING_PT_pose.poll(bpy.context)
    assert not ui.RIGGING_PT_guide.poll(bpy.context)
    assert bpy.ops.xbone.match_limb(control="IK.leg_L0_ankle", mode='FK') == {'FINISHED'}

# The 3D View and Rigging must resolve the same active control.
with bpy.context.temp_override(area=viewport, region=window_region(viewport)):
    bend = rig.pose.bones["FK.leg_L0_knee_bend"]
    rig.data.bones.active = bend.bone
    bend.select = True
with bpy.context.temp_override(area=area, region=window_region(area)):
    assert bpy.context.active_pose_bone == bend
    rig.data.collections["IK"].is_visible = False
    rig.data.collections["Additional FK"].is_visible = True
    before = {c.name: c.is_visible for c in ui.control_collections(rig)}
    assert bpy.ops.xbone.toggle_controls() == {'FINISHED'}
    assert not any(c.is_visible_effectively for c in ui.control_collections(rig))
    skeleton = rig.data.collections["Skeleton"]
    skeleton.is_visible = True
    assert skeleton.is_visible_effectively and not rig.data.collections["Mechanism"].is_visible
    assert rig.data.bones.active == bend.bone and bend.select
    assert bpy.ops.xbone.edit_source_guide() == {'FINISHED'}
    assert bpy.context.edit_object == guide
    assert bpy.ops.xbone.return_to_rig() == {'FINISHED'} and bpy.context.active_object == rig
    assert bpy.context.mode == 'POSE'
    assert {c.name: c.is_visible for c in ui.control_collections(rig)} == before
    assert bpy.ops.xbone.edit_source_guide() == {'FINISHED'}
    assert bpy.ops.xbone.build_rig() == {'FINISHED'} and bpy.context.active_object == rig
    # Hiding controls manually during posing is still possible.
    assert bpy.ops.xbone.toggle_controls() == {'FINISHED'}

area.type = 'WEIGHT_LAYERS'
assert isinstance(area.spaces.active, bpy.types.SpaceWeightLayers)
area.type = 'SELECTOR'
area.type = 'RIGGING'
with tempfile.TemporaryDirectory() as temp:
    filename = str(Path(temp) / "rigging-editor.blend")
    bpy.ops.wm.save_as_mainfile(filepath=filename)
    bpy.ops.wm.open_mainfile(filepath=filename)
    check_workspace(bpy.data.workspaces["Rigging"])
    area = next(a for a in bpy.context.screen.areas if a.type == 'RIGGING')
    assert isinstance(area.spaces.active, bpy.types.SpaceRigging)
    assert any(a.type == 'VIEW_3D' for a in bpy.context.screen.areas)
    with bpy.context.temp_override(area=area, region=window_region(area)):
        rig = bpy.context.active_object
        assert not any(c.is_visible for c in ui.control_collections(rig))
        assert bpy.ops.xbone.toggle_controls() == {'FINISHED'}
        assert {c.name: c.is_visible for c in ui.control_collections(rig)} == before
        assert rig.data.collections["Skeleton"].is_visible
        assert bpy.ops.xbone.edit_source_guide.poll()
        bpy.ops.object.mode_set(mode='OBJECT')
        assert bpy.ops.xbone.new_guide() == {'FINISHED'}
        assert bpy.ops.xbone.add_component(kind='LEG') == {'FINISHED'}
        assert bpy.ops.xbone.build_rig() == {'FINISHED'}
    # The same file can be opened as a project or used as a personal startup.
    bpy.data.batch_remove([bpy.data.workspaces["Rigging"]])
    startup = str(Path(temp) / "personal-startup.blend")
    bpy.ops.wm.save_as_mainfile(filepath=startup)
    bpy.ops.wm.open_mainfile(filepath=startup)
    assert not any(w.get("xbone_rigging_workspace") for w in bpy.data.workspaces)
    previous_tabs = set(bpy.data.workspaces.keys())
    active_tab = bpy.context.window.workspace.name
    bpy.ops.wm.read_homefile(filepath=startup)
    assert set(bpy.data.workspaces.keys()) == previous_tabs | {"Rigging"}
    assert bpy.context.window.workspace.name == active_tab
    check_workspace(bpy.data.workspaces["Rigging"])
print("Rigging editor: guides, rebuild, FK matching, viewport selection and .blend layout passed")
