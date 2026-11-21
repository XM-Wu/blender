/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include <algorithm>
#include <cmath>
#include <string>

#include "BKE_idprop.hh"
#include "BLI_math_matrix.hh"
#include "BLI_math_rotation_c.hh"
#include "BLI_math_vector.hh"
#include "overlay_armature.hh"

namespace blender::draw::overlay {

/* Guide geometry is display-only. All selection IDs resolve to real XBone joints. */
class ShifterGuideDrawing {
  struct Joint {
    float4x4 frame;
    int select_id;
    bool selected;
  };
  Armatures::DrawContext &ctx_;
  Map<int, Joint> joints_;
  const IDProperty *shapes_;
  bool enabled_;
  float radius_;

  const IDProperty *property(StringRef name) const
  {
    return IDP_GetPropertyFromGroup_null(ctx_.ob->id.properties, name);
  }

  static float4 color(int index, bool selected)
  {
    if (selected) {
      return {0.3f, 1.0f, 0.45f, 1.0f};
    }
    switch (index) {
      case 4:
        return {0.45f, 0.0f, 0.015f, 1};
      case 6:
        return {0.0f, 0.0f, 1.0f, 1};
      case 13:
        return {1.0f, 0.0f, 0.0f, 1};
      case 14:
        return {0.0f, 1.0f, 0.0f, 1};
      case 17:
        return {1.0f, 1.0f, 0.0f, 1};
      case 18:
        return {0.16f, 0.78f, 1.0f, 1};
      default:
        return {0.25f, 0.25f, 0.25f, 1};
    }
  }

  void line(const float3 &a, const float3 &b, const float4 &col, int id) const
  {
    const float4x4 &world = ctx_.ob->object_to_world();
    ctx_.bone_buf->wire_buf.append(math::transform_point(world, a),
                                   math::transform_point(world, b),
                                   col,
                                   ctx_.res->select_id(*ctx_.ob_ref, id));
  }

  bool glyph(int key, const Joint &joint) const
  {
    const IDProperty *prop = IDP_GetPropertyFromGroup_null(shapes_, std::to_string(key));
    if (!prop || prop->type != IDP_ARRAY || prop->subtype != IDP_FLOAT) {
      return false;
    }
    Span<float> values(IDP_array_float_get(prop), prop->len);
    while (values.size() >= 2) {
      const float4 col = color(int(values[0]), joint.selected);
      const float count = values[1];
      values = values.drop_front(2);
      if (!(count >= 0 && count <= values.size() / 3)) {
        break;
      }
      float3 previous;
      for (int i = 0; i < int(count); i++) {
        const float3 point = math::transform_point(joint.frame, float3(values.data() + 3 * i));
        if (i) {
          line(previous, point, col, joint.select_id);
        }
        previous = point;
      }
      values = values.drop_front(3 * int(count));
    }
    return true;
  }

 public:
  explicit ShifterGuideDrawing(Armatures::DrawContext &ctx) : ctx_(ctx)
  {
    const IDProperty *mode = property("show_shifter_guide");
    const IDProperty *role = property("rigging_role");
    enabled_ = mode && ELEM(mode->type, IDP_BOOLEAN, IDP_INT) && IDP_int_or_bool_get(mode) &&
               role && role->type == IDP_STRING && STREQ(IDP_string_get(role), "GUIDE");
    shapes_ = enabled_ ? property("shifter_guide_shapes") : nullptr;
    if (shapes_ && shapes_->type != IDP_GROUP) {
      shapes_ = nullptr;
    }
    const IDProperty *size = enabled_ ? property("shifter_guide_radius") : nullptr;
    radius_ = size && size->type == IDP_DOUBLE ? float(IDP_double_get(size)) : 0.025f;
  }

  bool enabled() const
  {
    return enabled_;
  }

