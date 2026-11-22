# SPDX-License-Identifier: GPL-2.0-or-later
"""A sagittal two-center knee. All runtime evaluation uses native constraints and drivers."""

import math
from mathutils import Matrix, Quaternion

from .limb_parameters import drive
from .builder import copy_pose
from . import biped_display


# An artistic profile in character proportions. The slider scales the peak
# anterior displacement of the proximal tibia relative to the distal femur.
BASE_TRAVEL = 0.035  # fraction of shin length at unit gain
MAX_GAIN = 4
PROFILE = "smoothstep(-0.5,0.906307787,-c)"  # cosine of flexion, 60 to 155 degrees


def set_leg_defaults(guide):
    """Migrate the inherited Shifter limit once; retain edited XBiped guide settings."""
    for bone in guide.data.bones:
        if bone.get("ik_limb") not in {"leg_L0", "leg_R0"}:
            continue
        if "maxstretch" not in bone:
            bone["maxstretch"] = 1.0
        ui = bone.id_properties_ui("maxstretch")
        if ui.as_dict()["default"] != 1.0:
            if bone["maxstretch"] == 1.5:
                bone["maxstretch"] = 1.0
            ui.update(min=1, default=1.0)


def create_fk(edit, helper, comp, tip, pole_direction):
    hip, knee = tip.parent.parent, tip.parent
    femur = knee.head_local - hip.head_local
    tibia = tip.head_local - knee.head_local
    y = tibia.normalized()
    x = (pole_direction - y * pole_direction.dot(y)).normalized()
    if not x.length_squared:
        x = y.orthogonal().normalized()
    basis = Matrix((x, y, x.cross(y))).transposed().to_4x4()
    fk_hip, fk_knee, fk_ankle = (edit["FK." + bone.name] for bone in (hip, knee, tip))

    def marker(label, position, parent):
        frame = basis.copy()
        frame.translation = position
        return helper(comp + "." + label, frame, parent)

    # A positive local X bend moves the thigh forward and folds the shin
    # back. Both segments stay rigid and the original FK channels remain additive.
    hinge = Matrix((x.cross(y), y, -x)).transposed().to_4x4()
    hinge.translation = fk_hip.head
    thigh_follow = helper(comp + ".fkThighFollow", hinge, fk_hip)
    hinge.translation = fk_knee.head
    control = edit.new("FK." + comp + "_knee_bend")
    control.head, control.tail = fk_knee.head, fk_knee.head + y * tibia.length * 0.21
    control.matrix = hinge
    control.parent = thigh_follow
    control.use_deform = False
    for coll in list(control.collections):
        coll.unassign(control)
    for coll in fk_knee.collections:
        coll.assign(control)
    fk_knee.parent = control
    edit["MCH.FKOutput." + hip.name].parent = thigh_follow
    center = marker("fkFemur", fk_knee.head, thigh_follow)
    up = marker("fkUpperRay", fk_knee.head + femur.normalized() * tibia.length, center)
    end = marker("fkLowerRay", fk_knee.head + tibia, fk_knee)
    axes = marker("fkGlideAxes", fk_knee.head, fk_knee)
    slider = marker("fkTibia", fk_knee.head, axes)
    pivot = helper(comp + ".fkPivot", fk_knee.matrix.copy(), fk_knee)
    edit["MCH.FKOutput." + knee.name].parent = pivot
    fk_ankle.parent = pivot
    for label in ("fkDelta", "ikDelta", "glideDelta"):
        helper(comp + "." + label, Matrix.Identity(4), None)
    return (fk_knee.name, center.name, up.name, end.name, axes.name, slider.name, pivot.name, tibia.length)


