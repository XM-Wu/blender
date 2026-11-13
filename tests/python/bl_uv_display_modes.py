# SPDX-License-Identifier: GPL-2.0-or-later
"""Graphical framebuffer test: factory defaults and island face-dot suppression."""
import bpy
import gpu
import numpy as np
import time
import json
import sys
import traceback
from pathlib import Path

out=Path(sys.argv[sys.argv.index('--')+1]);out.mkdir(parents=True,exist_ok=True)
cases=[(sync,mode) for sync in (False,True) for mode in ('FACE','ISLAND','EDGE')]
index=-1
ready=0
samples={}
area=None


def draw():
    if index<0 or index>=len(cases) or time.monotonic()<ready or bpy.context.area!=area:
        return
    key=cases[index]
    if key in samples: return
    region=bpy.context.region
    x,y=region.view2d.view_to_region(.5,.5,clip=False)
    assert 4<x<region.width-4 and 4<y<region.height-4
    samples[key]=np.array(gpu.state.active_framebuffer_get().read_color(x-3,y-3,7,7,4,0,'FLOAT')).copy()


def begin():
    bpy.context.window.workspace=bpy.data.workspaces['UV Editing']
    bpy.app.timers.register(prepare,first_interval=.5)


def prepare():
    global area
    try:
        if bpy.context.object.mode!='OBJECT': bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
        bpy.ops.mesh.primitive_plane_add()
        bpy.ops.object.mode_set(mode='EDIT')
        area=next(a for a in bpy.context.screen.areas if a.type=='IMAGE_EDITOR')
        assert area.spaces.active.uv_editor.edge_display_type=='BLACK'
        area.spaces.active.image=None
        with bpy.context.temp_override(area=area,region=next(r for r in area.regions if r.type=='WINDOW')):
            bpy.ops.image.view_all(fit_view=True)
        bpy.types.SpaceImageEditor.draw_handler_add(draw,(),'WINDOW','POST_PIXEL')
        bpy.app.timers.register(advance,first_interval=.5)
    except Exception:
        fail()


def fail():
    out.joinpath('result.json').write_text(json.dumps({'passed':False,'error':traceback.format_exc()}))
    traceback.print_exc();bpy.ops.wm.quit_blender()


def advance():
    global index,ready
    try:
        if index>=0 and cases[index] not in samples:
            area.tag_redraw();return .2
        if index>=0:
            sync,mode=cases[index]
            bpy.ops.screen.screenshot(filepath=str(out/f'{sync}-{mode}.png'))
        index+=1
        if index==len(cases):
            metrics={}
            for sync in (False,True):
                face=samples[sync,'FACE'];island=samples[sync,'ISLAND'];edge=samples[sync,'EDGE']
                delta=float(np.max(abs(face-island)))
                same=float(np.max(abs(edge-island)))
                assert delta>.05, (sync,'face dot did not differ',delta)
                assert same<.01, (sync,'island center differs from edge mode',same)
                metrics[str(sync)]={'face_dot_difference':delta,'island_vs_edge':same}
            out.joinpath('result.json').write_text(json.dumps({'passed':True,'black_default':True,'metrics':metrics},indent=2))
            bpy.ops.wm.quit_blender();return
        sync,mode=cases[index]
        with bpy.context.temp_override(area=area,region=next(r for r in area.regions if r.type=='WINDOW')):
            bpy.context.scene.tool_settings.use_uv_select_sync=sync
            bpy.ops.uv_tools.select_mode(type=mode)
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.uv.select_all(action='SELECT')
        ready=time.monotonic()+.15
        area.tag_redraw()
        return .4
    except Exception:
        fail()

bpy.app.timers.register(begin,first_interval=1)
