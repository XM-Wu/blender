# SPDX-License-Identifier: GPL-2.0-or-later
"""Run in graphical Blender. Sample the SDF edge and adjacent face interiors in both framebuffers."""
import bpy
import gpu
import numpy as np
import os
import traceback
import time
from mathutils import Vector, Quaternion
from bpy_extras.view3d_utils import location_3d_to_region_2d
from bl_ui import sdf_field

areas = {}
samples = {}
phase = 'setup'
deadline = time.monotonic() + 45


def capture(kind):
    if phase not in ('off','on','dim','native','restored','previous','next','first','last','bad_before','bad_after','fixed','wire_depth') or bpy.context.area != areas[kind]:
        return
    region = bpy.context.region
    fb = gpu.state.active_framebuffer_get()
    rows = []
    for u,v in ((.5,.27),(.5,.73),(.25,.25),(.75,.75)):
        if kind == 'uv':
            x,y = region.view2d.view_to_region(u,v,clip=False)
        else:
            screen = location_3d_to_region_2d(region,bpy.context.region_data,Vector((2*u-1,2*v-1,0)))
            x,y = map(round, screen)
        patch = np.array(fb.read_color(x-3,y-1,7,3,4,0,'FLOAT')).reshape(-1,4)
        rows.append(patch[:,:3].copy())
    samples[phase,kind] = np.array(rows)


