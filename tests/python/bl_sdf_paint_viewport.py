# SPDX-License-Identifier: GPL-2.0-or-later
"""Run in graphical factory-startup Blender. Uses bpy and GPU readback only."""
import os
import time
import traceback
from types import SimpleNamespace

import bpy
import gpu
import numpy as np
from bpy_extras.view3d_utils import location_3d_to_region_2d
from mathutils import Quaternion, Vector
from bl_ui import sdf_controls, sdf_field, space_sdf

areas = {}
patches = {}
samples = {}
phase = "setup"
deadline = time.monotonic() + 60


def region(area):
    return next(r for r in area.regions if r.type == 'WINDOW')


def point(kind, r):
    if kind == 'uv':
        return r.view2d.view_to_region(.75, .5, clip=False)
    p = location_3d_to_region_2d(r, areas[kind].spaces.active.region_3d, Vector((1.25, 0, 0)))
    return tuple(map(round, p))


def capture(kind):
    if phase not in {'black', 'drag3d', 'white', 'draguv', 'erased', 'previous', 'off'} or bpy.context.area != areas[kind]:
        return
    r = bpy.context.region
    x, y = point(kind, r)
    buf = gpu.state.active_framebuffer_get().read_color(x - 2, y - 2, 5, 5, 4, 0, 'FLOAT')
    samples[phase, kind] = float(np.array(buf)[..., :3].mean())
    if phase in ('drag3d', 'white', 'draguv', 'erased'):
        patches[phase, kind] = np.array(gpu.state.active_framebuffer_get().read_color(
            x-64,y-64,128,128,4,0,'FLOAT')).copy()
    if phase == 'black':
        fb = gpu.state.active_framebuffer_get()
        def patch():
            return np.array(fb.read_color(x-64,y-64,128,128,4,0,'FLOAT')).copy()
        before = patch()
        # Native hover callback takes window coordinates, whereas the
        # framebuffer callback is in region space.
        with gpu.matrix.push_pop():
            gpu.matrix.translate((-r.x,-r.y,0))
            space_sdf.hover_cursor((x+r.x,y+r.y))
        samples['cursor',kind] = int(np.count_nonzero(np.max(abs(patch() - before),axis=-1) > .3))



