# SPDX-License-Identifier: GPL-2.0-or-later
"""Build the EEVEE GF Toon material node asset with Blender in background mode."""
from pathlib import Path

import bpy


def build():
    tree = bpy.data.node_groups.new("GF Toon (EEVEE)", "ShaderNodeTree")
    tree["gf_toon"] = True
    iface = tree.interface
    iface.new_socket(name="Shader", in_out='OUTPUT', socket_type="NodeSocketShader")

    def socket(name, kind, default, panel=None, description=""):
        s = iface.new_socket(name=name, in_out='INPUT', socket_type=kind, parent=panel)
        s.default_value = default
        s.description = description
        if kind == "NodeSocketFloat":
            s.min_value, s.max_value = 0, 1
        return s

    def factor(name, default, panel=None, description=""):
        return socket(name, "NodeSocketFloat", default, panel, description)

    socket("Base Color", "NodeSocketColor", (.8, .8, .8, 1))
    socket("Shadow Tint", "NodeSocketColor", (.48, .42, .53, 1))
    factor("Threshold", .45)
    factor("Softness", .08, description="Width of the transition above the threshold").min_value = .0001
    face = iface.new_panel("Face", default_closed=True)
    factor("Face Coverage", 0, face, "Where to replace scene lighting with Face Light")
    factor("Face Light", 1, face, "Lit fraction from an authored face mask or directional SDF setup")
    bangs = iface.new_panel("Bangs", default_closed=True)
    factor("Fringe Shadow", 0, bangs, "Shadow mask on the receiving skin")
    factor("Bangs Transparency", 0, bangs, "Transparency mask on the bangs above facial features")
    hair = iface.new_panel("Hair and Lines", default_closed=True)
    factor("Highlight Mask", 0, hair)
    socket("Highlight Color", "NodeSocketColor", (.95, .88, .8, 1), hair)
    factor("Line Mask", 0, hair)
    socket("Line Color", "NodeSocketColor", (.025, .018, .03, 1), hair)
    normal = socket("Normal", "NodeSocketVector", (0, 0, 0),
                    description="World-space shading normal from a Normal Map or Bump node")
    normal.hide_value = True
    width = iface.new_socket(name="Outline Width", in_out='INPUT', socket_type="NodeSocketFloat", parent=hair)
    width.default_value = 0
    width.min_value = 0
    width.max_value = 16
    width.description = "Screen-space line width in pixels with EEVEE NPR Outlines enabled"

    def node(kind, x, y, label=""):
        n = tree.nodes.new(kind)
        n.location = x, y
        n.label = label
        return n

    def inputs(x, y, *names):
        n = node("NodeGroupInput", x, y)
        for s in n.outputs:
            s.hide = s.name not in names
        return n.outputs

    link = tree.links.new
    diffuse = node("ShaderNodeBsdfDiffuse", 0, 300)
    diffuse.inputs["Color"].default_value = (1, 1, 1, 1)
    mapped = inputs(-820, 590, "Normal")["Normal"]
    length = node("ShaderNodeVectorMath", -620, 590)
    length.operation = 'LENGTH'
    link(mapped, length.inputs[0])
    geometry = node("ShaderNodeNewGeometry", -620, 330)
    choose = node("ShaderNodeMix", -330, 520, "Use surface normal when unconnected")
    choose.data_type = 'VECTOR'
    link(length.outputs["Value"], choose.inputs[0])
    link(geometry.outputs["Normal"], choose.inputs[4])
    link(mapped, choose.inputs[5])
    link(choose.outputs[1], diffuse.inputs["Normal"])
    lighting = node("ShaderNodeShaderToRGB", 200, 300, "Scene diffuse lighting")
    link(diffuse.outputs[0], lighting.inputs[0])
    p = inputs(0, 0, "Threshold", "Softness")
    upper = node("ShaderNodeMath", 200, 0, "Upper band edge")
    upper.operation = 'ADD'
    link(p["Threshold"], upper.inputs[0])
    link(p["Softness"], upper.inputs[1])
    band = node("ShaderNodeMapRange", 420, 300)
    band.interpolation_type = 'SMOOTHSTEP'
    link(lighting.outputs["Color"], band.inputs["Value"])
    link(p["Threshold"], band.inputs["From Min"])
    link(upper.outputs[0], band.inputs["From Max"])

    def mix(x, y, label, fac, a, b, mode='MIX'):
        n = node("ShaderNodeMixRGB", x, y, label)
        n.blend_type = mode
        for i, value in enumerate((fac, a, b)):
            if isinstance(value, bpy.types.NodeSocket):
                link(value, n.inputs[i])
            else:
                n.inputs[i].default_value = value
        return n.outputs[0]

    p = inputs(420, -40, "Face Coverage", "Face Light")
    lit = mix(640, 300, "Face lighting override", p["Face Coverage"], band.outputs[0], p["Face Light"])
    p = inputs(640, -40, "Fringe Shadow")
    lit = mix(850, 300, "Fringe shadow", p["Fringe Shadow"], lit, (0, 0, 0, 1))
    p = inputs(850, -40, "Shadow Tint", "Base Color")
    tint = mix(1060, 300, "Shadow to light", lit, p["Shadow Tint"], (1, 1, 1, 1))
    color = mix(1270, 300, "Base times band", 1, p["Base Color"], tint, 'MULTIPLY')
    p = inputs(1270, -40, "Highlight Mask", "Highlight Color")
    color = mix(1480, 300, "Hair highlight", p["Highlight Mask"], color, p["Highlight Color"])
    p = inputs(1480, -40, "Line Mask", "Line Color")
    color = mix(1690, 300, "Line color", p["Line Mask"], color, p["Line Color"])
    emission = node("ShaderNodeEmission", 1900, 300)
    link(color, emission.inputs["Color"])
    transparent = node("ShaderNodeBsdfTransparent", 1900, 100)
    p = inputs(1900, -70, "Bangs Transparency")
    surface = node("ShaderNodeMixShader", 2110, 300)
    link(p["Bangs Transparency"], surface.inputs[0])
    link(emission.outputs[0], surface.inputs[1])
    link(transparent.outputs[0], surface.inputs[2])
    output = node("NodeGroupOutput", 2320, 300)
    link(surface.outputs[0], output.inputs[0])
    outline = node("ShaderNodeOutlineControl", 2110, -240)
    p = inputs(1860, -300, "Line Color", "Outline Width")
    link(p["Line Color"], outline.inputs["Line Color"])
    link(p["Outline Width"], outline.inputs["Line Width"])
    outline.inputs["Normal Edge Width"].default_value = 0
    tree.asset_mark()
    tree.asset_data.description = "EEVEE toon lighting with optional face, fringe, highlight and line masks"
    tree.asset_data.catalog_id = "4299658c-fba7-4620-ae37-cbdb106ef444"
    tree.default_group_node_width = 230
    asset = Path(__file__).resolve().parents[2] / "assets/nodes/gf_toon.blend"
    bpy.data.libraries.write(str(asset), {tree}, fake_user=True, compress=True)


if __name__ == "__main__":
    build()
