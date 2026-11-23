# SPDX-License-Identifier: GPL-2.0-or-later
import re
from uuid import uuid4

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


def guide_side(bone):
    # Native XBone extrusion names children Joint, Joint.001, ... . Their
    # parent component establishes the side even if they cross the centerline.
    source = bone
    inferred = None
    while source:
        label = source.get("biped_locator", source.name)
        for side in ("L", "R"):
            if counterpart(label, side):
                return side
        if re.search(r"_C\d+(?:_|$)|[._-]C(?:\.\d+)?$", label):
            break
        if "biped_locator" not in source and abs(source.head.x) > 1e-5:
            inferred = "L" if source.head.x > 0 else "R"
        source = source.parent
    return inferred


def reflect(point):
    return Vector((-point.x, point.y, point.z))


class XBONE_OT_symmetrize_guide(bpy.types.Operator):
    bl_idname = "xbone.symmetrize_guide"
    bl_label = "Symmetrize Guide"
    bl_description = "Mirror guide joints across the armature's local X plane"
    bl_options = {'REGISTER', 'UNDO'}

    side: EnumProperty(name="Source", items=(('L', "Left to Right", ""), ('R', "Right to Left", "")))
    selected_only: BoolProperty(name="Selected Branches Only", options={'HIDDEN', 'SKIP_SAVE'}, default=False)

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
            original = list(edit)
            sides = {b.name: guide_side(b) for b in original}
            other = 'R' if self.side == 'L' else 'L'
            pair_key = "_guide_mirror_pair"
            partners = {(sides[b.name], b[pair_key]): b for b in original if pair_key in b}
            selected = {b.name for b in original if b.select}
            restricted = (self.selected_only if self.properties.is_property_set("selected_only")
                          else bool(selected) and not was_object)

            def target(src):
                if pair_key in src:
                    partner = partners.get((other, src[pair_key]))
                    if partner:
                        return partner
                key = counterpart(src.get("biped_locator", src.name), self.side)
                named = labels.get(key) if key else None
                # An automatically named destination may already belong to a different branch.
                return named if named and pair_key not in named else None

            candidates = {b.name for b in original if sides[b.name] == self.side and
                          (not restricted or b.name in selected or
                           any(p.name in selected for p in b.parent_recursive))}
            # A previously unmirrored branch may need new ancestors to attach to.
            for name in tuple(candidates):
                for parent in edit[name].parent_recursive:
                    if sides[parent.name] != self.side or target(parent):
                        break
                    candidates.add(parent.name)
            pairs = {}
            sources = []
            for src in original:
                if src.name not in candidates:
                    continue
                label = src.get("biped_locator", src.name)
                opposite = counterpart(label, self.side)
                dst = target(src)
                if dst and dst.lock:
                    continue
                if dst is None:
                    dst = edit.new(counterpart(src.name, self.side) or opposite or (src.name + "." + other))
                    if opposite:
                        labels[opposite] = dst
                        if dst.name != (counterpart(src.name, self.side) or opposite):
                            src[pair_key] = dst[pair_key] = uuid4().hex
                    else:
                        # Persist the association for reverse mirrors and renames.
                        src[pair_key] = dst[pair_key] = uuid4().hex
                    dst.use_deform = src.use_deform
                    for coll in list(dst.collections):
                        coll.unassign(dst)
                    for coll in src.collections:
                        coll.assign(dst)
                    for key, value in src.items():
                        if key in {pair_key, "shifter_guide_id", "biped_frame", "biped_length", "biped_locator"}:
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
                    if opposite and opposite in guide_ids:
                        dst["shifter_guide_id"] = guide_ids[opposite]
                    if "negate_roll" in dst:
                        dst["negate_roll"] = self.side == 'L'
                pairs[src.name] = dst
                sources.append(src)
            for src in sources:
                dst = pairs[src.name]
                dst.use_connect = False
                if src.parent:
                    if sides[src.parent.name] == self.side:
                        dst.parent = pairs.get(src.parent.name) or target(src.parent)
                    else:
                        dst.parent = src.parent
                else:
                    dst.parent = None
                dst.head, dst.tail, dst.roll = reflect(src.head), reflect(src.tail), -src.roll
                if src.use_connect and dst.parent and (dst.head - dst.parent.tail).length < 1e-6:
                    dst.use_connect = True
                for key in ("div0", "div1", "supportJoints", "division", "maxstretch", "ik_default", "rollAngle"):
                    if key in src:
                        dst[key] = src[key]
            if not pairs:
                self.report({'WARNING'}, "No editable joints on the source side in this mirror scope")
                return {'CANCELLED'}
            self.report({'INFO'}, f"Mirrored {len(pairs)} guide joints")
            return {'FINISHED'}
        finally:
            obj.data.use_mirror_x = mirror
            if was_object:
                bpy.ops.object.mode_set(mode='OBJECT')
