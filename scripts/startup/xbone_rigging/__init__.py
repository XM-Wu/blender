# SPDX-License-Identifier: GPL-2.0-or-later

from pathlib import Path

import bpy
from bpy.props import EnumProperty, FloatProperty, StringProperty
from bpy.types import Operator, Panel
from bpy_extras.io_utils import ImportHelper
from mathutils import Vector

from . import builder, guides, rebuild
from . import guide_display, selector
from .guide_symmetry import XBONE_OT_symmetrize_guide


class XBONE_OT_biped_guide(Operator):
    bl_idname = "xbone.biped_guide"
    bl_label = "Biped Template"
    bl_options = {'REGISTER', 'UNDO'}
    template: EnumProperty(default='biped', items=(('biped', "Biped", ""), ('game_biped', "Game Biped", ""),
                                                   ('xbiped', "XBiped", "Biped with a gliding knee joint")))

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        game = self.template == 'game_biped'
        source = "game_biped.sgt" if game else "biped.sgt"
        points = guides.template_points(Path(__file__).with_name(source), 0.01 if game else 0.1, 'Y')
        names = {'biped': "Biped Guide", 'game_biped': "Game Biped Guide", 'xbiped': "XBiped Guide"}
        guide = guides.import_template(context, points, names[self.template])
        if self.template == 'xbiped':
            guide["xbiped"] = True
        return {'FINISHED'}


class XBONE_OT_import_guide(Operator, ImportHelper):
    bl_idname = "xbone.import_guide"
    bl_label = "Import Shifter Guide"
    bl_options = {'REGISTER', 'UNDO'}
    filename_ext = ".sgt"
    filter_glob: StringProperty(default="*.sgt", options={'HIDDEN'})
    scale: FloatProperty(name="Scale", default=0.1, min=0.0001)
    up_axis: EnumProperty(name="Up Axis", items=(('Y', "Y", ""), ('Z', "Z", "")))

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        try:
            points = guides.template_points(self.filepath, self.scale, self.up_axis)
        except (ValueError, OSError, TypeError, KeyError, IndexError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        guides.import_template(context, points, Path(self.filepath).stem + " Guide")
        return {'FINISHED'}


class XBONE_OT_new_guide(Operator):
    bl_idname = "xbone.new_guide"
    bl_label = "New Custom Guide"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        obj = guides.new_guide(context, "Character Guide")
        edit = obj.data.edit_bones
        edit.active = guides.joint(edit, "Root", (0, 0, 0))
        return {'FINISHED'}


class XBONE_OT_copy_guide(Operator):
    bl_idname = "xbone.copy_guide"
    bl_label = "Create Guide from Selected XBone"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (context.mode == 'OBJECT' and obj and obj.type == 'ARMATURE'
                and obj.data.is_xbone and not obj.get("rigging_role"))

    def execute(self, context):
        source = context.active_object
        guide = bpy.data.objects.new(source.name + ".Guide", source.data.copy())
        context.collection.objects.link(guide)
        guide.matrix_world = source.matrix_world.copy()
        guide["rigging_role"] = "GUIDE"
        guide["show_shifter_guide"] = True
        guide.show_in_front = True
        source.select_set(False)
        guide.select_set(True)
        context.view_layer.objects.active = guide
        return {'FINISHED'}


class XBONE_OT_tag_limb(Operator):
    bl_idname = "xbone.tag_limb"
    bl_label = "Toggle Two-Joint IK at Active Joint"
    bl_description = "Use the active joint as the endpoint and its two parents as the IK chain"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'EDIT_ARMATURE' and context.edit_object.get("rigging_role") == "GUIDE"
                and context.active_bone and context.active_bone.parent and context.active_bone.parent.parent)

    def execute(self, context):
        bone = context.active_bone
        if "ik_limb" in bone:
            del bone["ik_limb"]
        else:
            bone["ik_limb"] = bone.name
        return {'FINISHED'}


