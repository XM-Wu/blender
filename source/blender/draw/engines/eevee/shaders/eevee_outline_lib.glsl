/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include "infos/eevee_outline_infos.hh"

#include "eevee_defines.hh"
#include "gpu_shader_math_base.bsl.hh"
#include "gpu_shader_utildefines.bsl.hh"

/* Select one full-resolution source pixel per reduced outline pixel. Integer data (IDs,
 * packed parameters and surface depth) must never be filtered across material boundaries. */
int2 outline_source_texel(int2 texel, int2 source_extent, int2 target_extent)
{
  return min(int2((float2(texel) + 0.5f) * float2(source_extent) / float2(target_extent)),
             source_extent - int2(1));
}

/* Outline info is stored in integer channels to avoid Vulkan storage-image issues with UNORM
 * formats. Reserve the top width code-point so unpacking never hits 1.0 exactly and can
 * represent very large widths. */
#include "eevee_outline_parameters.bsl.hh"

float outline_pixel_world_size_at(float depth, int2 extent, int2 texel)
{
#if defined(DRW_VIEW_LIB_FUNCTIONS_DEFINED)
  float2 uv = (float2(texel) + 0.5f) / float2(extent);
  int2 next_texel = min(texel + int2(1, 0), extent - int2(1));
  float2 next_uv = (float2(next_texel) + 0.5f) / float2(extent);
  return distance(drw_point_screen_to_view(float3(uv, depth)),
                  drw_point_screen_to_view(float3(next_uv, depth)));
#else
  UNUSED_VARS(depth);
  UNUSED_VARS(extent);
  UNUSED_VARS(texel);
  return 1.0f;
#endif
}
