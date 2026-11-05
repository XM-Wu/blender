/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup eduv
 *
 * Texel Pack: packs UV islands on a texel grid with per-island constraints stored as face
 * attributes, automatic stacking of identical islands, pixel margins, texel density control,
 * groups and multiple UDIM tiles. See `doc/uv_pack_design.md`.
 *
 * The native #UV_OT_pack_islands is unaffected.
 */

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <memory>
#include <string>

#include "MEM_guardedalloc.h"

#include "DNA_image_types.h"
#include "DNA_mesh_types.h"
#include "DNA_object_types.h"
#include "DNA_scene_types.h"
#include "DNA_screen_types.h"
#include "DNA_space_types.h"

#include "BLI_listbase.hh"
#include "BLI_map.hh"
#include "BLI_set.hh"
#include "BLI_math_matrix_c.hh"
#include "BLI_math_matrix.hh"
#include "BLI_math_vector.hh"
#include "BLI_math_vector_c.hh"
#include "BLI_string.hh"
#include "BLI_vector.hh"

#include "BLT_translation.hh"

#include "BKE_context.hh"
#include "BKE_customdata.hh"
#include "BKE_editmesh.hh"
#include "BKE_global.hh"
#include "BKE_idprop.hh"
#include "BKE_layer.hh"
#include "BKE_mesh_types.hh"
#include "BKE_report.hh"
#include "BKE_screen.hh"

#include "DEG_depsgraph.hh"

#include "GEO_uv_texel_pack.hh"

#include "ED_mesh.hh"
#include "ED_screen.hh"
#include "ED_select_utils.hh"
#include "ED_undo.hh"
#include "ED_uvedit.hh"

#include "RNA_access.hh"
#include "RNA_define.hh"

#include "WM_api.hh"
#include "WM_types.hh"

#include "bmesh.hh"

#include "uvedit_intern.hh"

