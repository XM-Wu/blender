# SPDX-License-Identifier: GPL-2.0-or-later
import bpy
import bmesh
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
from bl_uv_texel_pack import make_islands,island_uvs,set_face_selection,hull,points,hull_distance,area_px,RES

def pack(**options):
    args=dict(resolution_x=RES,resolution_y=RES,precision='EXACT',margin=4,edge_padding=2,
              target='FIXED',udim_tile=1001,rotation='CARDINAL',use_stacking=False)
    args.update(options)
    return bpy.ops.uv.pack_to_others(**args)

def test_reference(hidden=False, crossing=False):
    specs=[dict(size=(1,1),uv=(.75 if crossing else .1,.1),uv_size=(.4,.4)),
           dict(size=(1,1),uv=(.1,.1),uv_size=(.1,.1))]
    obj,ranges=make_islands(specs)
    set_face_selection(obj,ranges[1])
    if hidden:
        bm=bmesh.from_edit_mesh(obj.data);bm.faces.ensure_lookup_table()
        bm.faces[0].hide_set(True);bmesh.update_edit_mesh(obj.data)
    before=island_uvs(obj,ranges)
    assert pack()=={'FINISHED'}
    after=island_uvs(obj,ranges)
    assert after[0]==before[0], 'Reference moved'
    assert abs(abs(area_px(after[1]))-abs(area_px(before[0])))<.02, 'Density mismatch'
    assert hull_distance(hull(points(after[0])),hull(points(after[1])))>=4-.01
    assert all(2/RES-1e-5<=c<=1-2/RES+1e-5 for f in after[1] for uv in f for c in uv)

test_reference()
test_reference(hidden=True)
test_reference(crossing=True)
# Separate mesh datablocks in the same edit session share obstacle space.
obj,ranges=make_islands([dict(size=(1,1),uv=(.1,.1),uv_size=(.4,.4))])
bpy.ops.object.mode_set(mode='OBJECT')
other=bpy.data.objects.new('Selected',obj.data.copy())
bpy.context.scene.collection.objects.link(other)
for loop in other.data.uv_layers.active.data:
    loop.uv *= .25
other.select_set(True)
bpy.context.view_layer.objects.active=other
bpy.ops.object.mode_set(mode='EDIT')
set_face_selection(obj,[])
set_face_selection(other,ranges[0])
before=island_uvs(obj,ranges)
assert pack()=={'FINISHED'}
after=island_uvs(other,ranges)
assert island_uvs(obj,ranges)==before
assert abs(abs(area_px(after[0]))-abs(area_px(before[0])))<.02
assert hull_distance(hull(points(before[0])),hull(points(after[0])))>=3.99
# UV sync off: a mesh-unselected face must still be an obstacle.
obj,ranges=make_islands([dict(size=(1,1),uv=(.1,.1),uv_size=(.4,.4)),
                        dict(size=(1,1),uv=(.1,.1),uv_size=(.2,.2))])
set_face_selection(obj,ranges[1])
bpy.context.scene.tool_settings.use_uv_select_sync=False
screen=bpy.data.workspaces['UV Editing'].screens[0]
area=next(a for a in screen.areas if a.type=='IMAGE_EDITOR')
with bpy.context.temp_override(screen=screen,area=area):
    bpy.ops.uv.select_all(action='SELECT')
    before=island_uvs(obj,ranges)
    assert pack()=={'FINISHED'}
    after=island_uvs(obj,ranges)
    assert before[0]==after[0]
    assert abs(abs(area_px(after[1]))-abs(area_px(before[0])))<.02
    assert hull_distance(hull(points(after[0])),hull(points(after[1])))>=3.99
# No reference: refuse instead of silently scaling to fill the tile.
obj,ranges=make_islands([dict(size=(1,1),uv=(.2,.2),uv_size=(.1,.1))])
before=island_uvs(obj,ranges)
assert pack()=={'CANCELLED'}
assert island_uvs(obj,ranges)==before
# No room at the reference density: do not shrink selected islands.
obj,ranges=make_islands([dict(size=(1,1),uv=(.01,.01),uv_size=(.98,.98)),
                        dict(size=(1,1),uv=(2,2),uv_size=(.1,.1))])
set_face_selection(obj,ranges[1]);before=island_uvs(obj,ranges)
assert pack()=={'CANCELLED'}
assert island_uvs(obj,ranges)==before
print('PASS: fixed references, hidden faces, cross-tile obstacles, sync-off selection, exact density and no-room preservation')
