# SPDX-License-Identifier: GPL-2.0-or-later
"""mGear picker geometry and XBone control bindings, shared by drawing and picking."""
from dataclasses import dataclass
from functools import lru_cache
import json
import math
from pathlib import Path

from mathutils import Vector
from mathutils.geometry import tessellate_polygon

BOARD_KEY = "xbone_selector_layout"


@lru_cache(maxsize=1)
def stock():
    return Path(__file__).with_name("biped.pkr").read_text(encoding="utf8")


@lru_cache(maxsize=1)
def xbiped_layout():
    layout = json.loads(stock())
    items = layout["tabs"][0]["data"]["items"]
    for side, x, color in (("L0", 115, [60, 173, 224, 255]), ("R0", -115, [70, 164, 80, 255])):
        items.append(dict(position=[x, -120], handles=[[-28, -9], [28, -9], [28, 9], [-28, 9]],
                          controls=["FK.leg_" + side + "_knee_bend"], text="Knee FK",
                          text_size=11, color=color))
    return json.dumps(layout, separators=(",", ":"))


@lru_cache(maxsize=1)
def maya_controls():
    with Path(__file__).with_name("biped_display.json").open(encoding="utf8") as stream:
        return {key: (value["role"], value["anchor"]) for key, value in json.load(stream)["controls"].items()}


def bindings(rig, names):
    """Resolve Maya control IDs using the original guide identity, also on older rigs."""
    pose = rig.pose.bones
    anchors = None
    result = {}
    reference = maya_controls()
    for name in names:
        short = name.rsplit("|", 1)[-1].rsplit(":", 1)[-1]
        bone = pose.get(name) or pose.get(short)
        if not bone and short in reference:
            role, label = reference[short]
            if role == "display":
                target = "Display." + short
            else:
                origin = rig.data.bones.get(label)
                if not origin or origin.get("biped_locator", label) != label:
                    if anchors is None:
                        anchors = {b["biped_locator"]: b.name for b in rig.data.bones if "biped_locator" in b}
                    label = anchors.get(label, label)
                target = role + "." + label
            bone = pose.get(target)
        if bone and not bone.bone.hide_select:
            result[name] = bone
    return result


def control_names(rig):
    shaped = tuple(p.name for p in rig.pose.bones if p.custom_shape and not p.bone.hide_select)
    return shaped or tuple(p.name for p in rig.pose.bones if not p.bone.hide_select)