class XBONE_OT_add_component(Operator):
    bl_idname = "xbone.add_component"
    bl_label = "Add Guide Component"
    bl_options = {'REGISTER', 'UNDO'}
    kind: EnumProperty(items=(('FK', "FK Joint", ""), ('ARM', "Arm", ""), ('LEG', "Leg", "")))
    side: EnumProperty(items=(('L', "Left", ""), ('R', "Right", ""), ('C', "Center", "")))
    length: FloatProperty(name="Segment Length", default=0.3, min=0.001, subtype='DISTANCE')

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_ARMATURE' and context.edit_object.get("rigging_role") == "GUIDE"

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        edit = context.edit_object.data.edit_bones
        parent = edit.active
        origin = parent.tail.copy() if parent else Vector((0, 0, 0))
        sign = -1 if self.side == 'R' else 1
        direction = Vector((sign, 0, 0) if self.kind == 'ARM' else (0, 0, -1))
        names = {"FK": ["Joint"], "ARM": ["UpperArm", "Forearm", "Hand"],
                 "LEG": ["Thigh", "Shin", "Foot"]}[self.kind]
        for i, name in enumerate(names):
            point = origin + direction * self.length * i
            if i == 1:
                point.y += self.length * (0.18 if self.kind == 'ARM' else -0.18)
            bone = guides.joint(edit, name + "." + self.side, point, parent, self.length * 0.3)
            bone.tail = point + direction * self.length
            if i:
                parent.tail = point
            parent = bone
        if len(names) == 3:
            bone["ik_limb"] = bone.name
            bone["ik_default"] = float(self.kind == 'LEG')
        edit.active = bone
        return {'FINISHED'}


class XBONE_OT_build_rig(Operator):
    bl_idname = "xbone.build_rig"
    bl_label = "Build Rig"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.get("rigging_role") == "GUIDE" and context.mode in {'EDIT_ARMATURE', 'OBJECT'}

    def execute(self, context):
        guide = context.active_object
        if context.mode == 'EDIT_ARMATURE':
            bpy.ops.object.mode_set(mode='OBJECT')
        if not guide.data.bones:
            self.report({'ERROR'}, "Guide contains no joints")
            return {'CANCELLED'}
        try:
            rebuild.build(context, guide)
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class XBONE_OT_match_limb(Operator):
    bl_idname = "xbone.match_limb"
    bl_label = "Match Controls and Switch"
    bl_options = {'UNDO'}
    control: StringProperty()
    mode: EnumProperty(items=(('FK', "FK", ""), ('IK', "IK", "")))

    @classmethod
    def poll(cls, context):
        return context.mode == 'POSE' and context.object.get("rigging_role") == "RIG"

    def execute(self, context):
        rig = context.object
        pose = rig.pose.bones
        target = pose.get(self.control)
        if target is None or "blend_property" not in target:
            return {'CANCELLED'}
        matrices = [pose[n].matrix.copy() for n in target["joints"]]
        changed = []
        if self.mode == 'IK':
            # Seed the solver with the matched rotation, including axial twist.
            for name, joint_name in zip(target["solver_chain"], target["joints"]):
                joint = pose[joint_name]
                pose[name].matrix_basis = rig.convert_space(pose_bone=joint, matrix=joint.matrix,
                                                           from_space='POSE', to_space='LOCAL')
                changed.append(pose[name])
            pole = pose[target["pole_control"]]
            pole_matrix = pole.matrix.copy()
            if "knee_glide_pivot" in target:
                from . import xbiped_knee
                pole_matrix.translation = xbiped_knee.match_pole(rig, target, matrices)
            else:
                pole_matrix.translation = builder.pole_point(*(m.translation for m in matrices))
            desired = matrices[-1]
            if "foot_effector" in target:
                offset = target.matrix.inverted_safe() @ pose[target["foot_effector"]].matrix
                desired = desired @ offset.inverted_safe()
            target.matrix = desired
            context.view_layer.update()
            pole.matrix = pole_matrix
            for name, matrix in zip(target.get("ik_match_frames", ()), matrices):
                context.view_layer.update()
                frame = pose[name]
                desired = matrix.copy()
                desired.translation = frame.head
                frame.matrix = desired
                changed.append(frame)
            if "wrist_rotation" in target:
                context.view_layer.update()
                rotation = pose[target["wrist_rotation"]]
                offset = rotation.matrix.inverted_safe() @ pose[target["wrist_result"]].matrix
                rotation.matrix = matrices[-1] @ offset.inverted_safe()
                changed.append(rotation)
            changed.extend((pole, target))
        else:
            for name, joint, matrix in zip(target["fk_controls"], target["joints"], matrices):
                pose[name].matrix = matrix @ pose[joint].bone.matrix_local.inverted_safe() @ pose[name].bone.matrix_local
                changed.append(pose[name])
                context.view_layer.update()
                if "knee_glide_pivot" in target and name == target["fk_controls"][1]:
                    # The animator rotates at the femoral end. The solved shin starts at
                    # the displaced tibial end, so remove this offset exactly once.
                    frame = pose[name].matrix.copy()
                    frame.translation -= pose[target["knee_glide_pivot"]].head - pose[name].head
                    pose[name].matrix = frame
                    context.view_layer.update()
        rig[target["blend_property"]] = float(self.mode == 'IK')
        if context.scene.tool_settings.use_keyframe_insert_auto:
            rig.keyframe_insert(data_path='["' + bpy.utils.escape_identifier(target["blend_property"]) + '"]')
            for bone in changed:
                rotation = {'QUATERNION': "rotation_quaternion", 'AXIS_ANGLE': "rotation_axis_angle"}
                for channel in ("location", rotation.get(bone.rotation_mode, "rotation_euler"), "scale"):
                    bone.keyframe_insert(data_path=channel, group=bone.name)
        rig.update_tag()
        context.view_layer.update()
        bpy.ops.pose.select_all(action='DESELECT')
        active = target if self.mode == 'IK' else pose[target["fk_controls"][-1]]
        rig.data.bones.active = active.bone
        active.select = True
        return {'FINISHED'}


