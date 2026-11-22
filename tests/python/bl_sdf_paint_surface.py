# SPDX-License-Identifier: GPL-2.0-or-later
"""Graphical Blender regression using region framebuffer readback and native scene ray casts."""
import os
import time
import traceback

import bpy
import gpu
import numpy as np
from bpy_extras import view3d_utils
from mathutils import Quaternion, Vector
from bl_ui import space_sdf as sdf

phase = 'setup'
frames = {}
areas = {}
deadline = time.monotonic() + 60


def capture(kind):
    if phase not in {'pick', 'selected', 'surface'} or bpy.context.area != areas[kind]:
        return
    r = bpy.context.region
    if kind == 'uv':
        x, y = r.view2d.view_to_region(.37, .38, clip=False)
        width = height = 1
    else:
        x = y = 0
        width, height = r.width, r.height
    frames[kind, phase] = np.asarray(gpu.state.active_framebuffer_get().read_color(
        x, y, width, height, 4, 0, 'FLOAT')).reshape(height, width, 4).copy()


def check_surface(area):
    pixels = frames['3d', 'surface']
    region = next(r for r in area.regions if r.type == 'WINDOW')
    rv = area.spaces.active.region_3d
    dg = bpy.context.evaluated_depsgraph_get()
    samples = {'Skin': [], 'Occluder': []}
    for y in range(5, region.height - 5, 9):
        for x in range(5, region.width - 5, 9):
            ray = view3d_utils.region_2d_to_vector_3d(region, rv, (x, y))
            origin = view3d_utils.region_2d_to_origin_3d(region, rv, (x, y))
            hit, loc, normal, _, obj, matrix = bpy.context.scene.ray_cast(dg, origin, ray)
            if hit and obj.name in samples and normal.dot(-ray) > .35:
                # The silhouette spans subpixel MSAA samples. Test the face interior.
                if obj.name == 'Occluder' and max(abs(v) for v in (matrix.inverted() @ loc).xy) > .18:
                    continue
                samples[obj.name].append((pixels[y, x], frames['3d','pick'][y, x]))
    skin = np.array(samples['Skin'])[:, 0]
    front, base_front = np.array(samples['Occluder']).transpose(1,0,2)
    black = np.mean((skin[:,:3].max(1) < .05) & (skin[:,3] > .95))
    visible = np.mean(abs(front - base_front).max(1) < .03)
    print("Surface/occluder samples and coverage:", {k: len(v) for k,v in samples.items()}, black, visible, flush=True)
    assert all(len(v) > 25 for v in samples.values())
    assert black > .99 and visible > .99


def tick():
    global phase
    try:
        assert time.monotonic() < deadline, phase
        if phase == 'setup':
            bpy.ops.object.select_all(action='SELECT')
            bpy.ops.object.delete()
            bpy.ops.mesh.primitive_cube_add(size=.42, location=(.35, .1, 1.08))
            bpy.context.object.name = "Occluder"
            bpy.ops.mesh.primitive_uv_sphere_add(segments=96, ring_count=48)
            bpy.context.object.name = "Skin"
            bpy.ops.sdf.workspace()
            phase = 'layout'
        elif phase == 'layout' and bpy.context.workspace.get('sdf_paint_layout'):
            areas['3d'] = next(a for a in bpy.context.screen.areas if a.type == 'VIEW_3D')
            areas['uv'] = next(a for a in bpy.context.screen.areas if a.ui_type == 'SDF')
            v = areas['3d'].spaces.active
            v.region_3d.view_rotation = Quaternion((1, 0, 0, 0))
            v.region_3d.view_distance = 4.5
            v.region_3d.view_perspective = 'PERSP'
            v.overlay.show_floor = v.overlay.show_axis_x = v.overlay.show_axis_y = False
            for space, kind in ((bpy.types.SpaceView3D,'3d'), (bpy.types.SpaceImageEditor,'uv')):
                space.draw_handler_add(capture, (kind,), 'WINDOW', 'POST_PIXEL')
            bpy.ops.sdf.add_mask(scope='PICK', resolution='256')
            phase = 'pick'
        elif phase == 'pick' and ('uv',phase) in frames:
            picker = sdf.active_picker(bpy.context)
            for index in picker.hits((.37,.38)):
                bpy.ops.sdf.island_action(action='TOGGLE', index=index)
            assert picker.selected
            phase = 'selected'
        elif phase == 'selected' and ('uv',phase) in frames:
            assert frames['uv','selected'][0,0,0] > frames['uv','pick'][0,0,0] + .3
            bpy.ops.sdf.island_action(action='CONFIRM')
            assert [src.object.name for src in bpy.context.scene.sdf_editor.mask.sources] == ['Skin']
            bpy.ops.sdf.key()
            phase = 'surface'
        elif phase == 'surface' and ('3d',phase) in frames:
            check_surface(areas['3d'])
            r = next(r for r in areas['3d'].regions if r.type=='WINDOW')
            with bpy.context.temp_override(area=areas['3d'],region=r):
                assert bpy.ops.sdf.radius('INVOKE_DEFAULT') == {'FINISHED'}
                assert any(op.bl_idname == 'WM_OT_radial_control' for op in bpy.context.window.modal_operators)
            print("Island preview, curved overlay, occlusion and radial control passed", flush=True)
            bpy.ops.wm.quit_blender()
            return
        for area in areas.values():
            area.tag_redraw()
        return .5
    except Exception:
        traceback.print_exc()
        os._exit(1)


bpy.app.timers.register(tick, first_interval=1)