namespace blender {

namespace texel_pack = geometry::texel_pack;

/* -------------------------------------------------------------------- */
/** \name Island Constraint Attributes
 *
 * Integer face attributes, zero (or missing) means "default".
 * \{ */

static constexpr const char *ATTR_ROTATION = "uvpack_rotation";
static constexpr const char *ATTR_FLIP = "uvpack_flip";
static constexpr const char *ATTR_SCALE = "uvpack_scale";
static constexpr const char *ATTR_DENSITY = "uvpack_density";
static constexpr const char *ATTR_GROUP = "uvpack_group";
static constexpr const char *ATTR_STACK = "uvpack_stack";
static constexpr const char *ATTR_LOCK = "uvpack_lock";

/** Values of #ATTR_ROTATION. */
enum {
  ISLAND_ROTATION_INHERIT = 0,
  ISLAND_ROTATION_NONE = 1,
  ISLAND_ROTATION_CARDINAL = 2,
  ISLAND_ROTATION_FREE = 3,
};

/** Values of #ATTR_FLIP. */
enum {
  ISLAND_FLIP_INHERIT = 0,
  ISLAND_FLIP_FORBID = 1,
  ISLAND_FLIP_ALLOW = 2,
};

enum ConstraintKind {
  CONSTRAINT_ROTATION = 0,
  CONSTRAINT_FLIP,
  CONSTRAINT_SCALE,
  CONSTRAINT_DENSITY,
  CONSTRAINT_GROUP,
  CONSTRAINT_STACK,
  CONSTRAINT_LOCK,
  CONSTRAINT_ALL,
};

static const char *constraint_attribute_name(const int kind)
{
  switch (kind) {
    case CONSTRAINT_ROTATION:
      return ATTR_ROTATION;
    case CONSTRAINT_FLIP:
      return ATTR_FLIP;
    case CONSTRAINT_SCALE:
      return ATTR_SCALE;
    case CONSTRAINT_DENSITY:
      return ATTR_DENSITY;
    case CONSTRAINT_GROUP:
      return ATTR_GROUP;
    case CONSTRAINT_STACK:
      return ATTR_STACK;
    case CONSTRAINT_LOCK:
      return ATTR_LOCK;
  }
  return nullptr;
}

/** Constraints of one island, resolved from its faces. */
struct IslandConstraints {
  int rotation = ISLAND_ROTATION_INHERIT;
  int flip = ISLAND_FLIP_INHERIT;
  bool keep_scale = false;
  /** Density weight, 1 when unset. */
  float density = 1.0f;
  int group = 0;
  int stack = 0;
  bool lock = false;
  /** Faces of the island disagree on some value. */
  bool mixed = false;
};

/**
 * Integer attribute value of an island: the value covering the largest surface area,
 * ties going to the smaller value.
 */
static int island_int_value(const FaceIsland &island,
                            const int cd_offset,
                            const Span<float> face_areas,
                            bool *r_mixed)
{
  if (cd_offset == -1) {
    return 0;
  }
  Map<int, float> weights;
  for (int i = 0; i < island.faces_len; i++) {
    weights.lookup_or_add(BM_ELEM_CD_GET_INT(island.faces[i], cd_offset), 0.0f) += face_areas[i];
  }
  if (weights.size() > 1) {
    *r_mixed = true;
  }
  int best_value = 0;
  float best_weight = -1.0f;
  for (const auto item : weights.items()) {
    if (item.value > best_weight || (item.value == best_weight && item.key < best_value)) {
      best_value = item.key;
      best_weight = item.value;
    }
  }
  return best_value;
}

static IslandConstraints island_constraints(const BMesh *bm,
                                            const FaceIsland &island,
                                            const Span<float> face_areas)
{
  IslandConstraints c;
  auto offset = [&](const char *name, const eCustomDataType type) {
    return CustomData_get_offset_named(&bm->pdata, type, name);
  };
  c.rotation = island_int_value(
      island, offset(ATTR_ROTATION, CD_PROP_INT32), face_areas, &c.mixed);
  c.flip = island_int_value(island, offset(ATTR_FLIP, CD_PROP_INT32), face_areas, &c.mixed);
  c.keep_scale = island_int_value(
                     island, offset(ATTR_SCALE, CD_PROP_INT32), face_areas, &c.mixed) != 0;
  c.group = island_int_value(island, offset(ATTR_GROUP, CD_PROP_INT32), face_areas, &c.mixed);
  c.stack = island_int_value(island, offset(ATTR_STACK, CD_PROP_INT32), face_areas, &c.mixed);
  c.lock = island_int_value(island, offset(ATTR_LOCK, CD_PROP_INT32), face_areas, &c.mixed) != 0;

  const int cd_density = offset(ATTR_DENSITY, CD_PROP_FLOAT);
  if (cd_density != -1) {
    double sum = 0.0;
    double weight = 0.0;
    for (int i = 0; i < island.faces_len; i++) {
      float value = BM_ELEM_CD_GET_FLOAT(island.faces[i], cd_density);
      if (value <= 0.0f) {
        value = 1.0f; /* Zero is unset. */
      }
      sum += double(value) * face_areas[i];
      weight += face_areas[i];
    }
    if (weight > 0.0) {
      c.density = float(sum / weight);
    }
  }
  return c;
}

Vector<float> ED_uvedit_texel_pack_info(const Scene *scene, Mesh *mesh)
{
  Vector<float> info;
  BMEditMesh *em = mesh->runtime->edit_mesh.get();
  if (!em || !em->bm) {
    return info;
  }
  BMesh *bm = em->bm;
  const BMUVOffsets offsets = BM_uv_map_offsets_get(bm);
  auto record = [&](const Span<float> values) {
    const int64_t start = info.size();
    info.append_n_times(0.0f, ED_UVEDIT_TEXEL_PACK_INFO_STRIDE);
    for (const int64_t i : values.index_range()) {
      info[start + i] = values[i];
    }
  };

  int offsets_by_kind[CONSTRAINT_ALL];
  for (int kind = 0; kind < CONSTRAINT_ALL; kind++) {
    offsets_by_kind[kind] = CustomData_get_offset_named(
        &bm->pdata,
        kind == CONSTRAINT_DENSITY ? CD_PROP_FLOAT : CD_PROP_INT32,
        constraint_attribute_name(kind));
  }

  struct IdEntry {
    int faces = 0;
    bool selected = false;
    float2 min = float2(FLT_MAX);
    float2 max = float2(-FLT_MAX);
  };
  Map<std::pair<int, float>, int> counts;
  Map<std::pair<int, int>, IdEntry> ids;
  int max_id[2] = {0, 0};
  int selected_faces = 0;

  BMFace *f;
  BMIter iter;
  BM_ITER_MESH (f, &iter, bm, BM_FACES_OF_MESH) {
    for (const int kind : {CONSTRAINT_GROUP, CONSTRAINT_STACK}) {
      if (offsets_by_kind[kind] != -1) {
        max_id[kind - CONSTRAINT_GROUP] = std::max(max_id[kind - CONSTRAINT_GROUP],
                                                   BM_ELEM_CD_GET_INT(f, offsets_by_kind[kind]));
      }
    }
    if (offsets.uv == -1 || !uvedit_face_visible_test(scene, f)) {
      continue;
    }
    const bool selected = uvedit_face_select_test(scene, bm, f);
    if (selected) {
      selected_faces++;
      for (int kind = 0; kind < CONSTRAINT_ALL; kind++) {
        float value = 0.0f;
        if (offsets_by_kind[kind] != -1) {
          value = kind == CONSTRAINT_DENSITY ? BM_ELEM_CD_GET_FLOAT(f, offsets_by_kind[kind]) :
                                               float(BM_ELEM_CD_GET_INT(f, offsets_by_kind[kind]));
        }
        if (kind == CONSTRAINT_DENSITY && value <= 0.0f) {
          value = 1.0f; /* Zero is unset. */
        }
        counts.lookup_or_add({kind, value}, 0)++;
      }
    }
    for (const int kind : {CONSTRAINT_GROUP, CONSTRAINT_STACK}) {
      if (offsets_by_kind[kind] == -1) {
        continue;
      }
      const int id = BM_ELEM_CD_GET_INT(f, offsets_by_kind[kind]);
      if (id <= 0) {
        continue;
      }
      IdEntry &entry = ids.lookup_or_add_default({kind, id});
      entry.faces++;
      entry.selected |= selected;
      BMLoop *l;
      BMIter liter;
      BM_ITER_ELEM (l, &liter, f, BM_LOOPS_OF_FACE) {
        const float2 uv(BM_ELEM_CD_GET_FLOAT_P(l, offsets.uv));
        entry.min = math::min(entry.min, uv);
        entry.max = math::max(entry.max, uv);
      }
    }
  }

  for (const auto item : counts.items()) {
    record({0.0f, float(item.key.first), item.key.second, float(item.value)});
  }
  for (const auto item : ids.items()) {
    const IdEntry &e = item.value;
    record({1.0f,
            float(item.key.first),
            float(item.key.second),
            float(e.faces),
            e.selected ? 1.0f : 0.0f,
            e.min.x,
            e.min.y,
            e.max.x,
            e.max.y});
  }
  record({2.0f, float(CONSTRAINT_GROUP), float(max_id[0])});
  record({2.0f, float(CONSTRAINT_STACK), float(max_id[1])});
  record({3.0f, float(selected_faces)});
  return info;
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Island Gathering
 * \{ */

enum IslandRole {
  ROLE_IGNORE = 0,
  ROLE_PACK,
  ROLE_OBSTACLE,
};

struct TexelIsland {
  Object *ob = nullptr;
  BMesh *bm = nullptr;
  FaceIsland *face_island = nullptr;
  bool selected = false;
  IslandRole role = ROLE_IGNORE;
  IslandConstraints constraints;
  float area_3d = 0.0f;
  Vector<float> face_areas;
  /** Distinct UV coordinates and the index of each loop into them (faces, then loops). */
  Vector<float2> unique_uvs;
  Vector<int> loop_unique;
  float2 uv_center = float2(0.0f);

  /* Stacking. */
  /** Island whose layout this one copies, -1 for none. */
  int stack_source = -1;
  /** For copies: index into the source's `unique_uvs` for every unique UV. */
  Vector<int> stack_map;
  /** Follows the transform of `stack_source` without being an exact copy. */
  bool stack_union = false;

  int engine_index = -1;
};

/**
 * \param use_mesh_selection: Islands of all visible faces are gathered, selected by their mesh
 * selection, as seen in the 3D viewport. Otherwise the UV editor's visibility and UV selection
 * are used.
 */
static void gather_islands(const Scene *scene,
                           const Span<Object *> objects,
                           Vector<TexelIsland> &r_islands,
                           const bool use_mesh_selection = false)
{
  const ToolSettings *ts = scene->toolsettings;
  const bool sync = (ts->uv_flag & UV_FLAG_SELECT_SYNC) != 0;
  for (Object *ob : objects) {
    BMesh *bm = BKE_editmesh_bmesh_get_for_write(ob);
    const BMUVOffsets offsets = BM_uv_map_offsets_get(bm);
    if (offsets.uv == -1) {
      continue;
    }
    float ob_m3[3][3];
    copy_m3_m4(ob_m3, ob->object_to_world().ptr());

    /* Without sync selection, only faces selected in the mesh are visible in the UV editor. */
    ListBaseT<FaceIsland> island_list = {};
    bm_mesh_calc_uv_islands(scene,
                            bm,
                            &island_list,
                            !sync && !use_mesh_selection,
                            false,
                            false,
                            1.0f,
                            offsets);

    for (FaceIsland &face_island_ref : island_list.items_mutable()) {
      BLI_remlink(&island_list, &face_island_ref);
      FaceIsland *face_island = &face_island_ref;
      TexelIsland island;
      island.ob = ob;
      island.bm = bm;
      island.face_island = face_island;
      island.face_areas.reserve(face_island->faces_len);

      Map<float2, int> unique_index;
      float2 uv_min(FLT_MAX), uv_max(-FLT_MAX);
      for (int i = 0; i < face_island->faces_len; i++) {
        BMFace *f = face_island->faces[i];
        if (use_mesh_selection ? BM_elem_flag_test(f, BM_ELEM_SELECT) :
                                 uvedit_face_select_test(scene, bm, f))
        {
          island.selected = true;
        }
        const float area = BM_face_calc_area_with_mat3(f, ob_m3);
        island.face_areas.append(area);
        island.area_3d += area;
        BMLoop *l;
        BMIter liter;
        BM_ITER_ELEM (l, &liter, f, BM_LOOPS_OF_FACE) {
          const float2 uv(BM_ELEM_CD_GET_FLOAT_P(l, offsets.uv));
          const int index = unique_index.lookup_or_add_cb(uv, [&]() {
            island.unique_uvs.append(uv);
            return int(island.unique_uvs.size() - 1);
          });
          island.loop_unique.append(index);
          uv_min = math::min(uv_min, uv);
          uv_max = math::max(uv_max, uv);
        }
      }
      island.uv_center = (uv_min + uv_max) * 0.5f;
      island.constraints = island_constraints(bm, *face_island, island.face_areas);
      r_islands.append(std::move(island));
    }
  }
}

static void free_islands(Vector<TexelIsland> &islands)
{
  for (TexelIsland &island : islands) {
    MEM_SAFE_DELETE(island.face_island->faces);
    MEM_SAFE_DELETE(island.face_island);
  }
  islands.clear();
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Stacking
 * \{ */

/** Island points centered and scaled to unit area, in pixel space. */
struct NormalizedShape {
  Vector<float2> points;
  float far_length = 0.0f;
  float far_angle = 0.0f;
  /** Points hashed on a grid with cells of `tolerance`. */
  Map<std::pair<int, int>, Vector<int>> grid;
  float cell = 1.0f;
  /** Point indices of every face in loop order. */
  Vector<Vector<int>> faces;
  /** Faces as keys independent of the first loop, see #face_key. */
  Set<std::string> face_keys;
};

/** Key of a face loop that doesn't depend on which loop comes first. */
static std::string face_key(Span<int> loop)
{
  int64_t first = 0;
  for (const int64_t i : loop.index_range()) {
    if (loop[i] < loop[first]) {
      first = i;
    }
  }
  std::string key;
  for (const int64_t i : loop.index_range()) {
    key += std::to_string(loop[(first + i) % loop.size()]);
    key += ',';
  }
  return key;
}

static float2 to_pixels(const float2 &uv, const int2 &resolution)
{
  return float2(uv.x * resolution.x, uv.y * resolution.y);
}

static NormalizedShape normalize_shape(const TexelIsland &island,
                                       const int2 &resolution,
                                       const float tolerance)
{
  NormalizedShape shape;
  float2 center(0.0f);
  for (const float2 &uv : island.unique_uvs) {
    center += to_pixels(uv, resolution);
  }
  center /= float(island.unique_uvs.size());

  /* Pixel area of the island polygons. */
  double area = 0.0;
  {
    const FaceIsland &fi = *island.face_island;
    int loop = 0;
    for (int i = 0; i < fi.faces_len; i++) {
      const int len = fi.faces[i]->len;
      const float2 a = to_pixels(island.unique_uvs[island.loop_unique[loop]], resolution);
      for (int j = 1; j + 1 < len; j++) {
        const float2 b = to_pixels(island.unique_uvs[island.loop_unique[loop + j]], resolution);
        const float2 c = to_pixels(island.unique_uvs[island.loop_unique[loop + j + 1]],
                                   resolution);
        area += std::abs(double(b.x - a.x) * (c.y - a.y) - double(b.y - a.y) * (c.x - a.x)) *
                0.5;
      }
      loop += len;
    }
  }
  const float scale = area > 1e-12 ? float(1.0 / std::sqrt(area)) : 1.0f;

  {
    const FaceIsland &fi = *island.face_island;
    int loop = 0;
    for (int i = 0; i < fi.faces_len; i++) {
      const int len = fi.faces[i]->len;
      Vector<int> face(island.loop_unique.as_span().slice(loop, len));
      shape.face_keys.add(face_key(face));
      shape.faces.append(std::move(face));
      loop += len;
    }
  }

  shape.cell = std::max(tolerance, 1e-4f);
  for (const int64_t i : island.unique_uvs.index_range()) {
    const float2 p = (to_pixels(island.unique_uvs[i], resolution) - center) * scale;
    shape.points.append(p);
    const float length = math::length(p);
    if (length > shape.far_length) {
      shape.far_length = length;
      shape.far_angle = std::atan2(p.y, p.x);
    }
    shape.grid
        .lookup_or_add_default(
            {int(std::floor(p.x / shape.cell)), int(std::floor(p.y / shape.cell))})
        .append(int(i));
  }
  return shape;
}

/**
 * Find a rigid (optionally mirrored) mapping of `b` onto `a` within `tolerance`.
 * \return true and the index of the matching point of `a` for every point of `b`.
 */
static bool match_shapes(const NormalizedShape &a,
                         const NormalizedShape &b,
                         const float tolerance,
                         const bool allow_mirror,
                         Vector<int> &r_map)
{
  if (a.points.size() != b.points.size() || a.points.is_empty()) {
    return false;
  }
  const float tolerance_sq = tolerance * tolerance;
  /* Grid cells to search around a point, `a` may be hashed with a smaller tolerance. */
  const int reach = std::max(1, int(std::ceil(tolerance / a.cell)));
  for (const bool mirror : {false, true}) {
    if (mirror && !allow_mirror) {
      break;
    }
    int candidates = 0;
    for (const int64_t j : b.points.index_range()) {
      float2 pb = b.points[j];
      if (mirror) {
        pb.x = -pb.x;
      }
      const float length = math::length(pb);
      if (std::abs(length - a.far_length) > tolerance) {
        continue;
      }
      if (++candidates > 32) {
        break;
      }
      const float angle = a.far_angle - std::atan2(pb.y, pb.x);
      const float c = std::cos(angle);
      const float s = std::sin(angle);

      Vector<int> map(b.points.size(), -1);
      Vector<bool> used(a.points.size(), false);
      bool ok = true;
      for (const int64_t k : b.points.index_range()) {
        float2 p = b.points[k];
        if (mirror) {
          p.x = -p.x;
        }
        const float2 q(c * p.x - s * p.y, s * p.x + c * p.y);
        const int cx = int(std::floor(q.x / a.cell));
        const int cy = int(std::floor(q.y / a.cell));
        int best = -1;
        float best_sq = tolerance_sq;
        for (int dy = -reach; dy <= reach; dy++) {
          for (int dx = -reach; dx <= reach; dx++) {
            const Vector<int> *cell = a.grid.lookup_ptr({cx + dx, cy + dy});
            if (!cell) {
              continue;
            }
            for (const int index : *cell) {
              const float d = math::distance_squared(q, a.points[index]);
              if (d <= best_sq) {
                best_sq = d;
                best = index;
              }
            }
          }
        }
        if (best == -1 || used[best]) {
          ok = false;
          break;
        }
        used[best] = true;
        map[k] = best;
      }
      /* The mapped faces must be faces of `a`. With the same loop order the copy keeps the
       * orientation of `a`; with reversed order the copy becomes a mirror image of it. That is
       * what mirror-symmetric mesh halves need (both unwrapped counter-clockwise), and is only
       * allowed with `allow_mirror`. Without this check, a flipped symmetric shape would match a
       * rotation of itself with a mirrored vertex mapping. */
      if (ok) {
        bool same_order = true;
        bool reversed_order = allow_mirror;
        for (const Vector<int> &face : b.faces) {
          Vector<int> mapped;
          for (const int index : face) {
            mapped.append(map[index]);
          }
          same_order = same_order && a.face_keys.contains(face_key(mapped));
          if (reversed_order) {
            std::reverse(mapped.begin(), mapped.end());
            reversed_order = a.face_keys.contains(face_key(mapped));
          }
          if (!same_order && !reversed_order) {
            break;
          }
        }
        ok = same_order || reversed_order;
      }
      if (ok) {
        r_map = std::move(map);
        return true;
      }
    }
  }
  return false;
}

/**
 * Mark copies of identical islands, see #TexelIsland::stack_source.
 * \return The number of islands that became copies or followers.
 */
static int detect_stacks(Vector<TexelIsland> &islands,
                         const int2 &resolution,
                         const bool use_auto,
                         const bool allow_mirror,
                         const float tolerance)
{
  int stacked = 0;
  /* Pointers keep references stable while the map grows. */
  Map<int, std::unique_ptr<NormalizedShape>> shapes;
  auto shape_of = [&](const int i) -> const NormalizedShape & {
    return *shapes.lookup_or_add_cb(i, [&]() {
      return std::make_unique<NormalizedShape>(normalize_shape(islands[i], resolution, tolerance));
    });
  };

  /* Manual stacks: the first island of every id is the source. */
  Map<int, int> manual_source;
  for (const int64_t i : islands.index_range()) {
    TexelIsland &island = islands[i];
    if (island.role != ROLE_PACK || island.constraints.stack <= 0) {
      continue;
    }
    const int source = manual_source.lookup_or_add(island.constraints.stack, int(i));
    if (source == i) {
      continue;
    }
    island.stack_source = source;
    Vector<int> map;
    /* Stacked on purpose: allow mirror images and a looser shape tolerance. */
    if (match_shapes(shape_of(source), shape_of(int(i)), std::max(tolerance, 0.05f), true, map)) {
      island.stack_map = std::move(map);
    }
    else {
      island.stack_union = true;
    }
    stacked++;
  }

  if (!use_auto) {
    return stacked;
  }

  /* Automatic stacks, bucketed by topology and surface area. */
  Map<std::pair<int, int>, Vector<int>> buckets;
  for (const int64_t i : islands.index_range()) {
    const TexelIsland &island = islands[i];
    if (island.role != ROLE_PACK || island.constraints.stack != 0) {
      continue;
    }
    buckets
        .lookup_or_add_default(
            {island.face_island->faces_len, int(island.unique_uvs.size())})
        .append(int(i));
  }
  for (const Vector<int> &bucket : buckets.values()) {
    Vector<int> sources;
    for (const int i : bucket) {
      TexelIsland &island = islands[i];
      for (const int source : sources) {
        if (islands[source].constraints.group != island.constraints.group) {
          continue;
        }
        const float a = islands[source].area_3d;
        const float b = island.area_3d;
        if (std::abs(a - b) > std::max(a, b) * std::max(0.05f, tolerance * 4.0f)) {
          continue;
        }
        Vector<int> map;
        if (match_shapes(shape_of(source), shape_of(i), tolerance, allow_mirror, map)) {
          island.stack_source = source;
          island.stack_map = std::move(map);
          stacked++;
          break;
        }
      }
      if (island.stack_source == -1) {
        sources.append(i);
      }
    }
  }
  return stacked;
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Pack Operator
 * \{ */

enum {
  TEXEL_PACK_TARGET_CLOSEST = 0,
  TEXEL_PACK_TARGET_ACTIVE,
  TEXEL_PACK_TARGET_FIXED,
};

enum {
  TEXEL_PACK_PRECISION_LOW = 0,
  TEXEL_PACK_PRECISION_MEDIUM,
  TEXEL_PACK_PRECISION_HIGH,
  TEXEL_PACK_PRECISION_EXACT,
};

struct TexelPackSettings {
  int2 resolution;
  int precision;
  float margin;
  float edge_padding;
  bool pixel_snap;
  texel_pack::DensityMode density_mode;
  float target_density;
  float density_tolerance;
  bool use_density_weights;
  bool match_locked_density;
  int rotation;
  bool allow_flip;
  bool use_stacking;
  bool stack_mirrored;
  float stack_tolerance;
  int stack_offset;
  int target;
  int udim_tile;
  int max_tiles;
  texel_pack::GroupLayout group_layout;
  bool only_selected;
  bool keep_unselected;
};

struct TexelPackJob {
  wmWindowManager *wm = nullptr;
  Scene *scene = nullptr;
  Vector<Object *> objects;
  TexelPackSettings settings;
  /** First UDIM tile number. */
  int start_tile = 1001;

  bContext *undo_context = nullptr;
  const char *undo_str = nullptr;
  bool use_job = false;

  std::string report;
  bool warning = false;
  bool changed = false;
};

static int2 udim_tile_coords(const int tile_number)
{
  const int n = std::max(0, tile_number - 1001);
  return int2(n % 10, n / 10);
}

static int udim_tile_number(const float2 &uv)
{
  const int u = std::clamp(int(std::floor(uv.x)), 0, 9);
  const int v = std::max(0, int(std::floor(uv.y)));
  return 1001 + u + v * 10;
}

static texel_pack::Rotation resolve_rotation(const int island_value, const int global_value)
{
  const int value = island_value != ISLAND_ROTATION_INHERIT ? island_value : global_value;
  switch (value) {
    case ISLAND_ROTATION_CARDINAL:
      return texel_pack::Rotation::Cardinal;
    case ISLAND_ROTATION_FREE:
      return texel_pack::Rotation::Free;
    default:
      return texel_pack::Rotation::None;
  }
}

/** Append fan triangles of all faces of `island` (pixel space, relative to `tile`). */
static void append_triangles(const TexelIsland &island,
                             const int2 &resolution,
                             const float2 &tile_offset,
                             Vector<float2> &r_triangles)
{
  const FaceIsland &fi = *island.face_island;
  int loop = 0;
  for (int i = 0; i < fi.faces_len; i++) {
    const int len = fi.faces[i]->len;
    auto point = [&](const int j) {
      return to_pixels(island.unique_uvs[island.loop_unique[loop + j]] - tile_offset, resolution);
    };
    /* A fan covers the whole polygon even when concave, which is conservative here. */
    for (int j = 1; j + 1 < len; j++) {
      r_triangles.append(point(0));
      r_triangles.append(point(j));
      r_triangles.append(point(j + 1));
    }
    loop += len;
  }
}

static void write_island_uvs(const TexelIsland &island, Span<float2> unique_uvs)
{
  const FaceIsland &fi = *island.face_island;
  const int cd_uv = fi.offsets.uv;
  int loop = 0;
  for (int i = 0; i < fi.faces_len; i++) {
    BMLoop *l;
    BMIter liter;
    BM_ITER_ELEM (l, &liter, fi.faces[i], BM_LOOPS_OF_FACE) {
      copy_v2_v2(BM_ELEM_CD_GET_FLOAT_P(l, cd_uv), unique_uvs[island.loop_unique[loop]]);
      loop++;
    }
  }
}

static void texel_pack_run(TexelPackJob &job, const bool *stop, float *progress)
{
  const TexelPackSettings &s = job.settings;
  Vector<TexelIsland> islands;
  gather_islands(job.scene, job.objects, islands);

  /* Roles. */
  int pack_count = 0;
  bool mixed = false;
  for (TexelIsland &island : islands) {
    if (island.constraints.lock) {
      island.role = ROLE_OBSTACLE;
    }
    else if (!s.only_selected || island.selected) {
      island.role = ROLE_PACK;
      pack_count++;
    }
    else if (s.keep_unselected) {
      island.role = ROLE_OBSTACLE;
    }
    mixed |= island.role == ROLE_PACK && island.constraints.mixed;
  }
  if (pack_count == 0) {
    job.report = RPT_("No UV islands to pack");
    job.warning = true;
    free_islands(islands);
    return;
  }

  int start_tile = job.start_tile;
  if (s.target == TEXEL_PACK_TARGET_CLOSEST) {
    float2 min(FLT_MAX), max(-FLT_MAX);
    for (const TexelIsland &island : islands) {
      if (island.role == ROLE_PACK) {
        min = math::min(min, island.uv_center);
        max = math::max(max, island.uv_center);
      }
    }
    start_tile = udim_tile_number((min + max) * 0.5f);
  }

  const int stacked = detect_stacks(
      islands, s.resolution, s.use_stacking, s.stack_mirrored, s.stack_tolerance);

  /* Engine input. */
  Vector<texel_pack::Island> engine;
  for (const int64_t i : islands.index_range()) {
    TexelIsland &island = islands[i];
    if (island.role == ROLE_IGNORE || island.stack_source != -1) {
      continue;
    }
    texel_pack::Island input;
    input.area_3d = island.area_3d;
    if (island.role == ROLE_OBSTACLE) {
      const int tile = udim_tile_number(island.uv_center);
      input.locked = true;
      input.locked_tile = tile - start_tile;
      append_triangles(island, s.resolution, float2(udim_tile_coords(tile)), input.triangles);
    }
    else {
      const IslandConstraints &c = island.constraints;
      input.rotation = resolve_rotation(c.rotation, s.rotation);
      input.allow_flip = c.flip == ISLAND_FLIP_INHERIT ? s.allow_flip :
                                                          c.flip == ISLAND_FLIP_ALLOW;
      input.keep_scale = c.keep_scale;
      input.density_weight = s.use_density_weights ? c.density : 1.0f;
      input.group = c.group;
      append_triangles(island, s.resolution, float2(0.0f), input.triangles);
    }
    island.engine_index = int(engine.size());
    engine.append(std::move(input));
  }
  /* Islands following a source without an exact match are packed as part of its shape. */
  for (TexelIsland &island : islands) {
    if (island.stack_union) {
      texel_pack::Island &input = engine[islands[island.stack_source].engine_index];
      append_triangles(island, s.resolution, float2(0.0f), input.triangles);
      input.area_3d += island.area_3d;
    }
  }

  texel_pack::Params params;
  params.width = s.resolution.x;
  params.height = s.resolution.y;
  const int grid_limit[] = {512, 1024, 2048};
  if (s.precision == TEXEL_PACK_PRECISION_EXACT) {
    params.cell_size = 1;
  }
  else {
    const int limit = grid_limit[std::clamp(s.precision, 0, 2)];
    params.cell_size = std::max(1, (std::max(s.resolution.x, s.resolution.y) + limit - 1) / limit);
  }
  params.margin = s.margin;
  params.edge_padding = s.edge_padding;
  params.pixel_snap = s.pixel_snap;
  params.density_mode = s.density_mode;
  params.target_density = s.target_density;
  params.density_tolerance = s.density_tolerance;
  params.match_locked_density = s.match_locked_density;
  params.max_tiles = s.max_tiles;
  params.group_layout = s.group_layout;
  params.stop = stop;
  params.progress = progress;

  const texel_pack::Result result = texel_pack::pack(engine, params);

  if (result.too_many_groups) {
    job.report = RPT_("More island groups than UDIM tiles, increase the tile limit");
    job.warning = true;
    free_islands(islands);
    return;
  }
  if (result.cancelled) {
    job.report = RPT_("Texel pack cancelled");
    job.warning = true;
    free_islands(islands);
    return;
  }

  /* Final UVs of every packed island, used by its copies too. */
  Map<int, Vector<float2>> final_uvs;
  auto transform_island = [&](const TexelIsland &island, const texel_pack::Island &placed) {
    const float2 tile = float2(udim_tile_coords(start_tile + placed.tile));
    Vector<float2> uvs(island.unique_uvs.size());
    for (const int64_t j : island.unique_uvs.index_range()) {
      const float2 px = placed.matrix * to_pixels(island.unique_uvs[j], s.resolution) +
                        placed.translation;
      uvs[j] = tile + float2(px.x / s.resolution.x, px.y / s.resolution.y);
    }
    return uvs;
  };
  for (const int64_t i : islands.index_range()) {
    const TexelIsland &island = islands[i];
    if (island.role != ROLE_PACK) {
      continue;
    }
    const int source = island.stack_source == -1 ? int(i) : island.stack_source;
    const texel_pack::Island &placed = engine[islands[source].engine_index];
    if (!placed.placed) {
      continue;
    }
    if (island.stack_source == -1 || island.stack_union) {
      final_uvs.add(int(i), transform_island(island, placed));
    }
  }
  for (const int64_t i : islands.index_range()) {
    const TexelIsland &island = islands[i];
    if (island.role != ROLE_PACK) {
      continue;
    }
    if (island.stack_source != -1 && !island.stack_union) {
      const Vector<float2> *source_uvs = final_uvs.lookup_ptr(island.stack_source);
      if (!source_uvs) {
        continue;
      }
      const texel_pack::Island &placed = engine[islands[island.stack_source].engine_index];
      const int tile = start_tile + placed.tile;
      const float2 offset = float2(udim_tile_coords(tile + s.stack_offset)) -
                            float2(udim_tile_coords(tile));
      Vector<float2> uvs(island.unique_uvs.size());
      for (const int64_t j : uvs.index_range()) {
        uvs[j] = (*source_uvs)[island.stack_map[j]] + offset;
      }
      write_island_uvs(island, uvs);
      job.changed = true;
    }
  }
  for (const auto item : final_uvs.items()) {
    write_island_uvs(islands[item.key], item.value);
    job.changed = true;
  }

  char buf[512];
  SNPRINTF(buf,
           RPT_("Texel pack: %d islands (%d stacked), %.1f%% used, %.1f px/m (±%.1f%%), %d "
                "tile(s)"),
           result.placed_count + stacked,
           stacked,
           result.utilization * 100.0f,
           result.density,
           result.density_deviation * 100.0f,
           result.tiles_used);
  job.report = buf;
  if (result.unplaced_count > 0) {
    SNPRINTF(buf, RPT_("; %d island(s) did not fit and were left unchanged"),
             result.unplaced_count);
    job.report += buf;
    job.warning = true;
  }
  if (mixed) {
    job.report += RPT_("; some islands have mixed constraint values");
  }
  free_islands(islands);
}

static void texel_pack_startjob(void *job_v, wmJobWorkerStatus *worker_status)
{
  TexelPackJob *job = static_cast<TexelPackJob *>(job_v);
  worker_status->progress = 0.02f;
  texel_pack_run(*job, &worker_status->stop, &worker_status->progress);
  worker_status->progress = 0.99f;
  worker_status->do_update = true;
}

static void texel_pack_endjob(void *job_v)
{
  TexelPackJob *job = static_cast<TexelPackJob *>(job_v);
  for (Object *ob : job->objects) {
    DEG_id_tag_update(static_cast<ID *>(ob->data), ID_RECALC_GEOMETRY);
    WM_main_add_notifier(NC_GEOM | ND_DATA, ob->data);
  }
  WM_main_add_notifier(NC_SPACE | ND_SPACE_IMAGE, nullptr);

  /* Keep the last report on the scene for the UV Tools panel. */
  IDProperty *group = IDP_EnsureProperties(&job->scene->id);
  IDP_ReplaceInGroup(group,
                     bke::idprop::create("uv_texel_pack_report", job->report.c_str()).release());
  WM_main_add_notifier(NC_SCENE | ND_TOOLSETTINGS, job->scene);

  if (job->use_job) {
    WM_global_report(job->warning ? RPT_WARNING : RPT_INFO, job->report.c_str());
  }
  if (job->undo_str) {
    ED_undo_push(job->undo_context, job->undo_str);
  }
}

static void texel_pack_freejob(void *job_v)
{
  WM_cursor_wait(false);
  TexelPackJob *job = static_cast<TexelPackJob *>(job_v);
  WM_locked_interface_set(job->wm, false);
  MEM_delete(job);
}

/** First Image editor of the screen, for the active UDIM tile and the 2D cursor. */
static const SpaceImage *find_space_image(bContext *C)
{
  if (const SpaceImage *sima = CTX_wm_space_image(C)) {
    return sima;
  }
  const bScreen *screen = CTX_wm_screen(C);
  if (!screen) {
    return nullptr;
  }
  for (const ScrArea &area : screen->areabase) {
    const SpaceLink *sl = area.spacedata.first();
    if (sl && sl->spacetype == SPACE_IMAGE) {
      return reinterpret_cast<const SpaceImage *>(sl);
    }
  }
  return nullptr;
}

static int active_udim_tile(const SpaceImage *sima)
{
  if (!sima) {
    return 1001;
  }
  const Image *image = sima->image;
  if (image && image->source == IMA_SRC_TILED) {
    const ImageTile *tile = static_cast<const ImageTile *>(
        BLI_findlink(&image->tiles, image->active_tile_index));
    if (tile) {
      return tile->tile_number;
    }
  }
  return udim_tile_number(float2(sima->cursor));
}

static TexelPackSettings texel_pack_settings_from_op(wmOperator *op)
{
  TexelPackSettings s;
  s.resolution = int2(RNA_int_get(op->ptr, "resolution_x"), RNA_int_get(op->ptr, "resolution_y"));
  s.precision = RNA_enum_get(op->ptr, "precision");
  s.margin = RNA_float_get(op->ptr, "margin");
  s.edge_padding = RNA_float_get(op->ptr, "edge_padding");
  s.pixel_snap = RNA_boolean_get(op->ptr, "pixel_snap");
  s.density_mode = texel_pack::DensityMode(RNA_enum_get(op->ptr, "density_mode"));
  s.target_density = RNA_float_get(op->ptr, "target_density");
  s.density_tolerance = RNA_float_get(op->ptr, "density_tolerance");
  s.use_density_weights = RNA_boolean_get(op->ptr, "use_density_weights");
  s.match_locked_density = RNA_boolean_get(op->ptr, "match_locked_density");
  s.rotation = RNA_enum_get(op->ptr, "rotation");
  s.allow_flip = RNA_boolean_get(op->ptr, "allow_flip");
  s.use_stacking = RNA_boolean_get(op->ptr, "use_stacking");
  s.stack_mirrored = RNA_boolean_get(op->ptr, "stack_mirrored");
  s.stack_tolerance = RNA_float_get(op->ptr, "stack_tolerance");
  s.stack_offset = RNA_int_get(op->ptr, "stack_offset");
  s.target = RNA_enum_get(op->ptr, "target");
  s.udim_tile = RNA_int_get(op->ptr, "udim_tile");
  s.max_tiles = RNA_int_get(op->ptr, "max_tiles");
  s.group_layout = texel_pack::GroupLayout(RNA_enum_get(op->ptr, "group_layout"));
  s.only_selected = RNA_boolean_get(op->ptr, "only_selected");
  s.keep_unselected = RNA_boolean_get(op->ptr, "keep_unselected");
  return s;
}

static wmOperatorStatus texel_pack_exec(bContext *C, wmOperator *op)
{
  const Main *bmain = CTX_data_main(C);
  Scene *scene = CTX_data_scene(C);
  ViewLayer *view_layer = CTX_data_view_layer(C);

  Vector<Object *> objects = BKE_view_layer_array_from_objects_in_edit_mode_unique_data_with_uvs(
      *bmain, scene, view_layer, CTX_wm_view3d(C));
  if (objects.is_empty()) {
    BKE_report(op->reports, RPT_WARNING, "No mesh with UVs in edit mode");
    return OPERATOR_CANCELLED;
  }

  TexelPackJob *job = MEM_new<TexelPackJob>(__func__);
  job->wm = CTX_wm_manager(C);
  job->scene = scene;
  job->objects = std::move(objects);
  job->settings = texel_pack_settings_from_op(op);
  job->use_job = (op->flag & OP_IS_INVOKE) != 0;
  switch (job->settings.target) {
    case TEXEL_PACK_TARGET_ACTIVE:
      job->start_tile = active_udim_tile(find_space_image(C));
      break;
    case TEXEL_PACK_TARGET_FIXED:
      job->start_tile = job->settings.udim_tile;
      break;
    default:
      break; /* Computed from the islands. */
  }

  /* Needed even when jobs aren't used, see: #146195. */
  G.is_break = false;

  if (job->use_job) {
    if (job->wm->op_undo_depth == 0) {
      /* The job must do its own undo push. */
      job->undo_context = C;
      job->undo_str = op->type->name;
    }
    wmJob *wm_job = WM_jobs_get(job->wm,
                                CTX_wm_window(C),
                                scene,
                                "Texel Packing UVs...",
                                WM_JOB_PROGRESS,
                                WM_JOB_TYPE_UV_PACK);
    WM_jobs_customdata_set(wm_job, job, texel_pack_freejob);
    WM_jobs_timer(wm_job, 0.1, 0, 0);
    WM_locked_interface_set_with_flags(job->wm, REGION_DRAW_LOCK_RENDER);
    WM_jobs_callbacks(wm_job, texel_pack_startjob, nullptr, nullptr, texel_pack_endjob);
    WM_cursor_wait(true);
    WM_jobs_start(job->wm, wm_job);
    return OPERATOR_FINISHED;
  }

  wmJobWorkerStatus worker_status = {};
  texel_pack_startjob(job, &worker_status);
  texel_pack_endjob(job);
  BKE_report(op->reports, job->warning ? RPT_WARNING : RPT_INFO, job->report.c_str());
  const bool changed = job->changed;
  texel_pack_freejob(job);
  return changed ? OPERATOR_FINISHED : OPERATOR_CANCELLED;
}

static wmOperatorStatus texel_pack_invoke(bContext *C, wmOperator *op, const wmEvent * /*event*/)
{
  return texel_pack_exec(C, op);
}

static bool texel_pack_poll(bContext *C)
{
  const Object *obedit = CTX_data_edit_object(C);
  return obedit && obedit->type == OB_MESH;
}

static const EnumPropertyItem texel_pack_rotation_items[] = {
    {ISLAND_ROTATION_NONE, "NONE", 0, "None", "Keep the current orientation"},
    {ISLAND_ROTATION_CARDINAL, "CARDINAL", 0, "90°", "Rotate by multiples of 90 degrees"},
    {ISLAND_ROTATION_FREE,
     "FREE",
     0,
     "Free",
     "Rotate to the smallest bounding box, then by multiples of 90 degrees"},
    {0, nullptr, 0, nullptr, nullptr},
};

static void texel_pack_def_settings(wmOperatorType *ot)
{
  static const EnumPropertyItem precision_items[] = {
      {TEXEL_PACK_PRECISION_LOW, "LOW", 0, "Low", "Occupancy grid of at most 512 cells"},
      {TEXEL_PACK_PRECISION_MEDIUM, "MEDIUM", 0, "Medium", "Occupancy grid of at most 1024 cells"},
      {TEXEL_PACK_PRECISION_HIGH, "HIGH", 0, "High", "Occupancy grid of at most 2048 cells"},
      {TEXEL_PACK_PRECISION_EXACT, "EXACT", 0, "Exact", "One occupancy cell per texture pixel"},
      {0, nullptr, 0, nullptr, nullptr},
  };
  static const EnumPropertyItem density_items[] = {
      {int(texel_pack::DensityMode::Uniform),
       "UNIFORM",
       0,
       "Uniform",
       "Give all islands the same texel density and fill the tiles"},
      {int(texel_pack::DensityMode::Relative),
       "RELATIVE",
       0,
       "Keep Relative",
       "Keep the relative UV scale of islands and fill the tiles"},
      {int(texel_pack::DensityMode::Target),
       "TARGET",
       0,
       "Target",
       "Use a fixed texel density, overflowing into further tiles"},
      {0, nullptr, 0, nullptr, nullptr},
  };
  static const EnumPropertyItem target_items[] = {
      {TEXEL_PACK_TARGET_CLOSEST,
       "CLOSEST",
       0,
       "Closest Tile",
       "Start at the tile closest to the islands"},
      {TEXEL_PACK_TARGET_ACTIVE,
       "ACTIVE",
       0,
       "Active Tile",
       "Start at the active image tile, or the tile under the 2D cursor"},
      {TEXEL_PACK_TARGET_FIXED, "FIXED", 0, "Fixed Tile", "Start at the given UDIM tile"},
      {0, nullptr, 0, nullptr, nullptr},
  };
  static const EnumPropertyItem group_items[] = {
      {int(texel_pack::GroupLayout::Mixed), "MIXED", 0, "Mixed", "Ignore island groups"},
      {int(texel_pack::GroupLayout::Cluster),
       "CLUSTER",
       0,
       "Cluster",
       "Place islands of the same group next to each other"},
      {int(texel_pack::GroupLayout::TilePerGroup),
       "TILE_PER_GROUP",
       0,
       "Tile per Group",
       "Give every group its own UDIM tile"},
      {0, nullptr, 0, nullptr, nullptr},
  };

  RNA_def_int(ot->srna, "resolution_x", 1024, 16, 32768, "Width", "Texture width", 16, 16384);
  RNA_def_int(ot->srna, "resolution_y", 1024, 16, 32768, "Height", "Texture height", 16, 16384);
  RNA_def_enum(ot->srna,
               "precision",
               precision_items,
               TEXEL_PACK_PRECISION_MEDIUM,
               "Precision",
               "Resolution of the occupancy grid, higher is tighter but slower");
  RNA_def_float(ot->srna,
                "margin",
                4.0f,
                0.0f,
                1024.0f,
                "Margin",
                "Minimum distance between islands in pixels (mip level N needs 2^N)",
                0.0f,
                64.0f);
  RNA_def_float(ot->srna,
                "edge_padding",
                2.0f,
                0.0f,
                1024.0f,
                "Edge Padding",
                "Minimum distance between islands and the tile border in pixels",
                0.0f,
                64.0f);
  RNA_def_boolean(
      ot->srna, "pixel_snap", true, "Pixel Snap", "Move islands by whole pixels only");
  RNA_def_enum(ot->srna,
               "density_mode",
               density_items,
               int(texel_pack::DensityMode::Uniform),
               "Texel Density",
               "");
  RNA_def_float(ot->srna,
                "target_density",
                512.0f,
                0.001f,
                1e6f,
                "Target Density",
                "Texel density in pixels per meter",
                1.0f,
                8192.0f);
  RNA_def_float_factor(ot->srna,
                       "density_tolerance",
                       0.0f,
                       0.0f,
                       0.5f,
                       "Density Tolerance",
                       "How much islands may shrink below the common density to make them fit",
                       0.0f,
                       0.5f);
  RNA_def_boolean(ot->srna,
                  "use_density_weights",
                  true,
                  "Density Weights",
                  "Multiply the density of islands by their density constraint");
  RNA_def_boolean(ot->srna,
                  "match_locked_density",
                  true,
                  "Match Locked Density",
                  "With locked islands and uniform density, use their density instead of "
                  "filling the tiles");
  RNA_def_enum(ot->srna,
               "rotation",
               texel_pack_rotation_items,
               ISLAND_ROTATION_CARDINAL,
               "Rotation",
               "Rotation of islands without their own rotation constraint");
  RNA_def_boolean(ot->srna,
                  "allow_flip",
                  false,
                  "Allow Flip",
                  "Allow mirroring islands without their own flip constraint (changes tangent "
                  "space handedness)");
  RNA_def_boolean(
      ot->srna, "use_stacking", true, "Stack Identical", "Overlap islands with identical shapes");
  RNA_def_boolean(ot->srna,
                  "stack_mirrored",
                  false,
                  "Stack Mirrored",
                  "Also overlap mirror images of islands (changes tangent space handedness)");
  RNA_def_float_factor(ot->srna,
                       "stack_tolerance",
                       0.01f,
                       0.0001f,
                       0.2f,
                       "Stack Tolerance",
                       "Shape difference allowed for stacking, relative to the island size",
                       0.0001f,
                       0.1f);
  RNA_def_int(ot->srna,
              "stack_offset",
              0,
              0,
              99,
              "Stack Offset",
              "Move stacked copies this many UDIM tiles away, for baking",
              0,
              10);
  RNA_def_enum(ot->srna, "target", target_items, TEXEL_PACK_TARGET_CLOSEST, "Pack To", "");
  RNA_def_int(ot->srna, "udim_tile", 1001, 1001, 2000, "UDIM Tile", "First tile", 1001, 1100);
  RNA_def_int(ot->srna,
              "max_tiles",
              1,
              1,
              100,
              "Max Tiles",
              "Number of UDIM tiles islands may use",
              1,
              10);
  RNA_def_enum(ot->srna,
               "group_layout",
               group_items,
               int(texel_pack::GroupLayout::Mixed),
               "Groups",
               "How islands with a group constraint are laid out");
  RNA_def_boolean(ot->srna, "only_selected", true, "Selected Only", "Only pack selected islands");
  RNA_def_boolean(ot->srna,
                  "keep_unselected",
                  false,
                  "Avoid Unselected",
                  "Keep unselected islands in place and pack around them");
}

void UV_OT_texel_pack(wmOperatorType *ot)
{
  ot->name = "Texel Pack";
  ot->idname = "UV_OT_texel_pack";
  ot->description =
      "Pack UV islands on a texel grid with pixel margins, texel density control, stacking, "
      "groups and per-island constraints";

  /* The operator handles undo itself, so the job system can push it after the job completes. */
  ot->flag = OPTYPE_REGISTER;

  ot->exec = texel_pack_exec;
  ot->invoke = texel_pack_invoke;
  ot->poll = texel_pack_poll;

  texel_pack_def_settings(ot);
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Constraint Operators
 * \{ */

static const EnumPropertyItem constraint_kind_items[] = {
    {CONSTRAINT_ROTATION, "ROTATION", 0, "Rotation", ""},
    {CONSTRAINT_FLIP, "FLIP", 0, "Flip", ""},
    {CONSTRAINT_SCALE, "SCALE", 0, "Keep Scale", ""},
    {CONSTRAINT_DENSITY, "DENSITY", 0, "Density", ""},
    {CONSTRAINT_GROUP, "GROUP", 0, "Group", ""},
    {CONSTRAINT_STACK, "STACK", 0, "Stack", ""},
    {CONSTRAINT_LOCK, "LOCK", 0, "Lock", ""},
    {CONSTRAINT_ALL, "ALL", 0, "All", ""},
    {0, nullptr, 0, nullptr, nullptr},
};

static const EnumPropertyItem island_rotation_items[] = {
    {ISLAND_ROTATION_INHERIT, "INHERIT", 0, "Default", "Use the packing setting"},
    {ISLAND_ROTATION_NONE, "NONE", 0, "None", "Keep the current orientation"},
    {ISLAND_ROTATION_CARDINAL, "CARDINAL", 0, "90°", "Rotate by multiples of 90 degrees"},
    {ISLAND_ROTATION_FREE, "FREE", 0, "Free", "Rotate to the smallest bounding box first"},
    {0, nullptr, 0, nullptr, nullptr},
};

static const EnumPropertyItem island_flip_items[] = {
    {ISLAND_FLIP_INHERIT, "INHERIT", 0, "Default", "Use the packing setting"},
    {ISLAND_FLIP_FORBID, "FORBID", 0, "Forbid", "Never mirror"},
    {ISLAND_FLIP_ALLOW, "ALLOW", 0, "Allow", "May be mirrored"},
    {0, nullptr, 0, nullptr, nullptr},
};

static void constraint_def_values(wmOperatorType *ot, const int default_kind)
{
  RNA_def_enum(ot->srna, "kind", constraint_kind_items, default_kind, "Constraint", "");
  RNA_def_enum(ot->srna, "rotation", island_rotation_items, 0, "Rotation", "");
  RNA_def_enum(ot->srna, "flip", island_flip_items, 0, "Flip", "");
  RNA_def_boolean(ot->srna, "keep_scale", true, "Keep Scale", "Keep the current UV scale");
  RNA_def_float(ot->srna,
                "density",
                1.0f,
                0.01f,
                100.0f,
                "Density",
                "Texel density multiplier",
                0.1f,
                10.0f);
  RNA_def_int(ot->srna, "group", 1, 0, INT_MAX, "Group", "Group id, 0 for none", 0, 100);
  RNA_def_int(ot->srna,
              "stack",
              1,
              -1,
              INT_MAX,
              "Stack",
              "Stack id: 0 automatic, -1 never stack, above 0 stack with islands of the same id",
              -1,
              100);
  RNA_def_boolean(ot->srna, "lock", true, "Lock", "Keep the island in place");
}

static int constraint_int_value(wmOperator *op, const int kind)
{
  switch (kind) {
    case CONSTRAINT_ROTATION:
      return RNA_enum_get(op->ptr, "rotation");
    case CONSTRAINT_FLIP:
      return RNA_enum_get(op->ptr, "flip");
    case CONSTRAINT_SCALE:
      return RNA_boolean_get(op->ptr, "keep_scale") ? 1 : 0;
    case CONSTRAINT_GROUP:
      return RNA_int_get(op->ptr, "group");
    case CONSTRAINT_STACK:
      return RNA_int_get(op->ptr, "stack");
    case CONSTRAINT_LOCK:
      return RNA_boolean_get(op->ptr, "lock") ? 1 : 0;
  }
  return 0;
}

/** Write `value` (or the default when `clear`) of `kind` to every face of the selected islands. */
static wmOperatorStatus constraint_apply(bContext *C, wmOperator *op, const bool clear)
{
  const Main *bmain = CTX_data_main(C);
  Scene *scene = CTX_data_scene(C);
  ViewLayer *view_layer = CTX_data_view_layer(C);
  const int kind = RNA_enum_get(op->ptr, "kind");

  Vector<Object *> objects = BKE_view_layer_array_from_objects_in_edit_mode_unique_data_with_uvs(
      *bmain, scene, view_layer, CTX_wm_view3d(C));

  /* Outside the UV editor (e.g. the 3D viewport sidebar), use the mesh selection. */
  const bool use_mesh_selection = CTX_wm_space_image(C) == nullptr;
  Vector<TexelIsland> islands;
  gather_islands(scene, objects, islands, use_mesh_selection);

  int selected_islands = 0;
  for (const TexelIsland &island : islands) {
    selected_islands += island.selected;
  }
  if (selected_islands == 0) {
    free_islands(islands);
    BKE_report(op->reports, RPT_WARNING, "No selected UV islands");
    return OPERATOR_CANCELLED;
  }

  for (Object *ob : objects) {
    BMesh *bm = BKE_editmesh_bmesh_get_for_write(ob);
    bool any_selected = false;
    for (const TexelIsland &island : islands) {
      any_selected |= island.bm == bm && island.selected;
    }
    if (!any_selected) {
      continue;
    }

    for (int k = CONSTRAINT_ROTATION; k <= CONSTRAINT_LOCK; k++) {
      if (k != kind && !(clear && kind == CONSTRAINT_ALL)) {
        continue;
      }
      const char *name = constraint_attribute_name(k);
      const eCustomDataType type = k == CONSTRAINT_DENSITY ? CD_PROP_FLOAT : CD_PROP_INT32;
      if (clear && CustomData_get_offset_named(&bm->pdata, type, name) == -1) {
        continue;
      }
      BM_data_layer_ensure_named(bm, &bm->pdata, type, name);
      const int cd_offset = CustomData_get_offset_named(&bm->pdata, type, name);
      const int int_value = clear ? 0 : constraint_int_value(op, k);
      const float float_value = clear ? 0.0f : RNA_float_get(op->ptr, "density");
      for (const TexelIsland &island : islands) {
        if (island.bm != bm || !island.selected) {
          continue;
        }
        for (int i = 0; i < island.face_island->faces_len; i++) {
          BMFace *f = island.face_island->faces[i];
          if (type == CD_PROP_FLOAT) {
            BM_ELEM_CD_SET_FLOAT(f, cd_offset, float_value);
          }
          else {
            BM_ELEM_CD_SET_INT(f, cd_offset, int_value);
          }
        }
      }
    }
    DEG_id_tag_update(static_cast<ID *>(ob->data), ID_RECALC_GEOMETRY);
    WM_event_add_notifier(C, NC_GEOM | ND_DATA, ob->data);
  }
  free_islands(islands);
  return OPERATOR_FINISHED;
}

static wmOperatorStatus constraint_set_exec(bContext *C, wmOperator *op)
{
  if (RNA_enum_get(op->ptr, "kind") == CONSTRAINT_ALL) {
    BKE_report(op->reports, RPT_ERROR, "Choose a single constraint to set");
    return OPERATOR_CANCELLED;
  }
  return constraint_apply(C, op, false);
}

static wmOperatorStatus constraint_clear_exec(bContext *C, wmOperator *op)
{
  return constraint_apply(C, op, true);
}

void UV_OT_texel_pack_constraint_set(wmOperatorType *ot)
{
  ot->name = "Set Island Constraint";
  ot->idname = "UV_OT_texel_pack_constraint_set";
  ot->description = "Store a Texel Pack constraint on the selected UV islands";
  ot->flag = OPTYPE_REGISTER | OPTYPE_UNDO;
  ot->exec = constraint_set_exec;
  ot->poll = texel_pack_poll;
  constraint_def_values(ot, CONSTRAINT_ROTATION);
}

void UV_OT_texel_pack_constraint_clear(wmOperatorType *ot)
{
  ot->name = "Clear Island Constraint";
  ot->idname = "UV_OT_texel_pack_constraint_clear";
  ot->description = "Reset Texel Pack constraints of the selected UV islands to the default";
  ot->flag = OPTYPE_REGISTER | OPTYPE_UNDO;
  ot->exec = constraint_clear_exec;
  ot->poll = texel_pack_poll;
  constraint_def_values(ot, CONSTRAINT_ALL);
}

static bool island_matches(const IslandConstraints &c, wmOperator *op, const int kind)
{
  switch (kind) {
    case CONSTRAINT_ROTATION:
      return c.rotation == constraint_int_value(op, kind);
    case CONSTRAINT_FLIP:
      return c.flip == constraint_int_value(op, kind);
    case CONSTRAINT_SCALE:
      return int(c.keep_scale) == constraint_int_value(op, kind);
    case CONSTRAINT_DENSITY:
      return std::abs(c.density - RNA_float_get(op->ptr, "density")) < 1e-3f;
    case CONSTRAINT_GROUP:
      return c.group == constraint_int_value(op, kind);
    case CONSTRAINT_STACK:
      return c.stack == constraint_int_value(op, kind);
    case CONSTRAINT_LOCK:
      return int(c.lock) == constraint_int_value(op, kind);
    case CONSTRAINT_ALL:
      return c.rotation != 0 || c.flip != 0 || c.keep_scale || c.density != 1.0f ||
             c.group != 0 || c.stack != 0 || c.lock;
  }
  return false;
}

static wmOperatorStatus constraint_select_exec(bContext *C, wmOperator *op)
{
  const Main *bmain = CTX_data_main(C);
  Scene *scene = CTX_data_scene(C);
  ViewLayer *view_layer = CTX_data_view_layer(C);
  const ToolSettings *ts = scene->toolsettings;
  const int kind = RNA_enum_get(op->ptr, "kind");
  const bool extend = RNA_boolean_get(op->ptr, "extend");

  Vector<Object *> objects = BKE_view_layer_array_from_objects_in_edit_mode_unique_data_with_uvs(
      *bmain, scene, view_layer, CTX_wm_view3d(C));

  const bool use_mesh_selection = CTX_wm_space_image(C) == nullptr;
  Vector<TexelIsland> islands;
  gather_islands(scene, objects, islands, use_mesh_selection);

  int found = 0;
  for (Object *ob : objects) {
    BMesh *bm = BKE_editmesh_bmesh_get_for_write(ob);
    if (use_mesh_selection) {
      BMEditMesh *em = BKE_editmesh_from_object(ob);
      if (!extend) {
        EDBM_flag_disable_all(bm, BM_ELEM_SELECT);
      }
      for (const TexelIsland &island : islands) {
        if (island.bm != bm || !island_matches(island.constraints, op, kind)) {
          continue;
        }
        for (int i = 0; i < island.face_island->faces_len; i++) {
          BM_face_select_set(bm, island.face_island->faces[i], true);
        }
        found++;
      }
      EDBM_selectmode_flush(bm, em->selectmode);
      EDBM_uvselect_clear(bm);
      DEG_id_tag_update(static_cast<ID *>(ob->data), ID_RECALC_SELECT);
      WM_event_add_notifier(C, NC_GEOM | ND_SELECT, ob->data);
      continue;
    }
    if (!extend) {
      ED_uvedit_deselect_all(scene, ob, SEL_DESELECT);
    }
    if (ts->uv_flag & UV_FLAG_SELECT_SYNC) {
      uvedit_select_prepare_sync_select(scene, bm);
    }
    bool changed = false;
    for (const TexelIsland &island : islands) {
      if (island.bm != bm || !island_matches(island.constraints, op, kind)) {
        continue;
      }
      for (int i = 0; i < island.face_island->faces_len; i++) {
        uvedit_face_select_set(scene, bm, island.face_island->faces[i], true);
      }
      changed = true;
      found++;
    }
    if (changed) {
      if (ts->uv_flag & UV_FLAG_SELECT_SYNC) {
        if (bm->uv_select_sync_valid) {
          BM_mesh_uvselect_flush_from_loop_verts_only_select(bm);
          BM_mesh_uvselect_sync_to_mesh(bm);
        }
        else {
          BM_mesh_select_flush_from_verts(bm, true);
        }
      }
      else {
        uvedit_select_flush_from_verts(scene, bm, true);
      }
    }
    DEG_id_tag_update(static_cast<ID *>(ob->data), ID_RECALC_SELECT);
    WM_event_add_notifier(C, NC_GEOM | ND_SELECT, ob->data);
  }
  free_islands(islands);

  if (found == 0) {
    BKE_report(op->reports, RPT_INFO, "No UV islands with this constraint");
  }
  return OPERATOR_FINISHED;
}

void UV_OT_texel_pack_constraint_select(wmOperatorType *ot)
{
  ot->name = "Select Islands by Constraint";
  ot->idname = "UV_OT_texel_pack_constraint_select";
  ot->description =
      "Select UV islands with the given Texel Pack constraint value (All: any constraint)";
  ot->flag = OPTYPE_REGISTER | OPTYPE_UNDO;
  ot->exec = constraint_select_exec;
  ot->poll = texel_pack_poll;
  constraint_def_values(ot, CONSTRAINT_ROTATION);
  RNA_def_boolean(ot->srna, "extend", false, "Extend", "Add to the current selection");
}

/** \} */

}  // namespace blender
