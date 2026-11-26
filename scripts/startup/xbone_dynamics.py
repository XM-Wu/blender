# SPDX-License-Identifier: GPL-2.0-or-later
"""Real-time secondary motion preview for XBone joint chains."""

import math
import traceback

import bpy
import blf
import gpu
from bpy.app.handlers import persistent
from bpy.app.translations import pgettext_data as data_, pgettext_iface as iface_, pgettext_rpt as rpt_
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty
from mathutils import Quaternion, Vector
from gpu_extras.batch import batch_for_shader

from _xblender_live import PreviewClock, publish_sample, redraw_preview_controls


STEP = 1.0 / 60.0
EPS = 1e-7
TARGET_TAG = "_xbone_preview_rotation_target_v1"
_session = None
_undo_preview = None


class XBD_Chain(bpy.types.PropertyGroup):
    root: StringProperty(name="Root Bone")
    tip: StringProperty(name="Tip Bone")
    stiffness: FloatProperty(name="Pose Stiffness", default=24, min=0, max=240)
    bend_stiffness: FloatProperty(name="Bend Stiffness", subtype='FACTOR', default=0.5, min=0, max=1,
                                  description="Resistance to changes in the angle between adjacent segments")
    damping: FloatProperty(name="Damping", default=5, min=0, max=60)
    root_angle: FloatProperty(name="Root Swing Limit", subtype='ANGLE', default=math.radians(65), min=0, max=math.pi)
    tip_angle: FloatProperty(name="Tip Swing Limit", subtype='ANGLE', default=math.radians(85), min=0, max=math.pi)
    tip_stiffness: FloatProperty(name="Tip Stiffness Factor", default=0.4, min=0, max=2)
    radius: FloatProperty(name="Bone Radius", subtype='DISTANCE', default=0.025, min=0)


class XBD_Collider(bpy.types.PropertyGroup):
    bone: StringProperty(name="Attachment Bone")
    end: StringProperty(name="End Bone")
    shape: EnumProperty(name="Shape", items=(('SPHERE', "Sphere", ""), ('CAPSULE', "Capsule", "")))
    radius: FloatProperty(name="Radius", subtype='DISTANCE', default=0.15, min=0.0001)


class XBD_Settings(bpy.types.PropertyGroup):
    enabled: BoolProperty(default=False)
    chains: CollectionProperty(type=XBD_Chain)
    colliders: CollectionProperty(type=XBD_Collider)
    chain_index: IntProperty(min=0, options=set())
    collider_index: IntProperty(min=0, options=set())
    gravity: FloatProperty(name="Gravity Strength", default=9.81, min=0, max=100)


def is_xbone(obj):
    return bool(obj and obj.type == 'ARMATURE' and obj.data.is_xbone)


def chain_names(rig, chain):
    bone = rig.pose.bones.get(chain.tip)
    names = []
    while bone:
        names.append(bone.name)
        if bone.name == chain.root:
            if len(names) > 1:
                return names[::-1]
            break
        bone = bone.parent
    raise ValueError("The tip must be a descendant of the root bone")


def closest_pair(a, b, c, d):
    """Closest points on two finite segments; c == d represents a sphere."""
    u, v, w = b - a, d - c, a - c
    uu, vv, uv = u.length_squared, v.length_squared, u.dot(v)
    if uu < EPS * EPS:
        return 0, a, c + v * max(0, min(1, w.dot(v) / vv)) if vv > EPS * EPS else c
    denom = uu * vv - uv * uv
    s = max(0, min(1, (uv * w.dot(v) - vv * w.dot(u)) / denom)) if denom > EPS * uu * vv else 0
    t = max(0, min(1, (uv * s + w.dot(v)) / vv)) if vv > EPS * EPS else 0
    s = max(0, min(1, (uv * t - w.dot(u)) / uu))
    return s, a + u * s, c + v * t


def direction_limited(direction, axis, limit):
    angle = axis.angle(direction, 0.0)
    if angle <= limit:
        return direction
    rotation = axis.rotation_difference(direction)
    return Quaternion(rotation.axis, limit) @ axis


