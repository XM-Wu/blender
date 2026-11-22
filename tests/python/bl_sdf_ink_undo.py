# SPDX-License-Identifier: GPL-2.0-or-later
"""Run with background factory-startup Blender."""
import bpy
from pathlib import Path
from tempfile import TemporaryDirectory

bpy.ops.mesh.primitive_plane_add()
bpy.ops.object.mode_set(mode='SDF_PAINT')
bpy.ops.sdf.add_mask(resolution='256')
bpy.ops.sdf.key()
def settings():
    return bpy.context.scene.sdf_editor
def bitmap():
    return settings().mask.keys[0].bitmap

area = next(a for a in bpy.context.screen.areas if a.type == 'VIEW_3D')
with bpy.context.temp_override(area=area,region=next(r for r in area.regions if r.type=='WINDOW')):
    empty = bitmap()
    bpy.ops.sdf.fill(mode='ALL')
    filled = bitmap()
    assert empty != filled
    bpy.ops.ed.undo_push(message="White drawing")
    wm = bpy.context.window_manager
    wm.sdf_brush_radius = 25
    bpy.ops.sdf.brush_setting(action='LARGER')
    wm.sdf_pressure_enabled = False
    bpy.ops.sdf.toggle_auto_key()
    settings().mask_collapsed = True
    bpy.ops.sdf.brush_setting(action='BLACK')
    assert not settings().white
    bpy.ops.sdf.fill(mode='ALL')
    assert bitmap() == empty
    bpy.ops.ed.undo_push(message="Black drawing")
    bpy.ops.ed.undo()
    assert bitmap() == filled and not settings().white, (settings().ink, bitmap()==filled)
    assert settings().radius == wm.sdf_brush_radius == 30
    assert not settings().use_pressure and not settings().auto_key and settings().mask_collapsed
    wm.sdf_brush_radius = 46
    bpy.ops.ed.redo()
    assert bitmap() == empty and not settings().white and settings().radius == 46
    bpy.ops.ed.undo()
    # Two masks exist in both snapshots. Preserve the active mask and playhead,
    # including when undo removes the exact key that was just auto-recorded.
    bpy.ops.sdf.add_mask(name="Detail", resolution='256')
    bpy.ops.sdf.key()  # First endpoint of Detail at zero.
    settings().value = 1
    bpy.ops.sdf.key()
    settings().value = 0
    settings().index = 0
    bpy.ops.ed.undo_push(message="Before changing paint context")
    settings().index = 1
    bpy.ops.sdf.toggle_auto_key()
    bpy.ops.sdf.jump(value=.375)
    settings().ink = 'WHITE'
    bpy.ops.sdf.fill(mode='ALL')
    assert len(settings().mask.keys) == 3
    bpy.ops.ed.undo_push(message="Paint at .375")
    bpy.ops.ed.undo()
    assert settings().value == .375 and settings().auto_key
    assert settings().mask.name == "Detail" and [key.value for key in settings().mask.keys] == [0, 1]
    bpy.ops.sdf.jump(value=.625)
    bpy.ops.ed.redo()
    assert settings().value == .625 and settings().mask.keys[2].value == .375
    assert settings().radius == 46 and not settings().use_pressure
    settings().ink = 'BLACK'
with TemporaryDirectory() as tmp:
    path = str(Path(tmp)/"ink.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path)
    bpy.ops.wm.open_mainfile(filepath=path)
    assert settings().mask.keys[2].bitmap == filled and settings().ink == 'BLACK'
    assert settings().radius == 46 and settings().value == .625 and settings().index == 1
print("SDF undo/redo passed: radius, ink, pressure, Preview, active mask, auto-key and save/load")
