/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once

#include "gpu_shader_material_interface.bsl.hh"

[[node]]
void node_npr_light_info(float4 lamp_color, float lamp_power, float lamp_type,
                         float3 lamp_position, float3 lamp_direction, float lamp_radius,
                         float lamp_spot_size, float lamp_sun_angle, float lamp_visible,
                         float4x4 transform, float4 &color, float &power, int &type,
                         float3 &position, float3 &direction, float &radius,
                         float &spot_size, float &sun_angle, float &visible,
                         float3 &origin, float3 &x_axis, float3 &y_axis, float3 &z_axis)
{
  color = lamp_color;
  power = lamp_power;
  type = int(lamp_type);
  position = lamp_position;
  direction = lamp_direction;
  radius = lamp_radius;
  spot_size = lamp_spot_size;
  sun_angle = lamp_sun_angle;
  visible = lamp_visible;
  origin = transform[3].xyz;
  x_axis = transform[0].xyz;
  y_axis = transform[1].xyz;
  z_axis = transform[2].xyz;
}
