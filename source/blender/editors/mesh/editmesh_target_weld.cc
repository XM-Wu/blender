/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup edmesh
 *
 * Maya-style target weld.
 *
 * Vertex mode welds the dragged vertex onto the vertex under the cursor.
 * The target vertex keeps its position.
 *
 * Edge mode welds the dragged edge onto another edge. Endpoints are paired so a
 * vertex that is already shared stays put, otherwise the pairing with the shorter
 * total travel is used. That avoids twisting the edge when the two are roughly
 * parallel. Each source vertex is then welded onto its paired target vertex, so
 * the source edge collapses onto the target edge and the target edge stays.
 */

#include "MEM_guardedalloc.h"

#include "DNA_mesh_types.h"
#include "DNA_object_types.h"
#include "DNA_scene_types.h"
#include "DNA_userdef_types.h"

#include "BLI_math_base_c.hh"
#include "BLI_math_matrix_c.hh"
#include "BLI_math_vector_c.hh"

#include "BKE_context.hh"
#include "BKE_editmesh.hh"
#include "BKE_report.hh"
#include "BKE_screen.hh"

#include "BLT_translation.hh"

#include "GPU_immediate.hh"
#include "GPU_matrix.hh"
#include "GPU_state.hh"

#include "ED_gizmo_utils.hh"
#include "ED_mesh.hh"
#include "ED_screen.hh"
#include "ED_space_api.hh"
#include "ED_view3d.hh"

#include "WM_api.hh"
#include "WM_types.hh"

#include "bmesh.hh"

#include "mesh_intern.hh"

namespace blender {

static bool g_target_weld_modal = false;

static void VIEW3D_GT_target_weld(wmGizmoType *gzt);
static void VIEW3D_GGT_target_weld(wmGizmoGroupType *gzgt);

static const float g_target_weld_col_hover[4] = {1.0f, 0.78f, 0.2f, 0.95f};
static const float g_target_weld_col_source[4] = {1.0f, 0.42f, 0.08f, 1.0f};
static const float g_target_weld_col_target[4] = {0.15f, 0.9f, 0.4f, 1.0f};
static const float g_target_weld_col_guide[4] = {0.7f, 0.9f, 1.0f, 0.9f};

struct TargetWeldPick {
  Object *ob = nullptr;
  BMVert *vert = nullptr;
  BMEdge *edge = nullptr;
};

struct TargetWeldEdgePlan {
  BMVert *src[2] = {nullptr, nullptr};
  BMVert *dst[2] = {nullptr, nullptr};
  int count = 0;
};

struct TargetWeldOp {
  void *draw_handle = nullptr;
  ARegion *region = nullptr;
  Object *ob = nullptr;
  short press_type = 0;
  BMVert *src_vert = nullptr;
  BMEdge *src_edge = nullptr;
  BMVert *dst_vert = nullptr;
  BMEdge *dst_edge = nullptr;
  TargetWeldEdgePlan plan;
};

struct TargetWeldGizmo {
  wmGizmo gizmo;
  float matrix[4][4];
  float points[2][3];
  int point_count;
  bool active;
};

/** Hide one element for the duration of a nearest-element query, then restore it. */
struct TargetWeldHideRestore {
  BMElem *elem = nullptr;
  bool restore = false;

  explicit TargetWeldHideRestore(BMElem *elem_in) : elem(elem_in)
  {
    if (elem != nullptr && !BM_elem_flag_test(elem, BM_ELEM_HIDDEN)) {
      BM_elem_flag_enable(elem, BM_ELEM_HIDDEN);
      restore = true;
    }
  }

