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
#include "UI_view2d.hh"
#include "WM_types.hh"

namespace blender {

template<eSpace_Type Type> static SpaceLink *panel_space_create(const ScrArea *, const Scene *)
{
  SpaceLink *space = MEM_new<SpaceLink>(__func__);
  space->spacetype = Type;
  ARegion *header = BKE_area_region_new();
  header->regiontype = RGN_TYPE_HEADER;
  header->alignment = (Type == SPACE_WEIGHT_LAYERS || (U.uiflag & USER_HEADER_BOTTOM)) ?
                          RGN_ALIGN_BOTTOM :
                          RGN_ALIGN_TOP;
  BLI_addtail(&space->regionbase, header);
  ARegion *window = BKE_area_region_new();
  window->regiontype = RGN_TYPE_WINDOW;
  window->v2d.scroll = V2D_SCROLL_RIGHT | V2D_SCROLL_VERTICAL_HIDE;
  BLI_addtail(&space->regionbase, window);
  return space;
}

template<eSpace_Type Type> static void register_panel_space(const char *name)
{
  auto st = std::make_unique<SpaceType>();
  st->spaceid = Type;
  STRNCPY(st->name, name);
  st->struct_size = sizeof(SpaceLink);
  st->create = panel_space_create<Type>;
  st->duplicate = [](SpaceLink *space) { return MEM_dupalloc(space); };
  st->blend_write = [](BlendWriter *writer, SpaceLink *space) { writer->write_struct(space); };

  ARegionType *window = MEM_new_zeroed<ARegionType>(__func__);
  window->regionid = RGN_TYPE_WINDOW;
  /* ED_region_panels_init installs View2D Buttons List, where the wheel scrolls panels.
   * The generic View2D map handles the same event as zoom before that map gets it. */
  window->keymapflag = ED_KEYMAP_UI;
  window->init = [](wmWindowManager *wm, ARegion *region) {
    ED_region_panels_init(wm, region);
    if constexpr (Type == SPACE_WEIGHT_LAYERS) {
      region->v2d.keepzoom |= V2D_LOCKZOOM_X | V2D_LOCKZOOM_Y;
      ui::view2d_curRect_validate(&region->v2d);
    }
  };
  window->layout = ED_region_panels_layout;
  window->draw = ED_region_panels_draw;
  window->listener = [](const wmRegionListenerParams *params) {
    ED_region_tag_redraw(params->region);
  };
  BLI_addtail(&st->regiontypes, window);

  ARegionType *header = MEM_new_zeroed<ARegionType>(__func__);
  header->regionid = RGN_TYPE_HEADER;
  header->prefsizey = HEADERY;
  header->keymapflag = ED_KEYMAP_UI | ED_KEYMAP_HEADER | ED_KEYMAP_VIEW2D;
  header->init = [](wmWindowManager *, ARegion *region) { ED_region_header_init(region); };
  header->layout = ED_region_header_layout;
  header->draw = ED_region_header_draw;
  header->listener = window->listener;
  BLI_addtail(&st->regiontypes, header);
  BKE_spacetype_register(std::move(st));
}

void ED_spacetype_weight_layers()
{
  register_panel_space<SPACE_WEIGHT_LAYERS>("Weight Layers");
}

void ED_spacetype_rigging()
{
  register_panel_space<SPACE_RIGGING>("Rigging");
}

}  // namespace blender
