# SPDX-License-Identifier: GPL-2.0-or-later
"""Redistribute a selected edit chain along a smooth rest-space curve."""

from bisect import bisect_right

import bpy
from bpy.props import IntProperty


def selected_chain(context):
    selected = set(context.selected_editable_bones or ())
    if any(b.id_data != context.edit_object.data for b in selected):
        raise ValueError("Select a chain on a single armature")
    roots = [b for b in selected if b.parent not in selected]
    if len(roots) != 1:
        raise ValueError("Select one continuous parent-child chain")
    chain = [roots[0]]
    while True:
        children = [c for c in chain[-1].children if c in selected]
        if not children:
            break
        if len(children) > 1:
            raise ValueError("Select a single path through the branch")
        chain.append(children[0])
    if len(chain) != len(selected):
        raise ValueError("Select all joints between the endpoints")
    if context.edit_object.data.is_xbone and len(chain) < 2:
        raise ValueError("Select at least two XBone joints")
    return chain


def curve_point(a, control, b, t):
    if control is None:
        return a.lerp(b, t)
    return a.lerp(control, t).lerp(control.lerp(b, t), t)


def sample_path(points, intervals):
    """Round corners with tangent quadratic Beziers and sample by distance along the curve."""
    # Preserve input joint indices for attachments, including coincident guide pivots.
    anchors = [points[0]]
    indices = [0]
    for point in points[1:]:
        if (point - anchors[-1]).length > 1e-8:
            anchors.append(point)
        indices.append(len(anchors) - 1)
    if len(anchors) < 2:
        raise ValueError("The selected chain has zero length")

    pieces = []
    distances = [0.]
    # Each lookup interval stores its piece and the corresponding parameter range.
    lookup = []

    def append_piece(a, b, control=None):
        if (b - a).length_squared == 0 and control is None:
            return
        piece = len(pieces)
        pieces.append((a, control, b))
        steps = 32 if control is not None else 1
        previous = a
        for step in range(1, steps + 1):
            t = step / steps
            position = curve_point(a, control, b, t)
            distances.append(distances[-1] + (position - previous).length)
            lookup.append((piece, (step - 1) / steps, t))
            previous = position

    previous = anchors[0]
    knots = [0.]
    for a, corner, b in zip(anchors, anchors[1:], anchors[2:]):
        incoming, outgoing = corner - a, b - corner
        if incoming.normalized().dot(outgoing.normalized()) >= 1.0 - 1e-7:
            append_piece(previous, corner)
            knots.append(distances[-1])
            previous = corner
            continue
        # Limit rounding by the shorter adjacent bone so a long segment cannot pull
        # a nearby joint or a short bend far away from the authored path.
        trim = .5 * min(incoming.length, outgoing.length)
        enter = corner - incoming.normalized() * trim
        leave = corner + outgoing.normalized() * trim
        append_piece(previous, enter)
        start = distances[-1]
        append_piece(enter, leave, corner)
        # Equal trims make the corner symmetric about its arc-length midpoint.
        knots.append((start + distances[-1]) * .5)
        previous = leave
    append_piece(previous, anchors[-1])
    knots.append(distances[-1])
    length = distances[-1]
    if length < 1e-7:
        raise ValueError("The selected chain has zero length")
    samples = [points[0]]
    for i in range(1, intervals):
        d = length * i / intervals
        index = bisect_right(distances, d) - 1
        piece, t0, t1 = lookup[index]
        t = t0 + (t1 - t0) * (d - distances[index]) / (distances[index + 1] - distances[index])
        a, control, b = pieces[piece]
        samples.append(curve_point(a, control, b, t))
    samples.append(points[-1])
    return samples, [knots[i] for i in indices]