def configure_bend(rig, comp, target, shin_length):
    bend = rig.pose.bones["FK." + comp + "_knee_bend"]
    bend.rotation_mode = 'XYZ'
    bend.lock_location = bend.lock_scale = (True, True, True)
    bend.lock_rotation = (False, True, True)
    # Outboard arc on the side of the knee, clear of the central mid control.
    lateral = shin_length * .19 * (1 if comp.endswith("L0") else -1)
    radius = shin_length * .16
    arc = [(lateral, radius * math.cos(t), radius * math.sin(t))
           for t in (math.radians(i) for i in range(-60, 211, 9))]
    base = arc[-3]
    arrow = [(base[0] - radius * .18, base[1], base[2]), arc[-1],
             (base[0] + radius * .18, base[1], base[2])]
    widget = biped_display.shape(bend.name, [arc, arrow])
    biped_display.configure(bend, widget, 18 if comp.endswith("L0") else 14)
    target["knee_fk_control"] = bend.name
    bend["limb_control"] = target.name
    follow = rig.pose.bones["MCH.Triangle." + comp + ".fkThighFollow"]
    follow.rotation_mode = 'XYZ'
    driver = follow.driver_add("rotation_euler", 0).driver
    var = driver.variables.new()
    var.name = "bend"
    var.targets[0].id = rig
    # The raw animator channel is independent of the parent's evaluated pose.
    var.targets[0].data_path = bend.path_from_id("rotation_euler") + "[0]"
    driver.expression = "-0.1*bend"
    driver = bend.driver_add("hide").driver
    var = driver.variables.new()
    var.name = "blend"
    var.targets[0].id = rig
    var.targets[0].data_path = '["' + target["blend_property"] + '"]'
    driver.expression = "blend >= 1"


def match_pole(rig, target, matrices):
    """Recover the input pole from the femoral bend plane before roll and reverse."""
    hip, tibia, ankle = (m.translation for m in matrices)
    comp = rig.pose.bones[target["joints"][-1]].bone["ik_limb"]
    femur = tibia + rig.pose.bones["MCH.Triangle." + comp + ".glideDelta"].head
    axis = (ankle - hip).normalized()
    radial = femur - hip - axis * (femur - hip).dot(axis)
    if radial.length_squared < 1e-12 or not axis.length_squared:
        return rig.pose.bones[target["pole_control"]].head.copy()
    sign = 1 if comp.endswith("R0") else -1
    cancel_roll = Quaternion(axis, -math.radians(target["roll"]) * sign)
    direction = cancel_roll @ radial.normalized()
    if target["reverse"] > 0.5:
        direction.negate()
    radius = radial.length + 0.6 * ((femur - hip).length + (ankle - tibia).length)
    return femur - radial + direction * radius


def distance(owner, name, first, second):
    owner[name] = 0.0
    driver = owner.driver_add('["' + name + '"]').driver
    driver.type = 'AVERAGE'
    var = driver.variables.new()
    var.type = 'LOC_DIFF'
    for target, bone in zip(var.targets, (first, second)):
        target.id = owner.id_data
        target.bone_target = bone
    return owner, name


