# SPDX-License-Identifier: GPL-2.0-or-later
"""Classic Shifter chain scale, metacarpal interpolation and jaw/lip linkage."""
import bpy
from mathutils import Matrix, Vector

from .limb_parameters import rotation_input


def connect(rig, guide):
    arm = rig.data
    names = {b["biped_locator"]: b.name for b in guide.data.bones if "biped_locator" in b}
    unit = guide["biped_display_scale"]
    bpy.ops.object.mode_set(mode='EDIT')
    edit = arm.edit_bones

    def helper(name, frame, parent):
        bone = edit.new("MCH." + name)
        bone.head = frame.translation
        bone.tail = bone.head + Vector((0, unit, 0))
        bone.matrix = frame
        bone.parent = parent
        bone.use_deform = False
        for group in list(bone.collections):
            group.unassign(bone)
        arm.collections["Mechanism"].assign(bone)
        return bone

    digits = []
    digit_spaces = []
    palms = []
    for guide_bone in guide.data.bones:
        label = guide_bone.get("biped_locator", "")
        kind = label.split("_")[0]
        if kind not in {"finger", "thumb", "tongue"} or label.count("_") != 1:
            continue
        parent = edit[guide_bone.name].parent
        prev = None
        for i in range(4 if kind == "tongue" else 3):
            loc = names[label + (f"_{i - 1}_loc" if i else "")]
            fk = edit["FK." + loc]
            if prev:
                frame = prev.matrix.copy()
                frame.translation = fk.head
                ref = helper("DigitRef." + loc, frame, prev)
                space = helper("DigitSpace." + loc, frame, parent)
                fk.parent = space
                digit_spaces.append((space.name, ref.name, parent.name))
            prev = fk
            bind = label + f"_{i}_jnt"
            # Shifter's deform locators are siblings. A parent constraint
            # supplies position/orientation; scale comes from the FK local channels.
            scale = helper("DigitScale." + loc, fk.matrix.copy(), parent)
            edit["MCH.Bind." + bind].parent = scale
            digits.append((scale.name, fk.name, parent.name))

    for side in ("L0", "R0"):
        label = "meta_" + side
        root = edit[names[label]]
        master = edit["FK." + root.name]
        for i in range(4):
            loc = edit[names[label + (f"_{i - 1}_loc" if i else "")]]
            frame = master.matrix.copy()
            frame.translation = loc.head
            motion = helper(f"Palm.{label}.{i}", frame, root.parent)
            if i:
                edit["FK." + loc.name].parent = motion
            else:
                reference = helper("PalmRoot." + label, loc.matrix.copy(), motion)
                root_ref = (loc.name, reference.name)
            palms.append((motion.name, master.name, i / 3, root_ref if not i else (loc.name, "FK." + loc.name)))

    eyes = []
    for side in ('L0', 'R0'):
        eye = edit[names["eye_" + side]]
        over = eye.parent
        base = helper("EyeUp." + side, over.matrix.copy(), over.parent)
        world = helper("EyeAim." + side, Matrix.Identity(4), None)
        orient = helper("EyeRotation." + side, over.matrix.copy(), over)
        local_target = helper("EyeLocalTarget." + side, Matrix.Identity(4), None)
        local_up = helper("EyeLocalUp." + side, Matrix.Identity(4), None)
        holder = helper("EyeFrame." + side, over.matrix.copy(), None)
        out = helper("EyeResult." + side, eye.matrix.copy(), holder)
        f = base.matrix.copy()
        f.translation += f.to_3x3().col[1] * unit
        mark = helper("EyeUpDirection." + side, f, base)
        eyes.append((eye.name, over.name, base.name, mark.name, world.name, orient.name, holder.name, out.name,
                     local_target.name, local_up.name))
    mouth = edit[names["mouth_C0"]]
    jaw = edit["FK." + names["mouth_C0_jaw"]]
    jaw.parent = mouth
    frame = jaw.matrix.copy()
    frame.translation = edit[names["mouth_C0_rotcenter"]].head
    center = helper("MouthCenter", frame, edit[names["mouth_C0_rotcenter"]])
    frame.translation = mouth.head
    low = helper("MouthLower", frame, center)
    upper = helper("MouthUpper", frame, center)
    jaw_ref = helper("MouthJaw", edit[names["mouth_C0_jaw"]].matrix.copy(), low)
    for suffix, parent in (("up", upper), ("low", low)):
        edit["FK." + names["mouth_C0_lip" + suffix]].parent = parent
        edit["Display.mouth_C0_teeth" + suffix + "_ctl"].parent = parent
    edit["Display.faceUI_C0_ctl"].hide_select = False
    bpy.ops.object.mode_set(mode='POSE')
    pose = rig.pose.bones
    for eye, over, base, mark, world, orient, holder, out, local_target, local_up in eyes:
        c = pose[base].constraints.new('COPY_LOCATION')
        c.target, c.subtarget = rig, over
        c.owner_space = c.target_space = 'POSE'
        target = pose[eye].constraints[0].subtarget
        pose[eye].constraints.remove(pose[eye].constraints[0])
        for source, dest in ((target, local_target), (mark, local_up)):
            c = pose[dest].constraints.new('COPY_LOCATION')
            c.target, c.subtarget = rig, source
            c.owner_space, c.target_space = 'POSE', 'CUSTOM'
            c.space_object, c.space_subtarget = rig, over
        c = pose[world].constraints.new('DAMPED_TRACK')
        c.target, c.subtarget, c.track_axis = rig, local_target, 'TRACK_Z'
        c = pose[world].constraints.new('LOCKED_TRACK')
        c.target, c.subtarget = rig, local_up
        c.lock_axis, c.track_axis = 'LOCK_Z', 'TRACK_Y'
        c = pose[orient].constraints.new('COPY_ROTATION')
        c.target, c.subtarget = rig, world
        c.target_space, c.owner_space = 'POSE', 'LOCAL'
        for n, s in ((holder, orient), (eye, out)):
            c = pose[n].constraints.new('COPY_TRANSFORMS')
            c.target, c.subtarget = rig, s
            c.target_space = c.owner_space = 'POSE'
    for space, ref, parent in [*digit_spaces, *digits]:
        for kind in ('COPY_LOCATION', 'COPY_ROTATION'):
            c = pose[space].constraints.new(kind)
            c.target, c.subtarget = rig, ref
            c.target_space = c.owner_space = 'CUSTOM'
            c.space_object, c.space_subtarget = rig, parent
    for scale, fk, _ in digits:
        c = pose[scale].constraints.new('COPY_SCALE')
        c.target, c.subtarget = rig, fk
        c.target_space = c.owner_space = 'LOCAL'
    for motion, master, weight, ref in palms:
        if ref:
            con = pose[ref[0]].constraints[0]
            con.subtarget = ref[1]
            con.target_space = con.owner_space = 'POSE'
        p = pose[motion]
        for kind in ('COPY_LOCATION', 'COPY_ROTATION', 'COPY_SCALE'):
            con = p.constraints.new(kind)
            con.target, con.subtarget = rig, master
            con.target_space = con.owner_space = 'LOCAL'
            con.influence = weight

    con = pose[names["mouth_C0_jaw"]].constraints[0]
    con.subtarget = "MCH.MouthJaw"
    con.target_space = con.owner_space = 'POSE'
    face = pose["Display.faceUI_C0_ctl"]
    face.lock_location = face.lock_rotation = face.lock_scale = (True, True, True)
    for key, value, limit in (("siderot", 20, 100), ("vertrot", 40, 100), ("fronttrans", 1, 1),
                              ("verttrans", .2, 1), ("floowlips", .05, 1), ("lipsAlignSpeed", 10, 100)):
        face["mouth_" + key] = float(value)
        face.id_properties_ui("mouth_" + key).update(default=float(value), min=0, max=limit)
    jaw = pose["FK." + names["mouth_C0_jaw"]]
    jaw.rotation_mode = 'XYZ'
    jaw.lock_rotation = (True, True, False)
    jaw.lock_scale = (True, True, True)
    inputs = {"x": jaw.path_from_id("location") + "[0]", "y": jaw.path_from_id("location") + "[1]",
              "z": jaw.path_from_id("location") + "[2]", "rz": jaw.path_from_id("rotation_euler") + "[2]"}
    for alias, key in (("side", "siderot"), ("vert", "vertrot"), ("front", "fronttrans"), ("lift", "verttrans"),
                       ("f", "floowlips"), ("speed", "lipsAlignSpeed")):
        inputs[alias] = face.path_from_id() + '["mouth_' + key + '"]'

    def channel(owner, prop, i, expr, aliases):
        driver = pose[owner].driver_add(prop, i).driver
        for alias in aliases.split():
            if alias == 'rz':
                rotation_input(driver, alias, jaw, 'Z')
                continue
            var = driver.variables.new()
            var.name = alias
            var.targets[0].id = rig
            var.targets[0].data_path = inputs[alias]
        driver.expression = expr
    for name in ("MCH.MouthUpper", "MCH.MouthLower", "MCH.MouthCenter"):
        pose[name].rotation_mode = 'XYZ'
    gain = 1 / unit
    rx = f"radians(-y*vert*{gain:.12g})"
    ry = f"radians(x*side*{gain:.12g})"
    clamp = f"min(1,max(0,y*speed*{gain:.12g}))"
    channel("MCH.MouthCenter", "rotation_euler", 2, "rz", "rz")
    channel("MCH.MouthLower", "rotation_euler", 0, rx, "y vert")
    channel("MCH.MouthLower", "rotation_euler", 1, ry, "x side")
    channel("MCH.MouthLower", "location", 1, "min(0,y*lift)", "y lift")
    channel("MCH.MouthLower", "location", 2, "z*front", "z front")
    channel("MCH.MouthUpper", "rotation_euler", 0, f"f*{rx}+(1-f)*min(0,{rx})", "f y vert")
    channel("MCH.MouthUpper", "rotation_euler", 1, f"{ry}*(f+(1-f)*{clamp})", "x side f y speed")
    channel("MCH.MouthUpper", "location", 1, "f*min(0,y*lift)", "f y lift")
    channel("MCH.MouthUpper", "location", 2, f"z*front*(f+(1-f)*{clamp})", "z front f y speed")