def project_chain_lengths(points, lengths, axes):
    """Solve the linearized distance constraints together, with a pinned first joint.

    Neighboring constraints share just one particle, so the mass-weighted gradient
    matrix is tridiagonal. A forward elimination and backward substitution transmit
    tension along even a densely subdivided chain in linear time.
    """
    directions, diagonal, impulse = [], [], []
    for i, length in enumerate(lengths):
        edge = points[i + 1] - points[i]
        distance = edge.length
        direction = edge / distance if distance > EPS else axes[i]
        rhs = length - distance
        diag = 1.0 if i == 0 else 2.0
        if i:
            coupling = -directions[-1].dot(direction)
            factor = coupling / diagonal[-1]
            diag -= factor * coupling
            rhs -= factor * impulse[-1]
        directions.append(direction)
        diagonal.append(diag)
        impulse.append(rhs)
    following = 0.
    for i in reversed(range(len(lengths))):
        rhs = impulse[i]
        if i + 1 < len(lengths):
            rhs += directions[i].dot(directions[i + 1]) * following
        following = rhs / diagonal[i]
        offset = directions[i] * following
        points[i + 1] += offset
        if i:
            points[i] -= offset


def project_bend(points, i, rest_angle, rest_normal, relaxation):
    """Project the three-point angle toward the authored bend, keeping joint zero pinned."""
    left = points[i] - points[i - 1]
    right = points[i + 1] - points[i]
    a, b = left.length, right.length
    if min(a, b) <= EPS:
        return
    left /= a
    right /= b
    normal = left.cross(right)
    sine = normal.length
    angle = math.atan2(sine, left.dot(right))
    error = angle - rest_angle
    if abs(error) < 1e-5:
        return
    if sine <= EPS:
        normal = rest_normal - left * rest_normal.dot(left)
        if normal.length <= EPS:
            normal = left.orthogonal()
    normal.normalize()
    g0 = normal.cross(left) / a
    g2 = normal.cross(right) / b
    g1 = -g0 - g2
    norm = g1.length_squared + g2.length_squared
    if i > 1:
        norm += g0.length_squared
    step = -relaxation * error / norm
    if i > 1:
        points[i - 1] += step * g0
    points[i] += step * g1
    points[i + 1] += step * g2


class ChainState:
    def __init__(self, names):
        self.names = names
        self.positions = []
        self.previous = []
        self.anchor = None
        self.outputs = []

    def reset(self, pose):
        self.positions = [p.copy() for p in pose]
        self.previous = [p.copy() for p in pose]
        self.anchor = pose[0].copy()

    def advance(self, pose, cfg, gravity, colliders):
        old = self.positions
        count = len(pose) - 1
        rest = [b - a for a, b in zip(pose, pose[1:])]
        lengths = [v.length for v in rest]
        axes = [v.normalized() for v in rest]
        normals = [a.cross(b) for a, b in zip(axes, axes[1:])]
        angles = [math.atan2(n.length, a.dot(b)) for n, a, b in zip(normals, axes, axes[1:])]
        decay = math.exp(-cfg.damping * STEP)
        predicted = [pose[0].copy()]
        # Predict all free particles from the same time level, before projecting any joints.
        for i in range(count):
            j = i + 1
            fraction = i / max(1, count - 1)
            stiffness = cfg.stiffness * (1 + fraction * (cfg.tip_stiffness - 1))
            velocity = (old[j] - self.previous[j]) * decay
            parent = pose[0] if i == 0 else old[i]
            acceleration = gravity + stiffness * (parent + rest[i] - old[j])
            predicted.append(old[j] + velocity + acceleration * STEP**2)

        limits = [cfg.root_angle + i / max(1, count - 1) * (cfg.tip_angle - cfg.root_angle)
                  for i in range(count)]
        passes = 8
        relaxation = 1 - (1 - cfg.bend_stiffness) ** (1 / passes)
        for sweep in range(passes):
            if sweep % 4 == 0:
                project_chain_lengths(predicted, lengths, axes)
            indices = range(count) if sweep % 2 == 0 else reversed(range(count))
            for i in indices:
                edge = predicted[i + 1] - predicted[i]
                axis = edge.normalized() if edge.length > EPS else axes[i]
                if lengths[i] > EPS:
                    axis = direction_limited(axis, axes[i], limits[i])
                correction = edge - axis * lengths[i]
                if i:
                    correction *= 0.5
                    predicted[i] += correction
                predicted[i + 1] -= correction
                if i and relaxation and min(lengths[i - 1], lengths[i]) > EPS:
                    project_bend(predicted, i, angles[i - 1], normals[i - 1], relaxation)

        # Anchor the final output exactly to the render skeleton's fixed-length segments.
        for i, (axis, length) in enumerate(zip(axes, lengths)):
            j = i + 1
            parent = predicted[i]
            if length < EPS:
                predicted[j] = parent.copy()
                continue
            limit = limits[i]
            direction = predicted[j] - parent
            direction = direction.normalized() if direction.length > EPS else axis
            direction = direction_limited(direction, axis, limit)
            for _ in range(6):
                corrected = False
                for c, d, radius in colliders:
                    t, point, center = closest_pair(parent, parent + direction * length, c, d)
                    normal = point - center
                    depth = radius + cfg.radius - normal.length
                    if depth <= EPS or t < EPS:
                        continue
                    # Rotate around the pinned parent; a contact at mid-segment
                    # requires a larger tip displacement than one at the tip.
                    tangent = normal - direction * normal.dot(direction)
                    if tangent.length < EPS:
                        tangent = direction.orthogonal()
                    direction = (direction + tangent.normalized() * depth / (t * length)).normalized()
                    direction = direction_limited(direction, axis, limit)
                    corrected = True
                if not corrected:
                    break
            predicted[j] = parent + direction * length
        self.previous, self.positions = old, predicted


