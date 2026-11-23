# SPDX-License-Identifier: GPL-2.0-or-later

from pathlib import Path

import bpy
from bpy.app.handlers import persistent
from bpy.props import EnumProperty, FloatProperty, StringProperty
from bpy.types import Header, Operator, Panel
from bpy_extras.io_utils import ImportHelper
from mathutils import Vector

from . import builder, guides, rebuild
from . import guide_display, mesh_bindings, selector, smart_guide
from .guide_symmetry import XBONE_OT_symmetrize_guide


def ensure_rigging_workspace():
    for workspace in bpy.data.workspaces:
        if workspace.library is None and workspace.get("xbone_rigging_workspace"):
            return workspace
    path = Path(__file__).resolve().parents[2] / "presets/workspaces/rigging.blend"
    with bpy.data.libraries.load(str(path)) as (_, data):
        data.workspaces = ["Rigging"]
    return data.workspaces[0]


@persistent
def add_startup_workspace(filepath):
    # Blender sends an empty load_post path for both factory and user home files.
    if not filepath and not bpy.context.preferences.app_template:
        ensure_rigging_workspace()


class XBONE_OT_workspace(Operator):
    bl_idname = "xbone.workspace"
    bl_label = "Rigging Workspace"
    bl_description = "Open the XBone rigging workspace"

    def execute(self, context):
        context.window.workspace = ensure_rigging_workspace()
        return {'FINISHED'}


class XBONE_OT_biped_guide(Operator):
    bl_idname = "xbone.biped_guide"
    bl_label = "Biped Template"
    bl_options = {'REGISTER', 'UNDO'}
    template: EnumProperty(default='biped', items=(('biped', "Biped", ""), ('game_biped', "Game Biped", ""),
                                                   ('xbiped', "XBiped", "Biped with gliding elbow and knee joints")))

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        game = self.template == 'game_biped'
        source = "game_biped.sgt" if game else "biped.sgt"
        points = guides.template_points(Path(__file__).with_name(source), 0.01 if game else 0.1, 'Y')
        names = {'biped': "Biped Guide", 'game_biped': "Game Biped Guide", 'xbiped': "XBiped Guide"}
        guide = guides.import_template(context, points, names[self.template], at_origin=self.template == 'xbiped')
        if self.template == 'xbiped':
            from . import xbiped_limb
            guide["xbiped"] = True
            xbiped_limb.set_limb_defaults(guide)
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
    bl_label = "Apply Guide and Rebuild"
    bl_description = "Build from this guide and apply its changes to the associated rig, if one exists"
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
        bend_control = target.get("elbow_fk_control") or target.get("knee_fk_control")
        glide_pivot = target.get("elbow_glide_pivot") or target.get("knee_glide_pivot")
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
            if glide_pivot:
                from . import xbiped_limb
                pole_matrix.translation = xbiped_limb.match_pole(rig, target, matrices)
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
            if bend_control:
                changed.append(pose[bend_control])
            for name, joint, matrix in zip(target["fk_controls"], target["joints"], matrices):
                if bend_control and name == target["fk_controls"][0]:
                    # Retain the animator's bend channel above the upper segment's counter-rotation.
                    output = pose["MCH.FKOutput." + joint]
                    pose[name].matrix = matrix @ output.matrix.inverted_safe() @ pose[name].matrix
                else:
                    pose[name].matrix = matrix @ pose[joint].bone.matrix_local.inverted_safe() @ pose[name].bone.matrix_local
                changed.append(pose[name])
                context.view_layer.update()
                if glide_pivot and name == target["fk_controls"][1]:
                    # The lower segment begins at the displaced pivot. Remove its offset once.
                    frame = pose[name].matrix.copy()
                    frame.translation -= pose[glide_pivot].head - pose[name].head
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


def switch_guide_rig(context, operator, target, mode):
    """Switch the editing context without evaluating the guide as input to a build."""
    source = context.active_object
    layer = context.view_layer
    if target.data == source.data:
        operator.report({'WARNING'}, "Guide and rig must have independent armature data")
        return {'CANCELLED'}
    if not target.is_editable or (mode == 'EDIT' and not target.data.is_editable):
        operator.report({'WARNING'}, "The target armature is read-only")
        return {'CANCELLED'}

    was_hidden = target.hide_get(view_layer=layer)
    target.hide_set(False, view_layer=layer)
    view = context.space_data if context.area and context.area.type == 'VIEW_3D' else None
    if target.hide_select or not target.visible_get(view_layer=layer, viewport=view):
        target.hide_set(was_hidden, view_layer=layer)
        operator.report({'WARNING'}, "Make the target armature visible and selectable in this view layer")
        return {'CANCELLED'}

    previous_mode = source.mode
    selected = list(context.selected_objects)
    try:
        if mode == 'EDIT':
            guide_display.ensure(target)
        if previous_mode != 'OBJECT':
            # Leaving Edit Mode commits pending edits to the guide's own armature.
            bpy.ops.object.mode_set(mode='OBJECT')
        for obj in selected:
            obj.select_set(False, view_layer=layer)
        target.select_set(True, view_layer=layer)
        layer.objects.active = target
        if bpy.ops.object.mode_set(mode=mode) != {'FINISHED'}:
            raise RuntimeError("Could not switch armature mode")
    except (RuntimeError, OSError, ValueError) as exc:
        if layer.objects.active == target and target.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        target.select_set(False, view_layer=layer)
        for obj in selected:
            obj.select_set(True, view_layer=layer)
        layer.objects.active = source
        if source.mode != previous_mode:
            bpy.ops.object.mode_set(mode=previous_mode)
        target.hide_set(was_hidden, view_layer=layer)
        operator.report({'WARNING'}, str(exc))
        return {'CANCELLED'}

    source.hide_set(True, view_layer=layer)
    return {'FINISHED'}


