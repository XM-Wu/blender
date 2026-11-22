# SPDX-License-Identifier: GPL-2.0-or-later
"""Real-time secondary motion preview for XBone joint chains."""

import math
import time
import traceback

import bpy
import blf
import gpu
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty
from mathutils import Quaternion, Vector
from gpu_extras.batch import batch_for_shader


STEP = 1.0 / 60.0
EPS = 1e-7
TARGET_TAG = "_xbone_preview_rotation_target_v1"
_session = None


class XBD_Chain(bpy.types.PropertyGroup):
    root: StringProperty(name="起始关节")
    tip: StringProperty(name="末端关节")
    stiffness: FloatProperty(name="姿态回复", default=24, min=0, max=240)
    damping: FloatProperty(name="阻尼", default=5, min=0, max=60)
    root_angle: FloatProperty(name="根部摆角", subtype='ANGLE', default=math.radians(65), min=0, max=math.pi)
    tip_angle: FloatProperty(name="梢部摆角", subtype='ANGLE', default=math.radians(85), min=0, max=math.pi)
    tip_stiffness: FloatProperty(name="梢部回复倍率", default=0.4, min=0, max=2)
    radius: FloatProperty(name="骨段半径", subtype='DISTANCE', default=0.025, min=0)


class XBD_Collider(bpy.types.PropertyGroup):
    bone: StringProperty(name="绑定关节")
    end: StringProperty(name="终点关节")
    shape: EnumProperty(name="形状", items=(('SPHERE', "球", ""), ('CAPSULE', "胶囊", "")))
    radius: FloatProperty(name="半径", subtype='DISTANCE', default=0.15, min=0.0001)


class XBD_Settings(bpy.types.PropertyGroup):
    enabled: BoolProperty(default=False)
    chains: CollectionProperty(type=XBD_Chain)
    colliders: CollectionProperty(type=XBD_Collider)
    gravity: FloatProperty(name="重力强度", default=9.81, min=0, max=100)


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
    raise ValueError("请选择不同的起始和末端关节，末端必须是起始关节的后代")


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
        count = len(pose) - 1
        self.positions[0] = pose[0].copy()
        for i in range(count):
            parent = self.positions[i]
            j = i + 1
            old = self.positions[j]
            rest = pose[j] - pose[i]
            length = rest.length
            if length < EPS:
                self.previous[j], self.positions[j] = old, parent.copy()
                continue
            axis = rest / length
            fraction = i / max(1, count - 1)
            stiffness = cfg.stiffness * (1 + fraction * (cfg.tip_stiffness - 1))
            limit = cfg.root_angle + fraction * (cfg.tip_angle - cfg.root_angle)
            velocity = (old - self.previous[j]) * math.exp(-cfg.damping * STEP)
            predicted = old + velocity + (gravity + stiffness * (parent + rest - old)) * STEP**2
            direction = predicted - parent
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
            self.previous[j], self.positions[j] = old, parent + direction * length