def tick():
    global phase
    try:
        assert time.monotonic()<deadline, phase
        c = bpy.context
        if phase == 'setup':
            bpy.ops.object.select_all(action='SELECT')
            bpy.ops.object.delete()
            mesh = bpy.data.meshes.new('Two quads')
            front = [(-1,-1,0),(0,-1,0),(1,-1,0),(-1,1,0),(0,1,0),(1,1,0)]
            # Coprojected rear polygons with opposite ink are submitted last.
            mesh.from_pydata(front + [(x,y,-.15) for x,y,_ in front], [],
                             [(0,1,4,3),(1,2,5,4),(6,7,10,9),(7,8,11,10)])
            obj=bpy.data.objects.new('Canvas',mesh)
            c.scene.collection.objects.link(obj)
            c.view_layer.objects.active=obj
            obj.select_set(True)
            mesh.uv_layers.new(name='First')
            layer = mesh.uv_layers.new(name='Paint')
            for loop in mesh.loops:
                x,y,z = mesh.vertices[loop.vertex_index].co
                if z < 0:
                    y = -y
                layer.uv[loop.index].vector = ((x+1)/2,(y+1)/2)
            sub=obj.modifiers.new('Subdivide','SUBSURF')
            sub.subdivision_type='SIMPLE'
            sub.levels=2
            bpy.ops.sdf.workspace()
            phase='layout'
        elif phase=='layout':
            if c.mode!='PAINT_SDF':
                return .2
            areas['uv']=next(a for a in c.screen.areas if a.ui_type=='SDF')
            areas['3d']=next(a for a in c.screen.areas if a.type=='VIEW_3D')
            space=areas['3d'].spaces.active
            space.region_3d.view_rotation=Quaternion((1,0,0,0))
            space.region_3d.view_location=(0,0,0)
            space.region_3d.view_distance=5
            space.region_3d.view_perspective='PERSP'
            space.overlay.show_floor=space.overlay.show_axis_x=space.overlay.show_axis_y=False
            bpy.ops.sdf.add_mask(scope='MESHES',resolution='256',uv_name='Paint')
            bpy.ops.sdf.key()
            bits=np.zeros((256,256),bool)
            bits[128:]=True
            c.scene.sdf_editor.mask.keys[0].bitmap=sdf_field.encode(bits)
            for cls,kind in ((bpy.types.SpaceImageEditor,'uv'),(bpy.types.SpaceView3D,'3d')):
                cls.draw_handler_add(capture,(kind,),'WINDOW','POST_PIXEL')
            phase='off'
        elif all((phase,k) in samples for k in areas):
            s=c.scene.sdf_editor
            if phase=='off':
                c.window_manager.sdf_show_wire=True
                phase='on'
            elif phase=='on':
                c.window_manager.sdf_wire_opacity=.2
                phase='dim'
            elif phase=='dim':
                s.show_wire=False
                areas['3d'].spaces.active.overlay.show_wireframes=True
                phase='native'
            elif phase=='native':
                areas['3d'].spaces.active.overlay.show_wireframes=False
                phase='restored'
            elif phase=='restored':
                for k in areas:
                    base=samples['off',k]
                    delta=np.abs(samples['on',k]-base).max(axis=(1,2))
                    dim=np.abs(samples['dim',k]-base).max(axis=(1,2))
                    print(k, 'edge/interior contrast',delta,'dimmed',dim,flush=True)
                    assert min(delta[:2])>.08 and max(delta[2:])<.02
                    assert all(dim[:2]<delta[:2])
                    assert np.max(abs(samples['restored',k]-base))<.02
                assert np.max(abs(samples['native','3d']-samples['off','3d']))>.08
                assert np.all(sdf_field.decode(s.mask.keys[0].bitmap,256)==(np.arange(256)[:,None]>=128))
                space = areas['3d'].spaces.active
                space.shading.type = 'WIREFRAME'
                space.overlay.show_overlays = False
                phase = 'wire_depth'
            elif phase == 'wire_depth':
                assert samples['wire_depth','3d'][2].max() < .03
                assert samples['wire_depth','3d'][3].min() > .97
                space = areas['3d'].spaces.active
                space.shading.type = 'SOLID'
                space.overlay.show_overlays = True
                # Insert keys out of chronological order to check temporal neighbor selection.
                mid = s.mask.keys[0].bitmap
                s.value = 1
                bpy.ops.sdf.key()
                s.mask.keys[-1].bitmap = sdf_field.encode(np.ones((256, 256), bool))
                s.value = .45
                bpy.ops.sdf.key()
                s.mask.keys[-1].bitmap = mid
                s.mask.keys[0].bitmap = sdf_field.encode(np.zeros((256, 256), bool))
                c.window_manager.sdf_onion_skin = 'PREVIOUS'
                phase = 'previous'
            elif phase == 'previous':
                c.window_manager.sdf_onion_skin = 'NEXT'
                phase = 'next'
            elif phase == 'next':
                from bl_ui.sdf_preview import onion_reference
                assert abs(onion_reference(s.mask,.6,'PREVIOUS').value-.45)<1e-6
                assert onion_reference(s.mask,.6,'NEXT').value==1
                s.value = 0
                s.onion_skin = 'PREVIOUS'
                phase = 'first'
            elif phase == 'first':
                s.value = 1
                s.onion_skin = 'NEXT'
                phase = 'last'
            elif phase=='last':
                for k in areas:
                    prev = samples['previous',k].mean(axis=1)
                    following = samples['next',k].mean(axis=1)
                    print(k, 'onion previous/next:',prev,following,flush=True)
                    for colors in (prev, following):
                        assert np.all(colors[:,0] > colors[:,1] + .08)
                        assert np.all(colors[:,0] > colors[:,2] + .08)
                        assert colors[1].mean() > colors[0].mean() + .15
                    assert prev[0,0] > .1 and following[0].mean() > prev[0].mean() + .1
                    assert samples['first',k].max()<.03 and samples['last',k].min()>.97
                assert np.all(sdf_field.decode(s.mask.keys[-1].bitmap,256)==(np.arange(256)[:,None]>=128))
                # Make the latter key black. The upper half now regresses at .45 -> 1.
                from bl_ui.space_sdf import key_at
                key_at(s.mask,1).bitmap = key_at(s.mask,0).bitmap
                bpy.ops.sdf.check_conflicts()
                assert abs(s.value-.45) < 1e-6
                s.show_wire = True
                phase = 'bad_before'
            elif phase == 'bad_before':
                s.value = 1
                phase = 'bad_after'
            elif phase == 'bad_after':
                for k in areas:
                    for state in ('bad_before','bad_after'):
                        patches = samples[state,k]
                        assert patches[3,:,0].min() > .98 and patches[3,:,1:].max() < .02
                        assert patches[2].max() < .03
                from bl_ui.space_sdf import key_at
                key_at(s.mask,1).bitmap = key_at(s.mask,.45).bitmap
                phase = 'fixed'
            else:
                for k in areas:
                    assert samples['fixed',k][3].min() > .97
                bpy.ops.sdf.check_conflicts()
                assert s.mask.conflict_pair[0] < 0
                print('SDF wireframe, onion-key and live conflict overlay regressions passed',flush=True)
                os._exit(0)
        for area in areas.values():
            area.tag_redraw()
        return .5
    except Exception:
        traceback.print_exc()
        os._exit(1)

bpy.app.timers.register(tick,first_interval=.5)
