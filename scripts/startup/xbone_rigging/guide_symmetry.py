# SPDX-License-Identifier: GPL-2.0-or-later
import re

import bpy
from bpy.props import BoolProperty, EnumProperty
from mathutils import Matrix, Vector


def counterpart(name, side):
    other = "R" if side == "L" else "L"
    # Shifter component IDs include an index before the optional locator name.
    flipped, count = re.subn(r"_" + side + r"(?=\d+(?:_|$))", "_" + other, name, count=1)
    if count:
        return flipped
    # Custom Guide components use Blender's .L/.R convention including .001 duplicates.
    if re.search(r"[._-]" + side + r"(?:\.\d+)?$", name):
        flipped = bpy.utils.flip_name(name)
        return flipped if flipped != name else None
    return None


def reflect(point):
    return Vector((-point.x, point.y, point.z))


class XBONE_OT_symmetrize_guide(bpy.types.Operator):
    bl_idname = "xbone.symmetrize_guide"
    bl_label = "Symmetrize Guide"
    bl_description = "Mirror guide joints across the armature's local X plane"
    bl_options = {'REGISTER', 'UNDO'}

    side: EnumProperty(name="Source", items=(('L', "Left to Right", ""), ('R', "Right to Left", "")))
    selected_only: BoolProperty(name="Selected Source Joints Only", default=False)

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == 'ARMATURE' and obj.get("rigging_role") == "GUIDE" and context.mode in {
            'OBJECT', 'EDIT_ARMATURE'}

    def execute(self, context):
        obj = context.active_object
        was_object = context.mode == 'OBJECT'
        if was_object:
            bpy.ops.object.mode_set(mode='EDIT')
        edit = obj.data.edit_bones
        mirror = obj.data.use_mirror_x
        obj.data.use_mirror_x = False
        try:
            labels = {b.get("biped_locator", b.name): b for b in edit}
            pairs = {}
            sources = []
            candidates = [b for b in edit if not self.selected_only or b.select]
            if self.selected_only:
                for b in list(candidates):
                    for parent in b.parent_recursive:
                        opposite = counterpart(parent.get("biped_locator", parent.name), self.side)
                        if not opposite or opposite in labels:
                            break
                        if parent not in candidates:
                            candidates.append(parent)
            for src in candidates:
                label = src.get("biped_locator", src.name)
                opposite = counterpart(label, self.side)
                if not opposite:
                    continue
                dst = labels.get(opposite)
                if dst and dst.lock:
                    continue
                if dst is None:
                    dst = edit.new(counterpart(src.name, self.side) or opposite)
                    labels[opposite] = dst
                    dst.use_deform = src.use_deform
                    for coll in list(dst.collections):
                        coll.unassign(dst)
                    for coll in src.collections:
                        coll.assign(dst)
                    for key, value in src.items():
                        if key in {"shifter_guide_id", "biped_frame", "biped_length", "biped_locator"}:
                            continue
                        if isinstance(value, str):
                            value = counterpart(value, self.side) or value
                        elif key.endswith("_references"):
                            value = [counterpart(n, self.side) or n for n in value]
                        dst[key] = value
                    if "biped_locator" in src:
                        dst["biped_locator"] = opposite
                        reflection = Matrix.Diagonal((-1, 1, 1, 1))
                        values = src["biped_frame"]
                        frame = reflection @ Matrix([values[i:i + 4] for i in range(0, 16, 4)]) @ reflection
                        dst["biped_frame"] = [v for row in frame for v in row]
                        dst["biped_length"] = src["biped_length"]
                    guide_ids = obj.get("shifter_guide_ids", {})
                    if opposite in guide_ids:
                        dst["shifter_guide_id"] = guide_ids[opposite]
                    if "negate_roll" in dst:
                        dst["negate_roll"] = self.side == 'L'
                pairs[src.name] = dst
                sources.append(src)
            for src in sources:
                dst = pairs[src.name]
                dst.use_connect = False
                if src.parent:
                    label = src.parent.get("biped_locator", src.parent.name)
                    dst.parent = pairs.get(src.parent.name) or labels.get(counterpart(label, self.side) or label)
                else:
                    dst.parent = None
                dst.head, dst.tail, dst.roll = reflect(src.head), reflect(src.tail), -src.roll
                if src.use_connect and dst.parent and (dst.head - dst.parent.tail).length < 1e-6:
                    dst.use_connect = True
                for key in ("div0", "div1", "supportJoints", "division", "maxstretch", "ik_default", "rollAngle"):
                    if key in src:
                        dst[key] = src[key]
            if not pairs:
                self.report({'WARNING'}, "No editable target joints for this source side")
                return {'CANCELLED'}
            self.report({'INFO'}, f"Mirrored {len(pairs)} guide joints")
            return {'FINISHED'}
        finally:
            obj.data.use_mirror_x = mirror
            if was_object:
                bpy.ops.object.mode_set(mode='OBJECT')
