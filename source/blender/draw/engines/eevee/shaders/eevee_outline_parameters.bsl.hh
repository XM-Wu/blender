/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once

#include "gpu_shader_math_base.bsl.hh"

#define OUTLINE_INFO_CODE_MAX 65535u
#define OUTLINE_WIDTH_PACK_MAX 65534u
/* Mask isolating the low-16-bit width code in an info.R channel. The upper 16 bits of R now carry
 * depth_threshold_range and depth_edge_width (see outline_info_r_pack); without this mask,
 * outline_width_unpack would read the full 32-bit channel and decode a width of ~65534 whenever
 * any upper byte is non-zero, flooding JFA coverage to the whole screen. */
#define OUTLINE_WIDTH_PACK_MASK 65535u

uint outline_unorm16_pack(float value)
{
  return uint(saturate(value) * float(OUTLINE_INFO_CODE_MAX) + 0.5f);
}

uint outline_unorm_pack(float value, uint max_value)
{
  return uint(saturate(value) * float(max_value) + 0.5f);
}

uint outline_width_pack(float width)
{
  width = max(width, 0.0f);
  if (width <= 0.0f) {
    return 0u;
  }
  return min(outline_unorm16_pack(width / (width + 1.0f)), OUTLINE_WIDTH_PACK_MAX);
}

float outline_width_unpack(uint width_packed)
{
  width_packed = min(width_packed & OUTLINE_WIDTH_PACK_MASK, OUTLINE_WIDTH_PACK_MAX);
  if (width_packed == 0u) {
    return 0.0f;
  }
  const float width_normalized = float(width_packed) / float(OUTLINE_INFO_CODE_MAX);
  return width_normalized / (1.0f - width_normalized);
}

/* Outline info packing (UINT_32_32_32_32, each channel is a 32-bit unsigned int).
 * R: [31..24: depth_edge_width  u8] [23..16: depth_threshold_range u8] [15..0: outline_width_pack]
 * G: [31..24: normal_edge_width u8] [23..16: normal_threshold_range u8] [15..0: depth_threshold unorm16]
 * B: [31: freestyle_edge bit] [30..23: id_edge_width u8] [19..18: priority]
 *    [17..16: ID combine] [15..0: normal_threshold unorm16]
 * A: [31..24: expansion block u8] [15: id_edge bit] [14..0: outline_id 15 bits].
 * Store the inverse mask so cleared pixels and old shaders allow expansion. */
#define OUTLINE_THRESHOLD_VALUE_MASK OUTLINE_INFO_CODE_MAX
#define OUTLINE_UNORM8_MAX 255u

#define OUTLINE_DEPTH_THRESHOLD_RANGE_SHIFT 16u
#define OUTLINE_DEPTH_EDGE_WIDTH_SHIFT 24u
#define OUTLINE_NORMAL_THRESHOLD_RANGE_SHIFT 16u
#define OUTLINE_NORMAL_EDGE_WIDTH_SHIFT 24u
#define OUTLINE_ID_EDGE_WIDTH_SHIFT 23u

#define OUTLINE_ID_VALUE_MASK 32767u
#define OUTLINE_ID_EDGE_BIT 32768u
#define OUTLINE_NORMAL_THRESHOLD_VALUE_MASK OUTLINE_INFO_CODE_MAX
#define OUTLINE_FREESTYLE_EDGE_BIT 2147483648u

/* R channel: line width + depth range + depth edge width. */
uint outline_info_r_pack(float line_width,
                         float depth_threshold_range,
                         float depth_edge_width)
{
  return outline_width_pack(line_width) |
         (outline_unorm_pack(depth_threshold_range, OUTLINE_UNORM8_MAX)
          << OUTLINE_DEPTH_THRESHOLD_RANGE_SHIFT) |
         (outline_unorm_pack(depth_edge_width, OUTLINE_UNORM8_MAX)
          << OUTLINE_DEPTH_EDGE_WIDTH_SHIFT);
}

float outline_depth_threshold_range_unpack(uint info_r)
{
  return float((info_r >> OUTLINE_DEPTH_THRESHOLD_RANGE_SHIFT) & OUTLINE_UNORM8_MAX) /
         float(OUTLINE_UNORM8_MAX);
}

