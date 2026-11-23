# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise branching Guide symmetry and arc-length resampling through edit operators."""
import bpy
from mathutils import Vector


def select(edit, *bones):
    bpy.ops.armature.select_all(action='DESELECT')
    for b in bones:
        b.select = b.select_head = b.select_tail = True
    edit.active = bones[0] if bones else None


def near(value, expected):
    assert (value - Vector(expected)).length < .0001, (value, expected)


def check_pair(src, dst):
    for a, b in ((src.head, dst.head), (src.tail, dst.tail)):
        near(b, (-a.x, a.y, a.z))


bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.xbone.biped_guide(template='xbiped')
guide = bpy.context.object
bpy.ops.object.mode_set(mode='EDIT')
edit = guide.data.edit_bones
select(edit, edit["shoulder_L0"])
assert bpy.ops.xbone.add_component(kind='FK', side='L') == {'FINISHED'}
base = edit.active
base.name = "Cable.L"
for position in ((.6, .3, .9), (.7, .5, .72)):
    assert bpy.ops.armature.extrude() == {'FINISHED'}
    tip = edit.active
    tip.translate(Vector(position) - tip.head)
    assert bpy.ops.armature.xbone_finish_extrude() == {'FINISHED'}
mid = tip.parent
assert '.L' not in tip.name
select(edit, base)
leg = edit["leg_R0"]
leg.translate(Vector((.16, .23, .12)))
saved = leg.head.copy()
assert bpy.ops.xbone.symmetrize_guide(side='L') == {'FINISHED'}
near(leg.head, saved)
mirrored = edit["Cable.R"]
other_mid, = mirrored.children
other_tip, = other_mid.children
count = len(edit)
for a, b in ((base, mirrored), (mid, other_mid), (tip, other_tip)):
    check_pair(a, b)
other_mid.name = "RenamedSegment.R"
other_tip.translate(Vector((.17, .05, .19)))
select(edit, other_mid)
assert bpy.ops.xbone.symmetrize_guide(side='R') == {'FINISHED'}
check_pair(tip, other_tip)
assert len(edit) == count
select(edit)
assert bpy.ops.xbone.symmetrize_guide(side='L') == {'FINISHED'}
check_pair(edit["leg_L0"], leg)
assert len(edit) == count
assert bpy.ops.xbone.build_rig() == {'FINISHED'}
rig = bpy.context.object
assert rig.pose.bones["RenamedSegment.R"].parent.name == "Cable.R"

# XBone tail directions are local joint frames, independent of the pivot chain.
bpy.ops.object.mode_set(mode='OBJECT')
bpy.ops.xbone.new_guide()
arm = bpy.context.object.data
edit = arm.edit_bones
a = edit.active
a.name = "Start"
def new(name, parent, point):
    b = edit.new(name)
    b.parent = parent
    b.head = Vector(point)
    b.tail = b.head + Vector((0,.1,0))
    return b
b = new("Bend", a, (1,0,0))
c = new("End", b, (1,3,0))
d = new("Branch", b, (2,1,0))
outside = d.head.copy()
select(edit, a, b, c)
assert bpy.ops.armature.resample_chain(count=33) == {'FINISHED'}
chain = [c, *c.parent_recursive][::-1]
assert len(chain) == 33 and d.parent == b
near(chain[0].head, (0,0,0))
near(chain[-1].head, (1,3,0))
# The old straight-segment sampler left a hard elbow. Several new pivots should
# now follow the inside of the bend, with small, gradual changes in direction.
assert sum(j.head.x < .995 and j.head.y > .005 for j in chain) >= 3
edges = [q.head - p.head for p, q in zip(chain, chain[1:])]
assert max(p.angle(q) for p, q in zip(edges, edges[1:])) < .5
assert max(e.length for e in edges) / min(e.length for e in edges) < 1.02
near(d.head, outside)
select(edit, *chain)
assert bpy.ops.armature.resample_chain(count=2) == {'FINISHED'}
assert c.parent == a and d.parent in {a, c}
near(d.head, outside)
near(c.head, (1,3,0))
side_branch = new("Side", a, (-1,0,0))
select(edit, a, c, side_branch)
assert bpy.ops.armature.resample_chain(count=4) == {'CANCELLED'}

bpy.ops.object.mode_set(mode='OBJECT')
bpy.ops.object.armature_add(enter_editmode=True)
arm = bpy.context.object.data
edit = arm.edit_bones
a = edit[0]
a.head, a.tail = (0,0,0), (0,1,0)
select(edit, a)
assert bpy.ops.armature.resample_chain(count=4) == {'FINISHED'}
chain = [edit.active, *edit.active.parent_recursive][::-1]
assert len(chain) == 4
for i, bone in enumerate(chain):
    near(bone.head, (0,i/4,0))
    near(bone.tail, (0,(i+1)/4,0))
print("Guide mirror scope, extruded branches, reverse mirror, build and chain resampling passed")