class Preview:
    def __init__(self, rig, context):
        self.rig = rig
        self.layer = context.view_layer
        self.scene = context.scene
        self.clock = time.monotonic()
        self.remainder = 0
        self.frame = self.scene.frame_current
        cfg = rig.xbone_dynamics
        if not cfg.chains:
            raise ValueError("请先添加一条动骨链")
        self.chains = [ChainState(chain_names(rig, chain)) for chain in cfg.chains]
        occupied = set()
        for chain in self.chains:
            if occupied.intersection(chain.names):
                raise ValueError("动骨链不能共用关节")
            occupied.update(chain.names)
        moving = {n for chain in self.chains for n in chain.names[:-1]}
        for chain in self.chains:
            if any(b.name in moving for b in rig.pose.bones[chain.names[0]].parent_recursive):
                raise ValueError("动骨链的锚点不能由另一条动骨链驱动")
        for col in cfg.colliders:
            for name in ([col.bone, col.end] if col.shape == 'CAPSULE' else [col.bone]):
                bone = rig.pose.bones.get(name)
                if not bone or any(b.name in moving for b in bone.parent_recursive):
                    raise ValueError("请选择独立于动骨链的碰撞关节")

    def install(self):
        for chain in self.chains:
            for name in chain.names[:-1]:
                # Unlinked targets are evaluated through the constraint relation.
                target = bpy.data.objects.new("XBone Preview Rotation", None)
                target[TARGET_TAG] = True
                target.rotation_mode = 'QUATERNION'
                con = self.rig.pose.bones[name].constraints.new('COPY_ROTATION')
                chain.outputs.append((name, con, target))
                con.name = "XBone 动骨预览"
                con.target = target
                con.owner_space = 'POSE'
                con.mute = True

    def tick(self, steps):
        rig = self.rig
        if rig.mode == 'EDIT' or self.layer.objects.get(rig.name) != rig:
            raise ValueError("骨架已退出预览")
        for chain in self.chains:
            for _, con, _ in chain.outputs:
                con.mute = True
        self.layer.update()
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
            for i, (name, con, target) in enumerate(chain.outputs):
                src = inverse @ (pose[i + 1] - pose[i])
                dst = inverse @ (chain.positions[i + 1] - chain.positions[i])
                orient = bones[name].matrix.to_quaternion()
                if min(src.length, dst.length) > EPS:
                    orient = src.rotation_difference(dst) @ orient
                target.rotation_quaternion = orient
                con.mute = False
        self.layer.update()

    def close(self):
        for chain in self.chains:
            for name, con, target in chain.outputs:
                try:
                    self.rig.pose.bones[name].constraints.remove(con)
                except (ReferenceError, KeyError, RuntimeError):
                    pass
                try:
                    bpy.data.objects.remove(target, do_unlink=True)
                except ReferenceError:
                    pass
        try:
            self.layer.update()
        except ReferenceError:
            pass  # The view layer may have been removed while previewing.


def redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type in {'VIEW_3D', 'PROPERTIES'}:
                area.tag_redraw()


def advance_timer():
    if _session is None:
        return None
    try:
        now = time.monotonic()
        elapsed = now - _session.clock
        _session.clock = now
        if elapsed > 0.5:
            for chain in _session.chains:
                chain.positions.clear()
        _session.remainder += min(elapsed, 4 * STEP)
        steps = int(_session.remainder / STEP)
        if steps:
            _session.remainder -= steps * STEP
            _session.tick(steps)
        return max(0.001, STEP - _session.remainder)
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
            redraw()


@persistent
def before_change(*_):
    stop_preview()


@persistent
def clean_restored_targets(*_):
    # Undo snapshots taken while interacting with the rig can retain preview IDs.
    targets = {obj for obj in bpy.data.objects if obj.get(TARGET_TAG)}
    if targets:
        for obj in bpy.data.objects:
            if is_xbone(obj):
                for bone in obj.pose.bones:
                    for con in list(bone.constraints):
                        if con.type == 'COPY_ROTATION' and con.target in targets:
                            bone.constraints.remove(con)
        for obj in targets:
            bpy.data.objects.remove(obj, do_unlink=True)


class XBD_OT_edit(bpy.types.Operator):
    bl_idname = "xbone.dynamics_edit"
    bl_label = "编辑 XBone 动骨"
    bl_options = {'UNDO', 'INTERNAL'}
    action: EnumProperty(items=[(s, s, "") for s in ('ENABLE', 'DISABLE', 'CHAIN', 'COLLIDER', 'REMOVE_CHAIN', 'REMOVE_COLLIDER')])
    index: IntProperty()

    @classmethod
    def poll(cls, context):
        return bool(is_xbone(context.object) and _session is None)

    def execute(self, context):
        rig = context.object
        cfg = rig.xbone_dynamics
        if self.action in {'ENABLE', 'DISABLE'}:
            cfg.enabled = self.action == 'ENABLE'
        if self.action == 'CHAIN' or (self.action == 'ENABLE' and not cfg.chains):
            c = cfg.chains.add()
            bone = context.active_pose_bone
            if bone:
                c.root = bone.name
                while len(bone.children) == 1:
                    bone = bone.children[0]
                c.tip = bone.name
        elif self.action == 'COLLIDER':
            cfg.colliders.add()
        elif self.action == 'REMOVE_CHAIN':
            cfg.chains.remove(self.index)
        elif self.action == 'REMOVE_COLLIDER':
            cfg.colliders.remove(self.index)
        return {'FINISHED'}


