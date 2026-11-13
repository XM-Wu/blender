# SPDX-License-Identifier: GPL-2.0-or-later
"""XM must override addon circle-selection C without removing other presets' keys."""
import bpy
from pathlib import Path
from bl_operators.uv_tools import _sync_xm_seam_keymap, _restore_xm_addon_keys

preset=Path(__file__).resolve().parents[2]/'scripts/presets/keyconfig/XM_KeyMapping.py'
bpy.utils.keyconfig_set(str(preset))
configs=bpy.context.window_manager.keyconfigs
assert configs.active.name=='XM_KeyMapping'
keymap=configs.addon.keymaps.new(name='Mesh',space_type='EMPTY')
circle=keymap.keymap_items.new('mesh.select_circle_xray','C','PRESS')
shift=keymap.keymap_items.new('mesh.select_circle_xray','C','PRESS',shift=True)
box=keymap.keymap_items.new('mesh.select_box_xray','B','PRESS')
ctrl=keymap.keymap_items.new('mesh.select_circle_xray','C','PRESS',ctrl=True)
_sync_xm_seam_keymap()
assert not circle.active and not shift.active and box.active and ctrl.active
configs.active=configs.default
_sync_xm_seam_keymap()
assert circle.active and shift.active and box.active and ctrl.active
configs.active=configs.get('XM_KeyMapping')
_sync_xm_seam_keymap()
assert not circle.active
_restore_xm_addon_keys()
assert circle.active
print('PASS: XM addon C conflict suppressed; modifiers and other presets preserved')