class XBONE_OT_edit_source_guide(Operator):
    bl_idname = "xbone.edit_source_guide"
    bl_label = "Edit Source Guide"
    bl_description = "Edit this rig's source guide; the rig keeps its current pose and bindings"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        rig = context.active_object
        if (context.scene.xbone_marker_session or context.mode not in {'POSE', 'OBJECT'} or
                not rig or rig.get("rigging_role") != "RIG"):
            return False
        guide = rig.get("rigging_guide")
        return (isinstance(guide, bpy.types.Object) and guide.type == 'ARMATURE' and
                guide.get("rigging_role") == "GUIDE" and context.view_layer.objects.get(guide.name) == guide)

    def execute(self, context):
        rig = context.active_object
        guide = rig["rigging_guide"]
        result = switch_guide_rig(context, self, guide, 'EDIT')
        if result == {'FINISHED'} and guide.get("rigging_target") != rig:
            guide["rigging_target"] = rig
        return result


class XBONE_OT_return_to_rig(Operator):
    bl_idname = "xbone.return_to_rig"
    bl_label = "Return to Rig"
    bl_description = "Resume posing the existing rig; pending guide edits are kept on the guide"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        guide = context.active_object
        if (context.scene.xbone_marker_session or context.mode not in {'EDIT_ARMATURE', 'OBJECT'} or
                not guide or guide.get("rigging_role") != "GUIDE"):
            return False
        target = rebuild.target_for(context, guide)
        return target is not None and context.view_layer.objects.get(target.name) == target

    def execute(self, context):
        return switch_guide_rig(context, self, rebuild.target_for(context, context.active_object), 'POSE')


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


class RIGGING_HT_header(Header):
    bl_space_type = 'RIGGING'

    def draw(self, context):
        layout = self.layout
        layout.template_header()
        obj = context.active_object
        role = obj.get("rigging_role") if obj and obj.type == 'ARMATURE' else None
        if role in {"GUIDE", "RIG"} and not context.scene.xbone_marker_session:
            row = layout.row(align=True)
            row.operator("xbone.edit_source_guide", text="Guide", depress=role == "GUIDE")
            row.operator("xbone.return_to_rig", text="Rig", depress=role == "RIG")
        layout.label(text=obj.name if obj and obj.type == 'ARMATURE' else "Rigging",
                     icon='ARMATURE_DATA')


class RiggingPanel:
    bl_space_type = 'RIGGING'
    bl_region_type = 'WINDOW'


class RIGGING_PT_templates(RiggingPanel, Panel):
    bl_label = "Create Guide"

    @classmethod
    def poll(cls, context):
        if context.scene.xbone_marker_session:
            return False
        obj = context.active_object
        return context.mode == 'OBJECT' or not (obj and obj.get("rigging_role") in {"RIG", "GUIDE"})

    def draw(self, context):
        layout = self.layout
        if context.mode != 'OBJECT':
            layout.operator("object.mode_set", text="Switch to Object Mode to Create a Guide").mode = 'OBJECT'
            return
        templates = layout.grid_flow(row_major=True, even_columns=True)
        templates.operator("xbone.biped_guide").template = 'biped'
        templates.operator("xbone.biped_guide", text="XBiped Template").template = 'xbiped'
        templates.operator("xbone.biped_guide", text="Game Biped Template").template = 'game_biped'
        layout.operator("xbone.start_landmarks", icon='EMPTY_AXIS')
        layout.operator("xbone.import_guide", text="Import Shifter Template (.sgt)")
        layout.operator("xbone.new_guide")
        if XBONE_OT_copy_guide.poll(context):
            layout.operator("xbone.copy_guide")


