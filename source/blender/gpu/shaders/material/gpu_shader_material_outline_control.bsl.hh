/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once

#include "gpu_shader_material_interface.bsl.hh"

[[node]]
void node_output_outline(
    float4 line_color,
    float line_alpha,
    float line_width,
    float depth_threshold,
    float depth_threshold_range,
    float depth_edge_width,
    float normal_threshold,
    float normal_threshold_range,
    float normal_edge_width,
    int outline_id,
    bool id_edge,
    float id_edge_width,
    bool freestyle_edge,
    float expansion_mask,
    float id_combine,
    float edge_priority,
    [[resource_table]] KernelGlobals &kg,
    ShadingData &sd,
    Closure &dummy)
{
  npr_outline_write(line_color, line_alpha, line_width, depth_threshold, depth_threshold_range, depth_edge_width, normal_threshold, normal_threshold_range, normal_edge_width, outline_id, id_edge, id_edge_width, freestyle_edge, expansion_mask, id_combine, edge_priority, kg, sd, dummy);
}
