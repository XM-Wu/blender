# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise the two knee centers at the controls and on the actual skin skeleton."""
import json
import math
import tempfile
from pathlib import Path

import bpy
from mathutils import Matrix, Vector
import xbone_rigging
from xbone_rigging import selector_board


def update(rig):
    rig.update_tag()
    bpy.context.view_layer.update()


def close(actual, expected, message, tolerance=0.00015):
    assert (actual - expected).length < tolerance, (message, tuple(actual), tuple(expected))


def check_leg(rig, comp):
    p = rig.pose.bones
    tip = comp + "_ankle"
    knee = comp + "_knee"
    ctl = p["IK." + tip]
    fk = p["FK." + knee]
    prefix = "MCH.Triangle." + comp + "."
    hip, mid, ankle = [p[n].bone.head_local.copy() for n in (comp, knee, tip)]
    u, v = mid - hip, ankle - mid
    axis = u.cross(v).normalized()
    a, b = u.length, v.length
    strength = "knee_glide"
    blend = ctl["blend_property"]
    rig[blend] = 0.
    update(rig)
    rest = fk.matrix.copy()

    ui = ctl.id_properties_ui(strength).as_dict()
    assert ui["default"] == 1 and ui["max"] == 4
    for angle, gain in ((30, 1), (90, 1), (150, 1), (160, 1), (160, 2), (105, 4), (160, 4)):
        ctl[strength] = gain
        frame = Matrix.Rotation(math.radians(angle) - u.angle(v), 4, axis) @ rest
        frame.translation = rest.translation
        fk.matrix = frame
        update(rig)
        f = fk.head.copy()
        t = p[knee].head.copy()
        end = p[tip].head.copy()
        close(t, p[prefix + "fkPivot"].head, "FK tibia")
        close(end, p["FK." + tip].head, "FK ankle")
        assert abs((t - end).length - b) < 0.00015 and abs((f - p[comp].head).length - a) < 0.00015
        assert abs(math.degrees((f - p[comp].head).angle(end - t)) - angle) < .03
        glide = (t - f).length
        if angle == 160:
            assert abs(glide - .035*b*gain) < .00015
        if gain > 1:
            coupling = p["MCH.Reach." + comp]["knee_coupling"]
            assert abs(coupling - glide/(.035*b*gain)) < .001
        if angle <= 60:
            assert glide < 0.00015
        else:
            assert glide > .001 and abs((t - f).dot((end - t).normalized())) < 0.00015
        close(p["MCH.Curve." + comp + ".femoralEnd"].head, f, "FK upper skin anchor")
        close(p["Display." + comp + "_mid_ctl"].head, t, "FK lower skin anchor")
        before = {n: p[n].matrix.copy() for n in (comp, knee, tip, comp + "_3_jnt", comp + "_4_jnt", comp + "_end_jnt")}
        for mode in ('IK', 'FK'):
            assert bpy.ops.xbone.match_limb(control=ctl.name, mode=mode) == {'FINISHED'}
            update(rig)
            for name, previous in before.items():
                close(p[name].head, previous.translation, (angle, mode, name), .00045)
                assert abs(p[name].matrix.to_quaternion().dot(previous.to_quaternion())) > .999, (angle, mode, name)
            if mode == 'IK':
                close(p[prefix + "mid"].head, f, "IK femur")
                close(p[prefix + "tibia"].head, t, "IK tibia")
                close(p[tip].head, p[ctl["foot_effector"]].head, "IK effector")

    ctl[strength] = 1.
    rig[blend] = 1.
    update(rig)
    planted = p[tip].head.copy()
    femur = p[prefix + "mid"]
    tibia = p[prefix + "tibia"]
    assert (femur.head - tibia.head).length > .03 * b
    # Check that changing glide changes weighted joint positions with the same IK target.
    skin = p[comp + "_4_jnt"]
    before = skin.head.copy()
    ctl[strength] = 0.
    update(rig)
    close(tibia.head, femur.head, "glide off")
    close(p[tip].head, planted, "pinned ankle")
    assert (skin.head - before).length > .001
    ctl[strength] = 1.
    update(rig)
    close(skin.head, before, "glide on")
    shaft = p[comp + "_6_jnt"]
    foot = p[comp + "_end_jnt"]
    twisted = shaft.matrix.to_quaternion()
    foot_frame = foot.matrix.copy()
    ctl["knee_internal_rotation"] = 0.
    update(rig)
    angle = twisted.rotation_difference(shaft.matrix.to_quaternion()).angle
    assert abs(min(angle, math.tau - angle) - math.radians(6)) < .002, (comp, angle)
    assert abs(foot_frame.to_quaternion().dot(foot.matrix.to_quaternion())) > .99999
    close(foot.head, foot_frame.translation, "tibial rotation / planted ankle")
    ctl["knee_internal_rotation"] = 6.
    pole = p[ctl["pole_control"]]
    pole.location.x += .1
    update(rig)
    close(p[tip].head, planted, "pole / ankle")
    pole.location.x -= .1

    body = p["FK.body_C0"]
    body.location.z -= .035
    update(rig)
    close(p[tip].head, planted, "moving parent / planted foot")
    body.location.z += .035
    update(rig)
    ctl["roll"] = 24.0
    pose_goal = ctl.matrix.copy()
    pose_goal.translation += Vector((.045, .023, -.020))
    ctl.matrix = pose_goal
    update(rig)
    locked = {n: p[n].matrix.copy() for n in (comp, knee, tip, comp + "_4_jnt", comp + "_end_jnt")}
    for mode in ('FK', 'IK'):
        assert bpy.ops.xbone.match_limb(control=ctl.name, mode=mode) == {'FINISHED'}
        for name, saved in locked.items():
            close(p[name].head, saved.translation, ("rolled knee", mode, name), .0005)
    rig[blend] = .5
    update(rig)
    for name, saved in locked.items():
        close(p[name].head, saved.translation, ("matched blend", name), .0005)
    rig[blend] = 1.
    update(rig)
    assert ctl["maxstretch"] == ctl.id_properties_ui("maxstretch").as_dict()["default"] == 1.0
    ctl["reverse"] = 1.
    update(rig)
    reversed_knee = p[knee].head.copy()
    for mode in ('FK', 'IK'):
        assert bpy.ops.xbone.match_limb(control=ctl.name, mode=mode) == {'FINISHED'}
        close(p[knee].head, reversed_knee, ("reversed knee", mode), .0005)
    ctl["reverse"] = 0.
    update(rig)
    foot_roll = p["Display.foot_" + comp.split("_")[1] + "_roll_ctl"]
    foot_roll.rotation_euler.x = .22
    update(rig)
    close(p[tip].head, p[ctl["foot_effector"]].head, "reverse-foot IK")
    foot_roll.rotation_euler.x = 0.
    update(rig)
    far = ctl.matrix.copy()
    far.translation.z -= 2 * a + 2 * b
    ctl.matrix = far
    update(rig)
    close(tibia.head, femur.head, "extended knee")
    assert (p[tip].head - p[ctl["foot_effector"]].head).length > .05
    ctl.matrix = pose_goal
    update(rig)