class RIGGING_PT_guide(RiggingPanel, Panel):
    bl_label = "Guide"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.get("rigging_role") == "GUIDE"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        layout.operator("xbone.toggle_guide_display", depress=bool(obj.get("show_shifter_guide")))
        if smart_guide.XBONE_OT_edit_landmarks.poll(context):
            layout.operator("xbone.edit_landmarks")
        row = layout.row(align=True)
        row.operator("xbone.symmetrize_guide", text="Left to Right").side = 'L'
        row.operator("xbone.symmetrize_guide", text="Right to Left").side = 'R'
        if context.mode == 'OBJECT':
            layout.operator("object.mode_set", text="Edit Guide").mode = 'EDIT'
        if context.mode == 'EDIT_ARMATURE':
            layout.operator("xbone.add_component")
            layout.operator("xbone.tag_limb")
            bone = obj.data.edit_bones.active
            if bone:
                for key, label in (("div0", "Upper Divisions"), ("div1", "Lower Divisions"),
                                   ("supportJoints", "Support Joints"), ("division", "Divisions")):
                    if key in bone:
                        layout.prop(bone, '["' + key + '"]', text=label)
        label = "Apply Guide and Rebuild" if rebuild.target_for(context, obj) else "Build Rig"
        layout.operator("xbone.build_rig", text=label)


class RIGGING_PT_rig(RiggingPanel, Panel):
    bl_label = "Rig"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.get("rigging_role") == "RIG"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        if context.mode == 'OBJECT':
            layout.operator("object.mode_set", text="Pose Rig", icon='POSE_HLT').mode = 'POSE'
        if smart_guide.XBONE_OT_edit_landmarks.poll(context):
            layout.operator("xbone.edit_landmarks")
        skeleton = obj.data.collections.get("Skeleton")
        if skeleton:
            layout.prop(skeleton, "is_visible", text="Show Skeleton")
        extra = obj.data.collections.get("Additional FK")
        if extra and obj.data.collections.get("Biped Display"):
            layout.prop(extra, "is_visible", text="Show Additional FK Controls")


class RIGGING_PT_hands(RiggingPanel, Panel):
    bl_label = "Hand Controls"

    @classmethod
    def poll(cls, context):
        rig = context.active_object
        return (context.mode in {'POSE', 'OBJECT'} and rig is not None and rig.get("rigging_role") == "RIG"
                and "xbiped_hand_L" in rig)

    def draw(self, context):
        rig = context.active_object
        for side, label in (('L', "Left Grip"), ('R', "Right Grip")):
            host = rig.pose.bones.get(rig.get("xbiped_hand_" + side, ""))
            if not host:
                continue
            header, body = self.layout.panel("xbiped_hand_" + side, default_closed=True)
            header.prop(host, '["grip"]', text=label, slider=True)
            if body:
                for key in ("thumb_curl", "index_curl", "middle_curl", "ring_curl", "pinky_curl", "spread", "cup"):
                    body.prop(host, '["' + key + '"]', text=key.replace("_", " ").title(), slider=True)


class RIGGING_PT_pose(RiggingPanel, Panel):
    bl_label = "Pose Controls"

    @classmethod
    def poll(cls, context):
        return context.mode in {'OBJECT', 'POSE'} and RIGGING_PT_rig.poll(context)

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        active = context.active_pose_bone
        if active and "UI_" in active.name:
            for key in active.keys():
                if key.startswith(("mouth_","neck_","spine_","shoulder_","control_")):
                    layout.prop(active,'["'+bpy.utils.escape_identifier(key)+'"]',text=key)
        limbs = layout.grid_flow(row_major=True, columns=-2, even_columns=True)
        for control in obj.pose.bones:
            prop = control.get("blend_property")
            if prop:
                box = limbs.box()
                box.prop(obj, '["' + bpy.utils.escape_identifier(prop) + '"]', slider=True)
                if "elbow_glide" in control:
                    box.prop(control, '["elbow_glide"]', text="Elbow Glide", slider=True)
                    box.prop(control, '["forearm_rotation"]', text="Forearm Rotation (°)")
                if "knee_glide" in control:
                    box.prop(control, '["knee_glide"]', text="Knee Glide", slider=True)
                    box.prop(control, '["knee_internal_rotation"]', text="Tibial Rotation (°)")
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


classes = (XBONE_OT_workspace, XBONE_OT_biped_guide, XBONE_OT_import_guide, XBONE_OT_new_guide,
           XBONE_OT_copy_guide, XBONE_OT_tag_limb, XBONE_OT_add_component, XBONE_OT_build_rig,
           XBONE_OT_match_limb, XBONE_OT_edit_source_guide, XBONE_OT_return_to_rig, XBONE_OT_symmetrize_guide,
           XBONE_OT_toggle_guide_display, RIGGING_HT_header, RIGGING_PT_templates,
           *smart_guide.classes, RIGGING_PT_guide, RIGGING_PT_rig, *mesh_bindings.classes, RIGGING_PT_hands, RIGGING_PT_pose)
_register, _unregister = bpy.utils.register_classes_factory(classes)


def register():
    smart_guide.register()
    mesh_bindings.register()
    _register()
    selector.register()
    bpy.app.handlers.load_post.append(add_startup_workspace)


def unregister():
    bpy.app.handlers.load_post.remove(add_startup_workspace)
    selector.unregister()
    _unregister()
    mesh_bindings.unregister()
    smart_guide.unregister()