  void add(const float4x4 &frame,
           const IDProperty *props,
           const float *parent_head,
           int select_id,
           bool selected)
  {
    const Joint joint{frame, select_id | BONESEL_ANY, selected};
    const IDProperty *id = IDP_GetPropertyFromGroup_null(props, "shifter_guide_id");
    if (id && id->type == IDP_INT && joints_.add(IDP_int_get(id), joint) &&
        glyph(IDP_int_get(id), joint))
    {
      return;
    }
    /* Custom Guide joints use Shifter's root cube or locator sphere and three cross axes. */
    const float4 col = color(parent_head ? 17 : 13, selected);
    for (int axis = 0; axis < 3; axis++) {
      float3 a(0), b(0);
      a[axis] = -2 * radius_;
      b[axis] = 2 * radius_;
      line(math::transform_point(frame, a), math::transform_point(frame, b), col, joint.select_id);
      for (int i = 0; i < (parent_head ? 16 : 4); i++) {
        for (int j = 0; j < 2; j++) {
          float3 &p = j ? b : a;
          p = float3(0);
          if (parent_head) {
            const float t = float(i + j) * (2 * M_PI / 16);
            p[(axis + 1) % 3] = radius_ * cosf(t);
            p[(axis + 2) % 3] = radius_ * sinf(t);
          }
          else {
            p[axis] = j ? radius_ : -radius_;
            p[(axis + 1) % 3] = (i & 1) ? radius_ : -radius_;
            p[(axis + 2) % 3] = (i & 2) ? radius_ : -radius_;
          }
        }
        line(math::transform_point(frame, a),
             math::transform_point(frame, b),
             col,
             joint.select_id);
      }
    }
    if (parent_head && !ctx_.res->is_selection()) {
      line(float3(parent_head), frame.location(), color(0, false), joint.select_id);
    }
  }

  void finish()
  {
    if (!enabled_) {
      return;
    }
    const IDProperty *aux = property("shifter_guide_aux");
    if (aux && aux->type == IDP_ARRAY && aux->subtype == IDP_FLOAT) {
      const float *a = IDP_array_float_get(aux);
      for (int i = 0; i + 10 <= aux->len; i += 10, a += 10) {
        const Joint *owner = joints_.lookup_ptr(int(a[2]));
        const Joint *target = joints_.lookup_ptr(int(a[3]));
        if (!owner || !target) {
          continue;
        }
        Joint marker = *owner;
        float3 direction = math::transform_direction(marker.frame, float3(a + 6));
        if (int(a[0]) == 1) {
          const float3 aim = target->frame.location() - marker.frame.location();
          if (math::length_squared(aim) > 1e-14f && math::length_squared(direction) > 1e-14f) {
            float rotation[4];
            rotation_between_vecs_to_quat(
                rotation, math::normalize(direction), math::normalize(aim));
            for (int axis = 0; axis < 3; axis++) {
              mul_qt_v3(rotation, marker.frame[axis]);
            }
          }
        }
        else {
          const Joint *root = joints_.lookup_ptr(int(a[4]));
          const Joint *eff = joints_.lookup_ptr(int(a[5]));
          if (!root || !eff) {
            continue;
          }
          const float3 axis = target->frame.location() - root->frame.location();
          const float3 mid = owner->frame.location() - root->frame.location();
          const float3 unit = math::normalize(axis);
          const float3 bend = mid - unit * math::dot(unit, mid);
          if (math::length_squared(bend) > 1e-12f) {
            direction = math::normalize(bend);
          }
          marker.frame.location() += direction * a[9] *
                                     std::max({math::length(axis),
                                               math::length(mid),
                                               math::distance(eff->frame.location(),
                                                              root->frame.location())});
        }
        joints_.add_overwrite(int(a[1]), marker);
        glyph(int(a[1]), marker);
      }
    }
    if (ctx_.res->is_selection()) {
      return;
    }
    const IDProperty *paths = property("shifter_guide_paths");
    if (!paths || paths->type != IDP_ARRAY || paths->subtype != IDP_INT) {
      return;
    }
    Span<int> values(IDP_array_int_get(paths), paths->len);
    while (values.size() >= 2) {
      const int degree = values[0], count = values[1];
      values = values.drop_front(2);
      if (count < 0 || count > values.size()) {
        break;
      }
      Vector<float3, 8> points;
      int select_id = -1;
      for (int key : values.take_front(count)) {
        const Joint *joint = joints_.lookup_ptr(key);
        if (!joint) {
          break;
        }
        points.append(joint->frame.location());
        select_id = joint->select_id;
      }
      values = values.drop_front(count);
      if (points.size() != count) {
        continue;
      }
      if (degree == 3 && count == 4) {
        float3 prev = points[0];
        for (int k = 1; k <= 16; k++) {
          const float t = k / 16.0f, s = 1 - t;
          const float3 p = points[0] * (s * s * s) + points[1] * (3 * s * s * t) +
                           points[2] * (3 * s * t * t) + points[3] * (t * t * t);
          line(prev, p, color(0, false), select_id);
          prev = p;
        }
      }
      else {
        for (int k = 1; k < count; k++) {
          line(points[k - 1], points[k], color(0, false), select_id);
        }
      }
    }
  }
};
}  // namespace blender::draw::overlay
