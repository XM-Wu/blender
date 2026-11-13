# SPDX-License-Identifier: GPL-2.0-or-later
"""Check panel-produced arguments with stale New/Remove operator properties."""
import bpy
from types import SimpleNamespace
from bl_ui.properties_uv_tools import TexelPackIdListPanel

bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
bpy.ops.mesh.primitive_cube_add()
owner=bpy.context.object
attr=owner.data.attributes.new('uvpack_group','INT','FACE')
for item in attr.data: item.value=6
bpy.context.scene['uvpack_group_names']={'6':'Group 5'}
bpy.ops.mesh.primitive_plane_add()
target=bpy.context.object

class Layout:
    def __init__(self, previous):
        self.previous=previous
        self.calls=[]
    def row(self, **kw): return self
    def column(self, **kw): return self
    def box(self): return self
    def label(self, **kw): pass
    def operator(self, operator, **kw):
        props=SimpleNamespace()
        if operator=='uv_tools.group_membership':
            props=SimpleNamespace(operation=self.previous,choose=True,group=999)
            self.calls.append((kw,props))
        return props

for previous in ('NEW','REMOVE'):
    layout=Layout(previous)
    panel=SimpleNamespace(layout=layout,kind='GROUP',noun='Group',draw_extra=lambda layout:None)
    TexelPackIdListPanel.draw(panel,bpy.context)
    plus=[props for kw,props in layout.calls if kw.get('icon')=='ADD' and not kw.get('text')]
    assert len(plus)==1
    props=plus[0]
    assert vars(props)==dict(kind='GROUP',operation='ASSIGN',choose=False,group=6)
    assert bpy.ops.uv_tools.group_membership(**vars(props))=={'FINISHED'}
    assert all(item.value==6 for item in target.data.attributes['uvpack_group'].data)
    for item in target.data.attributes['uvpack_group'].data: item.value=0
assert bpy.ops.uv_tools.group_membership.get_rna_type().properties['operation'].is_skip_save
print('PASS: row + ignores cached New/Remove and uses ID 6 for display name Group 5')
