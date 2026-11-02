# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Run in a separate GUI process: blender --factory-startup --python this_file.

Selection-buffer tests require a real window/GPU. No user file is loaded or saved.
The process exits after testing all three region selectors on overlapping quads.
"""

import os
import sys
import traceback

import bpy
import bmesh
from mathutils import Quaternion


def run():
    try:
        assert not bpy.app.background, "A GUI/GPU context is required"
        assert not bpy.data.filepath, "Only run with --factory-startup"
        settings = bpy.context.tool_settings
        assert hasattr(settings, 'mesh_select_through'), "Build the new selection code first"
        assert not settings.mesh_select_through
        assert settings.mesh_select_backfacing

        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete(use_global=False)
        mesh = bpy.data.meshes.new('OverlappingQuads')
        mesh.from_pydata(
            [(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0),
             (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1)],
            [], [(0, 1, 2, 3), (7, 6, 5, 4)])
        obj = bpy.data.objects.new('OverlappingQuads', mesh)
        bpy.context.collection.objects.link(obj)
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        obj.scale = (1.5, 0.8, 1.2)
        bpy.ops.object.mode_set(mode='EDIT')
        bm = bmesh.from_edit_mesh(mesh)
        area = next(a for a in bpy.context.screen.areas if a.type == 'VIEW_3D')
        region = next(r for r in area.regions if r.type == 'WINDOW')
        space = area.spaces.active
        space.shading.type = 'SOLID'
        space.shading.show_xray = False
        rv3d = space.region_3d
        rv3d.view_rotation = Quaternion((1, 0, 0, 0))
        rv3d.view_location = (0, 0, 0)
        rv3d.view_distance = 10

        with bpy.context.temp_override(area=area, region=region):
            def select(tool, mode='SET'):
                # Redraw operators clear the current region; re-establish it for each call.
                with bpy.context.temp_override(area=area, region=region):
                    if tool == 'BOX':
                        bpy.ops.view3d.select_box(
                            'EXEC_DEFAULT', xmin=0, ymin=0, xmax=region.width,
                            ymax=region.height, mode=mode, wait_for_input=False)
                    elif tool == 'LASSO':
                        bpy.ops.view3d.select_lasso('EXEC_DEFAULT', mode=mode, path=[
                            {'name': '0', 'time': 0.0, 'loc': (0, 0)}, {'name': '1', 'time': 0.0, 'loc': (region.width, 0)},
                            {'name': '2', 'time': 0.0, 'loc': (region.width, region.height)}, {'name': '3', 'time': 0.0, 'loc': (0, region.height)},
                        ])
                    else:
                        bpy.ops.view3d.select_circle(
                            'EXEC_DEFAULT', x=region.width // 2, y=region.height // 2,
                            radius=max(region.width, region.height), mode=mode,
                            wait_for_input=False)

            for perspective in ('ORTHO', 'PERSP'):
                rv3d.view_perspective = perspective
                for select_mode, elements, per_face in (
                        ('VERT', bm.verts, 4), ('EDGE', bm.edges, 4), ('FACE', bm.faces, 1)):
                    bpy.ops.mesh.select_mode(type=select_mode)
                    bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
                    for tool in ('BOX', 'CIRCLE', 'LASSO'):
                        for through, backfacing, expected in (
                                (False, True, per_face), (True, True, per_face * 2),
                                (True, False, per_face)):
                            settings.mesh_select_through = through
                            settings.mesh_select_backfacing = backfacing
                            bpy.ops.mesh.select_all(action='DESELECT')
                            select(tool)
                            count = sum(e.select for e in elements)
                            assert count == expected, (perspective, select_mode, tool,
                                                       through, backfacing, count, expected)
                            assert not space.shading.show_xray, "Selection changed viewport shading"
                        # Filtering must also preserve ADD/SUB/AND selection semantics.
                        bpy.ops.mesh.select_all(action='SELECT')
                        select(tool, 'SUB')
                        assert sum(e.select for e in elements) == per_face
                        if tool != 'CIRCLE':
                            bpy.ops.mesh.select_all(action='SELECT')
                            select(tool, 'AND')
                            assert sum(e.select for e in elements) == per_face
                        print('PASS', perspective, select_mode, tool, flush=True)
            # Select Through bypasses occlusion, not explicit Hide visibility.
            bpy.ops.mesh.select_mode(type='FACE')
            settings.mesh_select_through = True
            settings.mesh_select_backfacing = True
            bpy.ops.mesh.select_all(action='DESELECT')
            bm.faces.ensure_lookup_table()
            bm.faces[0].hide_set(True)
            bmesh.update_edit_mesh(mesh)
            for tool in ('BOX', 'CIRCLE', 'LASSO'):
                bpy.ops.mesh.select_all(action='DESELECT')
                select(tool)
                assert sum(f.select for f in bm.faces) == 1
                assert not bm.faces[0].select
                print('PASS hidden faces', tool, flush=True)
        print('SELECT_THROUGH_TESTS_OK', flush=True)
        # This dedicated test process must not replace the user's quit.blend recovery.
        os._exit(0)
    except BaseException:
        traceback.print_exc()
        os._exit(1)
    return None


if '--factory-startup' not in sys.argv or bpy.data.filepath:
    raise RuntimeError("Launch a separate Blender process with --factory-startup to run this test")

bpy.context.preferences.view.show_splash = False
bpy.app.timers.register(run, first_interval=1.0)