@lru_cache(maxsize=16)
def custom_layout(names):
    items = []
    columns = max(1, math.ceil(math.sqrt(len(names) / 3)))
    for index, name in enumerate(names):
        x, y = index % columns * 160, -(index // columns) * 32
        items.append(dict(position=[x, y], handles=[[-77, -14], [77, -14], [77, 14], [-77, 14]],
                          controls=[name], text=name, text_size=11, color=[68, 110, 140, 255]))
    return json.dumps({"tabs": [{"name": "Controls", "data": {"items": items}}]})


def layout_source(rig):
    if BOARD_KEY in rig:
        return rig[BOARD_KEY]
    if "FK.leg_L0_knee_bend" in rig.pose.bones and "FK.leg_R0_knee_bend" in rig.pose.bones:
        return xbiped_layout()
    if "Display.spineUI_C0_ctl" in rig.pose.bones:
        return stock()
    return custom_layout(control_names(rig))


def point(value):
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError("Expected a 2D picker coordinate")
    x, y = map(float, value)
    if not all(math.isfinite(n) and abs(n) <= 1e7 for n in (x, y)):
        raise ValueError("Invalid picker coordinate")
    return x, y


def rgba(value):
    if len(value) not in (3, 4):
        raise ValueError("Expected RGB or RGBA color")
    return tuple(max(0, min(255, int(c))) / 255 for c in value) + ((1,) if len(value) == 3 else ())


@dataclass
class Button:
    points: tuple
    triangles: tuple
    bounds: tuple
    center: tuple
    color: tuple
    text: str
    text_color: tuple
    size: float
    controls: tuple
    script: str
    action: bool

    def contains(self, x, y):
        left, bottom, right, top = self.bounds
        if not (left <= x <= right and bottom <= y <= top):
            return False
        inside = False
        ax, ay = self.points[-1]
        for bx, by in self.points:
            if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
                inside = not inside
            ax, ay = bx, by
        return inside


def button(item):
    x, y = point(item.get("position", (0, 0)))
    handles = [point(h) for h in item["handles"]]
    if len(handles) == 2:
        (cx, cy), end = handles
        r = math.dist((cx, cy), end)
        handles = [(cx + r * math.cos(i * math.tau / 32), cy + r * math.sin(i * math.tau / 32)) for i in range(32)]
    if len(handles) < 3:
        raise ValueError("A picker shape needs two circle handles or a polygon")
    angle = math.radians(float(item.get("rotation", 0)))
    c, s = math.cos(angle), math.sin(angle)
    pts = tuple((x + c * a - s * b, y + s * a + c * b) for a, b in handles)
    xs, ys = zip(*pts)
    bounds = min(xs), min(ys), max(xs), max(ys)
    triangles = tuple(i for triangle in tessellate_polygon([[Vector((a, b, 0)) for a, b in pts]]) for i in triangle)
    names = item.get("controls", ())
    if not isinstance(names, (list, tuple)) or not all(isinstance(n, str) for n in names):
        raise ValueError("Invalid picker controls")
    size = float(item.get("text_size", 12))
    if not math.isfinite(size):
        raise ValueError("Invalid picker text size")
    return Button(pts, triangles, bounds, (x, y), rgba(item.get("color", (90, 110, 130, 255))),
                  str(item.get("text", "")), rgba(item.get("text_color", (255, 255, 255, 255))),
                  max(1, min(size, 200)), tuple(names), str(item.get("action_script", "")),
                  bool(item.get("action_mode")))


@dataclass
class Page:
    name: str
    buttons: tuple
    bounds: tuple
    names: tuple
    backgrounds: tuple


@lru_cache(maxsize=16)
def pages(source):
    result = []
    for tab in json.loads(source)["tabs"]:
        buttons = tuple(button(item) for item in tab["data"]["items"])
        bounds = tuple(f(b.bounds[i] for b in buttons)
                       for i, f in enumerate((min, min, max, max))) if buttons else (-100, -100, 100, 100)
        names = tuple(dict.fromkeys(n for b in buttons for n in b.controls))
        data = tab["data"]
        layers = data.get("backgrounds", ())
        if not layers and data.get("background"):
            layers = [{"path": data["background"], "size": data.get("background_size", (0, 0))}]
        backgrounds = tuple((f"{len(result)}_{i}", str(layer["path"]), point(layer.get("position", (0, 0))),
                             point(layer.get("size", (0, 0)))) for i, layer in enumerate(layers))
        result.append(Page(str(tab["name"]), buttons, bounds, names, backgrounds))
    if not result:
        raise ValueError("Picker has no pages")
    return tuple(result)


def current_page(rig, space):
    board = pages(layout_source(rig))
    return board[min(space.page, len(board) - 1)]


def background_rects(page, images):
    for slot, _, (x, y), (w, h) in page.backgrounds:
        image = images.get(slot)
        if image:
            w, h = (w or image.size[0]), (h or image.size[1])
            yield image, (x-w/2, y-h/2, x+w/2, y+h/2)


def view(page, space, region, images=None):
    x0, y0, x1, y1 = page.bounds
    for _, (left, bottom, right, top) in background_rects(page, images or {}):
        x0, y0, x1, y1 = min(x0, left), min(y0, bottom), max(x1, right), max(y1, top)
    scale = max(.001, min(max(1, region.width - 24) / max(1, x1 - x0),
                          max(1, region.height - 24) / max(1, y1 - y0))) * space.zoom
    ox = region.width / 2 + (space.pan[0] - (x0 + x1) / 2) * scale
    oy = region.height / 2 + (space.pan[1] - (y0 + y1) / 2) * scale
    return scale, ox, oy


def board_point(viewport, x, y):
    scale, ox, oy = viewport
    return (x - ox) / scale, (y - oy) / scale