def solve(rig, target, comp, fk, calc, a, b, d, length):
    pose = rig.pose.bones
    prefix = "MCH.Triangle." + comp + "."
    knee, center, up, end, axes, slider, pivot, rest_shin = fk
    configure_bend(rig, comp, target, rest_shin)
    target["knee_glide"] = 1.0
    target.id_properties_ui("knee_glide").update(min=0, max=MAX_GAIN, default=1.0,
                                                 description="Deep-flexion travel multiplier (1 = 3.5% of shin length)")
    target["knee_internal_rotation"] = 6.0
    target.id_properties_ui("knee_internal_rotation").update(
        min=0.0, max=30.0, default=6.0, description="Peak inward rotation of the shin in degrees")
    target["knee_glide_pivot"] = pivot
    copy_pose(pose[center], rig, knee, 'COPY_LOCATION')
    aim = copy_pose(pose[axes], rig, end, 'DAMPED_TRACK')
    aim.track_axis = 'TRACK_Y'
    anterior = copy_pose(pose[axes], rig, up, 'LOCKED_TRACK')
    anterior.lock_axis, anterior.track_axis = 'LOCK_Y', 'TRACK_X'
    # Both direction rays originate at the FK knee, including when the FK
    # control is translated. Twist alone must not change the measured bend.
    owner = pose[center]
    upper = distance(owner, "upper_ray", knee, up)
    lower = distance(owner, "lower_ray", knee, end)
    chord = distance(owner, "ray_chord", up, end)
    cos_fk = calc("fk_knee_cos", "clamp((u*u+v*v-w*w)/max(1e-12,2*u*v),-1,1)", u=upper, v=lower, w=chord)
    gain = calc("glide_gain", f"clamp(g,0,{MAX_GAIN})", g=(target, "knee_glide"))
    fk_weight = calc("glide_fk_weight", "g*" + PROFILE, g=gain, c=cos_fk)
    drive(pose[slider], "location", f"{rest_shin * BASE_TRAVEL:.12g}*w", {"w": fk_weight}, 0)
    copy_pose(pose[pivot], rig, slider, 'COPY_LOCATION')

    # H=hip, F=femoral end, T=tibial start, A=ankle. FT is perpendicular
    # to TA. Thus FA^2=b^2+s^2. Solve HFA, then recover T analytically.
    # The slide depends on the true angle between HF and TA. Fixed-point
    # refinement covers travel up to 14 percent of the shin.
    peak = calc("glide_peak", f"{BASE_TRAVEL}*b*g", b=b, g=gain)
    reach = calc("triangle_reach", "clamp(d,max(1e-7,sqrt((a-b)*(a-b)+p*p)),a+b)", d=d, a=a, b=b, p=peak)
    s = calc("glide_seed", "0.0")
    for i in range(7):
        effective = calc(f"glide_length_{i}", "sqrt(b*b+s*s)", b=b, s=s)
        projected = calc(f"glide_cos_{i}", "clamp((d*d-a*a-e*e)/max(1e-12,2*a*e),-1,1)",
                         d=reach, a=a, e=effective)
        cosine = calc(f"bend_cos_{i}", "(q*b-sqrt(max(0,1-q*q))*s)/max(1e-9,e)", q=projected, b=b, s=s, e=effective)
        s = calc(f"glide_{i}", "p*" + PROFILE, p=peak, c=cosine)
    # The secondary tibial rotation uses this same solved fraction. It acts
    # about the shin axis so the ankle target and foot orientation stay pinned.
    ik_weight = calc("glide_ik_weight", f"s/max(1e-9,b*{BASE_TRAVEL})", s=s, b=b)
    weight_bone, _ = fk_weight
    weight_bone["knee_coupling"] = 0.0
    driver = drive(weight_bone, '["knee_coupling"]', "(f+(i-f)*blend)/max(1,g)",
                   {"f": fk_weight, "i": ik_weight, "g": gain})
    var = driver.variables.new()
    var.name = "blend"
    var.targets[0].id = rig
    var.targets[0].data_path = '["' + target["blend_property"] + '"]'
    along = calc("triangle_along", "clamp((a*a+d*d-b*b-s*s)/(2*d),-a,a)", a=a, b=b, d=reach, s=s)
    side = calc("femur_x", "sqrt(max(0,a*a-y*y))*(-1 if r>0.5 else 1)", a=a, y=along, r=(target, "reverse"))
    # E=A-F=(-x,d-y). In the bend plane T=F+s*(s*E+b*perp(E))/(b^2+s^2).
    variables = {"s": s, "b": b, "x": side, "y": along, "d": reach, "r": (target, "reverse")}
    for axis, expr in enumerate(("x+s*(-s*x+b*(d-y)*(-1 if r>0.5 else 1))/max(1e-18,b*b+s*s)",
                                 "y+s*(s*(d-y)+b*x*(-1 if r>0.5 else 1))/max(1e-18,b*b+s*s)")):
        drive(pose[prefix + "tibia"], "location", f"({expr})*{length:.12g}", variables, axis)

    # Difference vectors keep the two roll-spline spans attached to their own
    # joint centers while preserving animator offsets on the shared mid control.
    for name, femur, tibia in (("fkDelta", knee, pivot), ("ikDelta", prefix + "mid", prefix + "tibia")):
        delta = pose[prefix + name]
        copy_pose(delta, rig, femur, 'COPY_LOCATION')
        subtract = copy_pose(delta, rig, tibia, 'COPY_LOCATION')
        subtract.use_offset = True
        subtract.invert_x = subtract.invert_y = subtract.invert_z = True
    delta = pose[prefix + "glideDelta"]
    copy_pose(delta, rig, prefix + "fkDelta", 'COPY_LOCATION')
    mix = copy_pose(delta, rig, prefix + "ikDelta", 'COPY_LOCATION')
    drv = mix.driver_add("influence").driver
    drv.type = 'AVERAGE'
    var = drv.variables.new()
    var.targets[0].id = rig
    var.targets[0].data_path = '["' + target["blend_property"] + '"]'
    return reach, along