float outline_depth_edge_width_unpack(uint info_r)
{
  return float((info_r >> OUTLINE_DEPTH_EDGE_WIDTH_SHIFT) & OUTLINE_UNORM8_MAX) /
         float(OUTLINE_UNORM8_MAX);
}

/* G channel: depth threshold + normal range + normal edge width. */
uint outline_info_g_pack(float depth_threshold,
                         float normal_threshold_range,
                         float normal_edge_width)
{
  return outline_unorm16_pack(depth_threshold) |
         (outline_unorm_pack(normal_threshold_range, OUTLINE_UNORM8_MAX)
          << OUTLINE_NORMAL_THRESHOLD_RANGE_SHIFT) |
         (outline_unorm_pack(normal_edge_width, OUTLINE_UNORM8_MAX)
          << OUTLINE_NORMAL_EDGE_WIDTH_SHIFT);
}

float outline_depth_threshold_unpack(uint threshold_packed)
{
  return float(threshold_packed & OUTLINE_THRESHOLD_VALUE_MASK) /
         float(OUTLINE_THRESHOLD_VALUE_MASK);
}

float outline_normal_threshold_range_unpack(uint info_g)
{
  return float((info_g >> OUTLINE_NORMAL_THRESHOLD_RANGE_SHIFT) & OUTLINE_UNORM8_MAX) /
         float(OUTLINE_UNORM8_MAX);
}

float outline_normal_edge_width_unpack(uint info_g)
{
  return float((info_g >> OUTLINE_NORMAL_EDGE_WIDTH_SHIFT) & OUTLINE_UNORM8_MAX) /
         float(OUTLINE_UNORM8_MAX);
}

/* B channel: normal threshold + id edge width + freestyle bit. */
uint outline_info_b_pack(float normal_threshold, float id_edge_width, bool freestyle_edge)
{
  uint packed_b = outline_unorm16_pack(normal_threshold);
  packed_b |= outline_unorm_pack(id_edge_width, OUTLINE_UNORM8_MAX)
              << OUTLINE_ID_EDGE_WIDTH_SHIFT;
  if (freestyle_edge) {
    packed_b |= OUTLINE_FREESTYLE_EDGE_BIT;
  }
  return packed_b;
}

float outline_normal_threshold_unpack(uint threshold_packed)
{
  return float(threshold_packed & OUTLINE_NORMAL_THRESHOLD_VALUE_MASK) /
         float(OUTLINE_NORMAL_THRESHOLD_VALUE_MASK);
}

float outline_id_edge_width_unpack(uint info_b)
{
  return float((info_b >> OUTLINE_ID_EDGE_WIDTH_SHIFT) & OUTLINE_UNORM8_MAX) /
         float(OUTLINE_UNORM8_MAX);
}

bool outline_freestyle_edge_unpack(uint threshold_packed)
{
  return (threshold_packed & OUTLINE_FREESTYLE_EDGE_BIT) != 0u;
}

/* A channel: outline id + id edge bit (unchanged). */
uint outline_id_pack(uint outline_id, bool id_edge)
{
  uint packed_id = min(outline_id, OUTLINE_ID_VALUE_MASK);
  if (id_edge) {
    packed_id |= OUTLINE_ID_EDGE_BIT;
  }
  return packed_id;
}

uint outline_id_unpack(uint outline_id_packed)
{
  return outline_id_packed & OUTLINE_ID_VALUE_MASK;
}

bool outline_id_edge_unpack(uint outline_id_packed)
{
  return (outline_id_packed & OUTLINE_ID_EDGE_BIT) != 0u;
}

uint outline_expansion_block_pack(float block)
{
  return outline_unorm_pack(block, OUTLINE_UNORM8_MAX) << 24u;
}

float outline_expansion_block_unpack(uint info_a)
{
  return float(info_a >> 24u) / float(OUTLINE_UNORM8_MAX);
}

uint outline_expansion_block_replace(uint info_a, float block)
{
  return (info_a & 0x00ffffffu) | outline_expansion_block_pack(block);
}
