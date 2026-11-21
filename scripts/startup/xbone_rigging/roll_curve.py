# SPDX-License-Identifier: GPL-2.0-or-later
"""Evaluate Shifter's per-span arc table and its interpolated unit tangents."""
import bpy
from mathutils import Vector


def node_group(subdivisions):
    name = f"XBone Shifter Roll Samples {subdivisions}"
    existing = bpy.data.node_groups.get(name)
    if existing and existing.get("xbone_roll_samples") == 1:
        return existing
    tree = bpy.data.node_groups.new(name, 'GeometryNodeTree')
    tree["xbone_roll_samples"] = 1
    for direction in ('INPUT', 'OUTPUT'):
        tree.interface.new_socket(name="Geometry", in_out=direction, socket_type="NodeSocketGeometry")

    def node(kind):
        return tree.nodes.new(kind)

    def feed(value, socket):
        if isinstance(value, bpy.types.NodeSocket):
            tree.links.new(value, socket)
        else:
            socket.default_value = value

    def vector(op, a, b=None):
        n = node('ShaderNodeVectorMath')
        n.operation = op
        feed(a, n.inputs[0])
        if b is not None:
            feed(b, n.inputs['Scale'] if op == 'SCALE' else n.inputs[1])
        return n.outputs['Vector']

    def attr(name, kind):
        n = node('GeometryNodeInputNamedAttribute')
        n.data_type = kind
        n.inputs['Name'].default_value = name
        return n.outputs['Attribute']
    inputs = node('NodeGroupInput')
    mesh = inputs.outputs['Geometry']
    pos = node('GeometryNodeInputPosition').outputs[0]
    points = []
    for i in range(4):
        n = node('GeometryNodeSampleIndex')
        n.data_type = 'FLOAT_VECTOR'
        n.domain = 'POINT'
        feed(mesh, n.inputs['Geometry'])
        feed(pos, n.inputs['Value'])
        feed(i, n.inputs['Index'])
        points.append(n.outputs['Value'])
    u = node('GeometryNodeSplineParameter').outputs['Factor']

    def lerp(a, b):
        return vector('ADD', a, vector('SCALE', vector('SUBTRACT', b, a), u))
    ab, bc, cd = [lerp(a, b) for a, b in zip(points, points[1:])]
    left, right = lerp(ab, bc), lerp(bc, cd)
    tangent = vector('NORMALIZE', vector('SUBTRACT', right, left))
    # The first entry of the rollSplineKine arc table is the original handle
    # vector. Later entries store normalized Bézier derivatives.
    source_scale = node('GeometryNodeSampleIndex')
    source_scale.data_type = 'FLOAT'
    feed(mesh, source_scale.inputs['Geometry'])
    feed(attr("xbone_inverse_unit", 'FLOAT'), source_scale.inputs['Value'])
    choose = node('GeometryNodeSwitch')
    choose.input_type = 'VECTOR'
    feed(node('GeometryNodeInputIndex').outputs[0], choose.inputs['Switch'])
    feed(tangent, choose.inputs['True'])
    feed(
        vector(
            'SCALE',
            vector(
                'SUBTRACT',
                points[1],
                points[0]),
            source_scale.outputs['Value']),
        choose.inputs['False'])

    def sample_table(count):
        line = node('GeometryNodeCurvePrimitiveLine')
        resample = node('GeometryNodeResampleCurve')
        resample.inputs['Mode'].default_value = 'Count'
        feed(line.outputs[0], resample.inputs['Curve'])
        feed(count, resample.inputs['Count'])
        save = node('GeometryNodeStoreNamedAttribute')
        save.data_type = 'FLOAT_VECTOR'
        save.domain = 'POINT'
        feed(resample.outputs[0], save.inputs['Geometry'])
        feed("xbone_curve_tangent", save.inputs['Name'])
        feed(choose.outputs[0], save.inputs['Value'])
        arc = node('GeometryNodeSetPosition')
        feed(save.outputs[0], arc.inputs['Geometry'])
        feed(lerp(left, right), arc.inputs['Position'])
        sample = node('GeometryNodeSampleCurve')
        sample.data_type = 'FLOAT_VECTOR'
        sample.mode = 'FACTOR'
        feed(arc.outputs[0], sample.inputs['Curves'])
        feed(attr("xbone_curve_tangent", 'FLOAT_VECTOR'), sample.inputs['Value'])
        feed(attr("xbone_fraction", 'FLOAT'), sample.inputs['Factor'])
        return sample
    dense = sample_table(subdivisions)

    def field(key):
        if subdivisions != 45:
            return dense.outputs[key]
        switch = node('GeometryNodeSwitch')
        switch.input_type = 'VECTOR'
        feed(attr("xbone_support", 'BOOLEAN'), switch.inputs['Switch'])
        feed(sparse.outputs[key], switch.inputs['False'])
        feed(dense.outputs[key], switch.inputs['True'])
        return switch.outputs[0]
    if subdivisions == 45:
        sparse = sample_table(10)
    offset = vector('SCALE', field('Value'), attr("xbone_direction", 'FLOAT'))
    output = node('GeometryNodeSetPosition')
    feed(mesh, output.inputs['Geometry'])
    test = node('FunctionNodeCompare')
    test.data_type = 'INT'
    test.operation = 'GREATER_EQUAL'
    feed(node('GeometryNodeInputIndex').outputs[0], next(s for s in test.inputs if s.name == 'A' and s.enabled))
    feed(4, next(s for s in test.inputs if s.name == 'B' and s.enabled))
    feed(test.outputs[0], output.inputs['Selection'])
    feed(vector('ADD', field('Position'), offset), output.inputs['Position'])
    feed(output.outputs[0], node('NodeGroupOutput').inputs[0])
    return tree


def create(rig, name, points, hooks, fractions, subdivisions, unit):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([*points, *[(0, 0, 0)] * (2 * len(fractions))], [], [])
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    rig.users_collection[0].objects.link(obj)
    obj.parent = rig
    obj.hide_select = obj.hide_render = obj.hide_viewport = True
    obj["xbone_solver_mesh"] = True
    mesh.attributes.new("xbone_inverse_unit", 'FLOAT', 'POINT').data[0].value = 1 / unit
    mesh.attributes.new("xbone_fraction", 'FLOAT', 'POINT')
    mesh.attributes.new("xbone_direction", 'FLOAT', 'POINT')
    mesh.attributes.new("xbone_support", 'BOOLEAN', 'POINT')
    for i, (joint, f, high) in enumerate(fractions):
        for k, suffix in enumerate(("point", "tangent")):
            idx = 4 + 2 * i + k
            mesh.attributes["xbone_support"].data[idx].value = high
            mesh.attributes["xbone_fraction"].data[idx].value = f
            mesh.attributes["xbone_direction"].data[idx].value = k * unit
            obj.vertex_groups.new(name=joint + "." + suffix).add([idx], 1, 'REPLACE')
    for i, h in enumerate(hooks):
        mod = obj.modifiers.new(h, 'HOOK')
        mod.object, mod.subtarget = rig, h
        mod.vertex_indices_set([i])
        mod.matrix_inverse = rig.data.bones[h].matrix_local.inverted_safe()
    mod = obj.modifiers.new("Roll spline", 'NODES')
    mod.node_group = node_group(subdivisions)
    return obj