def check_bend_control(rig, comp):
    p = rig.pose.bones
    arm = comp.startswith("arm")
    ctl = p["IK." + comp + ("_wrist" if arm else "_ankle")]
    bend = p[ctl["elbow_fk_control" if arm else "knee_fk_control"]]
    assert bend.custom_shape and not bend.bone.use_deform and not bend.bone.hide_select
    assert bend.bone.collections.get("FK")
    assert bend.name in selector_board.pages(selector_board.layout_source(rig))[0].names
    rig[ctl["blend_property"]] = 0.
    for name in ctl["fk_controls"]:
        p[name].matrix_basis = Matrix.Identity(4)
    update(rig)
    assert not bend.hide
    hip, knee, ankle = (p[n] for n in ctl["joints"])
    hip_rest, knee_rest, ankle_rest = (joint.head.copy() for joint in (hip, knee, ankle))
    skin = p[comp + "_4_jnt"]
    skin_rest = skin.head.copy()
    upper_length = (knee_rest - hip_rest).length
    lower_length = (ankle_rest - knee_rest).length
    rest_angle = (knee_rest-hip_rest).angle(ankle_rest-knee_rest)
    for degrees in (25, 120):
        bend.rotation_euler.x = math.radians(degrees)
        update(rig)
        femur = p["MCH.Curve." + comp + (".humeralPivot" if arm else ".femoralPivot")].head
        close(hip.head, hip_rest, "hip fixed")
        close(femur, bend.head, "handle follows moving knee")
        assert abs((femur-hip.head).length-upper_length) < .00015
        assert abs((ankle.head-knee.head).length-lower_length) < .00015
        assert (skin.head-skin_rest).length > .02*upper_length and (ankle.head-ankle_rest).length > .05
        angle = (femur-hip.head).angle(ankle.head-knee.head)
        assert abs(math.degrees(angle-rest_angle)-degrees) < .05, (comp, degrees, angle)
    saved = [(joint, joint.matrix.copy()) for joint in (hip, knee, ankle, skin)]
    for mode in ('IK', 'FK'):
        assert bpy.ops.xbone.match_limb(control=ctl.name, mode=mode) == {'FINISHED'}
        update(rig)
        assert bend.hide == (mode == 'IK')
        for joint, matrix in saved:
            close(joint.head, matrix.translation, (comp, mode, joint.name), .0005)
            assert abs(joint.matrix.to_quaternion().dot(matrix.to_quaternion())) > .999
    assert abs(bend.rotation_euler.x-math.radians(120)) < .0001


