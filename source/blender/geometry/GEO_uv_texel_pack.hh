/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include "BLI_math_matrix_types.hh"
#include "BLI_math_vector_types.hh"
#include "BLI_span.hh"
#include "BLI_vector.hh"

/** \file
 * \ingroup geo
 *
 * Texel-grid UV island packer.
 *
 * All coordinates are in texture pixels of a single tile (`u * width`, `v * height`), so margins,
 * edge padding and pixel snapping are exact integer operations on an occupancy raster.
 */

namespace blender::geometry::texel_pack {

enum class Rotation : int8_t {
  /** Keep the current orientation. */
  None = 0,
  /** Multiples of 90 degrees. */
  Cardinal = 1,
  /** Pre-rotate to the minimum area bounding box, then multiples of 90 degrees. */
  Free = 2,
};

enum class DensityMode : int8_t {
  /** Normalize texel density across islands, then scale everything to fill the tiles. */
  Uniform = 0,
  /** Keep the relative UV scale of islands, then scale everything to fill the tiles. */
  Relative = 1,
  /** Use a fixed texel density in pixels per meter, overflowing into further tiles. */
  Target = 2,
};

enum class GroupLayout : int8_t {
  /** Ignore groups. */
  Mixed = 0,
  /** Place islands of the same group next to each other. */
  Cluster = 1,
  /** Every group gets its own tile. */
  TilePerGroup = 2,
};

struct Island {
  /** Triangles in pixel space, three entries per triangle. */
  Vector<float2> triangles;
  /** World space surface area, used for texel density. */
  float area_3d = 0.0f;
  /** Multiplier of the texel density of this island. */
  float density_weight = 1.0f;
  Rotation rotation = Rotation::None;
  bool allow_flip = false;
  /** Keep the current UV scale instead of following the global density. */
  bool keep_scale = false;
  /** Zero means no group. */
  int group = 0;
  /**
   * Locked islands are obstacles at their current position in tile `locked_tile`;
   * `triangles` are then tile local.
   */
  bool locked = false;
  int locked_tile = 0;

  /* Results: `uv_px_out = matrix * uv_px_in + translation`, in the local space of `tile`. */
  bool placed = false;
  int tile = 0;
  float2x2 matrix = float2x2::identity();
  float2 translation = float2(0.0f);
};

struct Params {
  /** Texture size of one tile in pixels. */
  int width = 1024;
  int height = 1024;
  /** Pixels per occupancy cell (1 is exact). */
  int cell_size = 1;
  /** Minimum distance between islands, in pixels. */
  float margin = 4.0f;
  /** Minimum distance between islands and the tile border, in pixels. */
  float edge_padding = 2.0f;
  /** Translate islands by whole pixels only. */
  bool pixel_snap = true;
  DensityMode density_mode = DensityMode::Uniform;
  /** Pixels per meter for #DensityMode::Target. */
  float target_density = 512.0f;
  /** Fraction an island may shrink below the global density to make it fit (0..1). */
  float density_tolerance = 0.0f;
  /** In #DensityMode::Uniform, use the median density of locked islands instead of filling. */
  bool match_locked_density = true;
  int max_tiles = 1;
  GroupLayout group_layout = GroupLayout::Mixed;

  const bool *stop = nullptr;
  float *progress = nullptr;
};

struct Result {
  int placed_count = 0;
  int unplaced_count = 0;
  int tiles_used = 0;
  /** Packed island area divided by the area of the used tiles. */
  float utilization = 0.0f;
  /** Largest relative deviation from the median density, among density-normalized islands. */
  float density_deviation = 0.0f;
  /** Final density in pixels per meter (median), zero when unknown. */
  float density = 0.0f;
  bool cancelled = false;
  /** More groups than tiles with #GroupLayout::TilePerGroup. */
  bool too_many_groups = false;
};

/**
 * Pack `islands` in place, writing the transform of every non-locked island that could be
 * placed. Results are deterministic for a given input.
 */
Result pack(MutableSpan<Island> islands, const Params &params);

/** Texel density in pixels per meter of a set of pixel space triangles. */
float texel_density(Span<float2> triangles, float area_3d);

}  // namespace blender::geometry::texel_pack
