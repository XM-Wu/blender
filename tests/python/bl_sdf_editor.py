# SPDX-License-Identifier: GPL-2.0-or-later
"""Run with blender -b -noaudio --factory-startup --python-exit-code 1 -P this_file."""
import bpy
import numpy as np
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import zlib

from bl_ui import space_sdf as sdf
from bl_ui import sdf_field as field


def make_quad(name, x, offset):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    obj.location.x = x
    mesh.uv_layers.new(name="UV0")
    layer = mesh.uv_layers.new(name=name + "_face")
    mesh.uv_layers.active = layer
    for i, (u, v) in enumerate(((0, 0), (.25, 0), (.25, .5), (0, .5))):
        layer.uv[i].vector = u + offset, v + .25
    return obj


bpy.ops.object.select_all(action='DESELECT')
a, b = make_quad("Left", 0, 0), make_quad("Right", 2, .75)
for obj in (a, b):
    obj.select_set(True)
bpy.context.view_layer.objects.active = a
bpy.context.scene.tool_settings.use_uv_select_sync = True
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.select_all(action='SELECT')
assert not bpy.ops.sdf.add_mask.poll()
bpy.ops.object.mode_set(mode='SDF_PAINT')
assert bpy.context.mode == 'PAINT_SDF'
assert {obj.mode for obj in (a, b)} == {'SDF_PAINT'}
assert bpy.ops.sdf.add_mask(resolution="256", name="Face", scope='MESHES') == {'FINISHED'}
s = bpy.context.scene.sdf_editor
m = s.mask
assert len(m.sources) == 2
assert {x.uv_name for x in m.sources} == {'Left_face', 'Right_face'}
coverage = field.decode(m.coverage, m.size)
assert coverage.sum() == 2 * 64 * 128
assert bpy.ops.sdf.key() == {'FINISHED'}
assert not field.decode(m.keys[0].bitmap, m.size).any()
# Edit the original mask in place, including after switching the active UV.
# Removed islands keep their key pixels for a later re-selection.
saved_ink = field.encode(coverage)
m.keys[0].bitmap = saved_ink
area = next(area for area in bpy.context.screen.areas if area.type == 'VIEW_3D')
a.data.uv_layers.active_index = 0
with bpy.context.temp_override(area=area):
    assert bpy.ops.sdf.edit_islands() == {'FINISHED'}
    picker = sdf.active_picker(bpy.context)
    assert len(picker.selected) == 2
    right, = picker.hits((.875,.5))
    bpy.ops.sdf.island_action(action='TOGGLE', index=right)
    bpy.ops.sdf.island_action(action='CANCEL')
    assert np.array_equal(field.decode(m.coverage, m.size),coverage)
    bpy.ops.ed.undo_push(message="Before editing islands")
    bpy.ops.sdf.edit_islands()
    right, = sdf.active_picker(bpy.context).hits((.875,.5))
    bpy.ops.sdf.island_action(action='TOGGLE', index=right)
    bpy.ops.sdf.island_action(action='CONFIRM')
    assert len(s.masks) == len(m.keys) == len(m.sources) == 1
    assert m.sources[0].uv_name == 'Left_face' and m.keys[0].bitmap == saved_ink
    remaining = field.decode(m.coverage,m.size)
    assert remaining.sum() == 64*128 and not remaining[:,192:].any()
    assert np.array_equal(field.preview(sdf.key_images(m),0,m.coverage,m.size),remaining)
    # White-to-black transitions outside the edited coverage do not affect export.
    field.thresholds([(0,saved_ink),(1,field.encode(remaining))],m.coverage,m.size)
    bpy.ops.sdf.edit_islands()
    picker = sdf.active_picker(bpy.context)
    assert len(picker.selected) == 1
    bpy.ops.sdf.island_action(action='TOGGLE',index=picker.hits((.875,.5))[0])
    bpy.ops.sdf.island_action(action='CONFIRM')
    assert np.array_equal(field.preview(sdf.key_images(m),0,m.coverage,m.size),coverage)
m.keys[0].bitmap = field.encode(np.zeros_like(coverage))

