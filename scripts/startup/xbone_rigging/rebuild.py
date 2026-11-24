# SPDX-License-Identifier: GPL-2.0-or-later
"""Install a newly generated pose on the existing animation and skinning object."""
import json

import bpy

from . import builder, skin_names
from .biped_skeleton import even_samples, limb_samples


def target_for(context, guide):
    if "rigging_target" in guide:
        target = guide.get("rigging_target")
        if (isinstance(target, bpy.types.Object) and target.type == 'ARMATURE' and
                target.get("rigging_role") == "RIG" and target.get("rigging_guide") == guide and
                context.scene.objects.get(target.name) == target):
            return target
        return None
    candidates = [obj for obj in context.scene.objects if obj.get("rigging_guide") == guide
                  and obj.get("rigging_role") == "RIG"]
    return candidates[0] if len(candidates) == 1 else None


def driver_key(curve):
    return (curve.data_path, curve.array_index)


def manifest(rig):
    return {
        "constraints": {p.name: list(p.constraints.keys()) for p in rig.pose.bones if p.constraints},
        "drivers": [driver_key(d) for d in rig.animation_data.drivers] if rig.animation_data else [],
    }


def generated_paths(rig):
    # Also recognizes paths in files built before helper ownership was recorded.
    return {o for o in rig.children if o.get("xbone_solver_mesh")} | {c.target for p in rig.pose.bones for c in p.constraints
            if c.type == 'FOLLOW_PATH' and c.target and c.target.parent == rig
            and (c.target.get("xbone_solver_curve") or c.target.name.startswith("RollSpline."))}


def division_layout(arm, *, guide=False):
    layout = {}
    aliases = {b.get("xbiped_legacy_name", b.name) for b in arm.bones if b.use_deform}
    for b in arm.bones:
        name = b.get("biped_locator")
        if not name or (not guide and name + "_0_jnt" not in aliases):
            continue
        if name in {"arm_L0", "arm_R0", "leg_L0", "leg_R0"}:
            layout[name] = limb_samples(b.get("div0", 2), b.get("div1", 2), b.get("supportJoints", True))
        elif name in {"neck_C0", "spine_C0"}:
            layout[name] = even_samples(b.get("division", 5 if name == "spine_C0" else 3))
    return layout


def remap_weights(obj, old, new, group_names, target_names):
    """Distribute each weighted division between its nearest new neighbors."""
    groups = obj.vertex_groups
    by_index = {groups[n].index: i for i, n in enumerate(group_names) if n in groups}
    if not by_index:
        return
    destinations = []
    for t in old:
        hi = next((i for i, value in enumerate(new) if value >= t), len(new) - 1)
        lo = max(0, hi - 1)
        f = min(1.0, max(0.0, (t - new[lo]) / (new[hi] - new[lo]))) if hi != lo else 1.0
        destinations.append([(i, w) for i, w in ((lo, 1 - f), (hi, f)) if w])
    needed = {}
    for vertex in obj.data.vertices:
        values = [(by_index[g.group], g.weight) for g in vertex.groups if g.group in by_index]
        if not values:
            continue
        weights = {}
        for index, value in values:
            for i, fraction in destinations[index]:
                weights[i] = weights.get(i, 0.0) + value * fraction
        for index, _ in values:
            groups[group_names[index]].remove([vertex.index])
        for index, weight in weights.items():
            if index not in needed:
                name = target_names[index]
                needed[index] = groups.get(name) or groups.new(name=name)
            needed[index].add([vertex.index], weight, 'REPLACE')


def copy_properties(source, dest, keep_values):
    for key in source.keys():
        value = source[key]
        if keep_values and key in dest and isinstance(value, (int, float)) and type(value) is type(dest[key]):
            previous = dest.id_properties_ui(key).as_dict()
            if dest[key] != previous.get("default"):
                value = dest[key]
        dest[key] = value
        if isinstance(value, (int, float, str)):
            dest.id_properties_ui(key).update_from(source.id_properties_ui(key))


