/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include "BKE_idprop.hh"
#include "DNA_view3d_types.h"

namespace blender::draw::xtoon {

/* These settings are stored per viewport by gf_toon_viewport.py. */
inline bool active(const View3D &view)
{
  const IDProperty *previous = IDP_GetPropertyFromGroup_null(view.shading.prop, "gf_previous_preview");
  return view.shading.type == OB_MATERIAL && previous && previous->type == IDP_STRING &&
         IDP_string_get(previous)[0] != '\0';
}

inline float light_gain(const View3D &view)
{
  const IDProperty *gain = IDP_GetPropertyFromGroup_null(view.shading.prop, "gf_light_gain");
  return gain && gain->type == IDP_FLOAT ? IDP_float_get(gain) : 3.3f;
}

inline bool use_outline(const View3D &view)
{
  const IDProperty *setting = IDP_GetPropertyFromGroup_null(view.shading.prop, "gf_use_outline");
  return !setting || setting->type != IDP_BOOLEAN || IDP_bool_get(setting);
}

}  // namespace blender::draw::xtoon