def tick():
    global phase
    try:
        assert time.monotonic() < deadline, (phase, samples)
        if phase == 'setup':
            bpy.ops.object.select_all(action='SELECT')
            bpy.ops.object.delete()
            objs = []
            for index, x in enumerate((-1.25, 1.25)):
                bpy.ops.mesh.primitive_plane_add(location=(x, 0, 0))
                obj = bpy.context.object
                layer = obj.data.uv_layers.new(name='Face')
                obj.data.uv_layers.active = layer
                for coord in layer.uv:
                    coord.vector.x = (coord.vector.x + index) / 2
                mod = obj.modifiers.new("Subdivide", 'SUBSURF')
                mod.subdivision_type = 'SIMPLE'
                objs.append(obj)
            for obj in objs:
                obj.select_set(True)
            bpy.context.view_layer.objects.active = objs[0]
            assert bpy.ops.sdf.workspace() == {'FINISHED'}
            phase = 'layout'
        elif phase == 'layout':
            if not bpy.context.workspace.get("sdf_paint_layout"):
                return .2
            assert bpy.context.mode == 'PAINT_SDF'
            assert len(bpy.context.objects_in_mode) == 2
            tool = bpy.context.workspace.tools.from_space_view3d_mode('PAINT_SDF')
            assert tool and tool.idname == 'builtin.sdf_paint'
            km = bpy.context.window_manager.keyconfigs.user.keymaps['SDF Paint']
            assert any(k.active and k.idname == 'sdf.stroke' and k.type == 'LEFTMOUSE' for k in km.keymap_items)
            screen = bpy.context.screen
            areas['3d'] = next(a for a in screen.areas if a.type == 'VIEW_3D')
            areas['uv'] = next(a for a in screen.areas if a.ui_type == 'SDF')
            outliner = next(a for a in screen.areas if a.type == 'OUTLINER')
            props = next(a for a in screen.areas if a.type == 'PROPERTIES')
            assert areas['3d'].x < areas['uv'].x < props.x == outliner.x and outliner.y > props.y
            space = areas['3d'].spaces.active
            space.region_3d.view_rotation = Quaternion((1, 0, 0, 0))
            space.region_3d.view_distance = 7
            space.region_3d.view_location = (0, 0, 0)
            space.region_3d.view_perspective = 'PERSP'
            space.overlay.show_floor = space.overlay.show_axis_x = space.overlay.show_axis_y = False
            r = region(areas['3d'])
            with bpy.context.temp_override(area=areas['3d'], region=r):
                before = [o.matrix_world.copy() for o in bpy.context.objects_in_mode]
                assert bpy.ops.sdf.stroke('INVOKE_DEFAULT') == {'RUNNING_MODAL'}
                guard = bpy.context.window.modal_operators[-1]
                assert guard.idle
                assert guard.modal(bpy.context, SimpleNamespace(type='MOUSEMOVE')) == {'RUNNING_MODAL'}
                assert guard.modal(bpy.context, SimpleNamespace(type='LEFTMOUSE', value='RELEASE')) == {'CANCELLED'}
                assert before == [o.matrix_world for o in bpy.context.objects_in_mode]
            for key, action in (('LEFT_BRACKET','SMALLER'), ('RIGHT_BRACKET','LARGER')):
                for shift in (False,True):
                    assert any(k.type == key and k.shift == shift and k.properties.action == action
                               for k in km.keymap_items if k.idname == 'sdf.brush_setting')
            assert any(k.idname == 'sdf.radius' and k.type == 'F' for k in km.keymap_items)
            bpy.ops.sdf.add_mask(resolution='256', scope='PICK')
            picker = space_sdf.active_picker(bpy.context)
            assert len(picker.islands) == 2
            assert picker.hits((.75, .33)) != picker.hits((.25, .33))
            for point_uv in ((.25, .33), (.75, .33)):
                index, = picker.hits(point_uv)
                bpy.ops.sdf.island_action(action='TOGGLE', index=index)
            bpy.ops.sdf.island_action(action='CONFIRM')
            bpy.ops.sdf.key()
            bpy.context.scene.sdf_editor.value = 1
            bpy.ops.sdf.key()
            # Exercise the visible marker, playhead and panel through the same
            # region hit-testing used by the left-mouse keymap.
            uv_area = areas['uv']
            with bpy.context.temp_override(area=uv_area, region=region(uv_area)):
                ctx = bpy.context
                state = SimpleNamespace()
                controls = sdf_controls.CanvasControls(ctx)
                scale = controls.scale
                x0, x1 = controls.track
                def event(x, y, kind='LEFTMOUSE', value='PRESS'):
                    return SimpleNamespace(mouse_region_x=round(x * scale), mouse_region_y=round(y * scale),
                                           type=kind, value=value)
                op = sdf_controls.SDF_OT_canvas_control
                settings = ctx.scene.sdf_editor
                # The original press and its release must leave exact input active.
                class NumberState:
                    poll = staticmethod(op.poll)
                    cancel = op.cancel

                number = NumberState()
                rect = next(box for box, action in controls.targets if action == ('number',))
                nx, ny = rect[0]+rect[2]/2, rect[1]+rect[3]/2
                for confirm in (True, False):
                    assert op.press(number,ctx,event(nx,ny)) == {'RUNNING_MODAL'}
                    assert op.modal(number,ctx,event(nx,ny,'LEFTMOUSE','RELEASE')) == {'RUNNING_MODAL'}
                    assert sdf_controls.CanvasControls(ctx).number_input.selected
                    for char in '0.375':
                        key = SimpleNamespace(type='PERIOD' if char=='.' else 'TEXTINPUT',
                                              value='PRESS',ctrl=False,ascii=char)
                        assert op.modal(number,ctx,key) == {'RUNNING_MODAL'}
                    assert settings.value == 1
                    if confirm:
                        assert op.modal(number,ctx,event(nx,ny,'RET')) == {'FINISHED'}
                        assert settings.value == .375
                        settings.value = 1
                    else:
                        assert op.modal(number,ctx,event(nx,ny,'ESC')) == {'CANCELLED'}
                        assert settings.value == 1
                    assert sdf_controls.CanvasControls(ctx).number_input is None
                # Click just off the zero-key diamond, drag to the middle, cancel.
                assert op.press(state, ctx, event(x0 + 2, 30)) == {'RUNNING_MODAL'}
                assert settings.value == 0
                op.modal(state,ctx,event((x0 + x1) / 2,40,'MOUSEMOVE'))
                assert abs(settings.value - .5) < .002
                op.modal(state,ctx,event(0,0,'ESC'))
                assert settings.value == 1
                rect = next(box for box, action in controls.targets if action == ('fold',))
                x, y, w, h = rect
                op.press(state,ctx,event(x+12,y+h/2))
                assert settings.mask_collapsed
                op.press(state,ctx,event(x+12,y+h/2))
                assert not settings.mask_collapsed
                assert bpy.ops.wm.tool_set_by_id(name='builtin.sdf_fill') == {'FINISHED'}
                assert space_sdf.bucket_active(ctx)
                assert bpy.ops.wm.tool_set_by_id(name='builtin.sdf_paint') == {'FINISHED'}
            for cls, kind in ((bpy.types.SpaceImageEditor, 'uv'), (bpy.types.SpaceView3D, '3d')):
                cls.draw_handler_add(capture, (kind,), 'WINDOW', 'POST_PIXEL')
            phase = 'black'
        elif all((phase, kind) in samples for kind in areas):
            settings = bpy.context.scene.sdf_editor
            if phase == 'black':
                assert all(samples['cursor',k] > 12 for k in areas), samples
                area = areas['3d']
                r = region(area)
                with bpy.context.temp_override(area=area, region=r):
                    xy = point('3d', r)
                    assert np.allclose(space_sdf.hit_uv(bpy.context, r, xy, settings.mask), (.75, .5), atol=.01)
                    settings.radius = 10
                    bpy.ops.sdf.brush_setting(action='LARGER')
                    assert settings.radius == 12
                    bpy.ops.sdf.brush_setting(action='SMALLER')
                    assert settings.radius == 10
                    settings.ink = 'BLACK'
                    assert not settings.white
                    bpy.ops.sdf.brush_setting(action='SWAP')
                    assert settings.ink == 'WHITE'

                    # A 2K mask viewed from far away spans several texels per
                    # screen pixel. A radius-one stroke must still cover its entire path.
                    test_mask = SimpleNamespace(size=2048, sources=settings.mask.sources)
                    narrow = SimpleNamespace(mask=test_mask, radius=1, white=True, previous=None, previous_uv=None,
                                             bits=np.zeros((2048,2048),bool), coverage=np.ones((2048,2048),bool))
                    start, end = (xy[0]-29, xy[1]), (xy[0]+29, xy[1])
                    for pos in (start, end):
                        space_sdf.SDF_OT_stroke.dab(narrow,bpy.context,SimpleNamespace(
                            mouse_region_x=pos[0],mouse_region_y=pos[1]))
                    u1, v1 = space_sdf.hit_uv(bpy.context,r,start,settings.mask)
                    u2, v2 = space_sdf.hit_uv(bpy.context,r,end,settings.mask)
                    row = round(v1 * 2048 - .5)
                    assert narrow.bits[row,int(u1*2048)+1:int(u2*2048)].all(), (start,end,u1,u2)
                    # The same brush centre must cover more texels at the larger radius.
                    sizes = []
                    for radius in (4, 16):
                        settings.radius = radius
                        dab = SimpleNamespace(mask=settings.mask, radius=settings.radius, white=True,
                                              previous=None, previous_uv=None, bits=np.zeros((256,256),dtype=bool),
                                              coverage=sdf_field.decode(settings.mask.coverage,256))
                        space_sdf.SDF_OT_stroke.dab(dab, bpy.context, SimpleNamespace(
                            mouse_region_x=xy[0], mouse_region_y=xy[1]))
                        sizes.append(dab.bits.sum())
                    assert 14 < sizes[1] / sizes[0] < 18, sizes
                    assert bpy.ops.sdf.stroke('INVOKE_DEFAULT') == {'RUNNING_MODAL'}
                    stroke = space_sdf._stroke
                    stroke.previous = None
                    stroke.dab(bpy.context, SimpleNamespace(mouse_region_x=xy[0],mouse_region_y=xy[1]))
                phase = 'drag3d'
            elif phase == 'drag3d':
                area = areas['3d']
                r = region(area)
                with bpy.context.temp_override(area=area, region=r):
                    x,y = point('3d',r)
                    release = SimpleNamespace(mouse_region_x=x,mouse_region_y=y,type='LEFTMOUSE',value='RELEASE')
                    assert space_sdf._stroke.modal(bpy.context,release) == {'FINISHED'}
                    assert sdf_field.decode(settings.mask.keys[1].bitmap, 256)[128, 192]
                phase = 'white'
            elif phase == 'white':
                assert all(samples['white', k] - samples['black', k] > .65 for k in areas), samples
                assert np.count_nonzero(abs(patches['drag3d','3d']-patches['white','3d']).max(axis=-1)>.3)>12
                area = areas['uv']
                r = region(area)
                with bpy.context.temp_override(area=area,region=r):
                    settings.ink = 'BLACK'
                    assert bpy.ops.sdf.stroke('INVOKE_DEFAULT') == {'RUNNING_MODAL'}
                    stroke = space_sdf._stroke
                    stroke.previous = stroke.previous_uv = None
                    x,y = point('uv',r)
                    stroke.dab(bpy.context, SimpleNamespace(mouse_region_x=x,mouse_region_y=y))
                phase = 'draguv'
            elif phase == 'draguv':
                area = areas['uv']
                r = region(area)
                with bpy.context.temp_override(area=area,region=r):
                    x,y = point('uv',r)
                    event = SimpleNamespace(mouse_region_x=x,mouse_region_y=y,type='LEFTMOUSE',value='RELEASE')
                    assert space_sdf._stroke.modal(bpy.context,event) == {'FINISHED'}
                    assert not sdf_field.decode(settings.mask.keys[1].bitmap,256)[128,192]
                phase = 'erased'
            elif phase == 'erased':
                assert np.count_nonzero(abs(patches['draguv','uv']-patches['erased','uv']).max(axis=-1)>.3)>12
                settings.value = 0
                phase = 'previous'
            elif phase == 'previous':
                assert all(samples['previous', k] < .1 for k in areas), samples
                bpy.ops.object.mode_set(mode='OBJECT')
                assert not bpy.ops.sdf.key.poll()
                for area in areas.values():
                    with bpy.context.temp_override(area=area, region=region(area)):
                        assert not bpy.ops.sdf.stroke.poll()
                phase = 'off'
            else:
                assert samples['off', '3d'] > samples['previous', '3d'] + .1, samples
                print("SDF workspace and multi-mesh viewport paint passed", samples, flush=True)
                bpy.ops.wm.quit_blender()
                return
        for area in areas.values():
            area.tag_redraw()
        return .35
    except Exception:
        traceback.print_exc()
        os._exit(1)


bpy.app.timers.register(tick, first_interval=1)