def preview_driver(owner, path, index, state, source_path):
    # Plain property drivers also evaluate in files opened with auto-execution disabled.
    fc = owner.driver_add(path, index)
    fc.driver.type = 'AVERAGE'
    var = fc.driver.variables.new()
    var.type = 'SINGLE_PROP'
    var.targets[0].id = state
    var.targets[0].data_path = source_path
    return fc


class Preview:
    def __init__(self, rig, scene, layer):
        self.rig = rig
        self.state = None
        self.influence_drivers = []
        self.had_animation_data = rig.animation_data is not None
        self.layer = layer
        self.scene = scene
        self.clock = PreviewClock(STEP, 4)
        self.frame = self.scene.frame_current
        cfg = rig.xbone_dynamics
        if not cfg.chains:
            raise ValueError("Add a bone chain before starting the simulation")
        self.chains = [ChainState(chain_names(rig, chain)) for chain in cfg.chains]
        occupied = set()
        for chain in self.chains:
            if occupied.intersection(chain.names):
                raise ValueError("Bone chains cannot share bones")
            occupied.update(chain.names)
        moving = {n for chain in self.chains for n in chain.names[:-1]}
        for chain in self.chains:
            if any(b.name in moving for b in rig.pose.bones[chain.names[0]].parent_recursive):
                raise ValueError("A chain anchor cannot be driven by another dynamic chain")
        for col in cfg.colliders:
            for name in ([col.bone, col.end] if col.shape == 'CAPSULE' else [col.bone]):
                bone = rig.pose.bones.get(name)
                if not bone or any(b.name in moving for b in bone.parent_recursive):
                    raise ValueError("Choose a collider bone outside the simulated chain")

    def install(self):
        # Updating normal transform/constraint RNA sends editor notifications which
        # invalidate gizmo picking between hover and press. Drive both passes from
        # one private input ID, tagging only its evaluation when publishing a sample.
        self.state = bpy.data.objects.new("XBone Preview State", None)
        self.state[TARGET_TAG] = True
        self.state["enabled"] = 0.0
        for chain in self.chains:
            for name in chain.names[:-1]:
                # Unlinked targets are evaluated through the constraint relation.
                target = bpy.data.objects.new("XBone Preview Rotation", None)
                target[TARGET_TAG] = True
                target.rotation_mode = 'QUATERNION'
                con = self.rig.pose.bones[name].constraints.new('COPY_ROTATION')
                slot = f"rotation_{len(self.influence_drivers)}"
                chain.outputs.append((name, con, target, slot))
                con.name = data_("XBone Dynamics Preview")
                con.target = target
                con.owner_space = 'POSE'
                self.state[slot] = [1.0, 0.0, 0.0, 0.0]
                self.influence_drivers.append(preview_driver(con, "influence", -1, self.state, '["enabled"]'))
                for axis in range(4):
                    preview_driver(target, "rotation_quaternion", axis, self.state, f'["{slot}"][{axis}]')

    def tick(self, steps):
        rig = self.rig
        if rig.mode == 'EDIT' or self.layer.objects.get(rig.name) != rig:
            raise ValueError("The armature is no longer available for preview")
        self.state["enabled"] = 0.0
        publish_sample(self.state, self.layer, redraw=False)
        evaluated = rig.evaluated_get(self.layer.depsgraph)
        world = evaluated.matrix_world.copy()
        inverse = world.to_3x3().inverted_safe()
        bones = evaluated.pose.bones
        cfg = rig.xbone_dynamics
        colliders = [(world @ bones[c.bone].head,
                      world @ bones[c.end if c.shape == 'CAPSULE' else c.bone].head,
                      c.radius) for c in cfg.colliders]
        gravity = Vector((0, 0, -cfg.gravity))
        jumped = abs(self.scene.frame_current - self.frame) > 1
        self.frame = self.scene.frame_current
        for chain, settings in zip(self.chains, cfg.chains):
            pose = [world @ bones[n].head for n in chain.names]
            reach = sum((b - a).length for a, b in zip(pose, pose[1:]))
            if not chain.positions or jumped or (pose[0] - chain.anchor).length > max(2 * reach, EPS):
                chain.reset(pose)
            for _ in range(steps):
                chain.advance(pose, settings, gravity, colliders)
            chain.anchor = pose[0].copy()
            for i, (name, _, _, slot) in enumerate(chain.outputs):
                src = inverse @ (pose[i + 1] - pose[i])
                dst = inverse @ (chain.positions[i + 1] - chain.positions[i])
                orient = bones[name].matrix.to_quaternion()
                if min(src.length, dst.length) > EPS:
                    orient = src.rotation_difference(dst) @ orient
                self.state[slot] = list(orient)
        self.state["enabled"] = 1.0
        publish_sample(self.state, self.layer)

    def close(self):
        for fc in self.influence_drivers:
            try:
                self.rig.animation_data.drivers.remove(fc)
            except (ReferenceError, RuntimeError, AttributeError):
                pass
        for chain in self.chains:
            for name, con, target, _ in chain.outputs:
                try:
                    self.rig.pose.bones[name].constraints.remove(con)
                except (ReferenceError, KeyError, RuntimeError):
                    pass
                try:
                    bpy.data.objects.remove(target, do_unlink=True)
                except ReferenceError:
                    pass
        try:
            if self.state:
                bpy.data.objects.remove(self.state, do_unlink=True)
        except ReferenceError:
            pass
        try:
            anim = self.rig.animation_data
            if not self.had_animation_data and anim and not (anim.drivers or anim.action or anim.nla_tracks):
                self.rig.animation_data_clear()
            self.layer.update()
        except ReferenceError:
            pass  # The view layer may have been removed while previewing.


