# SPDX-License-Identifier: GPL-2.0-or-later
"""Run in graphical factory-startup Blender. Uses only bpy and viewport projection."""
import os
import traceback
from types import SimpleNamespace
import bpy
from mathutils import Quaternion, Vector
from bpy_extras import view3d_utils
from bl_ui import space_sdf as sdf

phase = 0


def tick():
    global phase
    try:
        if phase == 0:
            bpy.ops.object.select_all(action='SELECT')
            bpy.ops.object.delete()
            for x, display in ((-.4, 'WIRE'), (.4, 'SOLID')):
                bpy.ops.mesh.primitive_cube_add(size=.34, location=(x, 0, 1.2))
                bpy.context.object.display_type = display
            bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32)
            bpy.context.object.scale = (1, .83, 1)
            bpy.context.object.modifiers.new("Subdivision", 'SUBSURF').levels = 1
            bpy.ops.sdf.workspace()
        else:
            area = next(a for a in bpy.context.screen.areas if a.type == 'VIEW_3D')
            region = next(r for r in area.regions if r.type == 'WINDOW')
            with bpy.context.temp_override(area=area, region=region):
                ctx = bpy.context
                rv = ctx.region_data
                if phase == 1:
                    bpy.ops.sdf.add_mask(scope='MESHES', resolution='256')
                    bpy.ops.sdf.key()
                    rv.view_rotation = Quaternion((1, 0, 0, 0))
                    rv.view_location = (0, 0, 0)
                    rv.view_distance = 4
                    rv.view_perspective = 'PERSP'
                else:
                    def hit(x, y, z):
                        pixel = view3d_utils.location_3d_to_region_2d(region, rv, Vector((x,y,z)))
                        return sdf.brush_hit(ctx, region, pixel, ctx.scene.sdf_editor.mask)
                    samples = [hit(x,y,1.2) for x in (-.48,-.44,-.4,-.36,-.32) for y in (-.08,0,.08)]
                    print(rv.view_perspective, 'visible hits:', sum(h is not None for h in samples), '/', len(samples), flush=True)
                    assert all(samples), 'Wire-only geometry intercepted the brush'
                    assert bool(hit(.4,0,1.2)) == (phase == 4), 'Incorrect foreground occlusion'
                    assert not hit(1.12,0,0), 'Miss beyond silhouette must stay a miss'
                    # A grazing ray must hit the same evaluated surface and UV position.
                    for x in (-.85, -.90, -.95, -.98):
                        h = hit(x, 0, 0)
                        assert h, f"Lost the curved surface near silhouette at {x}"
                        uv, verts, uvs, matrix, _ = h
                        from mathutils.geometry import barycentric_transform
                        point = matrix @ barycentric_transform(uv, *uvs, *verts)
                        actual = view3d_utils.location_3d_to_region_2d(region, rv, point)
                        expected = view3d_utils.location_3d_to_region_2d(region, rv, Vector((x,0,0)))
                        assert (actual - expected).length < .1
                    if phase == 2:
                        pixel = view3d_utils.location_3d_to_region_2d(region, rv, Vector((-.4,0,1.2)))
                        assert bpy.ops.sdf.stroke('INVOKE_DEFAULT') == {'RUNNING_MODAL'}
                        stroke = sdf._stroke
                        stroke.bits[:] = False
                        stroke.previous = stroke.previous_hit = None
                        event = SimpleNamespace(type='INBETWEEN_MOUSEMOVE',mouse_region_x=round(pixel.x),
                                                mouse_region_y=round(pixel.y))
                        assert stroke.modal(ctx,event) == {'RUNNING_MODAL'}
                        assert stroke.bits.any(), "Queued motion sample did not paint"
                        stroke.cancel(ctx)
                        rv.view_perspective = 'ORTHO'
                    elif phase == 3:
                        bpy.ops.view3d.localview(frame_selected=False)
                    else:
                        print('Viewport surface picking passed', flush=True)
                        bpy.ops.wm.quit_blender()
                        return
                area.tag_redraw()
        phase += 1
        return .6
    except Exception:
        traceback.print_exc()
        os._exit(1)

bpy.app.timers.register(tick, first_interval=.5)
