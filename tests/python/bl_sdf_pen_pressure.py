# SPDX-License-Identifier: GPL-2.0-or-later
"""Graphical Blender regression. Native bpy operators with scripted pen samples."""
import os
import traceback
from types import SimpleNamespace

import bpy
import numpy as np
from mathutils import Quaternion, Vector
from bpy_extras.view3d_utils import location_3d_to_region_2d
from bl_ui import sdf_field as field, space_sdf as sdf

phase = 0


def event(xy, kind, value, pressure):
    return SimpleNamespace(type=kind, value=value, is_tablet=True, pressure=pressure,
                           mouse_region_x=round(xy[0]), mouse_region_y=round(xy[1]))


def tick():
    global phase
    try:
        c = bpy.context
        if phase == 0:
            bpy.ops.object.select_all(action='SELECT')
            bpy.ops.object.delete()
            bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32)
            bpy.ops.sdf.workspace()
        elif phase == 1:
            bpy.ops.sdf.add_mask(scope='MESHES', resolution='256')
            bpy.ops.sdf.key()
            area = next(a for a in c.screen.areas if a.type=='VIEW_3D')
            rv = area.spaces.active.region_3d
            rv.view_rotation = Quaternion((1,0,0,0))
            rv.view_location = (0,0,0)
            rv.view_distance = 5
        else:
            settings = c.scene.sdf_editor
            settings.radius = 12
            key = settings.mask.keys[0]
            empty = key.bitmap
            for kind in ('SDF','VIEW_3D'):
                area = next(a for a in c.screen.areas if a.ui_type==kind)
                r = next(r for r in area.regions if r.type=='WINDOW')
                with c.temp_override(area=area,region=r):
                    if kind=='SDF':
                        start = r.view2d.view_to_region(.2,.5,clip=False)
                        end = r.view2d.view_to_region(.8,.5,clip=False)
                    else:
                        def project(x):
                            return location_3d_to_region_2d(r,c.region_data,Vector((x,.25,.9)))
                        start, end = project(-.5), project(.5)
                    assert settings.auto_key
                    settings.value = .25 if kind == 'SDF' else .75
                    num_keys = len(settings.mask.keys)
                    assert sdf.key_at(settings.mask, settings.value) is None
                    km = c.window_manager.keyconfigs.user.keymaps['SDF Paint']
                    for letter, tool in (('Q','BRUSH'),('P','PEN')):
                        assert any(k.active and k.type==letter and k.idname=='sdf.select_tool'
                                   and k.properties.tool==tool for k in km.keymap_items)
                    assert bpy.ops.sdf.select_tool(tool='PEN')=={'FINISHED'}
                    assert sdf.active_sdf_tool(c)=='builtin.sdf_pen'
                    assert bpy.ops.sdf.stroke('INVOKE_DEFAULT')=={'RUNNING_MODAL'}
                    op = sdf._stroke
                    op.pen.nodes.clear()
                    op.modal(c,event(start,'LEFTMOUSE','PRESS',.25))
                    assert sdf.key_at(settings.mask, settings.value) is not None
                    op.modal(c,event(start,'LEFTMOUSE','RELEASE',0))
                    op.modal(c,event(end,'LEFTMOUSE','PRESS',1))
                    assert len(op.pen.nodes)==2 and len(settings.mask.keys)==num_keys+1
                    straight = op.bits.copy()
                    if kind=='SDF':
                        assert straight[:,int(.25*256)].sum() < straight[:,int(.75*256)].sum()
                    op.modal(c,event((end[0],end[1]+55),'MOUSEMOVE','NOTHING',1))
                    op.modal(c,event(end,'LEFTMOUSE','RELEASE',0))
                    assert np.any(straight != op.bits)
                    op.modal(c,event(end,'BACK_SPACE','PRESS',0))
                    assert len(op.pen.nodes)==1 and not op.bits.any()
                    op.modal(c,event(end,'LEFTMOUSE','PRESS',1))
                    assert op.modal(c,event(end,'RET','PRESS',0))=={'FINISHED'}
                    assert len(settings.mask.keys) == num_keys + 1
                    key = sdf.key_at(settings.mask, settings.value)
                    assert np.array_equal(field.decode(key.bitmap,256),straight)
                    settings.value += .05
                    assert bpy.ops.sdf.stroke('INVOKE_DEFAULT')=={'RUNNING_MODAL'}
                    op=sdf._stroke
                    assert op.modal(c,event(end,'ESC','PRESS',0))=={'CANCELLED'}
                    key = settings.mask.keys[num_keys]
                    assert np.array_equal(field.decode(key.bitmap,256),straight)
                    assert len(settings.mask.keys) == num_keys + 1
                    key.bitmap=empty
                    # Exercise the freehand brush's tablet, disabled-pressure and mouse paths.
                    assert bpy.ops.sdf.select_tool(tool='BRUSH')=={'FINISHED'}
                    assert sdf.active_sdf_tool(c)=='builtin.sdf_paint'
                    counts=[]
                    for tablet, enabled, pressure in ((True,True,.25),(True,True,1),(True,False,.25),(False,True,.25)):
                        settings.use_pressure=enabled
                        bpy.ops.sdf.stroke('INVOKE_DEFAULT')
                        op=sdf._stroke
                        op.bits[:]=False
                        op.previous=op.previous_uv=op.previous_hit=None
                        sample=event(start,'MOUSEMOVE','NOTHING',pressure)
                        sample.is_tablet=tablet
                        op.modal(c,sample)
                        assert sdf.key_at(settings.mask, settings.value) is not None
                        counts.append(int(op.bits.sum()))
                        op.modal(c,event(start,'ESC','PRESS',0))
                    assert 0<counts[0]<counts[1] and counts[1]==counts[2]==counts[3],counts
                    assert len(settings.mask.keys) == num_keys + 1
                    bpy.ops.sdf.stroke('INVOKE_DEFAULT')
                    op = sdf._stroke
                    op.modal(c,event(start,'MOUSEMOVE','NOTHING',1))
                    assert op.modal(c,event(start,'LEFTMOUSE','RELEASE',0)) == {'FINISHED'}
                    assert len(settings.mask.keys) == num_keys + 2
                    sdf.key_at(settings.mask, settings.value).bitmap = empty
                    # Black on black still records an intentional hold, immediately.
                    settings.value += .05
                    settings.white = False
                    bpy.ops.sdf.stroke('INVOKE_DEFAULT')
                    op = sdf._stroke
                    op.modal(c,event(start,'MOUSEMOVE','NOTHING',1))
                    assert sdf.key_at(settings.mask, settings.value) is not None
                    assert op.modal(c,event(start,'LEFTMOUSE','RELEASE',0)) == {'FINISHED'}
                    assert sdf.key_at(settings.mask, settings.value).bitmap == empty
                    settings.white = True
                    print(kind,'auto-key, pen and pressure samples passed',counts,flush=True)
            os._exit(0)
        phase+=1
        return .6
    except Exception:
        traceback.print_exc()
        os._exit(1)

bpy.app.timers.register(tick,first_interval=.6)