def advance_timer():
    if _session is None:
        return None
    try:
        elapsed, steps, delay = _session.clock.poll()
        if elapsed > .5:
            for chain in _session.chains:
                chain.positions.clear()
        if steps:
            _session.tick(steps)
        return delay
    except Exception:
        traceback.print_exc()
        stop_preview(from_timer=True)
        return None


def stop_preview(*, from_timer=False):
    global _session
    preview, _session = _session, None
    if preview:
        if not from_timer and bpy.app.timers.is_registered(advance_timer):
            bpy.app.timers.unregister(advance_timer)
        try:
            preview.close()
        finally:
            redraw_preview_controls()


def start_preview(preview):
    global _session
    _session = preview
    try:
        preview.install()
        preview.tick(0)
        bpy.app.timers.register(advance_timer, first_interval=STEP)
    except Exception:
        stop_preview()
        raise
    redraw_preview_controls()


@persistent
def before_change(*_):
    global _undo_preview
    _undo_preview = None
    stop_preview()


@persistent
def before_undo(*_):
    global _undo_preview
    _undo_preview = None
    if _session:
        # Undo may replace every RNA object. Keep only IDs and solver-owned vectors.
        particles = {tuple(c.names): (c.positions, c.previous, c.anchor) for c in _session.chains}
        _undo_preview = (_session.rig.session_uid, _session.scene.session_uid, _session.layer.name,
                         _session.frame, particles, _session.had_animation_data)
        stop_preview()


