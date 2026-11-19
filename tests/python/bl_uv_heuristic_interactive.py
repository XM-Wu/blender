# SPDX-License-Identifier: GPL-2.0-or-later
"""Run graphically with --enable-event-simulate --factory-startup --python this_file -- output.json.
Uses Blender's internal event simulator, never OS/desktop input injection.
"""
import bpy
import sys
import time
import json
import traceback
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
from bl_uv_texel_pack import make_islands, all_uvs, set_face_selection

output=Path(sys.argv[sys.argv.index('--')+1])
to_others='--pack-to-others' in sys.argv
cases=['TIME','IDLE','ENTER','ESC']
results=[]
index=-1
sent=False
started=0
before=None
obj=None
preview_seen=False
heartbeat_count=0


def setup():
    global index, sent, started, before, obj, preview_seen, heartbeat_count
    index+=1
    if index==len(cases):
        output.write_text(json.dumps({'passed':True,'cases':results},indent=2))
        bpy.ops.wm.quit_blender()
        return
    specs=[dict(size=size,uv=(2+i*.1,2),uv_size=(size[0]*.1,size[1]*.1))
           for i,size in enumerate(((3,1),(2,2),(1,4),(1,1),(2,1),(1,2),(3,2),(2,3)))]
    if to_others:
        specs.append(dict(size=(1,1),uv=(.8,.8),uv_size=(.1,.1)))
    obj,ranges=make_islands(specs)
    if to_others:
        set_face_selection(obj, [f for r in ranges[:-1] for f in r])
    before=all_uvs(obj)
    preview_seen=False
    heartbeat_count=0
    bpy.context.scene['uv_texel_pack_live_updates']=0
    bpy.context.scene.uv_tools_texel_pack.heuristic_search=True
    bpy.context.scene.uv_tools_distortion=False
    bpy.context.scene.uv_tools_checker=False
    case=cases[index]
    operator=bpy.ops.uv.pack_to_others if to_others else bpy.ops.uv.texel_pack
    result=operator('INVOKE_DEFAULT',resolution_x=256,resolution_y=256,
        precision='EXACT',rotation='CARDINAL',use_stacking=False,target='FIXED',udim_tile=1001,
        heuristic_search=True, search_time=.2 if case=='TIME' else 0,
        no_improvement_time=.1 if case=='IDLE' else 0)
    assert result=={'RUNNING_MODAL'},result
    sent=False
    started=time.monotonic()
    bpy.app.timers.register(check,first_interval=.1)


def check():
    global sent, preview_seen, heartbeat_count
    try:
        elapsed=time.monotonic()-started
        case=cases[index]
        heartbeat_count+=1
        if elapsed>10:
            raise AssertionError('Search did not stop: '+case)
        if bpy.ops.uv.texel_pack_control.poll():
            if bpy.context.scene.get('uv_texel_pack_live_updates',0)>0:
                preview_seen |= all_uvs(obj)!=before
                progress=bpy.context.scene.get('uv_texel_pack_progress','')
                assert all(field in progress for field in ('Attempts:', 'Coverage:', 'Density:', 'Tiles:', 'No improvement:')),progress
            if case in {'ENTER','ESC'} and not sent and elapsed>.4:
                bpy.context.window.event_simulate(type='RET' if case=='ENTER' else 'ESC',value='PRESS')
                bpy.context.window.event_simulate(type='RET' if case=='ENTER' else 'ESC',value='RELEASE')
                sent=True
            return .1
        # The worker has finished and unlocked the mesh before inspecting UVs.
        report=bpy.context.scene.get('uv_texel_pack_report','')
        if case=='ESC':
            assert preview_seen, 'No live UV preview before cancellation'
            assert sent and all_uvs(obj)==before, 'Cancel modified UVs'
            assert 'cancel' in report.lower(),report
        else:
            assert 'search' in report and all_uvs(obj)!=before,report
            if case=='ENTER': assert sent and preview_seen
        results.append({'case':case,'elapsed':elapsed,'preview_seen':preview_seen,
                        'heartbeats':heartbeat_count,'updates':bpy.context.scene.get('uv_texel_pack_live_updates'),
                        'report':report})
        print('PASS',case,elapsed,report,flush=True)
        bpy.app.timers.register(setup,first_interval=.3)
    except Exception:
        output.write_text(json.dumps({'passed':False,'error':traceback.format_exc(),'cases':results}))
        traceback.print_exc()
        if bpy.ops.uv.texel_pack_control.poll():
            bpy.ops.uv.texel_pack_control(cancel=True)
        bpy.app.timers.register(lambda: bpy.ops.wm.quit_blender() and None,first_interval=1)


bpy.app.timers.register(setup,first_interval=1)
