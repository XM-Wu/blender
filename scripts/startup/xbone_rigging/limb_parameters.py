# SPDX-License-Identifier: GPL-2.0-or-later
"""Shifter two-joint length, softness, slide and reverse feeding XBone pivot IK."""

PARAMETERS = (
    ("roll", "Roll", 0.0, -180, 180),
    ("ikscale", "IK Scale", 1.0, 0.001, 99),
    ("maxstretch", "Max Stretch", 1.5, 1, 99),
    ("slide", "Slide", 0.5, 0, 1),
    ("softness", "Softness", 0.0, 0, 1),
    ("reverse", "Reverse", 0.0, 0, 1),
)


def rotation_input(driver, name, bone, axis):
    """Read an animator channel as XYZ even when an existing rig uses quaternions."""
    var = driver.variables.new()
    var.name = name
    var.type = 'TRANSFORMS'
    target = var.targets[0]
    target.id = bone.id_data
    target.bone_target = bone.name
    target.transform_space = 'TRANSFORM_SPACE'
    target.rotation_mode = 'XYZ'
    target.transform_type = 'ROT_' + axis


def drive(owner, path, expression, variables, index=None):
    curve = owner.driver_add(path) if index is None else owner.driver_add(path, index)
    driver = curve.driver
    for alias, (bone, key) in variables.items():
        var = driver.variables.new()
        var.name = alias
        target = var.targets[0]
        target.id = owner.id_data
        if key == "SCALE_Y":
            var.type = 'TRANSFORMS'
            target = var.targets[0]
            target.id = owner.id_data
            target.bone_target = bone.name
            target.transform_type = key
            target.transform_space = 'LOCAL_SPACE'
        else:
            target.data_path = bone.path_from_id() + '["' + key + '"]'
    driver.expression = expression
    return driver


def configure(rig, target, origin, mechanism, limit):
    bones = rig.data.bones
    a, b, c = (bones[n] for n in mechanism)
    first = (a.head_local - b.head_local).length
    second = (b.head_local - c.head_local).length
    total = first + second
    for key, label, initial, low, high in PARAMETERS:
        target[key] = float(limit if key == "maxstretch" else initial)
        target.id_properties_ui(key).update(min=low, max=high, default=target[key], description=label)
    # Lengths below are normalized by total rest length; the original Shifter
    # softness envelope is one tenth of the scaled two-bone length.

    def calculation(name, expr, **variables):
        origin[name] = 1.0
        drive(origin, '["' + name + '"]', expr, variables)
        return origin, name

    origin["distance"] = 1.0
    d = origin, "distance"
    capped = calculation("capped", "min(d,limit)", d=d, limit=(target, "maxstretch"))
    soft = calculation("soft", "0.1*s", s=(target, "softness"))
    stretch = calculation("stretch",
                          "c/max(0.000001,1-s*exp(-(d-1+s)/s)) if s>0 and d>1-s else max(1,c)",
                          d=d, c=capped, s=soft)
    calculation("length", "scale*(k-2*min(r,1-r)*(k-c))",
                scale=(target, "ikscale"), k=stretch, c=capped, r=(target, "reverse"))
    calculation("upper_share", f"{first / total:.12g}*2*s if s<0.5 else 1-{second / total:.12g}*2*(1-s)",
                s=(target, "slide"))