def adopt(context, rig, fresh, meshes, changed):
    new_manifest = manifest(fresh)
    # Old files have no ownership list. Match by regenerated constraint/driver names.
    owned = json.loads(rig.get("rigging_generated", "{}"))
    old_paths = generated_paths(rig)
    old_arm = rig.data
    arm_name = old_arm.name
    controls = {p.name: (p.rotation_mode, p.matrix_basis.copy()) for p in rig.pose.bones if p.custom_shape}
    widgets = {p.custom_shape for p in rig.pose.bones if p.custom_shape and not p.custom_shape.users_collection}
    old_visibility = {c.name: c.is_visible for c in old_arm.collections_all}
    bpy.ops.object.mode_set(mode='OBJECT')
    new_paths = generated_paths(fresh)
    for bone in rig.pose.bones:
        names = (set(owned.get("constraints", {}).get(bone.name, ())) |
                 set(new_manifest["constraints"].get(bone.name, ())))
        for con in list(bone.constraints):
            # Legacy rigs lack the manifest; also drop obsolete solver targets.
            obsolete = (getattr(con, "target", None) == rig and
                        getattr(con, "subtarget", "").startswith("MCH.") and
                        con.subtarget not in fresh.data.bones)
            if con.name in names or obsolete:
                bone.constraints.remove(con)
    rig.data = fresh.data
    copy_properties(fresh, rig, True)
    anim = rig.animation_data_create()
    generated = {tuple(k) for k in owned.get("drivers", ())} | {tuple(k) for k in new_manifest["drivers"]}
    for curve in list(anim.drivers):
        if driver_key(curve) in generated:
            anim.drivers.remove(curve)
        elif curve.data_path.startswith('pose.bones["MCH.'):
            try:
                rig.path_resolve(curve.data_path)
            except ValueError:
                anim.drivers.remove(curve)
    for source in fresh.pose.bones:
        dest = rig.pose.bones[source.name]
        # Generated constraints evaluate before constraints added by an animator.
        for index, c in enumerate(source.constraints):
            copied = dest.constraints.copy(c)
            for prop in ("target", "pole_target", "space_object"):
                if getattr(copied, prop, None) == fresh:
                    setattr(copied, prop, rig)
            dest.constraints.move(len(dest.constraints) - 1, index)
        for attr in ("custom_shape", "use_custom_shape_bone_size", "custom_shape_scale_xyz",
                     "custom_shape_translation", "custom_shape_rotation_euler", "lock_location",
                     "lock_rotation", "lock_scale", "hide", "rotation_mode"):
            setattr(dest, attr, getattr(source, attr))
        copy_properties(source, dest, source.custom_shape is not None)
        dest.matrix_basis = source.matrix_basis
        if source.custom_shape and source.name in controls:
            dest.rotation_mode, dest.matrix_basis = controls[source.name]
    if fresh.animation_data:
        for curve in fresh.animation_data.drivers:
            copied = anim.drivers.from_existing(src_driver=curve)
            for variable in copied.driver.variables:
                for target in variable.targets:
                    if target.id == fresh:
                        target.id = rig
    for c in rig.data.collections_all:
        if c.name in old_visibility:
            c.is_visible = old_visibility[c.name]
    for mesh in meshes:
        if mesh.data.users > 1 or not mesh.data.is_editable:
            mesh.data = mesh.data.copy()
        for layout in changed.values():
            remap_weights(mesh, *layout)
    for path in new_paths:
        path.parent = rig
        for mod in path.modifiers:
            if mod.type == 'HOOK' and mod.object == fresh:
                # Assigning Hook.object recalculates its bind matrix from the
                # current pose. Retarget the original rest-space binding.
                bind = mod.matrix_inverse.copy()
                mod.object = rig
                mod.matrix_inverse = bind
        collection = rig.users_collection[0]
        if collection not in path.users_collection:
            collection.objects.link(path)
            for previous in list(path.users_collection):
                if previous != collection:
                    previous.objects.unlink(path)
    bpy.data.objects.remove(fresh, do_unlink=True)
    for path in old_paths:
        curve = path.data
        bpy.data.objects.remove(path, do_unlink=True)
        if not curve.users:
            bpy.data.batch_remove([curve])
    if not old_arm.users:
        bpy.data.armatures.remove(old_arm)
        rig.data.name = arm_name
    for widget in widgets:
        if not widget.users:
            geometry = widget.data
            bpy.data.objects.remove(widget)
            if geometry and not geometry.users:
                bpy.data.batch_remove([geometry])
    rig["rigging_generated"] = json.dumps(new_manifest)
    rig.hide_set(False)
    rig.select_set(True)
    context.view_layer.objects.active = rig
    rig.update_tag()
    context.view_layer.update()
    bpy.ops.object.mode_set(mode='POSE')
    return rig


def build(context, guide):
    target = target_for(context, guide)
    if not target and any(obj.get("rigging_guide") == guide for obj in context.scene.objects):
        raise ValueError("Choose the intended rig and switch to its guide before rebuilding")
    if target and (not target.is_editable or target not in context.view_layer.objects.values()):
        raise ValueError("The source rig must be editable in this view layer")
    old = division_layout(target.data) if target else {}
    new = division_layout(guide.data, guide=True)
    changed = {c: (old[c], new[c]) for c in old.keys() & new.keys() if old[c] != new[c]}
    anatomical = guide.get("xbiped") and (not target or target.data.get(skin_names.SCHEME))
    old_names = skin_names.name_map(target.data) if target and anatomical and changed else {}
    new_names = skin_names.name_map(guide.data) if anatomical and changed else {}
    for comp, (before, after) in list(changed.items()):
        source = [f"{comp}_{i}_jnt" for i in range(len(before))]
        dest = [f"{comp}_{i}_jnt" for i in range(len(after))]
        changed[comp] = (before, after, [old_names.get(n, n) for n in source], [new_names.get(n, n) for n in dest])
    affected_groups = {n for _, _, source, _ in changed.values() for n in source}
    meshes = [o for o in bpy.data.objects if changed and o.type == 'MESH'
              and any(m.type == 'ARMATURE' and m.object == target for m in o.modifiers)
              and not affected_groups.isdisjoint(o.vertex_groups.keys())]
    if any(not o.is_editable for o in meshes):
        raise ValueError("A linked skin mesh cannot be updated for the changed divisions")
    # Build and validate the entire rig before changing the live animation object.
    datablocks = (bpy.data.objects, bpy.data.armatures, bpy.data.meshes, bpy.data.curves)
    before = {item.session_uid for ids in datablocks for item in ids}
    try:
        fresh = builder.build(context, guide)
        if anatomical:
            skin_names.apply(fresh)
    except Exception:
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.data.batch_remove([item for ids in datablocks for item in ids if item.session_uid not in before])
        guide.hide_set(False)
        guide.select_set(True)
        context.view_layer.objects.active = guide
        raise
    if target:
        result = adopt(context, target, fresh, meshes, changed)
    else:
        result = fresh
        result["rigging_generated"] = json.dumps(manifest(result))
    guide["rigging_target"] = result
    for ids in datablocks:
        unused = [item for item in ids if item.session_uid not in before and not item.users]
        if unused:
            bpy.data.batch_remove(unused)
    return result
