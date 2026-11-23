/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup wm
 */

#include <algorithm>
#include <iterator>
#include <string>

#include "BKE_recents.hh"
#include "BKE_wm_runtime.hh"
#include "BLI_listbase.hh"
#include "BLI_string_utf8.hh"
#include "BLI_winstuff.hh"
#include "DNA_windowmanager_types.h"
#include "GHOST_IWindow.hh"
#include "WM_api.hh"
#include "wm_window.hh"

namespace blender {

static std::string monitor_identifier(const MONITORINFOEXW &monitor)
{
  /* Adapter names (DISPLAY1, ...) can be reassigned after rearranging monitors. */
  DISPLAY_DEVICEW device{};
  device.cb = sizeof(device);
  const wchar_t *id = monitor.szDevice;
  for (DWORD i = 0;
       EnumDisplayDevicesW(monitor.szDevice, i, &device, EDD_GET_DEVICE_INTERFACE_NAME);
       i++)
  {
    if ((device.StateFlags & DISPLAY_DEVICE_ACTIVE) && device.DeviceID[0]) {
      id = device.DeviceID;
      break;
    }
  }
  char utf8[4 * std::size(device.DeviceID)];
  BLI_strncpy_wchar_as_utf8(utf8, id, sizeof(utf8));
  return utf8;
}

struct MonitorSearch {
  std::string id;
  HMONITOR match = nullptr;
};

static BOOL CALLBACK find_monitor(HMONITOR handle, HDC, LPRECT, LPARAM data)
{
  MonitorSearch &search = *reinterpret_cast<MonitorSearch *>(data);
  MONITORINFOEXW info{};
  info.cbSize = sizeof(info);
  if (GetMonitorInfoW(handle, &info) && monitor_identifier(info) == search.id) {
    search.match = handle;
    return FALSE;
  }
  return TRUE;
}

/* WM positions use a bottom-left origin; GHOST/Win32 use top-left screen coordinates. */
static RECT startup_rect(const wmWindow &win, const int desktop_height)
{
  const int top = desktop_height - win.posy - win.sizey;
  return {win.posx, top, win.posx + win.sizex, top + win.sizey};
}

void wm_window_monitor_restore(wmWindow &win)
{
  MonitorSearch search{recents::Section("main.window").get<std::string>("monitor")};
  int desktop[2];
  if (search.id.empty() || !wm_get_desktopsize(desktop)) {
    return;
  }
  EnumDisplayMonitors(nullptr, nullptr, find_monitor, reinterpret_cast<LPARAM>(&search));
  HMONITOR target = search.match ? search.match :
                                   MonitorFromPoint({0, 0}, MONITOR_DEFAULTTOPRIMARY);
  const RECT initial = startup_rect(win, desktop[1]);
  if (MonitorFromRect(&initial, MONITOR_DEFAULTTONULL) == target) {
    return;
  }

  MONITORINFO info{};
  info.cbSize = sizeof(info);
  if (!GetMonitorInfoW(target, &info)) {
    return;
  }
  const RECT &work = info.rcWork;
  win.sizex = std::min<LONG>(win.sizex, work.right - work.left);
  win.sizey = std::min<LONG>(win.sizey, work.bottom - work.top);
  win.posx = work.left + (work.right - work.left - win.sizex) / 2;
  win.posy = desktop[1] - (work.top + (work.bottom - work.top - win.sizey) / 2 + win.sizey);
}

void wm_window_monitor_store(wmWindowManager &wm, wmWindow *win)
{
  if (!win) {
    win = wm.runtime->winactive;
  }
  while (win && win->parent) {
    win = win->parent;
  }
  if (!win || !win->runtime->ghostwin || WM_window_is_temp_screen(win)) {
    win = nullptr;
    for (wmWindow &candidate : wm.windows) {
      if (candidate.runtime->ghostwin && !candidate.parent &&
          !WM_window_is_temp_screen(&candidate))
      {
        win = &candidate;
        break;
      }
    }
  }
  if (!win) {
    return;
  }

  const auto *ghost = static_cast<GHOST_IWindow *>(win->runtime->ghostwin);
  RECT rect;
  if (ghost->getState() == GHOST_kWindowStateMinimized) {
    /* Win32 parks minimized windows off-screen. WM retains the last non-minimized bounds. */
    int desktop[2];
    if (!wm_get_desktopsize(desktop)) {
      return;
    }
    rect = startup_rect(*win, desktop[1]);
  }
  else {
    GHOST_Rect bounds;
    ghost->getWindowBounds(bounds);
    rect = {bounds.l_, bounds.t_, bounds.r_, bounds.b_};
  }
  MONITORINFOEXW info{};
  info.cbSize = sizeof(info);
  if (GetMonitorInfoW(MonitorFromRect(&rect, MONITOR_DEFAULTTONEAREST), &info)) {
    recents::Section("main.window").set("monitor", monitor_identifier(info));
  }
}

}  // namespace blender
