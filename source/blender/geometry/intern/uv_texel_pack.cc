/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup geo
 *
 * Texel-grid UV island packer, see `doc/uv_pack_design.md`.
 *
 * Islands are rasterized conservatively onto an occupancy grid whose cells are `cell_size`
 * texture pixels. The collision mask of an island is dilated by the margin, while the occupancy
 * grid stores undilated masks, so any two islands are at least `margin` pixels apart. Islands are
 * placed bottom-left first, and the global density is found by a parallel bracketing search.
 */

#include <algorithm>
#include <bit>
#include <cmath>
#include <numeric>

#include "BLI_convexhull_2d.hh"
#include "BLI_index_range.hh"
#include "BLI_map.hh"
#include "BLI_math_base.hh"
#include "BLI_math_matrix.hh"
#include "BLI_math_vector.hh"
#include "BLI_task.hh"

#include "GEO_uv_texel_pack.hh"

namespace blender::geometry::texel_pack {

/* -------------------------------------------------------------------- */
/** \name Bit Raster
 *
 * Column `x` of a row is bit `x % 64` of word `x / 64`.
 * \{ */

struct Raster {
  int width = 0;
  int height = 0;
  int words = 0;
  Vector<uint64_t> bits;

  void resize(const int w, const int h)
  {
    width = w;
    height = h;
    words = (w + 63) / 64;
    bits.clear();
    bits.append_n_times(0, int64_t(words) * h);
  }

  uint64_t *row(const int y)
  {
    return bits.data() + int64_t(y) * words;
  }
  const uint64_t *row(const int y) const
  {
    return bits.data() + int64_t(y) * words;
  }

  void set_span(const int x0, const int x1, const int y)
  {
    uint64_t *r = row(y);
    for (int x = x0; x <= x1; x++) {
      r[x >> 6] |= uint64_t(1) << (x & 63);
    }
  }