class XBD_OT_start(bpy.types.Operator):
    bl_idname = "xbone.dynamics_start"
    bl_label = "开始实时模拟"

    @classmethod
    def poll(cls, context):
        rig = context.object
        return bool(is_xbone(rig) and rig.mode != 'EDIT' and rig.xbone_dynamics.enabled and _session is None)

    def execute(self, context):
        global _session
        try:
            _session = Preview(context.object, context)
            _session.install()
            _session.tick(0)
            bpy.app.timers.register(advance_timer, first_interval=STEP)
        except Exception as error:
            stop_preview()
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        redraw()
        return {'FINISHED'}


class XBD_OT_stop(bpy.types.Operator):
    bl_idname = "xbone.dynamics_stop"
    bl_label = "停止模拟"
    bl_description = "停止 XBone 实时模拟，恢复当前动画姿态"

    @classmethod
    def poll(cls, context):
        return _session is not None

    def execute(self, context):
        stop_preview()
        return {'FINISHED'}


def edit_button(layout, action, text, icon='NONE', index=0):
    op = layout.operator("xbone.dynamics_edit", text=text, icon=icon)
    op.action, op.index = action, index


class XBD_PT_physics(bpy.types.Panel):
    bl_idname = "PHYSICS_PT_xbone_dynamics"
    bl_label = "XBone 动骨"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "physics"

    @classmethod
    def poll(cls, context):
        return is_xbone(context.object)

    def draw(self, context):
        layout = self.layout
        rig = context.object
        cfg = rig.xbone_dynamics
        if not cfg.enabled:
            edit_button(layout, 'ENABLE', "添加 XBone 动骨", 'ADD')
            return
        if _session:
            layout.operator("xbone.dynamics_stop", icon='CANCEL')
        else:
            layout.operator("xbone.dynamics_start", icon='PLAY')
        layout.prop(cfg, "gravity")
        for i, chain in enumerate(cfg.chains):
            box = layout.box()
            row = box.row()
            row.enabled = _session is None
            row.label(text=f"骨链 {i+1}")
            edit_button(row, 'REMOVE_CHAIN', "", 'X', i)
            col = box.column()
            col.enabled = _session is None
            for key in ('root', 'tip'):
                col.prop_search(chain, key, rig.data, "bones")
            for key in ('stiffness', 'tip_stiffness', 'damping', 'root_angle', 'tip_angle', 'radius'):
                box.prop(chain, key)
        edit_button(layout, 'CHAIN', "添加骨链", 'ADD')
        col = layout.column()
        col.enabled = _session is None
        for i, collider in enumerate(cfg.colliders):
            box = col.box()
            row = box.row()
            row.prop(collider, "shape", text="")
            edit_button(row, 'REMOVE_COLLIDER', "", 'X', i)
            box.prop_search(collider, "bone", rig.data, "bones")
            if collider.shape == 'CAPSULE':
                box.prop_search(collider, "end", rig.data, "bones")
            box.prop(collider, "radius")
        edit_button(col, 'COLLIDER', "添加碰撞体", 'ADD')
        edit_button(col, 'DISABLE', "关闭动骨效果")


def stop_bounds(context):
    toolbar = next((r for r in context.area.regions if r.type == 'TOOLS'), None)
    scale = context.preferences.system.ui_scale
    left = toolbar.width if toolbar and context.space_data.show_region_toolbar else 0
    x, y = left + 8 * scale, context.region.height - 35 * scale
    return x, y, 98 * scale, 27 * scale, scale


class XBD_GT_stop(bpy.types.Gizmo):
    bl_idname = "XBD_GT_stop"

    def draw(self, context):
        x, y, w, h, scale = stop_bounds(context)
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        shader.bind()
        shader.uniform_float('color', (0.45 if self.is_highlight else 0.28, 0.075, 0.055, 1))
        batch_for_shader(shader, 'TRI_FAN', {'pos': ((x, y), (x+w, y), (x+w, y+h), (x, y+h))}).draw(shader)
        text = "停止模拟"
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
    bl_label = "停止模拟"
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


classes = (XBD_Chain, XBD_Collider, XBD_Settings, XBD_OT_edit, XBD_OT_start, XBD_OT_stop, XBD_PT_physics,
           XBD_GT_stop, XBD_GGT_stop)
handlers = ((bpy.app.handlers.save_pre, before_change), (bpy.app.handlers.load_pre, before_change),
            (bpy.app.handlers.undo_pre, before_change), (bpy.app.handlers.redo_pre, before_change),
            (bpy.app.handlers.undo_post, clean_restored_targets), (bpy.app.handlers.redo_post, clean_restored_targets))


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
