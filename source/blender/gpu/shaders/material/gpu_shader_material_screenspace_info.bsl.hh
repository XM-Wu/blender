/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once

#include "gpu_shader_material_interface.bsl.hh"

[[node]]
void node_screenspace_info(float3 position, float explicit_position,
                           [[resource_table]] KernelGlobals &kg, const ShadingData &sd,
                           float4 &color, float &depth)
{
  screenspace_info_impl(kg, sd, position, explicit_position, color, depth);
}
