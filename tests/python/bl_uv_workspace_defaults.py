# SPDX-License-Identifier: GPL-2.0-or-later
"""Run --factory-startup --python this_file; optional -- screenshot.png (graphical)."""
import bpy
import sys
import traceback


def check():
    workspace=bpy.data.workspaces['UV Editing']
    screen=workspace.screens[0]
    view=next(a for a in screen.areas if a.type=='VIEW_3D')
    uv=next(a for a in screen.areas if a.type=='IMAGE_EDITOR')
    properties=[a for a in screen.areas if a.type=='PROPERTIES']
    tools=min(properties,key=lambda a:a.y)
    assert view.x<uv.x<tools.x,[(a.type,a.x,a.y) for a in screen.areas]
    assert uv.ui_type=='UV'
    assert tools.spaces.active.show_properties_uv_tools
    assert not tools.spaces.active.show_properties_scene
    # Available Properties contexts are computed during UI drawing, not background startup.
    if not bpy.app.background:
        assert tools.spaces.active.context=='UV_TOOLS',tools.spaces.active.context
    assert any(a.type=='OUTLINER' for a in screen.areas)
    # The ordinary Layout workspace must retain normal property tabs.
    for area in bpy.data.workspaces['Layout'].screens[0].areas:
        if area.type=='PROPERTIES':
            assert area.spaces.active.show_properties_render
    print('PASS: left 3D, center UV, right UV Tools, Outliner retained',flush=True)


if bpy.app.background:
    check()
    bpy.ops.wm.read_factory_settings(use_empty=False)
    check()
else:
    def start():
        bpy.context.window.workspace=bpy.data.workspaces['UV Editing']
        bpy.app.timers.register(finish,first_interval=1)
    def finish():
        try:
            check()
            if '--' in sys.argv:
                bpy.ops.screen.screenshot(filepath=sys.argv[sys.argv.index('--')+1])
        except Exception:
            traceback.print_exc()
        bpy.ops.wm.quit_blender()
    bpy.app.timers.register(start,first_interval=1)