def check_elbow(rig, comp):
    p = rig.pose.bones
    ctl = p["IK." + comp + "_wrist"]
    elbow = p[comp + "_elbow"]
    wrist = p[comp + "_wrist"]
    bend = p[ctl["elbow_fk_control"]]
    proximal = p["MCH.Curve." + comp + ".humeralPivot"]
    length = (wrist.bone.head_local - elbow.bone.head_local).length
    assert ctl["maxstretch"] == 1
    assert ctl.id_properties_ui("elbow_glide").as_dict()["max"] == 4
    bend.rotation_euler.x = math.radians(151)
    for gain in (0, 1, 2, 4):
        ctl["elbow_glide"] = gain
        update(rig)
        assert abs((elbow.head - proximal.head).length - gain*.035*length) < .00025
        original = wrist.matrix.copy()
        assert bpy.ops.xbone.match_limb(control=ctl.name, mode='IK') == {'FINISHED'}
        update(rig)
        close(wrist.head, ctl.head, "elbow match / wrist pinned")
        close(wrist.head, original.translation, "FK to wrist IK")
        assert abs(wrist.matrix.to_quaternion().dot(original.to_quaternion())) > .999
        assert bpy.ops.xbone.match_limb(control=ctl.name, mode='FK') == {'FINISHED'}
    bpy.ops.xbone.match_limb(control=ctl.name, mode='IK')
    ctl["rotation_space"] = 1
    update(rig)
    fixed = wrist.matrix.copy()
    ctl["elbow_glide"] = 0
    update(rig)
    close(wrist.head, fixed.translation, "wrist lock changing elbow glide")
    assert abs(wrist.matrix.to_quaternion().dot(fixed.to_quaternion())) > .999
    orientation = p[ctl["wrist_rotation"]]
    orientation.matrix = orientation.matrix @ Matrix.Rotation(.4, 4, 'X')
    update(rig)
    close(wrist.head, fixed.translation, "independent wrist rotation")
    assert abs(wrist.matrix.to_quaternion().dot(fixed.to_quaternion())) < .99
    ctl["elbow_glide"] = 2.
    update(rig)
    shaft = p[comp + "_6_jnt"]
    twisted = shaft.matrix.to_quaternion()
    wrist_frame = wrist.matrix.copy()
    ctl["forearm_rotation"] = 0.
    update(rig)
    twist = twisted.rotation_difference(shaft.matrix.to_quaternion()).angle
    assert abs(min(twist, math.tau-twist) - math.radians(6)) < .003, (comp, twist)
    close(wrist.head, wrist_frame.translation, "forearm rotation / wrist position")
    assert abs(wrist_frame.to_quaternion().dot(wrist.matrix.to_quaternion())) > .99999
    ctl["forearm_rotation"] = 6.
    pole = p[ctl["pole_control"]]
    original_pole = pole.matrix.copy()
    shifted = original_pole.copy()
    shifted.translation += Vector((.05, .03, .1))
    before = elbow.head.copy()
    pole.matrix = shifted
    update(rig)
    assert (elbow.head-before).length > .001
    close(wrist.head, wrist_frame.translation, "moving elbow pole / pinned wrist")
    pole.matrix = original_pole
    update(rig)


