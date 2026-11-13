# SPDX-License-Identifier: GPL-2.0-or-later
"""Graphical test: --factory-startup --enable-event-simulate --python this_file -- output_dir."""
import bpy
import bmesh
import json
import traceback
import runpy
import sys
from pathlib import Path
from bl_operators.uv_tools import _quick_toolbar_origin

out=Path(sys.argv[sys.argv.index('--')+1]);out.mkdir(parents=True,exist_ok=True)
preset=Path(__file__).resolve().parents[2]/'scripts/presets/keyconfig/XM_KeyMapping.py'
namespace=runpy.run_path(str(preset))
data=namespace['industry_compatible'].generate_keymaps(namespace['industry_compatible'].Params())
namespace['_add_xm_items'](data)
for name,expected in (('UV Editor','uv_tools.mark_seam'),('Mesh','mesh.mark_seam'),('3D View',None)):
    items=namespace['_keymap_items'](data,name)
    plain=[item for item in items if item[1].get('type')=='C' and not any(item[1].get(k,False) for k in ('shift','ctrl','alt','oskey','hyper'))]
    assert [item[0] for item in plain]==([expected] if expected else []),(name,plain)

def mesh(): return bmesh.from_edit_mesh(bpy.context.object.data)
def clear():
    for edge in mesh().edges: edge.seam=False
    bmesh.update_edit_mesh(bpy.context.object.data)
def region(area): return next(r for r in area.regions if r.type=='WINDOW')
def event(type,value,x,y,shift=False): bpy.context.window.event_simulate(type=type,value=value,x=int(x),y=int(y),shift=shift)
def click(index):
    with bpy.context.temp_override(area=uv,region=region(uv)):
        x,y,scale=_quick_toolbar_origin(bpy.context)
    r=region(uv);x+=r.x+(24+index*42)*scale;y+=r.y+24*scale
    event('MOUSEMOVE','NOTHING',x,y)
    event('LEFTMOUSE','PRESS',x,y)
    event('LEFTMOUSE','RELEASE',x,y)
def key_c(area,shift=False):
    r=region(area);x=r.x+r.width*.6;y=r.y+r.height*.5
    event('MOUSEMOVE','NOTHING',x,y)
    event('C','PRESS',x,y,shift);event('C','RELEASE',x,y,shift)

step=0
def advance():
    global step,uv,view
    try:
        if step==0:
            bpy.context.window.workspace=bpy.data.workspaces['UV Editing']
            bpy.context.preferences.use_preferences_save=False
            bpy.utils.keyconfig_set(str(preset))
        elif step==1:
            uv=next(a for a in bpy.context.screen.areas if a.type=='IMAGE_EDITOR')
            view=next(a for a in bpy.context.screen.areas if a.type=='VIEW_3D')
            if bpy.context.object.mode!='EDIT': bpy.ops.object.mode_set(mode='EDIT')
            bpy.context.scene.tool_settings.use_uv_select_sync=True
            bpy.ops.mesh.select_all(action='SELECT')
            clear()
            bpy.context.scene.uv_tools_checker=False
        elif step==2:
            bpy.ops.screen.screenshot(filepath=str(out/'toolbar.png'))
            click(0)
        elif step==3:
            assert bpy.context.scene.uv_tools_checker,'Checker click failed'
            click(0)
        elif step==4:
            assert not bpy.context.scene.uv_tools_checker,'Checker off failed'
            click(1)
        elif step==5:
            assert all(e.seam for e in mesh().edges),'Toolbar Mark Seam failed'
            clear();key_c(uv)
        elif step==6:
            assert all(e.seam for e in mesh().edges),'UV C did not mark seams'
            clear();key_c(view)
        elif step==7:
            assert all(e.seam for e in mesh().edges),'3D edit C did not mark seams'
            key_c(view,shift=True)
        elif step==8:
            assert not any(e.seam for e in mesh().edges),'3D Shift-C did not clear seams'
            key_c(uv)
        elif step==9:
            assert all(e.seam for e in mesh().edges),'C must mark after Shift-C'
            key_c(uv,shift=True)
        elif step==10:
            assert not any(e.seam for e in mesh().edges),'UV Shift-C did not clear seams'
            (out/'result.json').write_text(json.dumps({'passed':True,'checker_click':True,'toolbar_mark_seam':True,'uv_c':True,'mesh_c':True,'uv_shift_c':True,'mesh_shift_c':True}))
            print('PASS: toolbar clicks and XM C/Shift-C in UV/3D edit mode',flush=True)
            bpy.ops.wm.quit_blender()
            return
        step+=1
        return .6
    except Exception:
        (out/'result.json').write_text(json.dumps({'passed':False,'step':step,'error':traceback.format_exc()}))
        traceback.print_exc()
        bpy.ops.wm.quit_blender()

bpy.app.timers.register(advance,first_interval=1)
