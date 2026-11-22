# SPDX-License-Identifier: GPL-2.0-or-later
"""Run with factory-startup Blender in background mode."""
import bpy
import numpy as np
from bl_ui import sdf_field as field

bpy.ops.mesh.primitive_plane_add(location=(7, 2, 1))
obj = bpy.context.object
obj.scale.x = -2
layer = obj.data.uv_layers.new(name='Face')
for item, original in zip(layer.uv, obj.data.uv_layers[0].uv):
    u, v = original.vector
    item.vector = (.8 - .6 * u, .2 + .6 * v)
bpy.ops.object.mode_set(mode='SDF_PAINT')
bpy.ops.sdf.add_mask(resolution='256', uv_name='Face')
assert bpy.context.scene.sdf_editor.auto_key
assert not bpy.context.scene.sdf_editor.mask.keys
assert bpy.ops.sdf.fill(mode='BUCKET', uv=(0, 0)) == {'CANCELLED'}
assert not bpy.context.scene.sdf_editor.mask.keys
assert bpy.ops.sdf.fill(mode='HIGH_U') == {'FINISHED'}
assert len(bpy.context.scene.sdf_editor.mask.keys) == 1
bpy.context.scene.sdf_editor.ink = 'BLACK'
assert bpy.ops.sdf.fill(mode='HIGH_U') == {'FINISHED'}
s = bpy.context.scene.sdf_editor
m = s.mask
cov = field.decode(m.coverage, 256)
assert cov.any() and not cov.all()
initial = m.keys[0].bitmap
s.value = .75
bpy.ops.sdf.key()
key = m.keys[1]
left = np.arange(256) < 128
for mode, expected in [('NEG_X', ~left), ('POS_X', left), ('LOW_U', left), ('HIGH_U', ~left), ('ALL', True)]:
    key.bitmap = initial
    s.ink = 'WHITE'
    assert bpy.ops.sdf.fill(mode=mode) == {'FINISHED'}
    assert np.array_equal(field.decode(key.bitmap, 256), cov & expected), mode
    s.ink = 'BLACK'
    assert bpy.ops.sdf.fill(mode=mode) == {'FINISHED'}
    assert key.bitmap == initial
# A painted vertical barrier splits one continuous island into two black regions.
seed = np.zeros_like(cov)
seed[:, 127:129] = cov[:, 127:129]
key.bitmap = field.encode(seed)
s.ink = 'WHITE'
assert bpy.ops.sdf.fill(mode='BUCKET', uv=(.3, .5)) == {'FINISHED'}
assert np.array_equal(field.decode(key.bitmap, 256), seed | (cov & left))
assert bpy.ops.sdf.fill(mode='BUCKET', uv=(.01, .01)) == {'CANCELLED'}
assert m.keys[0].bitmap == initial
s.value = .3
assert s.auto_key and bpy.ops.sdf.fill.poll()
keys_before = [(entry.value, entry.bitmap) for entry in m.keys]
base = field.preview(keys_before, s.value, m.coverage, m.size)
assert bpy.ops.sdf.fill(mode='HIGH_U') == {'FINISHED'}
assert len(m.keys) == 3 and [(entry.value, entry.bitmap) for entry in m.keys][:2] == keys_before
assert np.array_equal(field.decode(m.keys[2].bitmap, m.size), base | (cov & ~left))
assert bpy.ops.sdf.fill(mode='HIGH_U') == {'CANCELLED'} and len(m.keys) == 3
s.value = .4
s.auto_key = False
assert not bpy.ops.sdf.fill.poll()
print("SDF connected, full-mask, mirrored UV and local-X fills passed")