bpy.ops.wm.read_factory_settings(use_empty=True)
cursor_position = Vector((3, -4, 2))
bpy.context.scene.cursor.location = cursor_position
assert bpy.ops.xbone.biped_guide(template='xbiped') == {'FINISHED'}
guide = bpy.context.object
close(guide.matrix_world.translation, Vector((0, 0, 0)), "guide at world origin")
assert guide["xbiped"]
for side in ("L0", "R0"):
    assert guide.data.bones[f"leg_{side}_ankle"]["maxstretch"] == 1
assert bpy.ops.xbone.build_rig() == {'FINISHED'}
rig = bpy.context.object
close(rig.matrix_world.translation, Vector((0, 0, 0)), "rig at world origin")
close(bpy.context.scene.cursor.location, cursor_position, "cursor position preserved")
with Path(xbone_rigging.__file__).with_name("biped_bind.json").open() as stream:
    reference = json.load(stream)
skin = {b.name: b.parent.name if b.parent else None for b in rig.data.bones if b.use_deform}
assert skin == {name: entry[0] for name, entry in reference.items()}
for comp in ("leg_L0", "leg_R0"):
    check_leg(rig, comp)
    check_bend_control(rig, comp)
for comp in ("arm_L0", "arm_R0"):
    check_bend_control(rig, comp)
    check_elbow(rig, comp)
assert not any(not f.driver.is_valid for f in rig.animation_data.drivers)
rig_name = rig.name
rig.pose.bones["IK.leg_R0_ankle"]["knee_glide"] = 2.5
update(rig)
saved = {n: rig.pose.bones[n].head.copy() for n in
         ("leg_L0_knee", "leg_L0_4_jnt", "leg_R0_knee", "leg_R0_end_jnt",
          "arm_L0_elbow", "arm_L0_4_jnt", "arm_R0_wrist")}
assert bpy.ops.xbone.edit_source_guide() == {'FINISHED'}
# Simulate a previous-version guide and one explicitly authored rig override.
bpy.ops.object.mode_set(mode='OBJECT')
old = guide.data.bones["leg_L0_ankle"]
old["maxstretch"] = 1.5
old.id_properties_ui("maxstretch").update(default=0.)
rig.pose.bones["IK.leg_R0_ankle"]["maxstretch"] = 1.25
assert bpy.ops.xbone.build_rig() == {'FINISHED'} and bpy.context.object == rig
with tempfile.TemporaryDirectory() as directory:
    path = str(Path(directory) / "xbiped.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path)
    bpy.ops.wm.open_mainfile(filepath=path)
    rig = bpy.data.objects[rig_name]
    assert rig["rigging_guide"]["xbiped"]
    assert "knee_glide" in rig.pose.bones["IK.leg_R0_ankle"]
    assert rig.pose.bones["IK.leg_R0_ankle"]["knee_glide"] == 2.5
    assert rig.pose.bones["IK.leg_L0_ankle"]["maxstretch"] == 1.
    assert rig.pose.bones["IK.leg_R0_ankle"]["maxstretch"] == 1.25
    assert abs(rig.pose.bones["FK.leg_L0_knee_bend"].rotation_euler.x-math.radians(120)) < .0001
    for name, location in saved.items():
        close(rig.pose.bones[name].head, location, ("rebuilt and reopened", name), .0005)
    assert not any(not f.driver.is_valid for f in rig.animation_data.drivers)
print("XBiped elbow and knee glide, skin joints, IK/FK, wrist rotation, rebuild and reload passed")
