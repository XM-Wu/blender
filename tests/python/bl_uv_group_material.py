# SPDX-License-Identifier: GPL-2.0-or-later
import bpy
import bmesh
from bl_operators.uv_tools_texel_pack import remove_unused_group_material_slots

bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)


def create(name, groups):
    mesh=bpy.data.meshes.new(name)
    coords=[];faces=[]
    for i in range(len(groups)):
        start=len(coords);coords.extend(((i*2,0,0),(i*2+1,0,0),(i*2,1,0)))
        faces.append((start,start+1,start+2))
    mesh.from_pydata(coords,[],faces)
    mesh.uv_layers.new()
    attr=mesh.attributes.new('uvpack_group','INT','FACE')
    attr.data.foreach_set('value',groups)
    obj=bpy.data.objects.new(name,mesh);bpy.context.collection.objects.link(obj)
    return obj


def material(mesh,index):
    slot=mesh.polygons[index].material_index
    return mesh.materials[slot] if slot<len(mesh.materials) else None


a=create('A',(0,1,2));b=create('B',(1,0));c=create('C',(2,))
instance=bpy.data.objects.new('A linked instance',a.data);bpy.context.collection.objects.link(instance)
b.hide_set(True)
original=bpy.data.materials.new('Original')
a.data.materials.append(original)
a.data.polygons[0].hide=True
existing=bpy.data.materials.new('Hair');existing.diffuse_color=(.2,.4,.6,1)
bpy.context.scene['uvpack_group_names']={'1':'Hair','2':'Cloth'}
before={ob.name:([tuple(v.co) for v in ob.data.vertices],
                 [tuple(v.uv) for v in ob.data.uv_layers.active.data]) for ob in (a,b,c)}
count=len(bpy.data.materials)
assert bpy.ops.uv_tools.group_material(group=1)=={'FINISHED'}
assert len(bpy.data.materials)==count
hair=material(a.data,1)
assert hair==existing and hair.name=='Hair'
assert material(b.data,0)==hair
assert material(a.data,0)==material(a.data,2)==original
assert material(b.data,1) is None, 'Ungrouped faces acquired a group material'
assert b.hide_get()
assert bpy.ops.uv_tools.group_material()=={'FINISHED'}
assert len(bpy.data.materials)==count+1
assert material(a.data,1)==material(b.data,0)
assert material(a.data,2)==material(c.data,0)
assert material(a.data,2).name=='Cloth'
assert material(a.data,0)==original and material(b.data,1) is None
assert [len(o.data.materials) for o in (a,b,c)]==[3,2,1]
assert hair.name in bpy.data.materials, 'Unused material datablocks must not be deleted'
a.select_set(True);bpy.context.view_layer.objects.active=a
bpy.ops.object.mode_set(mode='EDIT')
assert bpy.ops.uv_tools.group_material(group=2,material_mode='REPLACE')=={'FINISHED'}
bm=bmesh.from_edit_mesh(a.data);bm.faces.ensure_lookup_table()
cloth=a.data.materials[bm.faces[2].material_index]
assert cloth==material(c.data,0) and cloth.name=='Cloth'
assert a.data.materials[bm.faces[0].material_index]==original
assert [len(o.data.materials) for o in (a,b,c)]==[3,2,1]
bpy.ops.object.mode_set(mode='OBJECT')
assert before=={ob.name:([tuple(v.co) for v in ob.data.vertices],
                        [tuple(v.uv) for v in ob.data.uv_layers.active.data]) for ob in (a,b,c)}
count=len(bpy.data.materials)
assert bpy.ops.uv_tools.group_material(group=999)=={'CANCELLED'}
assert len(bpy.data.materials)==count
assert tuple(round(v,3) for v in existing.diffuse_color)==(.2,.4,.6,1)
# Reuse is idempotent; replacement keeps the exact name and migrates outside users.
slot_counts=[len(o.data.materials) for o in (a,b,c)]
assert bpy.ops.uv_tools.group_material(group=1)=={'FINISHED'}
assert [len(o.data.materials) for o in (a,b,c)]==slot_counts
external=create('Outside group',(0,))
external.data.materials.append(existing)
old_pointer=existing.as_pointer()
legacy=bpy.data.materials.new('Hair.001')
legacy_name=legacy.name
a.data.materials.append(legacy)
a.data.polygons[1].material_index=len(a.data.materials)-1
assert bpy.ops.uv_tools.group_material(group=1,material_mode='REPLACE')=={'FINISHED'}
replacement=bpy.data.materials['Hair']
assert replacement.as_pointer()!=old_pointer
assert material(a.data,1)==material(b.data,0)==material(external.data,0)==replacement
assert bpy.data.materials.get(legacy_name) is None
# Gaps in the slot list and object-linked overrides on another mesh instance.
d=create('Slot gaps',(0,0))
slot_materials=[bpy.data.materials.new('Slot test '+str(i)) for i in range(4)]
for mat in slot_materials:d.data.materials.append(mat)
d.data.polygons[0].material_index=1
d.data.polygons[1].material_index=3
d.data.polygons[1].hide=True
linked=bpy.data.objects.new('Slot override',d.data);bpy.context.collection.objects.link(linked)
override=bpy.data.materials.new('Object override')
linked.material_slots[3].link='OBJECT';linked.material_slots[3].material=override
linked.active_material_index=3
assert remove_unused_group_material_slots(d.data)==2
assert [face.material_index for face in d.data.polygons]==[0,1]
assert list(d.data.materials)==[slot_materials[1],slot_materials[3]]
assert linked.material_slots[1].link=='OBJECT' and linked.material_slots[1].material==override
assert linked.active_material_index==1
assert all(mat.name in bpy.data.materials for mat in slot_materials)
print('PASS: named new materials, shared meshes, hidden members, edit mode, name collisions and ungrouped face preservation')