class XBONE_OT_edit_source_guide(Operator):
    bl_idname = "xbone.edit_source_guide"
    bl_label = "Edit Source Guide"
    bl_description = "Edit this rig's guide and rebuild the same rig"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (context.mode in {'POSE', 'OBJECT'} and obj and obj.get("rigging_role") == "RIG"
                and obj.get("rigging_guide") in context.view_layer.objects.values())

    def execute(self, context):
        rig = context.object
        if context.mode == 'POSE':
            bpy.ops.object.mode_set(mode='OBJECT')
        guide = rig["rigging_guide"]
        guide_display.ensure(guide)
        guide["rigging_target"] = rig
        rig.hide_set(True)
        guide.hide_set(False)
        guide.select_set(True)
        context.view_layer.objects.active = guide
        bpy.ops.object.mode_set(mode='EDIT')
        return {'FINISHED'}


class XBONE_OT_toggle_guide_display(Operator):
    bl_idname = "xbone.toggle_guide_display"
    bl_label = "Shifter Guide Display"
    bl_description = "Display the component roots, locators and connections of this guide"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == 'ARMATURE' and obj.get("rigging_role") == "GUIDE"

    def execute(self, context):
        obj = context.active_object
        if "show_shifter_guide" in obj:
            obj["show_shifter_guide"] = not obj["show_shifter_guide"]
        else:
            was_edit = context.mode == 'EDIT_ARMATURE'
            if was_edit:
                bpy.ops.object.mode_set(mode='OBJECT')
            try:
                guide_display.ensure(obj)
            finally:
                if was_edit:
                    bpy.ops.object.mode_set(mode='EDIT')
        obj.update_tag()
        return {'FINISHED'}