def clean_restored_targets():
    # Undo snapshots taken while interacting with the rig can retain preview IDs.
    targets = {obj for obj in bpy.data.objects if obj.get(TARGET_TAG)}
    if targets:
        for obj in bpy.data.objects:
            if is_xbone(obj):
                if obj.animation_data:
                    for fc in list(obj.animation_data.drivers):
                        if any(t.id in targets for v in fc.driver.variables for t in v.targets):
                            obj.animation_data.drivers.remove(fc)
                for bone in obj.pose.bones:
                    for con in list(bone.constraints):
                        if con.type == 'COPY_ROTATION' and con.target in targets:
                            bone.constraints.remove(con)
        for obj in targets:
            bpy.data.objects.remove(obj, do_unlink=True)


@persistent
def after_undo(*_):
    global _undo_preview
    saved, _undo_preview = _undo_preview, None
    clean_restored_targets()
    if not saved:
        return
    rig_uid, scene_uid, layer_name, frame, particles, had_anim = saved
    scene = next((s for s in bpy.data.scenes if s.session_uid == scene_uid), None)
    layer = scene.view_layers.get(layer_name) if scene else None
    rig = next((o for o in layer.objects if o.session_uid == rig_uid), None) if layer else None
    if not is_xbone(rig) or rig.mode == 'EDIT' or not rig.xbone_dynamics.enabled:
        return
    try:
        preview = Preview(rig, scene, layer)
        if scene.frame_current == frame:
            for chain in preview.chains:
                state = particles.get(tuple(chain.names))
                if state:
                    chain.positions, chain.previous, chain.anchor = state
        anim = rig.animation_data
        if not had_anim and anim and not (anim.action or anim.drivers or anim.nla_tracks):
            preview.had_animation_data = False
        start_preview(preview)
    except ValueError:
        # Undo may remove the chain, a joint, or a collider required by the preview.
        return


class XBD_OT_edit(bpy.types.Operator):
    bl_idname = "xbone.dynamics_edit"
    bl_label = "Edit XBone Dynamics"
    bl_options = {'UNDO', 'INTERNAL'}
    action: EnumProperty(items=[
        ('ENABLE', "Add XBone Dynamics", ""), ('DISABLE', "Remove XBone Dynamics", ""),
        ('CHAIN', "Add Bone Chain", ""), ('REMOVE_CHAIN', "Remove Bone Chain", ""),
        ('COLLIDER', "Add Collider", ""), ('REMOVE_COLLIDER', "Remove Collider", ""),
    ])
    index: IntProperty()

    @classmethod
    def description(cls, context, properties):
        items = bpy.ops.xbone.dynamics_edit.get_rna_type().properties["action"].enum_items
        return bpy.app.translations.pgettext_tip(items[properties.action].name)

    @classmethod
    def poll(cls, context):
        return bool(is_xbone(context.object) and context.object.is_editable and _session is None)

    def execute(self, context):
        rig = context.object
        cfg = rig.xbone_dynamics
        if self.action in {'ENABLE', 'DISABLE'}:
            cfg.enabled = self.action == 'ENABLE'
        if self.action == 'CHAIN' or (self.action == 'ENABLE' and not cfg.chains):
            c = cfg.chains.add()
            cfg.chain_index = len(cfg.chains) - 1
            bone = context.active_pose_bone
            if bone:
                c.root = bone.name
                while len(bone.children) == 1:
                    bone = bone.children[0]
                c.tip = bone.name
        elif self.action == 'COLLIDER':
            cfg.colliders.add()
            cfg.collider_index = len(cfg.colliders) - 1
        elif self.action == 'REMOVE_CHAIN':
            if not 0 <= self.index < len(cfg.chains):
                return {'CANCELLED'}
            cfg.chains.remove(self.index)
            cfg.chain_index = max(0, min(self.index, len(cfg.chains) - 1))
        elif self.action == 'REMOVE_COLLIDER':
            if not 0 <= self.index < len(cfg.colliders):
                return {'CANCELLED'}
            cfg.colliders.remove(self.index)
            cfg.collider_index = max(0, min(self.index, len(cfg.colliders) - 1))
        return {'FINISHED'}