def resample(arm, chain, count):
    xbone = arm.is_xbone
    if xbone and count < 2:
        raise ValueError("An XBone chain needs at least two joints")
    points = [b.head.copy() for b in chain]
    if not xbone:
        points.append(chain[-1].tail.copy())
    intervals = count - 1 if xbone else count
    samples, distances = sample_path(points, intervals)
    length = distances[-1]
    segments = [min(bisect_right(distances, length * i / intervals) - 1, len(points) - 2)
                for i in range(count)]
    if not xbone and any((b - a).length < 1e-7 for a, b in zip(samples, samples[1:])):
        raise ValueError("This count would create a zero-length bone; choose another count")

    # Preserve both endpoint identities when possible, and as many original names as fit.
    old_count = len(chain)
    if count >= old_count:
        keep = {round(i * (count - 1) / max(1, old_count - 1)): i for i in range(old_count)}
    else:
        keep = {i: round(i * (old_count - 1) / max(1, count - 1)) for i in range(count)}
    retained = set(keep.values())
    removed = [b for i, b in enumerate(chain) if i not in retained]
    if any(b.lock for b in chain):
        raise ValueError("Unlock the selected chain before resampling")
    if any("biped_locator" in b or "shifter_guide_id" in b or "ik_limb" in b for b in removed):
        raise ValueError("This count removes a required Guide joint; increase the count")

    edit = arm.edit_bones
    root_parent, root_connected = chain[0].parent, chain[0].use_connect
    frames = [b.matrix.to_quaternion() for b in chain]
    lengths = [b.length for b in chain]
    axes = [b.tail - b.head for b in chain]
    connect = [b.use_connect for b in chain]
    originals = set(chain)
    branches = [(c, i, c.use_connect) for i, b in enumerate(chain) for c in b.children if c not in originals]
    for child, _, _ in branches:
        child.use_connect = False
    for b in chain:
        b.use_connect = False

    result = []
    for i in range(count):
        source_index = keep.get(i, segments[i])
        source = chain[source_index]
        bone = source if i in keep else edit.new(source.name)
        if i not in keep:
            bone.use_deform = source.use_deform
            for coll in list(bone.collections):
                coll.unassign(bone)
            for coll in source.collections:
                coll.assign(bone)
        result.append(bone)
        bone.parent = result[i - 1] if i else root_parent
        bone.head = samples[i]
        if xbone:
            # XBone tail encodes the joint frame; local Y need not aim at the next pivot.
            axis = axes[source_index]
            up = frames[source_index].to_matrix().col[2]
            if i + 1 < count and source_index + 1 < old_count:
                before = points[source_index + 1] - points[source_index]
                after = samples[i + 1] - samples[i]
                if before.length_squared > 1e-12 and after.length_squared > 1e-12:
                    turn = before.rotation_difference(after)
                    axis, up = turn @ axis, turn @ up
            bone.tail = bone.head + axis.normalized() * lengths[source_index]
        else:
            bone.tail = samples[i + 1]
            up = frames[source_index].to_matrix().col[2]
        bone.align_roll(up)
        if not i:
            bone.use_connect = root_connected
        elif not xbone:
            bone.use_connect = connect[source_index] if i in keep else True

    slots = {old: slot for slot, old in keep.items()}
    for child, old, connected in branches:
        # A connected Blender branch attaches at the parent's tail; XBone branches at its pivot.
        anchor = old if xbone else old + 1
        slot = slots.get(old)
        if slot is None:
            slot = max(0, min(count - 1, round(distances[anchor] / length * intervals) - int(not xbone)))
        child.parent = result[slot]
        if connected and (child.head - child.parent.tail).length < 1e-6:
            child.use_connect = True
    for bone in reversed(removed):
        edit.remove(bone)
    for bone in result:
        bone.select = bone.select_head = bone.select_tail = True
    edit.active = result[-1]


class ARMATURE_OT_resample_chain(bpy.types.Operator):
    bl_idname = "armature.resample_chain"
    bl_label = "Resample Chain"
    bl_description = "Round the bends and evenly space the selected chain along the smooth curve, keeping the endpoints"
    bl_options = {'REGISTER', 'UNDO'}

    count: IntProperty(name="Count", min=1, max=4096, default=3)

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_ARMATURE'

    def invoke(self, context, event):
        try:
            self.count = len(selected_chain(context))
        except ValueError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop(self, "count", text="Joint Count" if context.edit_object.data.is_xbone else "Bone Count")
        if self.count < len(context.selected_editable_bones or ()):
            self.layout.label(text="Removed bones lose their weight and animation bindings", icon='INFO')

    def execute(self, context):
        arm = context.edit_object.data
        mirror = arm.use_mirror_x
        try:
            chain = selected_chain(context)
            arm.use_mirror_x = False
            resample(arm, chain, self.count)
        except ValueError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        finally:
            arm.use_mirror_x = mirror
        return {'FINISHED'}