  ~TargetWeldHideRestore()
  {
    if (restore) {
      BM_elem_flag_disable(elem, BM_ELEM_HIDDEN);
    }
  }
};

static void target_weld_select_flags(const Scene *scene, bool *r_vert, bool *r_edge)
{
  const short selectmode = scene->toolsettings->selectmode;
  *r_vert = (selectmode & SCE_SELECT_VERTEX) != 0;
  *r_edge = (selectmode & SCE_SELECT_EDGE) != 0;
}

static ViewContext target_weld_viewcontext(bContext *C, const int mval[2])
{
  /* `ED_view3d_viewcontext_init` leaves `em` null, so nearest-element queries never hit. */
  ViewContext vc = em_setup_viewcontext(C);
  vc.mval[0] = mval[0];
  vc.mval[1] = mval[1];
  return vc;
}

static TargetWeldPick target_weld_pick(bContext *C,
                                       const int mval[2],
                                       BMVert *ignore_vert,
                                       BMEdge *ignore_edge)
{
  TargetWeldPick pick;
  /* Operator events do not set up the selection buffer's GL context. */
  view3d_operator_needs_gpu(C);

  Scene *scene = CTX_data_scene(C);
  bool want_vert = false;
  bool want_edge = false;
  target_weld_select_flags(scene, &want_vert, &want_edge);
  if (ignore_vert != nullptr) {
    want_edge = false;
  }
  else if (ignore_edge != nullptr) {
    want_vert = false;
  }
  if (!want_vert && !want_edge) {
    return pick;
  }

  ViewContext vc = target_weld_viewcontext(C, mval);
  if (vc.obedit == nullptr || vc.em == nullptr) {
    return pick;
  }

  const float dist_init = ED_view3d_select_dist_px();
  float dist_vert = dist_init;
  float dist_edge = dist_init;
  BMVert *vert = nullptr;
  BMEdge *edge = nullptr;

  if (want_vert) {
    TargetWeldHideRestore hide(reinterpret_cast<BMElem *>(ignore_vert));
    vert = EDBM_vert_find_nearest(&vc, &dist_vert);
  }
  if (want_edge) {
    TargetWeldHideRestore hide(reinterpret_cast<BMElem *>(ignore_edge));
    edge = EDBM_edge_find_nearest(&vc, &dist_edge);
  }

  /* Both select modes: the component closer to the cursor wins. */
  if (vert != nullptr && edge != nullptr) {
    if (dist_edge < dist_vert) {
      vert = nullptr;
    }
    else {
      edge = nullptr;
    }
  }

  pick.ob = vc.obedit;
  pick.vert = vert;
  pick.edge = edge;
  return pick;
}

/**
 * Pair source-edge endpoints onto the target edge.
 * A shared vertex is kept fixed. Otherwise the shorter pairing wins, which keeps
 * parallel edges from flipping. A vertex is not mapped onto itself, and a source
 * that is already someone else's destination is skipped so shared corners do not chain.
 */
static TargetWeldEdgePlan target_weld_edge_plan(BMEdge *src, BMEdge *dst)
{
  TargetWeldEdgePlan plan;
  if (src == nullptr || dst == nullptr || src == dst) {
    return plan;
  }

  BMVert *s0 = src->v1;
  BMVert *s1 = src->v2;

  auto score = [](BMVert *a, BMVert *da, BMVert *b, BMVert *db) {
    struct Score {
      float cost;
      int identities;
    };
    return Score{len_squared_v3v3(a->co, da->co) + len_squared_v3v3(b->co, db->co),
                 (a == da ? 1 : 0) + (b == db ? 1 : 0)};
  };

  const auto direct = score(s0, dst->v1, s1, dst->v2);
  const auto flipped = score(s0, dst->v2, s1, dst->v1);
  const bool use_flip = (flipped.identities > direct.identities) ||
                        (flipped.identities == direct.identities && flipped.cost < direct.cost);
  BMVert *d0 = use_flip ? dst->v2 : dst->v1;
  BMVert *d1 = use_flip ? dst->v1 : dst->v2;

  auto push = [&](BMVert *s, BMVert *d) {
    if (s == nullptr || d == nullptr || s == d || plan.count >= 2) {
      return;
    }
    for (int i = 0; i < plan.count; i++) {
      if (plan.dst[i] == s || plan.src[i] == d) {
        return;
      }
    }
    plan.src[plan.count] = s;
    plan.dst[plan.count] = d;
    plan.count++;
  };
  push(s0, d0);
  push(s1, d1);
  return plan;
}

static void target_weld_draw_elements(const float matrix[4][4],
                                      const float (*source)[3],
                                      const int source_len,
                                      const float source_color[4],
                                      const float (*target)[3],
                                      const int target_len,
                                      const float (*guides)[2][3],
                                      const int guide_len)
{
  if (source_len <= 0 && target_len <= 0 && guide_len <= 0) {
    return;
  }

  const float px = max_ff(U.pixelsize, 1.0f);
  GPU_depth_test(GPU_DEPTH_NONE);
  GPU_blend(GPU_BLEND_ALPHA);

  GPU_matrix_push();
  GPU_matrix_mul(matrix);

  uint pos = GPU_vertformat_attr_add(immVertexFormat(), "pos", gpu::VertAttrType::SFLOAT_32_32_32);
  immBindBuiltinProgram(GPU_SHADER_3D_UNIFORM_COLOR);

  if (guide_len > 0) {
    GPU_line_width(1.5f * px);
    immUniformColor4fv(g_target_weld_col_guide);
    immBegin(GPU_PRIM_LINES, guide_len * 2);
    for (int i = 0; i < guide_len; i++) {
      immVertex3fv(pos, guides[i][0]);
      immVertex3fv(pos, guides[i][1]);
    }
    immEnd();
  }

  auto draw_line = [&](const float (*points)[3], const int len, const float color[4], float width) {
    if (len < 2) {
      return;
    }
    GPU_line_width(width);
    immUniformColor4fv(color);
    immBegin(GPU_PRIM_LINES, 2);
    immVertex3fv(pos, points[0]);
    immVertex3fv(pos, points[1]);
    immEnd();
  };
  draw_line(source, source_len, source_color, 3.0f * px);
  draw_line(target, target_len, g_target_weld_col_target, 4.0f * px);
  immUnbindProgram();

  auto draw_points = [&](const float (*points)[3], const int len, const float color[4], float size) {
    if (len <= 0) {
      return;
    }
    immBindBuiltinProgram(GPU_SHADER_3D_POINT_UNIFORM_COLOR);
    GPU_point_size(size);
    immUniformColor4fv(color);
    immBegin(GPU_PRIM_POINTS, len);
    for (int i = 0; i < len; i++) {
      immVertex3fv(pos, points[i]);
    }
    immEnd();
    immUnbindProgram();
  };
  draw_points(source, source_len, source_color, 9.0f * px);
  draw_points(target, target_len, g_target_weld_col_target, 11.0f * px);

  GPU_matrix_pop();
  GPU_blend(GPU_BLEND_NONE);
  GPU_depth_test(GPU_DEPTH_LESS_EQUAL);
}

static void target_weld_op_draw(const bContext * /*C*/, ARegion * /*region*/, void *arg)
{
  const TargetWeldOp *data = static_cast<const TargetWeldOp *>(arg);
  if (data->ob == nullptr) {
    return;
  }

  float source[2][3];
  float target[2][3];
  float guides[2][2][3];
  int source_len = 0;
  int target_len = 0;
  int guide_len = 0;

  if (data->src_vert != nullptr) {
    copy_v3_v3(source[0], data->src_vert->co);
    source_len = 1;
    if (data->dst_vert != nullptr) {
      copy_v3_v3(target[0], data->dst_vert->co);
      target_len = 1;
      copy_v3_v3(guides[0][0], data->src_vert->co);
      copy_v3_v3(guides[0][1], data->dst_vert->co);
      guide_len = 1;
    }
  }
  else if (data->src_edge != nullptr) {
    copy_v3_v3(source[0], data->src_edge->v1->co);
    copy_v3_v3(source[1], data->src_edge->v2->co);
    source_len = 2;
    if (data->dst_edge != nullptr && data->plan.count > 0) {
      copy_v3_v3(target[0], data->dst_edge->v1->co);
      copy_v3_v3(target[1], data->dst_edge->v2->co);
      target_len = 2;
      for (int i = 0; i < data->plan.count; i++) {
        copy_v3_v3(guides[i][0], data->plan.src[i]->co);
        copy_v3_v3(guides[i][1], data->plan.dst[i]->co);
      }
      guide_len = data->plan.count;
    }
  }

  target_weld_draw_elements(data->ob->object_to_world().ptr(),
                            source,
                            source_len,
                            g_target_weld_col_source,
                            target,
                            target_len,
                            guides,
                            guide_len);
}

static void target_weld_status(bContext *C, const TargetWeldOp *data)
{
  const char *text = TIP_("Drag onto a vertex or edge");
  if (data->src_vert != nullptr) {
    text = (data->dst_vert != nullptr) ? TIP_("Release to weld vertex") :
                                         TIP_("Drag onto a vertex");
  }
  else if (data->src_edge != nullptr) {
    text = (data->dst_edge != nullptr && data->plan.count > 0) ? TIP_("Release to weld edge") :
                                                                 TIP_("Drag onto an edge");
  }
  ED_workspace_status_text(C, text);
}

static void target_weld_exit(bContext *C, wmOperator *op)
{
  TargetWeldOp *data = static_cast<TargetWeldOp *>(op->customdata);
  g_target_weld_modal = false;
  if (data == nullptr) {
    return;
  }
  if (data->draw_handle != nullptr && data->region != nullptr && data->region->runtime->type) {
    ED_region_draw_cb_exit(data->region->runtime->type, data->draw_handle);
  }
  ED_workspace_status_text(C, nullptr);
  if (data->region != nullptr) {
    ED_region_tag_redraw(data->region);
  }
  WM_cursor_modal_restore(CTX_wm_window(C));
  MEM_delete(data);
  op->customdata = nullptr;
}

static bool target_weld_apply(Object *ob,
                              BMVert **src,
                              BMVert **dst,
                              int count,
                              BMVert *edge_v1,
                              BMVert *edge_v2,
                              wmOperator *op)
{
  if (ob == nullptr || count <= 0) {
    return false;
  }

  BMesh *bm = BKE_editmesh_bmesh_get_for_write(ob);
  BMOperator bmop;
  if (!EDBM_op_init(bm, &bmop, op, "weld_verts")) {
    return false;
  }

  BMOpSlot *slot = BMO_slot_get(bmop.slots_in, "targetmap");
  int mapped = 0;
  for (int i = 0; i < count; i++) {
    if (src[i] == nullptr || dst[i] == nullptr || src[i] == dst[i]) {
      continue;
    }
    BMO_slot_map_elem_insert(&bmop, slot, src[i], dst[i]);
    mapped++;
  }
  if (mapped == 0) {
    BMO_op_finish(bm, &bmop);
    return false;
  }

  BMO_op_exec(bm, &bmop);
  if (!EDBM_op_finish(bm, &bmop, op, true)) {
    return false;
  }

  for (int i = 0; i < count; i++) {
    if (dst[i] != nullptr) {
      BM_elem_flag_enable(dst[i], BM_ELEM_SELECT);
    }
  }
  if (edge_v1 != nullptr && edge_v2 != nullptr) {
    if (BMEdge *surviving = BM_edge_exists(edge_v1, edge_v2)) {
      BM_elem_flag_enable(surviving, BM_ELEM_SELECT);
    }
  }
  BM_mesh_select_mode_flush(bm);

  EDBMUpdate_Params params{};
  params.calc_looptris = true;
  params.calc_normals = true;
  params.is_destructive = true;
  EDBM_update(id_cast<Mesh *>(ob->data), &params);
  return true;
}

static void target_weld_update_target(bContext *C, TargetWeldOp *data, const int mval[2])
{
  data->dst_vert = nullptr;
  data->dst_edge = nullptr;
  data->plan = {};

  TargetWeldPick pick = target_weld_pick(C, mval, data->src_vert, data->src_edge);
  if (pick.ob != data->ob) {
    return;
  }
  if (data->src_vert != nullptr) {
    if (pick.vert != nullptr && pick.vert != data->src_vert) {
      data->dst_vert = pick.vert;
    }
  }
  else if (data->src_edge != nullptr && pick.edge != nullptr && pick.edge != data->src_edge) {
    TargetWeldEdgePlan plan = target_weld_edge_plan(data->src_edge, pick.edge);
    if (plan.count > 0) {
      data->dst_edge = pick.edge;
      data->plan = plan;
    }
  }
}

static wmOperatorStatus target_weld_modal(bContext *C, wmOperator *op, const wmEvent *event)
{
  TargetWeldOp *data = static_cast<TargetWeldOp *>(op->customdata);
  if (data == nullptr) {
    return OPERATOR_CANCELLED;
  }

  if (event->type == EVT_ESCKEY || (event->type == RIGHTMOUSE && event->val == KM_PRESS)) {
    target_weld_exit(C, op);
    return OPERATOR_CANCELLED;
  }

  if (event->type == MOUSEMOVE) {
    target_weld_update_target(C, data, event->mval);
    target_weld_status(C, data);
    ED_region_tag_redraw(data->region);
    return OPERATOR_RUNNING_MODAL;
  }

  if (event->type == data->press_type && event->val == KM_RELEASE) {
    target_weld_update_target(C, data, event->mval);
    Object *ob = data->ob;
    BMVert *src_verts[2];
    BMVert *dst_verts[2];
    int count = 0;
    BMVert *edge_ends[2] = {nullptr, nullptr};

    if (data->src_vert != nullptr && data->dst_vert != nullptr) {
      src_verts[0] = data->src_vert;
      dst_verts[0] = data->dst_vert;
      count = 1;
    }
    else if (data->src_edge != nullptr && data->dst_edge != nullptr && data->plan.count > 0) {
      edge_ends[0] = data->dst_edge->v1;
      edge_ends[1] = data->dst_edge->v2;
      count = data->plan.count;
      for (int i = 0; i < count; i++) {
        src_verts[i] = data->plan.src[i];
        dst_verts[i] = data->plan.dst[i];
      }
    }

    target_weld_exit(C, op);

    if (count == 0) {
      return OPERATOR_CANCELLED;
    }
    if (!target_weld_apply(ob, src_verts, dst_verts, count, edge_ends[0], edge_ends[1], op)) {
      return OPERATOR_CANCELLED;
    }
    return OPERATOR_FINISHED;
  }

  return OPERATOR_PASS_THROUGH;
}

static wmOperatorStatus target_weld_invoke(bContext *C, wmOperator *op, const wmEvent *event)
{
  bool want_vert = false;
  bool want_edge = false;
  target_weld_select_flags(CTX_data_scene(C), &want_vert, &want_edge);
  if (!want_vert && !want_edge) {
    BKE_report(op->reports, RPT_WARNING, "Target Weld requires vertex or edge select mode");
    return OPERATOR_CANCELLED;
  }

  ARegion *region = CTX_wm_region(C);
  if (region == nullptr) {
    return OPERATOR_CANCELLED;
  }

  TargetWeldPick pick = target_weld_pick(C, event->mval, nullptr, nullptr);
  if (pick.ob == nullptr || (pick.vert == nullptr && pick.edge == nullptr)) {
    return OPERATOR_CANCELLED;
  }

  TargetWeldOp *data = MEM_new<TargetWeldOp>(__func__);
  data->region = region;
  data->ob = pick.ob;
  data->press_type = event->type;
  data->src_vert = pick.vert;
  data->src_edge = pick.edge;
  op->customdata = data;
  g_target_weld_modal = true;

  if (region->runtime->type) {
    data->draw_handle = ED_region_draw_cb_activate(
        region->runtime->type, target_weld_op_draw, data, REGION_DRAW_POST_VIEW);
  }

  WM_cursor_modal_set(CTX_wm_window(C), WM_CURSOR_CROSS);
  target_weld_status(C, data);
  ED_region_tag_redraw(region);
  WM_event_add_modal_handler(C, op);
  return OPERATOR_RUNNING_MODAL;
}

static void target_weld_cancel(bContext *C, wmOperator *op)
{
  target_weld_exit(C, op);
}

static bool target_weld_poll(bContext *C)
{
  return ED_operator_editmesh(C);
}

void MESH_OT_target_weld(wmOperatorType *ot)
{
  ot->name = "Target Weld";
  ot->idname = "MESH_OT_target_weld";
  ot->description = "Drag a vertex or edge onto another to weld it. The target keeps its position";

  ot->invoke = target_weld_invoke;
  ot->modal = target_weld_modal;
  ot->cancel = target_weld_cancel;
  ot->poll = target_weld_poll;

  ot->flag = OPTYPE_UNDO | OPTYPE_BLOCKING;

  WM_gizmotype_append(VIEW3D_GT_target_weld);
  WM_gizmogrouptype_append(VIEW3D_GGT_target_weld);
}

/* -------------------------------------------------------------------- */
/** \name Hover highlight
 * \{ */

static void target_weld_gizmo_draw(const bContext * /*C*/, wmGizmo *gz)
{
  TargetWeldGizmo *gz_weld = reinterpret_cast<TargetWeldGizmo *>(gz);
  if (g_target_weld_modal || !gz_weld->active || gz_weld->point_count <= 0) {
    return;
  }
  target_weld_draw_elements(gz_weld->matrix,
                            gz_weld->points,
                            gz_weld->point_count,
                            g_target_weld_col_hover,
                            nullptr,
                            0,
                            nullptr,
                            0);
}

static int target_weld_gizmo_test_select(bContext *C, wmGizmo *gz, const int mval[2])
{
  TargetWeldGizmo *gz_weld = reinterpret_cast<TargetWeldGizmo *>(gz);
  const int prev_count = gz_weld->point_count;
  float prev_co[2][3];
  if (prev_count > 0) {
    copy_v3_v3(prev_co[0], gz_weld->points[0]);
  }
  if (prev_count > 1) {
    copy_v3_v3(prev_co[1], gz_weld->points[1]);
  }

  gz_weld->active = false;
  gz_weld->point_count = 0;
  if (!g_target_weld_modal) {
    TargetWeldPick pick = target_weld_pick(C, mval, nullptr, nullptr);
    if (pick.ob != nullptr && pick.vert != nullptr) {
      copy_m4_m4(gz_weld->matrix, pick.ob->object_to_world().ptr());
      copy_v3_v3(gz_weld->points[0], pick.vert->co);
      gz_weld->point_count = 1;
      gz_weld->active = true;
    }
    else if (pick.ob != nullptr && pick.edge != nullptr) {
      copy_m4_m4(gz_weld->matrix, pick.ob->object_to_world().ptr());
      copy_v3_v3(gz_weld->points[0], pick.edge->v1->co);
      copy_v3_v3(gz_weld->points[1], pick.edge->v2->co);
      gz_weld->point_count = 2;
      gz_weld->active = true;
    }
  }

  bool changed = gz_weld->point_count != prev_count;
  if (!changed && gz_weld->point_count > 0) {
    changed = !equals_v3v3(prev_co[0], gz_weld->points[0]);
  }
  if (!changed && prev_count > 1 && gz_weld->point_count > 1) {
    changed = !equals_v3v3(prev_co[1], gz_weld->points[1]);
  }
  if (changed) {
    ARegion *region = CTX_wm_region(C);
    if (region != nullptr) {
      ED_region_tag_redraw(region);
    }
  }
  return gz_weld->active ? 0 : -1;
}

static void target_weld_gizmo_setup(wmGizmo *gz)
{
  gz->flag |= WM_GIZMO_HIDDEN_KEYMAP;
  TargetWeldGizmo *gz_weld = reinterpret_cast<TargetWeldGizmo *>(gz);
  gz_weld->active = false;
  gz_weld->point_count = 0;
  unit_m4(gz_weld->matrix);
}

static void VIEW3D_GT_target_weld(wmGizmoType *gzt)
{
  gzt->idname = "VIEW3D_GT_target_weld";
  gzt->draw = target_weld_gizmo_draw;
  gzt->test_select = target_weld_gizmo_test_select;
  gzt->setup = target_weld_gizmo_setup;
  gzt->struct_size = sizeof(TargetWeldGizmo);
}

static void target_weld_gzgroup_setup(const bContext * /*C*/, wmGizmoGroup *gzgroup)
{
  const wmGizmoType *gzt = WM_gizmotype_find("VIEW3D_GT_target_weld", true);
  WM_gizmo_new_ptr(gzt, gzgroup, nullptr);
}

static void VIEW3D_GGT_target_weld(wmGizmoGroupType *gzgt)
{
  gzgt->name = "Target Weld Hover";
  gzgt->idname = "VIEW3D_GGT_target_weld";
  gzgt->flag = WM_GIZMOGROUPTYPE_3D | WM_GIZMOGROUPTYPE_TOOL_FALLBACK_KEYMAP;
  gzgt->gzmap_params.spaceid = SPACE_VIEW3D;
  gzgt->gzmap_params.regionid = RGN_TYPE_WINDOW;
  gzgt->poll = ED_gizmo_poll_or_unlink_delayed_from_tool;
  gzgt->setup = target_weld_gzgroup_setup;
}

/** \} */

}  // namespace blender
