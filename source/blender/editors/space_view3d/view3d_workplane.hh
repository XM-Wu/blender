/* SPDX-FileCopyrightText: 2026 Blender Authors
 * SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once
#include "BKE_scene_runtime.hh"
#include "BLI_math_matrix_c.hh"
#include "BLI_math_rotation_c.hh"
#include "DEG_depsgraph_query.hh"
#include "DNA_scene_types.h"
namespace blender {
inline bool view3d_workplane_quat(const Scene *scene, float quat[4])
{
  scene = DEG_get_original(scene);
  unit_qt(quat);
  if (!scene->runtime->use_workplane) {
    return false;
  }
  const auto matrix = float3x3(scene->runtime->workplane_matrix);
  mat3_to_quat(quat, matrix.ptr());
  return true;
}
inline void view3d_workplane_axis_quat(const Scene *scene, float quat[4])
{
  float frame[4];
  if (view3d_workplane_quat(scene, frame)) {
    invert_qt_normalized(frame);
    mul_qt_qtqt(quat, quat, frame);
  }
}
}  // namespace blender
