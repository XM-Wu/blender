# SPDX-License-Identifier: GPL-2.0-or-later
"""Test surface-stable UV remapping, cancellation on conflicts, persistence and undo."""
from pathlib import Path
from tempfile import TemporaryDirectory
import bpy
import numpy as np
from bl_ui import sdf_field as field, space_sdf as sdf

bpy.ops.mesh.primitive_plane_add()
obj=bpy.context.object
mesh=obj.data
layer=mesh.uv_layers.active
for v in layer.uv:
    u,w=v.vector[:]
    v.vector=(.125+u*.25,.25+w*.25)
bpy.ops.object.mode_set(mode='SDF_PAINT')
bpy.ops.sdf.add_mask(scope='MESHES',resolution='256')
s=bpy.context.scene.sdf_editor
m=s.mask
cov=field.decode(m.coverage,256)
yy,xx=np.ogrid[:256,:256]
images=[]
for t in (0,.45,1):
    s.value=t
    bpy.ops.sdf.key()
    bits=cov & ((xx-40)**2+(yy-84)**2 < (9+20*t)**2)
    m.keys[-1].bitmap=field.encode(bits)
    images.append(bits)
assert m.sources[0].uv_snapshot
with TemporaryDirectory() as tmp:
    path=str(Path(tmp)/'remap.blend')
    bpy.ops.wm.save_as_mainfile(filepath=path)
    bpy.ops.wm.open_mainfile(filepath=path)
    s=bpy.context.scene.sdf_editor
    m=s.mask
    obj=m.sources[0].object
    mesh=obj.data
    old=mesh.uv_layers.get(m.sources[0].uv_name)
    target=mesh.uv_layers.new(name='Rotated')
    for v,orig in zip(target.uv,old.uv):
        u,w=orig.vector
        v.vector=(1-w,u)
    mesh.uv_layers.active=target
    bpy.ops.ed.undo_push(message='Before remapping')
    assert bpy.ops.sdf.rematch_uv(target='ACTIVE')=={'FINISHED'}
    assert m.sources[0].uv_name=='Rotated'
    assert np.array_equal(field.decode(m.coverage,256),np.rot90(cov,-1))
    for key, original in zip(m.keys,images):
        assert np.array_equal(field.decode(key.bitmap,256),np.rot90(original,-1))
    assert len(s.masks)==1 and [round(k.value,2) for k in m.keys]==[0,.45,1]
    # Mutating the recorded layer in place also uses the previous snapshot.
    for v in target.uv:
        v.vector.x=1-v.vector.x
    assert bpy.ops.sdf.rematch_uv()=={'FINISHED'}
    for key, original in zip(m.keys,images):
        assert np.array_equal(field.decode(key.bitmap,256),np.rot90(original,-1)[:,::-1])
    snapshot=(m.coverage,sdf.key_images(m),m.sources[0].uv_snapshot)
    assert bpy.ops.sdf.rematch_uv(target='NAMED',uv_name='missing')=={'CANCELLED'}
    assert snapshot==(m.coverage,sdf.key_images(m),m.sources[0].uv_snapshot)
    bpy.ops.ed.undo_push(message='After remapping')
    bpy.ops.ed.undo()
    m=bpy.context.scene.sdf_editor.mask
    assert np.array_equal(field.decode(m.coverage,256),cov)
    for key, original in zip(m.keys,images):
        assert np.array_equal(field.decode(key.bitmap,256),original)
    # A legacy footprint alone cannot identify a mirror within the same bounds.
    src = m.sources[0]
    layer = src.object.data.uv_layers[src.uv_name]
    for uv in layer.uv:
        uv.vector.x = .5 - uv.vector.x
    src.uv_snapshot = ""
    sdf.bind_legacy_uvs(None)
    assert src.uv_snapshot and src.inferred_snapshot
    assert bpy.ops.sdf.rematch_uv() == {'CANCELLED'}
    assert src.inferred_snapshot
    from shutil import copyfile
    reference = copyfile(path, Path(tmp) / 'original.blend')
    assert bpy.ops.sdf.rematch_uv(reference_file=str(reference)) == {'FINISHED'}
    assert not src.inferred_snapshot
    for key, original in zip(m.keys, images):
        assert np.array_equal(field.decode(key.bitmap,256)[:,32:96], original[:,32:96][:,::-1])
print('SDF remap passed: secondary layer, rotation, same-layer mirror, three keys, save/load, undo')

# Overlaps with different painted values must fail atomically unless the user
# explicitly requests the white union. The target is the same on both meshes.
bpy.ops.object.mode_set(mode='OBJECT')
bpy.ops.object.select_all(action='DESELECT')
pair=[]
for i in (0,1):
    bpy.ops.mesh.primitive_plane_add(location=(3*i,0,0))
    obj=bpy.context.object
    mesh=obj.data
    old=mesh.uv_layers.active
    for uv in old.uv:
        uv.vector.x=(uv.vector.x+i)/2
    target=mesh.uv_layers.new(name='Stacked')
    for v,o in zip(target.uv,old.uv):
        v.vector=(o.vector.x-i*.5,o.vector.y)
    mesh.uv_layers.active=old
    pair.append(obj)
for obj in pair:
    obj.select_set(True)
bpy.ops.object.mode_set(mode='SDF_PAINT')
bpy.ops.sdf.add_mask(scope='MESHES',resolution='256')
s=bpy.context.scene.sdf_editor
m=s.mask
bpy.ops.sdf.key()
s.ink='WHITE'
assert bpy.ops.sdf.fill(mode='HIGH_U')=={'FINISHED'}
before=(m.coverage,m.keys[0].bitmap,tuple(src.uv_snapshot for src in m.sources))
assert bpy.ops.sdf.rematch_uv(target='NAMED',uv_name='Stacked')=={'CANCELLED'}
assert before==(m.coverage,m.keys[0].bitmap,tuple(src.uv_snapshot for src in m.sources))
assert bpy.ops.sdf.rematch_uv(target='NAMED',uv_name='Stacked',merge_white=True)=={'FINISHED'}
assert np.array_equal(field.decode(m.coverage,256),field.decode(m.keys[0].bitmap,256))
print('SDF multi-mesh overlap conflict and white union passed')