class XBD_OT_start(bpy.types.Operator):
    bl_idname = "xbone.dynamics_start"
    bl_label = "Start Live Simulation"

    @classmethod
    def poll(cls, context):
        rig = context.object
        return bool(is_xbone(rig) and rig.mode != 'EDIT' and rig.xbone_dynamics.enabled and _session is None)

    def execute(self, context):
        try:
            start_preview(Preview(context.object, context.scene, context.view_layer))
        except Exception as error:
            self.report({'ERROR'}, rpt_(str(error)))
            return {'CANCELLED'}
        return {'FINISHED'}


class XBD_OT_stop(bpy.types.Operator):
    bl_idname = "xbone.dynamics_stop"
    bl_label = "Stop Simulation"
    bl_description = "Stop the live simulation and restore the current animated pose"

    @classmethod
    def poll(cls, context):
        return _session is not None

    def execute(self, context):
        stop_preview()
        return {'FINISHED'}


class DynamicsPanel:
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "physics"

    @classmethod
    def poll(cls, context):
        return is_xbone(context.object) and context.object.xbone_dynamics.enabled


class XBD_PT_physics(DynamicsPanel, bpy.types.Panel):
    bl_idname = "PHYSICS_PT_xbone_dynamics"
    bl_label = "XBone Dynamics"

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        rig = context.object
        if _session and _session.rig == rig:
            layout.operator("xbone.dynamics_stop", icon='CANCEL')
        else:
            layout.operator("xbone.dynamics_start", icon='PLAY')
        layout.prop(rig.xbone_dynamics, "gravity")


