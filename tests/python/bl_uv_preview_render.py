# SPDX-License-Identifier: GPL-2.0-or-later
"""Run in a graphical Blender with --factory-startup --python this_file -- output_dir.

Uses viewport framebuffer readback, not desktop/UI automation. The process exits
after capturing baseline, checker, restored and distortion frames in both editors.
"""
import bpy
import gpu
import json
import sys
import traceback
import time
from pathlib import Path
import numpy as np
from mathutils import Quaternion

OUT = Path(sys.argv[sys.argv.index('--') + 1])
OUT.mkdir(parents=True, exist_ok=True)
captures = {}
handles = []
phase = None
ready_at = 0
areas = {}
baseline_state = None


def state():
    area = areas['uv']
    region = next(r for r in area.regions if r.type == 'WINDOW')
    space = area.spaces.active
    return {
        'view': [tuple(region.view2d.region_to_view(x, y)) for x, y in ((0, 0), (100, 100))],
        'image': space.image.name if space.image else None,
        'images': sorted(i.name for i in bpy.data.images if not i.name.startswith('QA capture')),
        'materials': sorted(m.name for m in bpy.data.materials),
        'uv': [tuple(v.vector) for v in bpy.context.object.data.uv_layers.active.uv],
        'area': (area.width, area.height),
    }


def capture(kind):
    if (phase is None or time.monotonic() < ready_at or
            (phase, kind) in captures or bpy.context.area != areas.get(kind)):
        return
    width, height = bpy.context.region.width, bpy.context.region.height
    buffer = gpu.state.active_framebuffer_get().read_color(0, 0, width, height, 4, 0, 'FLOAT')
    captures[phase, kind] = np.array(buffer).reshape(height, width, 4).copy()


def redraw():
    for area in areas.values():
        area.tag_redraw()


def fail():
    (OUT / 'result.json').write_text(json.dumps({'error': traceback.format_exc()}))
    traceback.print_exc()
    bpy.ops.wm.quit_blender()


def setup():
    try:
        bpy.context.window.workspace = bpy.data.workspaces['UV Editing']
        bpy.app.timers.register(prepare, first_interval=0.5)
    except Exception:
        fail()


def prepare():
    global baseline_state
    try:
        if bpy.context.object and bpy.context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete(use_global=False)
        bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24)
        obj = bpy.context.object
        for v in obj.data.vertices:
            v.co.x *= 1.3
        for face in obj.data.polygons:
            face.use_smooth = True
        for area in bpy.context.screen.areas:
            if area.type == 'VIEW_3D':
                areas['3d'] = area
                space = area.spaces.active
                space.region_3d.view_distance = 4.5
                space.region_3d.view_location = (0, 0, 0)
                space.region_3d.view_rotation = Quaternion((0.88, 0.3, 0.15, 0.3)).normalized()
                space.region_3d.view_perspective = 'ORTHO'
                space.overlay.show_floor = False
                space.overlay.show_axis_x = space.overlay.show_axis_y = False
            elif area.type == 'IMAGE_EDITOR':
                areas['uv'] = area
                area.spaces.active.image = (None if '--no-image' in sys.argv else
                                           bpy.data.images.new('Existing non-square image', width=512, height=256))
                with bpy.context.temp_override(area=area, region=next(r for r in area.regions if r.type == 'WINDOW')):
                    bpy.ops.image.view_all(fit_view=True)
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='DESELECT')
        bpy.context.scene.tool_settings.use_uv_select_sync = True
        bpy.context.scene.uv_tools_checker = False
        bpy.context.scene.uv_tools_distortion = False
        for cls, kind in ((bpy.types.SpaceImageEditor, 'uv'), (bpy.types.SpaceView3D, '3d')):
            handles.append((cls, cls.draw_handler_add(capture, (kind,), 'WINDOW', 'POST_PIXEL')))
        baseline_state = state()
        bpy.app.timers.register(next_phase, first_interval=1.0)
    except Exception:
        fail()


