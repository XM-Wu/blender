# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise the two knee centers at the controls and on the actual skin skeleton."""
import json
import math
import tempfile
from pathlib import Path

import bpy
from mathutils import Matrix, Vector
import xbone_rigging


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

    for angle in (30, 90, 150, 160):
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
    ctl["maxstretch"] = 1.
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


bpy.ops.wm.read_factory_settings(use_empty=True)
assert bpy.ops.xbone.biped_guide(template='xbiped') == {'FINISHED'}
guide = bpy.context.object
assert guide["xbiped"]
assert bpy.ops.xbone.build_rig() == {'FINISHED'}
rig = bpy.context.object
with Path(xbone_rigging.__file__).with_name("biped_bind.json").open() as stream:
    reference = json.load(stream)
skin = {b.name: b.parent.name if b.parent else None for b in rig.data.bones if b.use_deform}
assert skin == {name: entry[0] for name, entry in reference.items()}
for comp in ("leg_L0", "leg_R0"):
    check_leg(rig, comp)
assert not any(not f.driver.is_valid for f in rig.animation_data.drivers)
rig_name = rig.name
saved = {n: rig.pose.bones[n].head.copy() for n in
         ("leg_L0_knee", "leg_L0_4_jnt", "leg_R0_knee", "leg_R0_end_jnt")}
assert bpy.ops.xbone.edit_source_guide() == {'FINISHED'}
assert bpy.ops.xbone.build_rig() == {'FINISHED'} and bpy.context.object == rig
with tempfile.TemporaryDirectory() as directory:
    path = str(Path(directory) / "xbiped.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path)
    bpy.ops.wm.open_mainfile(filepath=path)
    rig = bpy.data.objects[rig_name]
    assert rig["rigging_guide"]["xbiped"]
    assert "knee_glide" in rig.pose.bones["IK.leg_R0_ankle"]
    for name, location in saved.items():
        close(rig.pose.bones[name].head, location, ("rebuilt and reopened", name), .0005)
    assert not any(not f.driver.is_valid for f in rig.animation_data.drivers)
print("XBiped bilateral knee glide, skin joints, IK/FK matching, planted feet, rebuild and reload passed")
