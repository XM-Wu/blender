/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once

#include "gpu_shader_material_interface.bsl.hh"

[[node]]
void node_npr_rim(float4 color, float strength, float3 normal,
                  float angle, float arc_length, float arc_falloff,
                  float thickness, float thickness_falloff,
                  float width_px, float threshold, float softness, float samples,
                  float light_bias, float light_factor, float mask, float alpha,
                  float mode, float supported,
                  [[resource_table]] KernelGlobals &kg, const ShadingData &sd,
                  float4 &result, float &factor)
{
  npr_rim_impl(color, strength, normal, angle, arc_length, arc_falloff, thickness, thickness_falloff, width_px, threshold, softness, samples, light_bias, light_factor, mask, alpha, mode, supported, kg, sd, result, factor);
}
