/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once

#include "gpu_shader_material_interface.bsl.hh"

[[node]]
void node_npr_shader_info(float3 position, float3 normal, float exponent, float light_group,
                          [[resource_table]] KernelGlobals &kg, const ShadingData &sd,
                          float4 &diffuse, float &shadow, float4 &ambient,
                          float &half_lambert, float &blinn_phong, float &self_shadow, float &cast_shadow)
{
  npr_shader_info_impl(position, normal, exponent, light_group, kg, sd, diffuse, shadow, ambient, half_lambert, blinn_phong, self_shadow, cast_shadow);
}
