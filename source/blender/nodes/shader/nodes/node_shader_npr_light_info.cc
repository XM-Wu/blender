/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup shdnodes
 */

#include "node_shader_util.hh"

#include "BLI_math_base.hh"
#include "BLI_math_vector.hh"

#include "BKE_lib_id.hh"

#include "DNA_light_types.h"
#include "DNA_object_types.h"

#include "UI_interface_layout.hh"
#include "UI_resources.hh"

namespace blender {

namespace nodes::node_shader_npr_light_info_cc {

enum eNodeLightInfoOutputType {
  NODE_LIGHT_INFO_TYPE_NONE = -1,
  NODE_LIGHT_INFO_TYPE_POINT = 0,
  NODE_LIGHT_INFO_TYPE_SUN = 1,
  NODE_LIGHT_INFO_TYPE_SPOT = 2,
  NODE_LIGHT_INFO_TYPE_AREA = 3,
};

static int node_light_info_type(const bNode &node)
{
  const Object *light_object = reinterpret_cast<const Object *>(node.id);
  if (light_object == nullptr || light_object->type != OB_LAMP || light_object->data == nullptr) {
    return -1;
  }
  const Light *light = id_cast<Light *>(light_object->data);
  return light->type;
}

static int node_light_info_output_type_value(const int light_type)
{
  switch (light_type) {
    case LA_LOCAL:
      return NODE_LIGHT_INFO_TYPE_POINT;
    case LA_SUN:
      return NODE_LIGHT_INFO_TYPE_SUN;
    case LA_SPOT:
      return NODE_LIGHT_INFO_TYPE_SPOT;
    case LA_AREA:
      return NODE_LIGHT_INFO_TYPE_AREA;
    default:
      return NODE_LIGHT_INFO_TYPE_NONE;
  }
}

static void node_declare(NodeDeclarationBuilder &b)
{
  b.is_function_node();
  b.add_output<decl::Color>("Color"_ustr);
  b.add_output<decl::Float>("Power"_ustr);
  b.add_output<decl::Int>("Type"_ustr)
      .description("Light type index: 0 Point, 1 Sun, 2 Spot, 3 Area. Outputs -1 when no light is assigned");
  b.add_output<decl::Vector>("Position"_ustr)
      .description("World-space location of the selected light object")
      .available(false);
  b.add_output<decl::Vector>("Direction"_ustr)
      .description("World-space emission direction")
      .available(false);
  b.add_output<decl::Float>("Radius"_ustr)
      .description("Primary size parameter of the selected light")
      .available(false);
  b.add_output<decl::Float>("Spot Size"_ustr)
      .description("Spot cone angle in radians")
      .available(false);
  b.add_output<decl::Float>("Sun Angle"_ustr)
      .description("Sun angular diameter in radians")
      .available(false);
  b.add_output<decl::Float>("Visible"_ustr);
  b.add_output<decl::Vector>("Origin"_ustr);
  b.add_output<decl::Vector>("X Axis"_ustr);
  b.add_output<decl::Vector>("Y Axis"_ustr);
  b.add_output<decl::Vector>("Z Axis"_ustr);
}

static void node_shader_buts_light_info(ui::Layout &layout, bContext * /*C*/, PointerRNA *ptr)
{
  layout.prop(ptr, "light_object", ui::ITEM_R_SPLIT_EMPTY_NAME, "", ICON_NONE);
}

static void node_update(bNodeTree *ntree, bNode *node)
{
  bNodeSocket *position_socket = bke::node_find_socket(*node, SOCK_OUT, "Position"_ustr);
  bNodeSocket *direction_socket = bke::node_find_socket(*node, SOCK_OUT, "Direction"_ustr);
  bNodeSocket *radius_socket = bke::node_find_socket(*node, SOCK_OUT, "Radius"_ustr);
  bNodeSocket *spot_size_socket = bke::node_find_socket(*node, SOCK_OUT, "Spot Size"_ustr);
  bNodeSocket *sun_angle_socket = bke::node_find_socket(*node, SOCK_OUT, "Sun Angle"_ustr);

  const int light_type = node_light_info_type(*node);
  bke::node_set_socket_availability(
      *ntree, *position_socket, ELEM(light_type, LA_LOCAL, LA_SPOT, LA_AREA));
  bke::node_set_socket_availability(
      *ntree, *direction_socket, ELEM(light_type, LA_SUN, LA_SPOT, LA_AREA));
  bke::node_set_socket_availability(
      *ntree, *radius_socket, ELEM(light_type, LA_LOCAL, LA_SPOT, LA_AREA));
  bke::node_set_socket_availability(*ntree, *spot_size_socket, light_type == LA_SPOT);
  bke::node_set_socket_availability(*ntree, *sun_angle_socket, light_type == LA_SUN);
}

static void node_shader_light_info_values(const Object &light_object,
                                          const Light &light,
                                          float3 &r_position,
                                          float3 &r_direction,
                                          float &r_radius,
                                          float &r_spot_size,
                                          float &r_sun_angle)
{
  const float4x4 object_to_world = light_object.object_to_world();
  r_position = object_to_world.location();
  r_direction = float3(0.0f);
  r_radius = 0.0f;
  r_spot_size = 0.0f;
  r_sun_angle = 0.0f;

  const float3 z_axis = object_to_world.z_axis();
  if (light.type != LA_LOCAL && math::length_squared(z_axis) > 1e-12f) {
    r_direction = -math::normalize(z_axis);
  }

  const float3 scale = {
      math::length(object_to_world.x_axis()),
      math::length(object_to_world.y_axis()),
      math::length(object_to_world.z_axis()),
  };
  const float max_scale = std::max({scale.x, scale.y, scale.z});

  switch (light.type) {
    case LA_LOCAL:
      r_radius = light.radius * max_scale;
      break;
    case LA_SUN:
      r_radius = light.sun_angle;
      r_sun_angle = light.sun_angle;
      break;
    case LA_SPOT:
      r_radius = light.radius * max_scale;
      r_spot_size = light.spotsize;
      break;
    case LA_AREA: {
      const bool is_irregular = ELEM(light.area_shape, LA_AREA_RECT, LA_AREA_ELLIPSE);
      const float size_x = light.area_size * scale.x;
      const float size_y = (is_irregular ? light.area_sizey : light.area_size) * scale.y;
      r_radius = 0.5f * max_ff(size_x, size_y);
      break;
    }
  }
}

static int node_shader_gpu_light_info(GPUMaterial *mat,
                                      bNode *node,
                                      bNodeExecData * /*execdata*/,
                                      GPUNodeStack *in,
                                      GPUNodeStack *out)
{
  float4 color(0.0f);
  float power = 0.0f, type = -1.0f, radius = 0.0f, spot_size = 0.0f, sun_angle = 0.0f;
  float visible = 0.0f;
  float3 position(0), direction(0);
  float4x4 transform = float4x4::zero();
  const Object *object = reinterpret_cast<const Object *>(node->id);
  if (object && object->type == OB_LAMP && object->data) {
    const Light &light = *id_cast<Light *>(object->data);
    color = float4(light.r, light.g, light.b, 1.0f);
    power = light.energy;
    type = float(node_light_info_output_type_value(light.type));
    transform = object->object_to_world();
    visible = (object->visibility_flag & OB_HIDE_RENDER) == 0;
    node_shader_light_info_values(*object, light, position, direction, radius, spot_size, sun_angle);
  }
  return GPU_stack_link(mat, node, "node_npr_light_info", in, out,
                        GPU_uniform(color), GPU_uniform(&power), GPU_uniform(&type),
                        GPU_uniform(position), GPU_uniform(direction), GPU_uniform(&radius),
                        GPU_uniform(&spot_size), GPU_uniform(&sun_angle), GPU_uniform(&visible),
                        GPU_uniform(&transform[0][0]));
}

}  // namespace nodes::node_shader_npr_light_info_cc

void register_node_type_sh_npr_light_info()
{
  namespace file_ns = nodes::node_shader_npr_light_info_cc;

  static bke::bNodeType ntype;

  sh_node_type_base(&ntype, "ShaderNodeNPRLightInfo"_ustr, SH_NODE_NPR_LIGHT_INFO);
  ntype.ui_name = "NPR Light Info";
  ntype.ui_description = "Read flat color, power, transform, and size values from a chosen light";
  ntype.enum_name_legacy = "NPR_LIGHTINFO";
  ntype.nclass = NODE_CLASS_INPUT;
  ntype.declare = file_ns::node_declare;
  ntype.draw_buttons = file_ns::node_shader_buts_light_info;
  ntype.updatefunc = file_ns::node_update;
  ntype.add_ui_poll = object_eevee_shader_nodes_poll;
  ntype.gpu_fn = file_ns::node_shader_gpu_light_info;

  bke::node_register_type(ntype);
}

}  // namespace blender
