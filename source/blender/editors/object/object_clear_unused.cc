/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup edobj
 */

#include "BLI_listbase.hh"
#include "BLI_map.hh"
#include "BLI_set.hh"
#include "BLI_vector.hh"

#include "DNA_collection_types.h"
#include "DNA_image_types.h"
#include "DNA_object_force_types.h"
#include "DNA_object_types.h"
#include "DNA_scene_types.h"

#include "BKE_context.hh"
#include "BKE_image.hh"
#include "BKE_lib_id.hh"
#include "BKE_lib_query.hh"
#include "BKE_main.hh"
#include "BKE_report.hh"

#include "DEG_depsgraph_build.hh"
#include "ED_screen.hh"
#include "RNA_access.hh"
#include "RNA_define.hh"
#include "WM_api.hh"
#include "WM_types.hh"

#include "object_intern.hh"

namespace blender::ed::object {

static bool cleanup_eligible(const ID &id)
{
  return !ID_IS_LINKED(&id) && !ID_IS_OVERRIDE_LIBRARY(&id) && !id.asset_data &&
         !(id.flag & ID_FLAG_FAKEUSER);
}

static int delete_batch(Main &bmain, Set<ID *> &ids)
{
  const int count = ids.size();
  if (count) {
    BKE_id_multi_delete(&bmain, ids);
  }
  return count;
}

static int remove_unused_data(Main &bmain, const short type)
{
  Set<ID *> unused;
  for (ID &id : *which_libbase(&bmain, type)) {
    if (id.us != 0 || !cleanup_eligible(id)) {
      continue;
    }
    if (type == ID_IM) {
      Image &image = *id_cast<Image *>(&id);
      if (image.source == IMA_SRC_VIEWER ||
          ELEM(image.type, IMA_TYPE_R_RESULT, IMA_TYPE_COMPOSITE) || BKE_image_is_dirty(&image))
      {
        continue;
      }
    }
    unused.add(&id);
  }
  if (unused.is_empty()) {
    return 0;
  }
  /* Some real references (e.g. driver targets) do not increment the user count. */
  for (ID &owner : MainAllIDsIterator(bmain)) {
    BKE_library_foreach_ID_link(
        &bmain,
        &owner,
        [](LibraryIDLinkCallbackData *link) {
          if (*link->id_pointer && !(link->cb_flag & IDWALK_CB_LOOPBACK)) {
            static_cast<Set<ID *> *>(link->user_data)->remove(*link->id_pointer);
          }
          return IDWALK_RET_NOP;
        },
        &unused,
        IDWALK_READONLY | IDWALK_INCLUDE_UI);
  }
  return delete_batch(bmain, unused);
}

static bool plain_empty(const Object &ob)
{
  return ob.type == OB_EMPTY && cleanup_eligible(ob.id) && !ob.data &&
         ob.empty_drawtype != OB_EMPTY_IMAGE && !(ob.transflag & OB_DUPLI) &&
         !ob.instance_collection && !ob.rigidbody_constraint && !ob.rigidbody_object &&
         !(ob.pd && (ob.pd->forcefield != PFIELD_NULL || ob.pd->deflect)) && !ob.adt &&
         ob.constraints.is_empty() && ob.modifiers.is_empty() &&
         !(ob.id.properties && ob.id.properties->len) &&
         !(ob.id.system_properties && ob.id.system_properties->len);
}

static Set<Collection *> collection_subtrees(Vector<Collection *> pending)
{
  Set<Collection *> result;
  while (!pending.is_empty()) {
    Collection *collection = pending.pop_last();
    if (result.add(collection)) {
      for (CollectionChild &child : collection->children) {
        pending.append(child.collection);
      }
    }
  }
  return result;
}

struct EmptyReferences {
  Set<Object *> candidates;
  Set<const void *> membership_slots;
  Vector<Collection *> protected_roots;
};

static int check_empty_reference(LibraryIDLinkCallbackData *link)
{
  EmptyReferences &refs = *static_cast<EmptyReferences *>(link->user_data);
  ID *id = *link->id_pointer;
  if (!id || !ELEM(id->id_type(), ID_OB, ID_GR) ||
      (link->cb_flag & (IDWALK_CB_LOOPBACK | IDWALK_CB_EMBEDDED | IDWALK_CB_EMBEDDED_NOT_OWNING)))
  {
    return IDWALK_RET_NOP;
  }
  /* View-layer bases and layer collections mirror scene membership. Other pointers from
   * the same owner, such as a scene custom property or a bake cage, remain real uses. */
  if ((link->self_id->id_type() == ID_SCE && (link->cb_flag & IDWALK_CB_DIRECT_WEAK_LINK)) ||
      refs.membership_slots.contains(link->id_pointer))
  {
    return IDWALK_RET_NOP;
  }
  if (id->id_type() == ID_GR) {
    refs.protected_roots.append(id_cast<Collection *>(id));
  }
  else {
    /* Resolve parent links together, after all non-hierarchy references have been checked. */
    if (link->self_id->id_type() != ID_OB ||
        static_cast<void *>(link->id_pointer) != &id_cast<Object *>(link->self_id)->parent)
    {
      refs.candidates.remove(id_cast<Object *>(id));
    }
  }
  return IDWALK_RET_NOP;
}

static int remove_unused_empties(Main &bmain, Scene &scene)
{
  const Set<Collection *> local = collection_subtrees({scene.master_collection});
  EmptyReferences refs;
  for (Collection *collection : local) {
    for (CollectionObject &member : collection->gobject) {
      if (plain_empty(*member.ob)) {
        refs.candidates.add(member.ob);
      }
    }
  }
  if (refs.candidates.is_empty()) {
    return 0;
  }

  auto visit_collection = [&](Collection &collection) {
    for (CollectionObject &member : collection.gobject) {
      refs.membership_slots.add(&member.ob);
    }
    for (CollectionChild &child : collection.children) {
      refs.membership_slots.add(&child.collection);
    }
    if (!local.contains(&collection) || !cleanup_eligible(collection.id) ||
        !collection.exporters.is_empty() || collection.importer)
    {
      refs.protected_roots.append(&collection);
    }
  };
  for (Collection &collection : bmain.collections) {
    visit_collection(collection);
  }
  for (Scene &other : bmain.scenes) {
    if (other.master_collection) {
      visit_collection(*other.master_collection);
    }
  }
  for (ID &id : MainAllIDsIterator(bmain)) {
    BKE_library_foreach_ID_link(&bmain, &id, check_empty_reference, &refs, IDWALK_READONLY);
  }
  /* An instance or Geometry Nodes may consume an entire collection, including its subtrees. */
  for (Collection *collection : collection_subtrees(std::move(refs.protected_roots))) {
    for (CollectionObject &member : collection->gobject) {
      refs.candidates.remove(member.ob);
    }
  }

  Map<Object *, int> child_counts;
  for (Object &ob : bmain.objects) {
    if (ob.parent && refs.candidates.contains(ob.parent)) {
      child_counts.lookup_or_add(ob.parent, 0)++;
    }
  }
  Vector<Object *> leaves;
  for (Object *ob : refs.candidates) {
    if (!child_counts.contains(ob)) {
      leaves.append(ob);
    }
  }
  Set<ID *> unused;
  while (!leaves.is_empty()) {
    Object *ob = leaves.pop_last();
    unused.add(&ob->id);
    if (int *count = child_counts.lookup_ptr(ob->parent)) {
      if (--(*count) == 0) {
        leaves.append(ob->parent);
      }
    }
  }
  return delete_batch(bmain, unused);
}

static wmOperatorStatus clear_unused_exec(bContext *C, wmOperator *op)
{
  Main &bmain = *CTX_data_main(C);
  const int type = RNA_enum_get(op->ptr, "type");
  int count;
  if (type == ID_OB) {
    count = remove_unused_empties(bmain, *CTX_data_scene(C));
  }
  else if (type == ID_TE) {
    /* Deleting an unused texture may release its image. */
    count = remove_unused_data(bmain, ID_TE);
    count += remove_unused_data(bmain, ID_IM);
  }
  else {
    count = remove_unused_data(bmain, type);
  }
  BKE_reportf(op->reports, RPT_INFO, "Removed %d unused data-block(s)", count);
  if (!count) {
    return OPERATOR_CANCELLED;
  }
  DEG_relations_tag_update(&bmain);
  WM_event_add_notifier(C, NC_ID | NA_REMOVED, nullptr);
  WM_event_add_notifier(C, NC_SCENE | ND_OB_ACTIVE, nullptr);
  return OPERATOR_FINISHED;
}

void OBJECT_OT_clear_unused(wmOperatorType *ot)
{
  static const EnumPropertyItem types[] = {
      {ID_ME, "MESH", 0, "Unused Meshes", "Remove unused mesh data from this file"},
      {ID_TE,
       "IMAGE_TEXTURE",
       0,
       "Unused Images and Textures",
       "Remove unused textures and images from this file"},
      {ID_MA, "MATERIAL", 0, "Unused Materials", "Remove unused materials from this file"},
      {ID_OB,
       "EMPTY",
       0,
       "Unused Empties",
       "Remove unreferenced empty hierarchies from the current scene"},
      {0, nullptr, 0, nullptr, nullptr},
  };
  ot->name = "Clear Unused";
  ot->idname = "OBJECT_OT_clear_unused";
  ot->description = "Clean up unused data of the chosen type";
  ot->poll = ED_operator_objectmode_poll_msg;
  ot->exec = clear_unused_exec;
  ot->flag = OPTYPE_REGISTER | OPTYPE_UNDO;
  ot->prop = RNA_def_enum(ot->srna, "type", types, ID_ME, "Type", "Data to clean up");
}

}  // namespace blender::ed::object