class XBD_UL_chains(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        label = " → ".join(n for n in (item.root, item.tip) if n)
        layout.label(text=label or iface_("Chain {index}").format(index=index + 1),
                     icon='BONE_DATA', translate=False)


class XBD_UL_colliders(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        label = item.bone or iface_("Collider {index}").format(index=index + 1)
        layout.label(text=label, icon='MESH_UVSPHERE' if item.shape == 'SPHERE' else 'MESH_CAPSULE',
                     translate=False)


def dynamics_list(layout, cfg, prop, index_prop, list_type, add, remove):
    items = getattr(cfg, prop)
    index = getattr(cfg, index_prop)
    row = layout.row()
    row.template_list(list_type, "", cfg, prop, cfg, index_prop, rows=2)
    buttons = row.column(align=True)
    buttons.operator("xbone.dynamics_edit", text="", icon='ADD').action = add
    sub = buttons.row(align=True)
    sub.enabled = index < len(items)
    op = sub.operator("xbone.dynamics_edit", text="", icon='REMOVE')
    op.action, op.index = remove, index
    return items[index] if index < len(items) else None


class XBD_PT_chains(DynamicsPanel, bpy.types.Panel):
    bl_idname = "PHYSICS_PT_xbone_chains"
    bl_label = "Bone Chains"
    bl_parent_id = "PHYSICS_PT_xbone_dynamics"

    def draw(self, context):
        layout = self.layout
        rig = context.object
        chain = dynamics_list(layout, rig.xbone_dynamics, "chains", "chain_index",
                              "XBD_UL_chains", 'CHAIN', 'REMOVE_CHAIN')
        if chain is None:
            return
        layout.use_property_split = True
        anchors = layout.column()
        anchors.enabled = _session is None
        for key in ('root', 'tip'):
            anchors.prop_search(chain, key, rig.data, "bones")
        for key in ('stiffness', 'tip_stiffness', 'bend_stiffness', 'damping', 'root_angle', 'tip_angle', 'radius'):
            layout.prop(chain, key)


class XBD_PT_colliders(DynamicsPanel, bpy.types.Panel):
    bl_idname = "PHYSICS_PT_xbone_colliders"
    bl_label = "Colliders"
    bl_parent_id = "PHYSICS_PT_xbone_dynamics"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        rig = context.object
        collider = dynamics_list(layout, rig.xbone_dynamics, "colliders", "collider_index",
                                 "XBD_UL_colliders", 'COLLIDER', 'REMOVE_COLLIDER')
        if collider is None:
            return
        layout.use_property_split = True
        anchors = layout.column()
        anchors.enabled = _session is None
        anchors.prop(collider, "shape")
        anchors.prop_search(collider, "bone", rig.data, "bones")
        if collider.shape == 'CAPSULE':
            anchors.prop_search(collider, "end", rig.data, "bones")
        layout.prop(collider, "radius")


def stop_bounds(context):
    toolbar = next((r for r in context.area.regions if r.type == 'TOOLS'), None)
    scale = context.preferences.system.ui_scale
    left = toolbar.width if toolbar and context.space_data.show_region_toolbar else 0
    region = context.region
    top = region.height
    for other in context.area.regions:
        if (other.type in {'HEADER', 'TOOL_HEADER'} and other.alignment == 'TOP' and
                other.height > 1 and region.y < other.y < region.y + top):
            top = other.y - region.y
    x, y = left + 8 * scale, top - 35 * scale
    blf.size(0, 14 * scale)
    width, _ = blf.dimensions(0, iface_("Stop Simulation"))
    return x, y, width + 20 * scale, 27 * scale, scale


class XBD_GT_stop(bpy.types.Gizmo):
    bl_idname = "XBD_GT_stop"

    def draw(self, context):
        x, y, w, h, scale = stop_bounds(context)
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        shader.bind()
        shader.uniform_float('color', (0.45 if self.is_highlight else 0.28, 0.075, 0.055, 1))
        batch_for_shader(shader, 'TRI_FAN', {'pos': ((x, y), (x+w, y), (x+w, y+h), (x, y+h))}).draw(shader)
        text = iface_("Stop Simulation")
        # Use the host's UI font (including its CJK fallback).
        blf.size(0, 14 * scale)
        tw, th = blf.dimensions(0, text)
        blf.position(0, x + (w-tw)/2, y + (h-th)/2, 0)
        blf.color(0, 1, 1, 1, 1)
        blf.draw(0, text)

    def test_select(self, context, location):
        x, y, w, h, _ = stop_bounds(context)
        return 0 if x <= location[0] <= x+w and y <= location[1] <= y+h else -1


class XBD_GGT_stop(bpy.types.GizmoGroup):
    bl_idname = "XBD_GGT_stop"
    bl_label = "Stop Simulation"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'PERSISTENT', 'SCALE'}

    @classmethod
    def poll(cls, context):
        return _session is not None

    def setup(self, context):
        self.button = self.gizmos.new('XBD_GT_stop')
        self.button.target_set_operator("xbone.dynamics_stop")


def header_stop(self, context):
    if _session and not context.space_data.show_gizmo:
        self.layout.operator("xbone.dynamics_stop", icon='CANCEL')


classes = (XBD_Chain, XBD_Collider, XBD_Settings, XBD_OT_edit, XBD_OT_start, XBD_OT_stop, XBD_UL_chains, XBD_UL_colliders,
           XBD_PT_physics, XBD_PT_chains, XBD_PT_colliders,
           XBD_GT_stop, XBD_GGT_stop)
handlers = ((bpy.app.handlers.save_pre, before_change), (bpy.app.handlers.load_pre, before_change),
            (bpy.app.handlers.undo_pre, before_undo), (bpy.app.handlers.redo_pre, before_undo),
            (bpy.app.handlers.undo_post, after_undo), (bpy.app.handlers.redo_post, after_undo))


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Object.xbone_dynamics = PointerProperty(type=XBD_Settings)
    for callbacks, fn in handlers:
        callbacks.append(fn)
    bpy.types.VIEW3D_HT_header.prepend(header_stop)


def unregister():
    stop_preview()
    bpy.types.VIEW3D_HT_header.remove(header_stop)
    for callbacks, fn in handlers:
        callbacks.remove(fn)
    del bpy.types.Object.xbone_dynamics
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__" and not hasattr(bpy.types.Object, "xbone_dynamics"):
    register()