  bool get(const int x, const int y) const
  {
    return (row(y)[x >> 6] >> (x & 63)) & 1;
  }
};

/** 64 bits of `row` starting at column `bit`, columns outside the row read as zero. */
static uint64_t row_window(const uint64_t *row, const int words, const int bit)
{
  if (bit >= words * 64 || bit <= -64) {
    return 0;
  }
  const int word = bit >= 0 ? bit / 64 : -((-bit + 63) / 64);
  const int shift = bit - word * 64;
  const uint64_t lo = (word >= 0 && word < words) ? row[word] : 0;
  if (shift == 0) {
    return lo;
  }
  const uint64_t hi = (word + 1 >= 0 && word + 1 < words) ? row[word + 1] : 0;
  return (lo >> shift) | (hi << (64 - shift));
}

/** OR `value` into `row` starting at column `bit`, dropping columns outside the row. */
static void row_or(uint64_t *row, const int words, const int bit, const uint64_t value)
{
  if (value == 0 || bit >= words * 64 || bit <= -64) {
    return;
  }
  const int word = bit >= 0 ? bit / 64 : -((-bit + 63) / 64);
  const int shift = bit - word * 64;
  if (word >= 0 && word < words) {
    row[word] |= value << shift;
  }
  if (shift != 0 && word + 1 >= 0 && word + 1 < words) {
    row[word + 1] |= value >> (64 - shift);
  }
}

/** Longest run of set (`value == true`) or clear bits within the first `width` columns. */
static int longest_run(const uint64_t *row, const int width, const bool value, int *r_start)
{
  int best = 0;
  int best_start = 0;
  int run = 0;
  int x = 0;
  while (x < width) {
    const uint64_t word = value ? row[x >> 6] : ~row[x >> 6];
    if ((x & 63) == 0 && x + 64 <= width && (word == 0 || word == ~uint64_t(0))) {
      if (word == 0) {
        run = 0;
      }
      else {
        run += 64;
        if (run > best) {
          best = run;
          best_start = x + 64 - run;
        }
      }
      x += 64;
      continue;
    }
    if ((word >> (x & 63)) & 1) {
      run++;
      if (run > best) {
        best = run;
        best_start = x + 1 - run;
      }
    }
    else {
      run = 0;
    }
    x++;
  }
  if (r_start) {
    *r_start = best_start;
  }
  return best;
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Island Shapes
 * \{ */

/** An island rasterized at one scale and orientation. */
struct Shape {
  /** Island cells, `w * h`. */
  Raster raw;
  /** Collision mask, `raw` dilated by the margin on every side. */
  Raster dilated;
  /** Longest run of set bits per row of `dilated`, and where it starts. */
  Vector<int> run_length;
  Vector<int> run_start;
  int key_row = 0;
  /** `pixel = matrix * uv_px - origin` maps the island onto `raw` (in pixels, not cells). */
  float2x2 matrix;
  float2 origin;
};

/** Horizontal extent of a triangle clipped to the strip `y0 <= y <= y1`. */
static bool triangle_strip_x_range(
    const float2 tri[3], const float y0, const float y1, float *r_xmin, float *r_xmax)
{
  float xmin = FLT_MAX;
  float xmax = -FLT_MAX;
  for (int i = 0; i < 3; i++) {
    const float2 a = tri[i];
    const float2 b = tri[(i + 1) % 3];
    if (a.y >= y0 && a.y <= y1) {
      xmin = std::min(xmin, a.x);
      xmax = std::max(xmax, a.x);
    }
    if (a.y == b.y) {
      continue;
    }
    for (const float yb : {y0, y1}) {
      if ((yb - a.y) * (yb - b.y) <= 0.0f) {
        const float t = (yb - a.y) / (b.y - a.y);
        const float x = a.x + t * (b.x - a.x);
        xmin = std::min(xmin, x);
        xmax = std::max(xmax, x);
      }
    }
  }
  if (xmin > xmax) {
    return false;
  }
  *r_xmin = xmin;
  *r_xmax = xmax;
  return true;
}

/** Mark every cell touched by the triangle (cell coordinates). */
static void rasterize_triangle(Raster &raster, const float2 tri[3])
{
  const float ymin = std::min({tri[0].y, tri[1].y, tri[2].y});
  const float ymax = std::max({tri[0].y, tri[1].y, tri[2].y});
  const int row_min = std::max(0, int(std::floor(ymin)));
  const int row_max = std::min(raster.height - 1,
                               std::max(int(std::floor(ymin)), int(std::ceil(ymax)) - 1));
  for (int y = row_min; y <= row_max; y++) {
    float xmin, xmax;
    if (!triangle_strip_x_range(tri, float(y), float(y + 1), &xmin, &xmax)) {
      continue;
    }
    const int x0 = std::max(0, int(std::floor(xmin)));
    const int x1 = std::min(raster.width - 1,
                            std::max(int(std::floor(xmin)), int(std::ceil(xmax)) - 1));
    if (x0 <= x1) {
      raster.set_span(x0, x1, y);
    }
  }
}

static float2x2 orientation_matrix(const float angle, const bool flip, const float scale)
{
  const float c = std::cos(angle) * scale;
  const float s = std::sin(angle) * scale;
  float2x2 rotation(float2(c, s), float2(-s, c));
  if (flip) {
    rotation = rotation * float2x2(float2(-1.0f, 0.0f), float2(0.0f, 1.0f));
  }
  return rotation;
}

static Shape build_shape(const Span<float2> triangles,
                         const float2x2 &matrix,
                         const int cell_size,
                         const int margin_cells,
                         const bool pixel_snap)
{
  Shape shape;
  shape.matrix = matrix;

  Vector<float2> points(triangles.size());
  float2 min(FLT_MAX), max(-FLT_MAX);
  for (const int64_t i : triangles.index_range()) {
    points[i] = matrix * triangles[i];
    min = math::min(min, points[i]);
    max = math::max(max, points[i]);
  }
  const float cell = float(cell_size);
  shape.origin = pixel_snap ? math::floor(min / cell) * cell : min;

  const int w = std::max(1, int(std::ceil((max.x - shape.origin.x) / cell - 1e-4f)));
  const int h = std::max(1, int(std::ceil((max.y - shape.origin.y) / cell - 1e-4f)));
  shape.raw.resize(w, h);
  for (int64_t i = 0; i + 2 < points.size(); i += 3) {
    const float2 tri[3] = {(points[i] - shape.origin) / cell,
                           (points[i + 1] - shape.origin) / cell,
                           (points[i + 2] - shape.origin) / cell};
    rasterize_triangle(shape.raw, tri);
  }

  /* Dilate horizontally per row, then vertically. */
  const int m = margin_cells;
  const int dw = w + 2 * m;
  const int dh = h + 2 * m;
  Raster horizontal;
  horizontal.resize(dw, h);
  for (int y = 0; y < h; y++) {
    const uint64_t *src = shape.raw.row(y);
    uint64_t *dst = horizontal.row(y);
    for (int i = 0; i < shape.raw.words; i++) {
      for (int d = 0; d <= 2 * m; d++) {
        row_or(dst, horizontal.words, i * 64 + d, src[i]);
      }
    }
  }
  shape.dilated.resize(dw, dh);
  for (int y = 0; y < h; y++) {
    const uint64_t *src = horizontal.row(y);
    for (int d = 0; d <= 2 * m; d++) {
      uint64_t *dst = shape.dilated.row(y + d);
      for (int i = 0; i < horizontal.words; i++) {
        dst[i] |= src[i];
      }
    }
  }

  shape.run_length.resize(dh);
  shape.run_start.resize(dh);
  int best = -1;
  for (int y = 0; y < dh; y++) {
    shape.run_length[y] = longest_run(shape.dilated.row(y), dw, true, &shape.run_start[y]);
    if (shape.run_length[y] > best) {
      best = shape.run_length[y];
      shape.key_row = y;
    }
  }
  return shape;
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Occupancy
 * \{ */

struct Occupancy {
  Raster grid;
  /** Longest run of free cells per row. */
  Vector<int> max_free;

  void init(const int w, const int h)
  {
    grid.resize(w, h);
    max_free = Vector<int>(h, w);
  }

  void update_rows(const int y0, const int y1)
  {
    for (int y = std::max(0, y0); y <= std::min(grid.height - 1, y1); y++) {
      max_free[y] = longest_run(grid.row(y), grid.width, false, nullptr);
    }
  }

  bool collides(const Shape &shape, const int rx, const int ry, const int m) const
  {
    const int ox = rx - m;
    const int oy = ry - m;
    for (int r = 0; r < shape.dilated.height; r++) {
      const int gy = oy + r;
      if (gy < 0 || gy >= grid.height) {
        continue;
      }
      const uint64_t *mask = shape.dilated.row(r);
      const uint64_t *row = grid.row(gy);
      for (int i = 0; i < shape.dilated.words; i++) {
        if (mask[i] && (mask[i] & row_window(row, grid.words, ox + i * 64))) {
          return true;
        }
      }
    }
    return false;
  }

  void place(const Shape &shape, const int rx, const int ry)
  {
    for (int r = 0; r < shape.raw.height; r++) {
      const int gy = ry + r;
      if (gy < 0 || gy >= grid.height) {
        continue;
      }
      const uint64_t *mask = shape.raw.row(r);
      for (int i = 0; i < shape.raw.words; i++) {
        row_or(grid.row(gy), grid.words, rx + i * 64, mask[i]);
      }
    }
    update_rows(ry, ry + shape.raw.height - 1);
  }
};

struct Region {
  int x0, x1, y0, y1;
};

struct Spot {
  int rx = 0;
  int ry = 0;
  bool found = false;
};

/**
 * First fitting position (lowest row, then lowest column) of `shape` with its raw origin
 * inside `region`.
 */
static Spot find_spot(const Occupancy &occ, const Shape &shape, const Region &region, const int m)
{
  Spot spot;
  const Raster &grid = occ.grid;
  const int k = shape.key_row;
  const int key_length = shape.run_length[k];
  const int key_start = shape.run_start[k];

  for (int ry = region.y0; ry <= region.y1; ry++) {
    const int oy = ry - m;
    /* Every row of the mask needs a free run at least as long as its longest run
     * (minus what may hang outside the grid). */
    bool possible = true;
    for (int r = 0; r < shape.dilated.height; r++) {
      const int gy = oy + r;
      if (gy < 0 || gy >= grid.height) {
        continue;
      }
      if (shape.run_length[r] - 2 * m > occ.max_free[gy]) {
        possible = false;
        break;
      }
    }
    if (!possible) {
      continue;
    }

    const int gy_key = oy + k;
    auto try_range = [&](const int lo, const int hi) {
      for (int rx = std::max(lo, region.x0); rx <= std::min(hi, region.x1); rx++) {
        if (!occ.collides(shape, rx, ry, m)) {
          spot = {rx, ry, true};
          return true;
        }
      }
      return false;
    };

    if (key_length == 0 || gy_key < 0 || gy_key >= grid.height) {
      if (try_range(region.x0, region.x1)) {
        return spot;
      }
      continue;
    }

    /* Only columns where the key row of the mask lands in a free run can fit.
     * Columns outside the grid count as free. */
    const uint64_t *row = grid.row(gy_key);
    const int far = 1 << 28;
    int x = 0;
    while (x < grid.width) {
      if ((row[x >> 6] >> (x & 63)) & 1) {
        x++;
        continue;
      }
      const int run_begin = x;
      while (x < grid.width && !((row[x >> 6] >> (x & 63)) & 1)) {
        x++;
      }
      const int s = run_begin == 0 ? -far : run_begin;
      const int e = x == grid.width ? far : x;
      /* `ox + key_start >= s` and `ox + key_start + key_length <= e`, with `ox = rx - m`. */
      const int lo = s == -far ? region.x0 : s - key_start + m;
      const int hi = e == far ? region.x1 : e - key_length - key_start + m;
      if (lo <= hi && try_range(lo, hi)) {
        return spot;
      }
    }
  }
  return spot;
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Packing
 * \{ */

float texel_density(const Span<float2> triangles, const float area_3d)
{
  double area = 0.0;
  for (int64_t i = 0; i + 2 < triangles.size(); i += 3) {
    const float2 a = triangles[i + 1] - triangles[i];
    const float2 b = triangles[i + 2] - triangles[i];
    area += std::abs(double(a.x) * b.y - double(a.y) * b.x) * 0.5;
  }
  if (area_3d <= 1e-12f || area <= 1e-12) {
    return 0.0f;
  }
  return float(std::sqrt(area / area_3d));
}

static float pixel_area(const Span<float2> triangles)
{
  double area = 0.0;
  for (int64_t i = 0; i + 2 < triangles.size(); i += 3) {
    const float2 a = triangles[i + 1] - triangles[i];
    const float2 b = triangles[i + 2] - triangles[i];
    area += std::abs(double(a.x) * b.y - double(a.y) * b.x) * 0.5;
  }
  return float(area);
}

static float median(Vector<float> values)
{
  if (values.is_empty()) {
    return 0.0f;
  }
  std::sort(values.begin(), values.end());
  return values[values.size() / 2];
}

/** Per island data derived once from the input. */
struct Prepared {
  int index;
  float area_px;
  /** Scale multiplier: island scale is `global * factor`, or 1 for fixed scale islands. */
  float factor;
  bool fixed_scale;
  /** Density of the input UVs in pixels per meter (0 if degenerate). */
  float density;
  float pre_angle;
  int angle_count;
  bool allow_flip;
  /** Tiles this island may use. */
  int tile_first;
  int tile_last;
  int group;
  int group_rank;
};

/**
 * Order islands for placement: by group (largest group first) when groups are laid out
 * separately, then by estimated area at `global`, largest first.
 */
static void sort_for_placement(Vector<Prepared> &order,
                               const float global,
                               const GroupLayout layout)
{
  auto area_at = [&](const Prepared &p) {
    return p.fixed_scale ? p.area_px : p.area_px * p.factor * p.factor * global * global;
  };
  Map<int, float> group_area;
  for (const Prepared &p : order) {
    group_area.lookup_or_add(p.group, 0.0f) += area_at(p);
  }
  const bool by_group = layout != GroupLayout::Mixed;
  std::stable_sort(order.begin(), order.end(), [&](const Prepared &a, const Prepared &b) {
    if (by_group && a.group != b.group) {
      const float ga = group_area.lookup(a.group);
      const float gb = group_area.lookup(b.group);
      if (ga != gb) {
        return ga > gb;
      }
      return a.group < b.group;
    }
    const float aa = area_at(a);
    const float ab = area_at(b);
    if (aa != ab) {
      return aa > ab;
    }
    return a.index < b.index;
  });
}

struct Placement {
  bool placed = false;
  int tile = 0;
  float scale = 1.0f;
  float2x2 matrix;
  float2 translation;
};

struct Layout {
  bool complete = false;
  Vector<Placement> placements;
  int placed = 0;
};

struct Packer {
  MutableSpan<Island> islands;
  const Params &params;
  int cell;
  int grid_w;
  int grid_h;
  int margin_cells;
  int edge_cells;
  int tile_count;
  /** Occupancy of locked islands per tile, copied by every attempt. */
  Vector<Occupancy> locked_occupancy;
  Vector<Prepared> order_base;

  bool cancelled() const
  {
    return params.stop && *params.stop;
  }

  /** Try `scale` then the shrunken scales allowed by the tolerance. */
  Vector<float> scale_steps(const float scale, const bool fixed) const
  {
    Vector<float> steps = {scale};
    if (!fixed && params.density_tolerance > 0.0f) {
      const float tol = std::min(params.density_tolerance, 0.9f);
      steps.append(scale * (1.0f - tol * 0.5f));
      steps.append(scale * (1.0f - tol));
    }
    return steps;
  }

  Layout attempt(const float global, const bool partial) const
  {
    Layout layout;
    layout.placements.resize(islands.size());
    Vector<Occupancy> tiles = locked_occupancy;
    /* Bounding region of each group per tile, for clustering. */
    Map<std::pair<int, int>, Region> group_bounds;

    for (const Prepared &prep : order_base) {
      if (cancelled()) {
        return layout;
      }
      const Island &island = islands[prep.index];
      const float base_scale = prep.fixed_scale ? 1.0f : global * prep.factor;
      bool done = false;

      for (const float scale : scale_steps(base_scale, prep.fixed_scale)) {
        /* Build every orientation of this island. */
        const int flips = prep.allow_flip ? 2 : 1;
        const int count = prep.angle_count * flips;
        Array<Shape> shapes(count);
        threading::parallel_for(IndexRange(count), 1, [&](const IndexRange range) {
          for (const int o : range) {
            const float angle = prep.pre_angle + float(o % prep.angle_count) * float(M_PI_2);
            const float2x2 matrix = orientation_matrix(angle, o >= prep.angle_count, scale);
            shapes[o] = build_shape(
                island.triangles, matrix, cell, margin_cells, params.pixel_snap);
          }
        });

        for (int tile = prep.tile_first; tile <= prep.tile_last && !done; tile++) {
          Occupancy &occ = tiles[tile];
          auto full_region = [&](const Shape &shape) {
            return Region{edge_cells,
                          grid_w - edge_cells - shape.raw.width,
                          edge_cells,
                          grid_h - edge_cells - shape.raw.height};
          };

          /* Windows around the group's current bounds, growing until they cover the tile. */
          Vector<int> pads;
          const Region *bounds = nullptr;
          if (params.group_layout == GroupLayout::Cluster && prep.group != 0) {
            bounds = group_bounds.lookup_ptr({tile, prep.group});
          }
          if (bounds) {
            int pad = std::max(8, margin_cells * 2);
            while (pad < std::max(grid_w, grid_h)) {
              pads.append(pad);
              pad *= 2;
            }
          }
          pads.append(-1);

          for (const int pad : pads) {
            Array<Spot> spots(count);
            threading::parallel_for(IndexRange(count), 1, [&](const IndexRange range) {
              for (const int o : range) {
                Region region = full_region(shapes[o]);
                if (pad >= 0) {
                  region.x0 = std::max(region.x0, bounds->x0 - pad - shapes[o].raw.width);
                  region.x1 = std::min(region.x1, bounds->x1 + pad);
                  region.y0 = std::max(region.y0, bounds->y0 - pad - shapes[o].raw.height);
                  region.y1 = std::min(region.y1, bounds->y1 + pad);
                }
                if (region.x0 > region.x1 || region.y0 > region.y1) {
                  continue;
                }
                spots[o] = find_spot(occ, shapes[o], region, margin_cells);
              }
            });

            int best = -1;
            for (const int o : IndexRange(count)) {
              if (!spots[o].found) {
                continue;
              }
              if (best == -1) {
                best = o;
                continue;
              }
              const int top = spots[o].ry + shapes[o].raw.height;
              const int best_top = spots[best].ry + shapes[best].raw.height;
              if (top < best_top || (top == best_top && spots[o].rx < spots[best].rx)) {
                best = o;
              }
            }
            if (best == -1) {
              continue;
            }

            const Shape &shape = shapes[best];
            const Spot &spot = spots[best];
            occ.place(shape, spot.rx, spot.ry);
            Placement &placement = layout.placements[prep.index];
            placement.placed = true;
            placement.tile = tile;
            placement.scale = scale;
            placement.matrix = shape.matrix;
            placement.translation = float2(float(spot.rx * cell), float(spot.ry * cell)) -
                                    shape.origin;
            if (prep.group != 0) {
              const Region placed = {spot.rx,
                                     spot.rx + shape.raw.width,
                                     spot.ry,
                                     spot.ry + shape.raw.height};
              Region &region = group_bounds.lookup_or_add({tile, prep.group}, placed);
              region.x0 = std::min(region.x0, placed.x0);
              region.x1 = std::max(region.x1, placed.x1);
              region.y0 = std::min(region.y0, placed.y0);
              region.y1 = std::max(region.y1, placed.y1);
            }
            layout.placed++;
            done = true;
            break;
          }
        }
        if (done) {
          break;
        }
      }
      if (!done && !partial) {
        return layout;
      }
    }
    layout.complete = layout.placed == order_base.size();
    return layout;
  }
};

static float best_angle(const Span<float2> triangles)
{
  if (triangles.size() < 3) {
    return 0.0f;
  }
  const float angle = BLI_convexhull_aabb_fit_points_2d(triangles);
  /* Choose the sign giving the smaller bounding box, the convention isn't important here. */
  auto box_area = [&](const float a) {
    const float2x2 m = orientation_matrix(a, false, 1.0f);
    float2 min(FLT_MAX), max(-FLT_MAX);
    for (const float2 &p : triangles) {
      const float2 q = m * p;
      min = math::min(min, q);
      max = math::max(max, q);
    }
    return (max.x - min.x) * (max.y - min.y);
  };
  return box_area(angle) <= box_area(-angle) ? angle : -angle;
}

Result pack(MutableSpan<Island> islands, const Params &params)
{
  Result result;
  Packer packer{islands, params};
  packer.cell = std::max(1, params.cell_size);
  packer.grid_w = std::max(1, params.width / packer.cell);
  packer.grid_h = std::max(1, params.height / packer.cell);
  packer.margin_cells = std::max(0, int(std::ceil(params.margin / packer.cell - 1e-4f)));
  packer.edge_cells = std::max(0, int(std::ceil(params.edge_padding / packer.cell - 1e-4f)));

  /* Groups, ordered by id. */
  Vector<int> group_ids;
  for (const Island &island : islands) {
    if (!island.locked && !island.triangles.is_empty() && !group_ids.contains(island.group)) {
      group_ids.append(island.group);
    }
  }
  std::sort(group_ids.begin(), group_ids.end());

  packer.tile_count = std::max(1, params.max_tiles);
  if (params.group_layout == GroupLayout::TilePerGroup) {
    if (group_ids.size() > packer.tile_count) {
      result.too_many_groups = true;
      return result;
    }
    packer.tile_count = std::max<int>(1, group_ids.size());
  }

  /* Locked islands become obstacles. */
  packer.locked_occupancy.resize(packer.tile_count);
  for (Occupancy &occ : packer.locked_occupancy) {
    occ.init(packer.grid_w, packer.grid_h);
  }
  Vector<float> locked_densities;
  for (const Island &island : islands) {
    if (!island.locked || island.locked_tile < 0 || island.locked_tile >= packer.tile_count) {
      continue;
    }
    Occupancy &occ = packer.locked_occupancy[island.locked_tile];
    for (int64_t i = 0; i + 2 < island.triangles.size(); i += 3) {
      const float2 tri[3] = {island.triangles[i] / float(packer.cell),
                             island.triangles[i + 1] / float(packer.cell),
                             island.triangles[i + 2] / float(packer.cell)};
      rasterize_triangle(occ.grid, tri);
    }
    const float density = texel_density(island.triangles, island.area_3d);
    if (density > 0.0f) {
      locked_densities.append(density / std::max(island.density_weight, 1e-6f));
    }
  }
  for (Occupancy &occ : packer.locked_occupancy) {
    occ.update_rows(0, occ.grid.height - 1);
  }

  /* Prepare movable islands. */
  Vector<float> densities;
  for (const int64_t i : islands.index_range()) {
    const Island &island = islands[i];
    if (island.locked || island.triangles.size() < 3) {
      continue;
    }
    const float density = texel_density(island.triangles, island.area_3d);
    if (density > 0.0f && !island.keep_scale) {
      densities.append(density / std::max(island.density_weight, 1e-6f));
    }
  }
  const float median_density = densities.is_empty() ? 1.0f : median(densities);

  float total_factor_area = 0.0f;
  float total_fixed_area = 0.0f;
  for (const int64_t i : islands.index_range()) {
    Island &island = islands[i];
    island.placed = false;
    if (island.locked || island.triangles.size() < 3) {
      continue;
    }
    Prepared prep;
    prep.index = int(i);
    prep.area_px = pixel_area(island.triangles);
    prep.fixed_scale = island.keep_scale;
    prep.density = texel_density(island.triangles, island.area_3d);
    const float weight = std::max(island.density_weight, 1e-6f);
    if (params.density_mode == DensityMode::Relative) {
      prep.factor = weight;
    }
    else {
      /* Island scale `global * factor` gives a density of `global * weight`. */
      prep.factor = weight / (prep.density > 0.0f ? prep.density : median_density);
    }
    prep.pre_angle = island.rotation == Rotation::Free ? best_angle(island.triangles) : 0.0f;
    prep.angle_count = island.rotation == Rotation::None ? 1 : 4;
    prep.allow_flip = island.allow_flip;
    prep.group = island.group;
    prep.group_rank = int(group_ids.first_index_of_try(island.group));
    if (params.group_layout == GroupLayout::TilePerGroup) {
      prep.tile_first = prep.tile_last = prep.group_rank;
    }
    else {
      prep.tile_first = 0;
      prep.tile_last = packer.tile_count - 1;
    }
    if (prep.fixed_scale) {
      total_fixed_area += prep.area_px;
    }
    else {
      total_factor_area += prep.area_px * prep.factor * prep.factor;
    }
    packer.order_base.append(prep);
  }

  if (packer.order_base.is_empty()) {
    return result;
  }

  Layout best;
  const bool fill = params.density_mode != DensityMode::Target &&
                    !(params.density_mode == DensityMode::Uniform &&
                      params.match_locked_density && !locked_densities.is_empty()) &&
                    total_factor_area > 0.0f;

  if (!fill) {
    float global = 1.0f;
    if (params.density_mode == DensityMode::Target) {
      global = params.target_density;
    }
    else if (params.density_mode == DensityMode::Uniform && !locked_densities.is_empty()) {
      global = median(locked_densities);
    }
    else if (params.density_mode == DensityMode::Uniform) {
      global = median_density;
    }
    sort_for_placement(packer.order_base, global, params.group_layout);
    best = packer.attempt(global, true);
  }
  else {
    /* Bracket the largest global scale that packs everything, evaluating several candidates in
     * parallel per round. The candidates are deterministic, so the result is too. */
    const float capacity = float(params.width) * float(params.height) * packer.tile_count;
    const float usable = std::max(capacity * 0.6f - total_fixed_area, capacity * 0.05f);
    const float guess = std::sqrt(usable / total_factor_area);
    float lo = 0.0f;
    float hi = 0.0f;
    const int max_rounds = 16;
    for (int round = 0; round < max_rounds; round++) {
      if (packer.cancelled()) {
        break;
      }
      Vector<float> candidates;
      if (lo == 0.0f && hi == 0.0f) {
        candidates = {guess * 0.6f, guess * 0.8f, guess, guess * 1.25f};
      }
      else if (lo == 0.0f) {
        candidates = {hi * 0.1f, hi * 0.25f, hi * 0.5f, hi * 0.75f};
      }
      else if (hi == 0.0f) {
        candidates = {lo * 1.15f, lo * 1.35f, lo * 1.7f, lo * 2.5f};
      }
      else {
        if (hi / lo < 1.005f) {
          break;
        }
        for (int k = 1; k <= 4; k++) {
          candidates.append(lo * std::pow(hi / lo, float(k) / 5.0f));
        }
      }

      Array<Layout> layouts(candidates.size());
      threading::parallel_for(candidates.index_range(), 1, [&](const IndexRange range) {
        for (const int64_t c : range) {
          /* Each candidate needs its own ordering. */
          Packer local = packer;
          sort_for_placement(local.order_base, candidates[c], params.group_layout);
          layouts[c] = local.attempt(candidates[c], false);
        }
      });

      for (const int64_t c : candidates.index_range()) {
        if (layouts[c].complete && candidates[c] > lo) {
          lo = candidates[c];
          best = std::move(layouts[c]);
        }
      }
      float new_hi = 0.0f;
      for (const int64_t c : candidates.index_range()) {
        if (!layouts[c].complete && candidates[c] > lo) {
          new_hi = new_hi == 0.0f ? candidates[c] : std::min(new_hi, candidates[c]);
        }
      }
      if (hi > lo) {
        new_hi = new_hi == 0.0f ? hi : std::min(new_hi, hi);
      }
      hi = new_hi;

      if (lo == 0.0f && hi > 0.0f && hi < guess * 1e-6f) {
        break; /* Even tiny islands don't fit, locked or fixed scale islands take the space. */
      }
      if (params.progress) {
        *params.progress = 0.05f + 0.9f * float(round + 1) / float(max_rounds);
      }
    }

    if (lo == 0.0f && !packer.cancelled()) {
      /* Nothing packs completely: place what fits at the smallest tried scale. */
      const float global = hi > 0.0f ? hi : guess;
      sort_for_placement(packer.order_base, global, params.group_layout);
      best = packer.attempt(global, true);
      }
  }

  if (packer.cancelled()) {
    result.cancelled = true;
    return result;
  }

  /* Write results and statistics. */
  double packed_area = 0.0;
  Vector<float> final_densities;
  for (const Prepared &prep : packer.order_base) {
    if (best.placements.is_empty() || !best.placements[prep.index].placed) {
      result.unplaced_count++;
      continue;
    }
    const Placement &placement = best.placements[prep.index];
    Island &island = islands[prep.index];
    island.placed = true;
    island.tile = placement.tile;
    island.matrix = placement.matrix;
    island.translation = placement.translation;
    result.placed_count++;
    result.tiles_used = std::max(result.tiles_used, placement.tile + 1);
    packed_area += double(prep.area_px) * placement.scale * placement.scale;
    if (prep.density > 0.0f) {
      final_densities.append(prep.density * placement.scale /
                             std::max(island.density_weight, 1e-6f));
    }
  }
  for (const Island &island : islands) {
    if (island.locked && island.locked_tile >= 0 && island.locked_tile < result.tiles_used) {
      packed_area += pixel_area(island.triangles);
    }
  }
  if (result.tiles_used > 0) {
    result.utilization = float(packed_area /
                               (double(params.width) * params.height * result.tiles_used));
  }
  const float density = median(final_densities);
  result.density = density;
  for (const float d : final_densities) {
    if (density > 0.0f) {
      result.density_deviation = std::max(result.density_deviation,
                                          std::abs(d / density - 1.0f));
    }
  }
  return result;
}

/** \} */

}  // namespace blender::geometry::texel_pack