def next_phase():
    global phase, baseline_state, ready_at
    try:
        if phase is not None and not all((phase, kind) in captures for kind in areas):
            redraw()
            return 0.5
        if phase == 'perspective_checker':
            finish()
            return
        phase = {None: 'baseline', 'baseline': 'checker', 'checker': 'restored',
                 'restored': 'distortion', 'distortion': 'selected_baseline',
                 'selected_baseline': 'selected_distortion',
                 'selected_distortion': 'perspective_checker'}[phase]
        ready_at = time.monotonic() + 0.75
        if phase == 'baseline':
            baseline_state = state()
        if phase in {'checker', 'restored'}:
            with bpy.context.temp_override(area=areas['uv']):
                bpy.ops.uv_tools.checker()
            current = state()
            changes = {key: (baseline_state[key], current[key]) for key in current
                       if baseline_state[key] != current[key]}
            assert not changes, 'Checker modified state: ' + repr(changes)
        elif phase == 'distortion':
            bpy.context.scene.uv_tools_distortion = True
        elif phase == 'selected_baseline':
            bpy.context.scene.uv_tools_distortion = False
            import bmesh
            bm = bmesh.from_edit_mesh(bpy.context.object.data)
            for vert in bm.verts:
                vert.select_set(vert.co.z > 0.25)
            bm.select_flush_mode()
            bmesh.update_edit_mesh(bpy.context.object.data, loop_triangles=False)
        elif phase == 'selected_distortion':
            bpy.context.scene.uv_tools_distortion = True
        elif phase == 'perspective_checker':
            bpy.context.scene.uv_tools_checker = True
            areas['3d'].spaces.active.region_3d.view_perspective = 'PERSP'
            areas['3d'].spaces.active.region_3d.view_distance = 3.5
        redraw()
        return 2.0
    except Exception:
        fail()


def finish():
    result = {'state_preserved': state() == baseline_state, 'captures': {}}
    for (name, kind), pixels in captures.items():
        h, w, _ = pixels.shape
        image = bpy.data.images.new('QA capture', width=w, height=h, alpha=True)
        image.pixels.foreach_set(pixels.ravel())
        image.filepath_raw = str(OUT / (name + '-' + kind + '.png'))
        image.file_format = 'PNG'
        image.save()
        bpy.data.images.remove(image)
        np.save(OUT / (name + '-' + kind + '.npy'), pixels)
    for kind in areas:
        base = captures['baseline', kind][..., :3]
        checker = captures['checker', kind][..., :3]
        restored = captures['restored', kind][..., :3]
        distortion = captures['distortion', kind][..., :3]
        result['captures'][kind] = {
            'checker_changed_pixels': int(np.count_nonzero(np.max(abs(checker-base), axis=2) > 0.05)),
            'restored_changed_pixels': int(np.count_nonzero(np.max(abs(restored-base), axis=2) > 0.05)),
            'distortion_changed_pixels': int(np.count_nonzero(np.max(abs(distortion-base), axis=2) > 0.05)),
        }
        assert result['captures'][kind]['checker_changed_pixels'] > 1000
        assert result['captures'][kind]['restored_changed_pixels'] < 100
        assert result['captures'][kind]['distortion_changed_pixels'] > 1000
    base = captures['baseline', '3d']
    checker = captures['checker', '3d']
    solid_surface = (base[..., 3] < 0.01) & (checker[..., 3] > 0.99)
    contrast = np.quantile(checker[..., 0][solid_surface], [0.1, 0.9])
    result['checker_contrast'] = contrast.tolist()
    assert contrast[1] - contrast[0] > 0.3, 'Checker must contain visible dark and light cells'
    selected = captures['selected_baseline', '3d']
    diagnostic = captures['selected_distortion', '3d']
    # Compare fully covered line/vertex pixels. Antialiased fringes must change
    # when the underlying surface changes, because they blend with that surface.
    orange = ((selected[..., 0] > 0.8) & (selected[..., 1] > 0.15) &
              (selected[..., 2] < 0.1) & (selected[..., 3] > 0.999))
    same = np.max(abs(diagnostic[..., :3] - selected[..., :3]), axis=2) < 0.03
    result['selected_edge_pixels'] = int(orange.sum())
    result['selected_edge_preservation'] = float(same[orange].mean())
    assert orange.sum() > 100
    assert result['selected_edge_preservation'] > 0.99, 'Native selected edges must remain visible'
    result['passed'] = True
    (OUT / 'result.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)
    for cls, handle in handles:
        cls.draw_handler_remove(handle, 'WINDOW')
    # Exercise the window-close route used by the title-bar X, not just quit exec.
    # Only this disposable test process disables the unsaved-file confirmation.
    bpy.context.preferences.view.use_save_prompt = False
    bpy.context.preferences.use_preferences_save = False
    bpy.ops.wm.window_close()


bpy.app.timers.register(setup, first_interval=1.0)
