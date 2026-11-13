# SPDX-License-Identifier: GPL-2.0-or-later
"""Regression for additive UV double-click after the first click selects the seed."""
import bpy
import bmesh

screen = bpy.data.workspaces['UV Editing'].screens[0]
area = next(a for a in screen.areas if a.type == 'IMAGE_EDITOR')
region = next(r for r in area.regions if r.type == 'WINDOW')

for sync in (False, True):
    for mode in ('EDGE', 'FACE'):
        if bpy.context.object and bpy.context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete(use_global=False)
        bpy.ops.mesh.primitive_grid_add(x_subdivisions=4, y_subdivisions=4)
        bpy.ops.object.mode_set(mode='EDIT')
        with bpy.context.temp_override(screen=screen, area=area, region=region):
            ts = bpy.context.tool_settings
            ts.use_uv_select_sync = sync
            bpy.ops.uv_tools.select_mode(type=mode)
            if not sync:
                bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.uv.select_all(action='DESELECT')
            bm = bmesh.from_edit_mesh(bpy.context.object.data)
            uv = bm.loops.layers.uv.active
            def selected():
                if sync:
                    elements = bm.edges if mode == 'EDGE' else bm.faces
                    return {i for i, element in enumerate(elements) if element.select}
                return {(fi, li) for fi, face in enumerate(bm.faces)
                        for li, loop in enumerate(face.loops)
                        if (loop.uv_select_edge if mode == 'EDGE' else face.uv_select)}
            # Existing distant selection, followed by the first click of the double-click.
            bpy.ops.uv.select(location=(0.875, 0.875), extend=True)
            bpy.ops.uv.select(location=(0.25, 0.125), extend=True)
            before = selected()
            assert before, (sync, mode, 'empty fixture')
            bpy.ops.uv.select_loop(location=(0.25, 0.125), extend=True, toggle=False)
            after = selected()
            assert before <= after, (sync, mode, 'lost existing selection')
            assert len(after) > len(before), (sync, mode, 'did not extend loop')
            bpy.ops.uv.select_loop(location=(0.25, 0.125), extend=True, toggle=False)
            assert after <= selected(), (sync, mode, 'repeat deselected loop')
            bpy.ops.uv.select_loop(location=(0.25, 0.125), extend=True)
            assert not after <= selected(), (sync, mode, 'legacy toggle no longer deselects')
            print('PASS', 'sync' if sync else 'unsynced', mode)