class XBONE_PT_rigging(Panel):
    bl_label = "XBone Rigging"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Rigging"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        role = obj.get("rigging_role") if obj else None
        if context.mode == 'OBJECT':
            layout.operator("xbone.biped_guide").template = 'biped'
            layout.operator("xbone.biped_guide", text="XBiped Template").template = 'xbiped'
            layout.operator("xbone.biped_guide", text="Game Biped Template").template = 'game_biped'
            layout.operator("xbone.import_guide", text="Import Shifter Template (.sgt)")
            layout.operator("xbone.new_guide")
            if XBONE_OT_copy_guide.poll(context):
                layout.operator("xbone.copy_guide")
        if role == "GUIDE":
            layout.operator("xbone.toggle_guide_display", depress=bool(obj.get("show_shifter_guide")))
            row = layout.row(align=True)
            row.operator("xbone.symmetrize_guide", text="Left to Right").side = 'L'
            row.operator("xbone.symmetrize_guide", text="Right to Left").side = 'R'
            if context.mode == 'OBJECT':
                layout.operator("object.mode_set", text="Edit Guide").mode = 'EDIT'
            if context.mode == 'EDIT_ARMATURE':
                layout.operator("xbone.add_component")
                layout.operator("xbone.tag_limb")
                bone = context.active_bone
                if bone:
                    for key, label in (("div0", "Upper Divisions"), ("div1", "Lower Divisions"),
                                       ("supportJoints", "Support Joints"), ("division", "Divisions")):
                        if key in bone:
                            layout.prop(bone, '["' + key + '"]', text=label)
            label = "Rebuild Rig" if rebuild.target_for(context, obj) else "Build Rig"
            layout.operator("xbone.build_rig", text=label)
        if role == "RIG":
            layout.operator("xbone.edit_source_guide")
            skeleton = obj.data.collections.get("Skeleton")
            if skeleton:
                layout.prop(skeleton, "is_visible", text="Show Skeleton")
            extra = obj.data.collections.get("Additional FK")
            if extra and obj.data.collections.get("Biped Display"):
                layout.prop(extra, "is_visible", text="Show Additional FK Controls")
            active = context.active_pose_bone
            if active and "UI_" in active.name:
                for key in active.keys():
                    if key.startswith(("mouth_","neck_","spine_","shoulder_","control_")):
                        layout.prop(active,'["'+bpy.utils.escape_identifier(key)+'"]',text=key)
            for control in obj.pose.bones:
                prop = control.get("blend_property")
                if prop:
                    box = layout.box()
                    box.prop(obj, '["' + bpy.utils.escape_identifier(prop) + '"]', slider=True)
                    if "knee_glide" in control:
                        box.prop(control, '["knee_glide"]', text="Knee Glide", slider=True)
                        box.prop(control, '["knee_internal_rotation"]', text="Tibial Rotation (°)")
                    active = context.active_pose_bone
                    if active and (active == control or active.get("limb_control") == control.name) and "ikscale" in control:
                        from .limb_parameters import PARAMETERS
                        for key, label, *_ in PARAMETERS:
                            box.prop(control, '["' + key + '"]', text=label)
                        if "volume" in control:
                            box.prop(control, '["volume"]', text="Volume")
                        if "roundness" in control:
                            box.prop(control, '["roundness"]', text="Roundness")
                        if "aproll" in control:
                            box.prop(control, '["aproll"]', text="Armpit Roll")
                        for prop_name, label in (("ik_space", "IK Space"), ("pole_space", "Pole Space"),
                                                 ("rotation_space", "Wrist Rotation Space"), ("pin_space", "Pin Space")):
                            if prop_name in control:
                                refs = control[prop_name.replace("_space", "_references")]
                                box.prop(control, '["'+prop_name+'"]', text=label + " · " + refs[control[prop_name]])
                    row = box.row(align=True)
                    for mode in ('FK', 'IK'):
                        op = row.operator("xbone.match_limb", text="Match " + mode)
                        op.control, op.mode = control.name, mode


classes = (XBONE_OT_biped_guide, XBONE_OT_import_guide, XBONE_OT_new_guide,
           XBONE_OT_copy_guide, XBONE_OT_tag_limb, XBONE_OT_add_component, XBONE_OT_build_rig,
           XBONE_OT_match_limb, XBONE_OT_edit_source_guide, XBONE_OT_symmetrize_guide,
           XBONE_OT_toggle_guide_display, XBONE_PT_rigging)
_register, _unregister = bpy.utils.register_classes_factory(classes)


def register():
    _register()
    selector.register()


def unregister():
    selector.unregister()
    _unregister()