bpy.ops.object.mode_set(mode='OBJECT')
assert all(obj.mode == 'OBJECT' for obj in (a, b))
assert not any(op.poll() for op in (bpy.ops.sdf.add_mask, bpy.ops.sdf.key, bpy.ops.sdf.jump,
                                    bpy.ops.sdf.remove_mask, bpy.ops.sdf.export))
bpy.ops.object.mode_set(mode='SDF_PAINT')
for obj in (a, b):
    obj.data.uv_layers.active_index = 0
dg = bpy.context.evaluated_depsgraph_get()
faces, positions, uvs = sdf.geometry(m.sources[0], dg)
assert len(faces) == 2 and positions.shape == (2, 3, 3)
# The inactive member of a multi-object paint session also needs evaluated face mapping.
modifier = b.modifiers.new("Paint surface", 'SUBSURF')
modifier.levels = 1
bpy.context.view_layer.update()
other = next(src for src in m.sources if src.object == b)
faces, positions, uvs = sdf.geometry(other, bpy.context.evaluated_depsgraph_get())
assert len(set(faces)) == 4 and positions.shape == (8, 3, 3)
s.value = .5
bpy.ops.sdf.key()
ink = np.zeros_like(coverage)
field.stamp(ink, coverage, (.245, .5), 18, True)
assert ink.any() and not (ink & ~coverage).any()
m.keys[1].bitmap = field.encode(ink)
s.value = 1
bpy.ops.sdf.key()
assert np.array_equal(field.decode(m.keys[2].bitmap, m.size), ink)
ink2 = ink.copy()
field.stamp(ink2, coverage, (.245, .5), 38, True)
m.keys[2].bitmap = field.encode(ink2)
images = sdf.key_images(m)
t = field.thresholds(images, m.coverage, m.size)
for val in (.14, .42, .65, .84):
    expected = (t <= val) & coverage
    assert np.array_equal(expected, field.preview(images, val, m.coverage, m.size))
flat = [(0, images[0][1]), (1, field.encode(coverage))]
flat_preview = field.preview(flat, .5, m.coverage, m.size)
assert 0 < flat_preview.sum() < coverage.sum()
assert np.array_equal(flat_preview, (field.thresholds(flat, m.coverage, m.size) <= .5) & coverage)
try:
    field.thresholds([(0, images[-1][1]), (1, images[0][1])], m.coverage, m.size)
except ValueError:
    pass
else:
    raise AssertionError("Non-monotone keys must be reported")

area = next(a for a in bpy.context.screen.areas if a.type == 'VIEW_3D')
area.ui_type = 'SDF'
assert area.ui_type == area.spaces.active.mode == 'SDF'
area.ui_type = 'UV'
area.ui_type = 'IMAGE_EDITOR'
assert area.spaces.active.mode == 'VIEW'
area.ui_type = 'SDF'

with TemporaryDirectory() as tmp:
    path = Path(tmp) / "face.png"
    for invalid in ("", tmp, str(Path(tmp) / "missing" / "face.png")):
        try:
            bpy.ops.sdf.export('EXEC_DEFAULT', filepath=invalid)
        except RuntimeError as error:
            assert "Choose a PNG filename" in str(error) or "Export folder does not exist" in str(error)
        else:
            raise AssertionError(f"Accepted invalid export destination {invalid!r}")
    assert not list(Path(tmp).iterdir())
    assert bpy.ops.sdf.export(filepath=str(path)) == {'FINISHED'}
    png = path.read_bytes()
    assert struct.unpack(">2I5B", png[16:29]) == (256, 256, 16, 0, 0, 0, 0)
    length, = struct.unpack(">I", png[33:37])
    raw = zlib.decompress(png[41:41 + length])
    for y in range(256):
        row = raw[y * 513:(y + 1) * 513]
        assert row[0] == 0
        assert np.array_equal(np.frombuffer(row[1:], dtype=">u2"), np.rint(t[255-y] * 65535))
    blend = str(Path(tmp) / "face.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    bpy.ops.wm.open_mainfile(filepath=blend)
    s = bpy.context.scene.sdf_editor
    assert len(s.mask.sources) == 2 and sdf.key_images(s.mask) == images
    bpy.ops.object.mode_set(mode="SDF_PAINT")
    bpy.ops.sdf.remove_mask()
    assert not s.masks

print("SDF editor regression passed")
