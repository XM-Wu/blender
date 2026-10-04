# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Industry Compatible keymap for this fork.

D is left free so it can be held to edit the transform gizmo pivot.
Ctrl+Shift dragging a gizmo handle uses the same left-mouse gizmo drag.
On the Move tool, that drag vertex-slides in mesh edit mode.
Shift and middle mouse shrink/fattens along normals in mesh edit mode.
"""

import os
import bpy


# ------------------------------------------------------------------------------
# Keymap

DIRNAME, FILENAME = os.path.split(__file__)
IDNAME = os.path.splitext(FILENAME)[0]


def update_fn(_self, _context):
    load()


industry_compatible = bpy.utils.execfile(os.path.join(DIRNAME, "keymap_data", "industry_compatible_data.py"))


def _append_item(keyconfig_data, km_name, item):
    for name, _args, content in keyconfig_data:
        if name == km_name:
            content["items"].append(item)
            return
    raise RuntimeError("keymap not found: " + km_name)


def _add_xm_items(keyconfig_data):
    # Gizmo drags ignore unspecified modifiers, so Ctrl+Shift would miss the handle.
    # The Move tool then turns that drag into vertex slide. See gizmo_move_vert_slide.
    for km_name in ("Generic Gizmo Maybe Drag", "Generic Gizmo Drag"):
        _append_item(
            keyconfig_data,
            km_name,
            ("gizmogroup.gizmo_tweak",
             {"type": 'LEFTMOUSE', "value": 'CLICK_DRAG', "ctrl": True, "shift": True},
             None),
        )
    # Quick shrink/fatten. Release confirms, same as the Shrink/Fatten tool.
    _append_item(
        keyconfig_data,
        "Mesh",
        ("transform.shrink_fatten",
         {"type": 'MIDDLEMOUSE', "value": 'PRESS', "shift": True},
         {"properties": [("release_confirm", True)]}),
    )


def load():
    from sys import platform
    from bl_keymap_utils.io import keyconfig_init_from_data

    prefs = bpy.context.preferences

    kc = bpy.context.window_manager.keyconfigs.new(IDNAME)
    params = industry_compatible.Params(
        use_mouse_emulate_3_button=prefs.inputs.use_mouse_emulate_3_button,
        use_annotate_tool=False,
    )
    keyconfig_data = industry_compatible.generate_keymaps(params)
    _add_xm_items(keyconfig_data)

    if platform == "darwin":
        from bl_keymap_utils.platform_helpers import keyconfig_data_oskey_from_ctrl_for_macos
        keyconfig_data = keyconfig_data_oskey_from_ctrl_for_macos(keyconfig_data)

    keyconfig_init_from_data(kc, keyconfig_data)


if __name__ == "__main__":
    load()
