# SPDX-License-Identifier: GPL-2.0-or-later
"""Validate the installed icon resources, not the offline rasterizer."""
import time
import runpy
from pathlib import Path
from bl_operators import uv_tools

root=Path(__file__).resolve().parents[2]
names=runpy.run_path(str(root/'tools/utils/generate_uv_tools_icons.py'))['ICONS']
started=time.perf_counter()
for name in names:
    icon=uv_tools.icon_id(name)
    assert icon==uv_tools.icon_id(name), 'Preview was not cached: '+name
    preview=uv_tools._icons[name]
    assert tuple(preview.image_size)==(64,64)
    assert tuple(preview.icon_size)==(64,64)
    assert any(preview.image_pixels_float[3::4]), 'Empty artwork: '+name
print('PASS: {} installed icon resources loaded and cached in {:.4f}s'.format(len(names),time.perf_counter()-started))
