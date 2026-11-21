/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include "BKE_screen.hh"
#include "BLI_listbase.hh"
#include "BLI_string.hh"
#include "BLO_read_write.hh"
#include "DNA_space_types.h"
#include "ED_screen.hh"
#include "ED_space_api.hh"
#include "MEM_guardedalloc.h"
#include "UI_resources.hh"
#include "UI_view2d.hh"
#include "WM_api.hh"
#include "WM_types.hh"

namespace blender {

static SpaceLink *selector_create(const ScrArea *, const Scene *)
{
  SpaceSelector *space = MEM_new<SpaceSelector>(__func__);
  space->spacetype = SPACE_SELECTOR;
  ARegion *header = BKE_area_region_new();
  header->regiontype = RGN_TYPE_HEADER;
  header->alignment = (U.uiflag & USER_HEADER_BOTTOM) ? RGN_ALIGN_BOTTOM : RGN_ALIGN_TOP;
  BLI_addtail(&space->regionbase, header);
  ARegion *window = BKE_area_region_new();
  window->regiontype = RGN_TYPE_WINDOW;
  BLI_addtail(&space->regionbase, window);
  return reinterpret_cast<SpaceLink *>(space);
}

static void selector_listener(const wmRegionListenerParams *params)
{
  switch (params->notifier->category) {
    case NC_SCENE:
    case NC_OBJECT:
    case NC_GEOM:
    case NC_ANIMATION:
    case NC_SPACE:
    case NC_ID:
    case NC_SCREEN:
    case NC_WINDOW:
      ED_region_tag_redraw(params->region);
      break;
  }
}

void ED_spacetype_selector()
{
  auto st = std::make_unique<SpaceType>();
  st->spaceid = SPACE_SELECTOR;
  STRNCPY(st->name, "Selector");
  st->struct_size = sizeof(SpaceSelector);
  st->create = selector_create;
  st->duplicate = [](SpaceLink *space) {
    return reinterpret_cast<SpaceLink *>(MEM_dupalloc(reinterpret_cast<SpaceSelector *>(space)));
  };
  st->blend_write = [](BlendWriter *writer, SpaceLink *space) {
    writer->write_struct_cast<SpaceSelector>(space);
  };
  st->keymap = [](wmKeyConfig *config) {
    WM_keymap_ensure(config, "Selector", SPACE_SELECTOR, RGN_TYPE_WINDOW);
  };

  ARegionType *window = MEM_new_zeroed<ARegionType>(__func__);
  window->regionid = RGN_TYPE_WINDOW;
  window->keymapflag = ED_KEYMAP_UI;
  window->init = [](wmWindowManager *wm, ARegion *region) {
    wmKeyMap *keymap = WM_keymap_ensure(
        wm->runtime->defaultconf, "Selector", SPACE_SELECTOR, RGN_TYPE_WINDOW);
    WM_event_add_keymap_handler(&region->runtime->handlers, keymap);
  };
  window->draw = [](const bContext *, ARegion *) { ui::theme::frame_buffer_clear(TH_BACK); };
  window->listener = selector_listener;
  BLI_addtail(&st->regiontypes, window);

  ARegionType *header = MEM_new_zeroed<ARegionType>(__func__);
  header->regionid = RGN_TYPE_HEADER;
  header->prefsizey = HEADERY;
  header->keymapflag = ED_KEYMAP_UI | ED_KEYMAP_HEADER | ED_KEYMAP_VIEW2D;
  header->init = [](wmWindowManager *, ARegion *region) { ED_region_header_init(region); };
  header->layout = ED_region_header_layout;
  header->draw = ED_region_header_draw;
  header->listener = selector_listener;
  BLI_addtail(&st->regiontypes, header);
  BKE_spacetype_register(std::move(st));
}

}  // namespace blender
