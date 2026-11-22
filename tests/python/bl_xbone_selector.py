# SPDX-License-Identifier: GPL-2.0-or-later
"""Run with the XBone Blender executable in background mode."""
import json
import tempfile
from pathlib import Path

import bpy
from xbone_rigging import selector, selector_board as board

bpy.ops.wm.read_factory_settings(use_empty=True)
assert bpy.ops.xbone.biped_guide() == {'FINISHED'}
assert bpy.ops.xbone.build_rig() == {'FINISHED'}
rig = bpy.context.object
area = next(a for a in bpy.context.screen.areas if a.type == 'VIEW_3D')
area.type = 'SELECTOR'
space = area.spaces.active
assert isinstance(space, bpy.types.SpaceSelector) and space.zoom == 1
page = board.current_page(rig, space)
assert len(page.buttons) == 150 and sum(bool(b.controls) for b in page.buttons) == 123
mapped = board.bindings(rig, page.names)
missing = set(page.names) - mapped.keys()
assert missing == {'hip_C0_ctl', 'neck_C0_fk2_ctl', 'foot_R0_fk2_ctl', 'foot_L0_fk2_ctl'}, missing
assert len(selector.stock_actions()) == 19
region = next(r for r in area.regions if r.type == 'WINDOW')
with tempfile.TemporaryDirectory() as directory:
    with bpy.context.temp_override(area=area, region=region):
        for i, item in enumerate(page.buttons):
            names = [mapped[n].name for n in item.controls if n in mapped]
            if not names:
                continue
            assert bpy.ops.xbone.selector_pick(index=i) == {'FINISHED'}, item.controls
            selected = {p.name for p in rig.pose.bones if p.select}
            assert selected == set(names), (i, item.controls, selected)
            assert rig.data.bones.active.name == names[-1]
        a, b = (next(i for i, item in enumerate(page.buttons) if item.controls == (n,))
                for n in ('arm_L0_ikRot_ctl', 'foot_L0_fk0_ctl'))
        bpy.ops.xbone.selector_pick(index=a)
        bpy.ops.xbone.selector_pick(index=b, mode='ADD')
        assert len([p for p in rig.pose.bones if p.select]) == 2
        bpy.ops.xbone.selector_pick(index=a, mode='TOGGLE')
        assert rig.data.bones.active.name == mapped['foot_L0_fk0_ctl'].name
        for i, item in enumerate(page.buttons):
            command = selector.stock_actions().get(item.script)
            if not command:
                continue
            if command[0] == "match":
                target = mapped[command[1]]
                prop = target["blend_property"]
                old = rig[prop]
                assert bpy.ops.xbone.selector_pick(index=i) == {'FINISHED'}
                assert rig[prop] == 1 - old
            elif command[0] == "space":
                owner, prop = selector.space_property(rig, *command[1:3])
                assert owner and prop in owner, command
                target = board.bindings(rig, [command[3]])[command[3]]
                previous = target.matrix.copy()
                refs = selector.reference_names(owner, prop)
                assert refs, command
                assert bpy.ops.xbone.selector_space(control=owner.name, driven=target.name, key=prop,
                                                    value=len(refs) - 1) == {'FINISHED'}
                bpy.context.view_layer.update()
                assert max(abs(x - y) for row, old in zip(target.matrix, previous)
                           for x, y in zip(row, old)) < 1e-3, command
        btn = page.buttons[a]
        assert btn.contains(*btn.center) and selector.overlaps(btn, btn.bounds)
        assert not selector.overlaps(btn, (9999, 9999, 10000, 10000))
        data = {"tabs": [{"name": n, "data": {"items": [{"position": [0, 0], "handles": [[0, 0], [8, 0]],
                                                         "controls": ["arm_L0_ikRot_ctl"], "color": [12, 42, 225, 255]}]}} for n in ("Body", "Hand")]}
        src = Path(directory) / "input.pkr"
        image = bpy.data.images.new("Picker background test", width=4, height=4)
        image.filepath_raw = str(Path(directory) / "background.png")
        image.file_format = 'PNG'
        image.save()
        bpy.data.images.remove(image)
        data["tabs"][1]["data"]["background"] = "background.png"
        data["tabs"][1]["data"]["background_size"] = [100, 80]
        src.write_text(json.dumps(data))
        assert bpy.ops.xbone.selector_import(filepath=str(src)) == {'FINISHED'}
        assert bpy.ops.xbone.selector_page(index=1) == {'FINISHED'}
        assert bpy.ops.xbone.selector_pick(index=0) == {'FINISHED'}
        assert bpy.ops.xbone.selector_edit(operation='ADD', position=(28, 37), label="Wrist") == {'FINISHED'}
        assert bpy.ops.xbone.selector_edit(operation='MOVE', index=1, position=(23, 47)) == {'FINISHED'}
        dst = Path(directory) / "output.pkr"
        assert bpy.ops.xbone.selector_export(filepath=str(dst)) == {'FINISHED'}
        item = json.loads(dst.read_text())["tabs"][1]["data"]["items"][1]
        assert item["position"] == [23, 47] and item["controls"] == [mapped["arm_L0_ikRot_ctl"].name]
        space.zoom, space.pan = 2.5, (4, 8)
    area.type = 'OUTLINER'
    area.type = 'SELECTOR'
    assert area.spaces.active.page == 1
    filename = str(Path(directory) / "selector.blend")
    bpy.ops.wm.save_as_mainfile(filepath=filename)
    bpy.ops.wm.open_mainfile(filepath=filename)
    area = next(a for a in bpy.context.screen.areas if a.type == 'SELECTOR')
    space = area.spaces.active
    assert (space.page, space.zoom, tuple(space.pan)) == (1, 2.5, (4, 8))
    assert board.current_page(bpy.context.object, space).buttons[-1].text == "Wrist"
    image, bounds = next(board.background_rects(board.current_page(bpy.context.object, space),
                                                bpy.context.object["xbone_selector_images"]))
    assert image.packed_file and bounds == (-50, -40, 50, 40)
    first = bpy.context.object
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.armature_add()
    other = bpy.context.object
    region = next(r for r in area.regions if r.type == 'WINDOW')
    with bpy.context.temp_override(area=area, region=region):
        assert bpy.ops.xbone.selector_character(name=other.name) == {'FINISHED'}
        assert board.current_page(other, space).buttons[0].controls == ('Bone',)
        assert bpy.ops.xbone.selector_pick(index=0) == {'FINISHED'}
        assert other.data.bones.active.name == "Bone"
        assert bpy.ops.xbone.selector_character(name=first.name) == {'FINISHED'}
        assert board.current_page(first, space).buttons[-1].text == "Wrist"
print("Selector: stock Biped control buttons, selection modes, IK/FK, reference mappings, layout edits, PKR and blend round trips passed")
