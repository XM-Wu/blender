# SPDX-License-Identifier: GPL-2.0-or-later
"""Additive XBiped hand poses in the fitted guide's palm and finger frames."""

from math import radians

from mathutils import Matrix, Vector

from . import biped_display
from .limb_parameters import drive


# Flexion (knuckle, middle, distal) and knuckle fan at full strength, in degrees.
DIGITS = (
    ("thumb",  "thumb_{}0",  (29, 30, 27), 18),
    ("index",  "finger_{}0", (65, 85, 55), 12),
    ("middle", "finger_{}1", (70, 90, 58), 3),
    ("ring",   "finger_{}2", (75, 92, 60), -6),
    ("pinky",  "finger_{}3", (80, 94, 62), -15),
)

# A small hand outline in the dorsal plane, with the thumb on the radial side.
OUTLINE = ((-.43, -.52), (.19, -.52), (.56, -.12), (.65, .14), (.55, .19),
           (.32, -.01), (.32, .51), (.22, .55), (.16, .49), (.16, .18),
           (.12, .64), (.01, .66), (-.04, .60), (-.04, .19), (-.08, .55),
           (-.19, .55), (-.24, .49), (-.24, .12), (-.29, .36), (-.39, .37),
           (-.44, .30), (-.43, -.52))


def frame_at(position, direction, toward):
    """Local X curls the forward Y axis toward the projected palm-facing Z axis."""
    y = direction.normalized()
    z = toward - y * toward.dot(y)
    if z.length_squared < 1e-12:
        z = y.orthogonal()
    z.normalize()
    frame = Matrix((y.cross(z), y, z)).transposed().to_4x4()
    frame.translation = position
    return frame


def create(rig, names, side, helper):
    """Insert rest-aligned offset parents without taking ownership of the FK channels."""
    arm = rig.data
    edit = arm.edit_bones
    wrist = edit[names[f"arm_{side}0_wrist"]]
    knuckles = [edit[names[f"finger_{side}{i}"]].head.copy() for i in range(4)]
    center = sum(knuckles, Vector()) * 0.25
    across = knuckles[0] - knuckles[3]
    width = max(across.length, 0.001)
    forward = center - wrist.head
    if forward.length_squared < 1e-12:
        forward = wrist.matrix.to_3x3().col[1].copy()
    forward.normalize()
    across -= forward * across.dot(forward)
    if across.length_squared < 1e-12:
        across = forward.orthogonal()
    across.normalize()
    handedness = 1 if side == 'L' else -1
    palmar = forward.cross(across).normalized() * handedness
    palm = (center + wrist.head) * 0.5
    hand_frame = frame_at(palm - palmar * (0.65 * width), forward, palmar)

    host = edit.new("HandGrip." + side)
    host.head = hand_frame.translation
    host.tail = host.head + forward * (width * 0.4)
    host.matrix = hand_frame
    host.parent = wrist
    host.use_deform = False
    for collection in list(host.collections):
        collection.unassign(host)
    arm.collections["Biped Display"].assign(host)
    host_name = host.name
    rig["xbiped_hand_" + side] = host_name
    drivers = []
    for digit, pattern, angles, fan in DIGITS:
        component = pattern.format(side)
        locators = [names[component + loc] for loc in ("", "_0_loc", "_1_loc", "_2_loc")]
        for i, angle in enumerate(angles):
            fk = edit["FK." + locators[i]]
            direction = edit[locators[i + 1]].head - fk.head
            if direction.length_squared < 1e-12:
                direction = forward
            # The thumb closes across the palm towards the ring finger.
            toward = knuckles[2] + palmar * (0.45 * width) - fk.head if digit == "thumb" else palmar
            frame = frame_at(fk.head, direction, toward)
            offset = helper(f"HandCurl.{component}.{i}", frame, fk.parent)
            fk.parent = offset
            expr = f"{radians(angle):.9f} * (min(1, max(0, grip + curl)) + 0.25 * min(0, grip + curl))"
            drivers.append((offset.name, 0, expr, {"grip": "grip", "curl": digit + "_curl"}))
            if i == 0:
                fan_sign = 1 if frame.to_3x3().col[2].cross(direction).dot(across) >= 0 else -1
                drivers.append((offset.name, 2, f"{radians(fan * fan_sign):.9f} * spread", {"spread": "spread"}))

    # Curl the ulnar metacarpals around the long axis of the palm, retaining the
    # existing Shifter metacarpal master and interpolation underneath each offset.
    for i, angle in enumerate((4, 12, 24), 1):
        motion = edit[f"MCH.Palm.meta_{side}0.{i}"]
        offset = helper(f"HandCup.{side}.{i}", frame_at(palm, forward, palmar), motion.parent)
        motion.parent = offset
        drivers.append((offset.name, 1, f"{radians(-handedness * angle):.9f} * cup", {"cup": "cup"}))
    return host_name, side, width, drivers


def configure(rig, hand):
    name, side, width, drivers = hand
    host = rig.pose.bones[name]
    host.lock_location = host.lock_rotation = host.lock_scale = (True, True, True)
    points = [(x * width * 0.65 * (-1 if side == 'L' else 1), y * width * 0.65, 0) for x, y in OUTLINE]
    widget = biped_display.shape(rig.name + "." + name, [points])
    biped_display.configure(host, widget, 18 if side == 'L' else 13)

    properties = [("grip", 0, "Close the whole hand"), ("spread", -1, "Fan the fingers apart"),
                  ("cup", 0, "Cup the palm towards the thumb")]
    properties.extend((digit + "_curl", -1, "Adjust " + digit + " curl relative to Grip")
                      for digit, *_ in DIGITS)
    for key, minimum, description in properties:
        host[key] = 0.0
        host.id_properties_ui(key).update(default=0.0, min=minimum, max=1.0, description=description)
    for bone, axis, expr, inputs in drivers:
        owner = rig.pose.bones[bone]
        owner.rotation_mode = 'XYZ'
        drive(owner, "rotation_euler", expr, {alias: (host, key) for alias, key in inputs.items()}, axis)
