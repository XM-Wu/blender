# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

import sys
from pathlib import Path
import bpy, bmesh
from mathutils import Vector
import bl_operators
if '--installed' not in sys.argv:
    bl_operators.__path__.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/startup/bl_operators'))
# Load the current source, not the previous installed build.
import importlib
from bl_operators import mesh_omni_outset as op
importlib.reload(op)
from bl_operators.omni_extrude_geometry import extrude


def mesh(coords, edges, faces, kind='EDGE'):
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    data = bpy.data.meshes.new('Test')
    data.from_pydata(coords, edges, faces)
    obj = bpy.data.objects.new('Test', data)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.context.tool_settings.mesh_select_mode = (False, kind == 'EDGE', kind == 'FACE')
    bm = bmesh.from_edit_mesh(data)
    for seq in (bm.faces, bm.edges, bm.verts):
        for e in seq:
            e.select_set(False)
    return obj, bm


def select(bm, kind, predicate=lambda e: True):
    for e in (bm.faces if kind == 'FACE' else bm.edges):
        e.select_set(predicate(e))
    bm.select_flush_mode()


def valid(bm):
    assert all(all(abs(x) < 1e6 for x in v.co) for v in bm.verts)
    assert all(f.calc_area() > 1e-8 for f in bm.faces), 'degenerate face'


def test_wire():
    obj, bm = mesh([(0,0,0),(1,0,0)], [(0,1)], [])
    select(bm, 'EDGE')
    assert extrude(obj, 'EDGE', 1, 0, divisions=3, direction='OUTWARD')
    assert len(bm.faces) == 3
    assert sum(e.select for e in bm.edges) == 1
    assert max(v.co.z for v in bm.verts) == 1
    extrude(obj, 'EDGE', 1, 0, direction='OUTWARD')
    assert len(bm.faces) == 4
    valid(bm)


def test_face():
    obj, bm = mesh([(0,0,0),(2,0,0),(2,2,0),(0,2,0)], [], [(0,1,2,3)], 'FACE')
    select(bm, 'FACE')
    extrude(obj, 'FACE', 1, .25, divisions=3)
    caps = [f for f in bm.faces if f.select]
    assert len(caps) == 1
    assert abs(caps[0].calc_area() - 2.25) < 1e-5
    assert all(abs(v.co.z - 1) < 1e-5 for v in caps[0].verts)
    assert len(bm.faces) == 14, len(bm.faces)
    valid(bm)


def test_symmetry():
    obj, bm = mesh([(-2,0,0),(-1,0,0),(1,0,0),(2,0,0)], [(0,1),(2,3)], [])
    obj.data.use_mirror_x = True
    select(bm, 'EDGE', lambda e: e.verts[0].co.x > 0)
    extrude(obj, 'EDGE', 1, 0, direction='Z', translation=(.5,0,0))
    assert len(bm.faces) == 2
    assert sum(e.select for e in bm.edges) == 2
    coords = {tuple(round(x,5) for x in v.co) for v in bm.verts}
    assert all((-x,y,z) in coords for x,y,z in coords), coords
    valid(bm)


def test_mirror():
    obj, bm = mesh([(0,0,0),(2,0,0),(2,2,0),(0,2,0)], [], [(0,1,2,3)], 'FACE')
    mod = obj.modifiers.new('Mirror', 'MIRROR')
    mod.use_clip = True
    select(bm, 'FACE')
    extrude(obj, 'FACE', 1, .25, divisions=2)
    caps = [f for f in bm.faces if f.select]
    assert len(caps) == 1
    assert sum(abs(v.co.x) < 1e-6 for v in caps[0].verts) == 2
    assert not any(all(abs(v.co.x) < 1e-6 for v in f.verts) for f in bm.faces)
    valid(bm)


def test_connected_edges():
    for together, expected_verts in ((True, 6), (False, 7)):
        obj, bm = mesh([(0,0,0),(1,0,0),(1,1,0)], [(0,1),(1,2)], [])
        select(bm, 'EDGE')
        extrude(obj, 'EDGE', 1, 0, together=together, direction='Z')
        assert len(bm.verts) == expected_verts, len(bm.verts)
        assert len(bm.faces) == 2
        assert sum(e.select for e in bm.edges) == 2
        valid(bm)


def test_cube():
    if bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.object
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.context.tool_settings.mesh_select_mode = (False, False, True)
    bm = bmesh.from_edit_mesh(obj.data)
    for f in bm.faces:
        f.select_set(False)
    for e in bm.edges:
        e.select_set(False)
    for v in bm.verts:
        v.select_set(False)
    select(bm, 'FACE', lambda f: f.normal.z > .9)
    backup = bm.copy()
    extrude(obj, 'FACE', 1, .2, divisions=3)
    assert all(e.is_manifold for e in bm.edges)
    assert sum(f.select for f in bm.faces) == 1
    valid(bm)
    extrude(obj, 'FACE', 1, .1)
    assert all(e.is_manifold for e in bm.edges)
    assert max(v.co.z for v in bm.verts) == 3
    valid(bm)
    op._restore_bmesh_from_backup(bm, backup, obj.data)
    backup.free()
    assert len(bm.faces) == 6
    assert sum(f.select for f in bm.faces) == 1
    assert len(bm.verts.layers.int) == 0


def test_mirror_object():
    obj, bm = mesh([(1,0,0),(3,0,0),(3,2,0),(1,2,0)], [], [(0,1,2,3)], 'FACE')
    empty = bpy.data.objects.new('Mirror Origin', None)
    bpy.context.collection.objects.link(empty)
    empty.location.x = 1
    bpy.context.view_layer.update()
    mod = obj.modifiers.new('Mirror', 'MIRROR')
    mod.mirror_object = empty
    mod.use_clip = True
    select(bm, 'FACE')
    extrude(obj, 'FACE', 1, .2, divisions=3)
    cap = next(f for f in bm.faces if f.select)
    assert sum(abs(v.co.x - 1) < 1e-6 for v in cap.verts) == 2
    assert not any(all(abs(v.co.x - 1) < 1e-6 for v in f.verts) for f in bm.faces)
    valid(bm)


def test_vertical_wire():
    obj, bm = mesh([(0,0,0),(0,0,1)], [(0,1)], [])
    select(bm, 'EDGE')
    extrude(obj, 'EDGE', 1, 0, direction='OUTWARD')
    valid(bm)
    assert len(bm.faces) == 1


def test_multi_axis():
    coords = [(x,y,z) for x,y in ((1,1),(-1,1),(1,-1),(-1,-1)) for z in (0,1)]
    obj, bm = mesh(coords, [(i,i+1) for i in range(0,8,2)], [])
    obj.data.use_mirror_x = obj.data.use_mirror_y = True
    select(bm, 'EDGE', lambda e: all(v.co.x > 0 and v.co.y > 0 for v in e.verts))
    extrude(obj, 'EDGE', 1, 0, direction='OUTWARD')
    assert len(bm.faces) == 4
    valid(bm)
    coords = {tuple(round(x,5) for x in v.co) for v in bm.verts}
    assert all((-x,y,z) in coords and (x,-y,z) in coords for x,y,z in coords)


def test_registration():
    for cls in op.classes:
        old = getattr(bpy.types, cls.__name__, None)
        if old is not None:
            bpy.utils.unregister_class(old)
        bpy.utils.register_class(cls)
    old_macro = getattr(bpy.types, "MESH_OT_omni_outset_move", None)
    if old_macro is not None:
        bpy.utils.unregister_class(old_macro)
    bpy.utils.register_class(op.MESH_OT_omni_outset_move)
    op._define_extrude_macro()
    obj, bm = mesh([(0,0,0),(1,0,0)], [(0,1)], [])
    select(bm, 'EDGE')
    result = bpy.ops.mesh.omni_outset_edge('EXEC_DEFAULT', extrude=2, direction='Z', divisions=2)
    assert result == {'FINISHED'}, result
    assert len(bm.faces) == 2
    assert max(v.co.z for v in bm.verts) == 2
    valid(bm)


def test_modal_cancel_and_numeric():
    from types import SimpleNamespace
    class Interaction(op._ExtrudeInteraction):
        _kind = 'EDGE'
        extrude = 0.0
        offset = 0.0
        divisions = 1
        keep_faces_together = True
        direction = 'OUTWARD'
        translation = (0, 0, 0)
        taper = 1.0
        def report(self, *_args):
            pass
    obj, bm = mesh([(0,0,0),(1,0,0)], [(0,1)], [])
    select(bm, 'EDGE')
    original = [(tuple(v.co), v.select) for v in bm.verts]
    class Context:
        def __getattr__(self, name):
            return getattr(bpy.context, name)
        window_manager = SimpleNamespace(modal_handler_add=lambda _op: None)
    context = Context()
    start, stop, redraw = op.hud.start, op.hud.stop, op.hud.tag_redraw
    op.hud.start = op.hud.stop = op.hud.tag_redraw = lambda *_args: None
    def event(kind, char='', ctrl=False):
        return SimpleNamespace(type=kind, ascii=char, value='PRESS', ctrl=ctrl,
                               shift=False, mouse_x=100, mouse_y=100)
    try:
        tool = Interaction()
        assert tool.invoke(context, event('E')) == {'RUNNING_MODAL'}
        tool.modal(context, event('Z'))
        tool.modal(context, event('TWO', '2'))
        tool.modal(context, event('WHEELUPMOUSE'))
        assert tool.divisions == 2
        assert max(v.co.z for v in bm.verts) == 2
        assert len(bm.faces) == 2
        assert tool.modal(context, event('ESC')) == {'CANCELLED'}
        assert [(tuple(v.co), v.select) for v in bm.verts] == original
        assert len(bm.faces) == 0
    finally:
        op.hud.start, op.hud.stop, op.hud.tag_redraw = start, stop, redraw


def test_scaled_face_and_uv():
    obj, bm = mesh([(0,0,0),(2,0,0),(2,2,0),(0,2,0)], [], [(0,1,2,3)], 'FACE')
    obj.scale = (2, 3, 4)
    bpy.context.view_layer.update()
    uv = bm.loops.layers.uv.new('UVMap')
    for face in bm.faces:
        for loop in face.loops:
            loop[uv].uv = loop.vert.co.xy / 2
    select(bm, 'FACE')
    extrude(obj, 'FACE', 2, .5, divisions=2)
    cap = next(f for f in bm.faces if f.select)
    assert all(abs((obj.matrix_world @ v.co).z - 2) < 1e-5 for v in cap.verts)
    assert abs(cap.calc_area() * 6 - 15) < 1e-5
    assert len(bm.loops.layers.uv) == 1
    uv = bm.loops.layers.uv.active
    assert {tuple(loop[uv].uv) for loop in cap.loops} == {(0,0),(1,0),(1,1),(0,1)}
    valid(bm)


def test_interior_edge():
    obj, bm = mesh([(0,0,0),(1,0,0),(2,0,0),(0,1,0),(1,1,0),(2,1,0)], [],
                   [(0,1,4,3),(1,2,5,4)])
    select(bm, 'EDGE', lambda e: len(e.link_faces) == 2)
    extrude(obj, 'EDGE', 1, 0, divisions=2, direction='OUTWARD')
    assert len(bm.faces) == 4
    assert sum(e.select for e in bm.edges) == 1
    assert max(v.co.z for v in bm.verts) == 1
    valid(bm)


def test_individual_faces():
    obj, bm = mesh([(0,0,0),(1,0,0),(2,0,0),(0,1,0),(1,1,0),(2,1,0)], [],
                   [(0,1,4,3),(1,2,5,4)], 'FACE')
    select(bm, 'FACE')
    extrude(obj, 'FACE', 1, .1, divisions=2, together=False)
    caps = [f for f in bm.faces if f.select]
    assert len(caps) == 2
    assert all(abs(f.calc_area() - .64) < 1e-5 for f in caps)
    valid(bm)


def test_coincident_wires():
    obj, bm = mesh([(0,0,0),(1,0,0),(0,0,0),(1,0,0)], [(0,1),(2,3)], [])
    select(bm, 'EDGE')
    extrude(obj, 'EDGE', 1, 0, divisions=2, direction='Z')
    assert len(bm.verts) == 12
    assert len(bm.faces) == 4
    assert sum(e.select for e in bm.edges) == 2
    valid(bm)


def test_face_symmetry_seam():
    obj, bm = mesh([(-1,0,0),(0,0,0),(1,0,0),(-1,1,0),(0,1,0),(1,1,0)], [],
                   [(0,1,4,3),(1,2,5,4)], 'FACE')
    obj.data.use_mirror_x = True
    select(bm, 'FACE', lambda f: f.calc_center_median().x > 0)
    extrude(obj, 'FACE', 1, .1, divisions=2)
    assert sum(f.select for f in bm.faces) == 2
    assert not any(all(abs(v.co.x) < 1e-6 for v in f.verts) for f in bm.faces)
    coords = {tuple(round(x,5) for x in v.co) for v in bm.verts}
    assert all((-x,y,z) in coords for x,y,z in coords)
    valid(bm)


def test_both_sides_selected():
    obj, bm = mesh([(-2,0,0),(-1,0,0),(1,0,0),(2,0,0)], [(0,1),(2,3)], [])
    obj.data.use_mirror_x = True
    select(bm, 'EDGE')
    extrude(obj, 'EDGE', 1, 0, direction='Z', translation=(.5,0,0), taper=.5)
    coords = {tuple(round(x,5) for x in v.co) for v in bm.verts}
    assert all((-x,y,z) in coords for x,y,z in coords)
    assert len(bm.faces) == 2
    valid(bm)


def test_mirror_split_vertices():
    # Coordinate-only nearest-neighbor matching used to choose the disconnected
    # duplicates instead of the vertices belonging to the actual opposite edge.
    import itertools
    for order in itertools.permutations(range(4)):
        negative = [(-2,0,0),(-1,0,0),(-2,0,0),(-1,0,0)]
        coords = [(2,0,0),(1,0,0)] + [negative[i] for i in order]
        obj, bm = mesh(coords, [(0,1),(2+order.index(0),2+order.index(1))], [])
        obj.use_mesh_mirror_x = True
        select(bm, 'EDGE', lambda e: e.verts[0].co.x > 0)
        extrude(obj, 'EDGE', 1, 0, direction='Z')
        assert len(bm.faces) == 2, order
        assert sum(e.select for e in bm.edges) == 2
        assert sum(v.co.x < 0 and v.co.z > .9 for v in bm.verts) == 2
        valid(bm)


def test_mirror_split_faces():
    positive = [(1,0,0),(2,0,0),(2,1,0),(1,1,0)]
    negative = [(-x,y,z) for x,y,z in positive]
    obj, bm = mesh(positive + negative + negative, [], [(0,1,2,3),(7,6,5,4)], 'FACE')
    obj.use_mesh_mirror_x = True
    select(bm, 'FACE', lambda f: f.calc_center_median().x > 0)
    extrude(obj, 'FACE', 1, 0)
    assert sum(f.select for f in bm.faces) == 2
    assert sum(v.co.x < 0 and v.co.z > .9 for v in bm.verts) == 4
    valid(bm)


def test_vertex_mode_edge_dispatch():
    obj, bm = mesh([(-2,0,0),(-1,0,0),(1,0,0),(2,0,0)], [(0,1),(2,3)], [])
    obj.use_mesh_mirror_x = True
    select(bm, 'EDGE', lambda e: e.verts[0].co.x > 0)
    bpy.ops.mesh.select_mode(type='VERT')
    assert op._extrude_selection_kind(bpy.context) == 'EDGE'
    assert bpy.ops.mesh.omni_outset_edge.poll()
    result = bpy.ops.mesh.omni_outset_edge('EXEC_DEFAULT', extrude=1, direction='Z')
    assert result == {'FINISHED'}
    assert len(bm.faces) == 2
    valid(bm)


def test_vertex_mode_face_dispatch():
    positive = [(1,0,0),(2,0,0),(2,1,0),(1,1,0)]
    obj, bm = mesh(positive + [(-x,y,z) for x,y,z in positive], [],
                   [(0,1,2,3),(7,6,5,4)], 'FACE')
    obj.use_mesh_mirror_x = True
    select(bm, 'FACE', lambda f: f.calc_center_median().x > 0)
    bpy.ops.mesh.select_mode(type='VERT')
    assert op._extrude_selection_kind(bpy.context) == 'FACE'
    assert bpy.ops.mesh.omni_outset_face.poll()
    result = bpy.ops.mesh.omni_outset_face('EXEC_DEFAULT', extrude_dist=1)
    assert result == {'FINISHED'}
    assert sum(f.select for f in bm.faces) == 2
    valid(bm)


def test_xm_extrude_binding():
    import runpy
    if '--installed' in sys.argv:
        keymap_path = Path(bpy.utils.system_resource('SCRIPTS')) / 'presets/keyconfig/XM_KeyMapping.py'
    else:
        keymap_path = Path(__file__).resolve().parents[2] / 'scripts/presets/keyconfig/XM_KeyMapping.py'
    keymap = runpy.run_path(str(keymap_path))
    data = keymap['industry_compatible'].generate_keymaps(keymap['industry_compatible'].Params())
    keymap['_add_xm_items'](data)
    items = keymap['_keymap_items'](data, 'Mesh')
    bindings = [item for item in items if item[1].get('type') == 'E' and item[1].get('ctrl')
                and not item[1].get('shift') and not item[1].get('alt')]
    assert len(bindings) == 1, bindings
    assert bindings[0][0] == 'wm.tool_set_by_id', bindings
    assert dict(bindings[0][2]['properties']) == {'name': 'builtin.extrude_outset', 'cycle': False, 'use_last': True}
    drag_items = keymap['_keymap_items'](data, '3D View Tool: Edit Mesh, Extrude')
    assert drag_items[0][0] == 'mesh.omni_outset_move'


def test_native_gizmo_edge_symmetry():
    # New caps start at the base coordinates; the native transform must only
    # mirror between selected caps, never move an unselected base vertex.
    obj, bm = mesh([(1,0,0),(1,1,0),(-1,0,0),(-1,1,0)], [(0,1),(2,3)], [])
    obj.use_mesh_mirror_x = True
    select(bm, 'EDGE', lambda e: e.verts[0].co.x > 0)
    result = bpy.ops.mesh.omni_outset_move('EXEC_DEFAULT',
                                         TRANSFORM_OT_translate={'value': (1,0,0)})
    assert result == {'FINISHED'}
    expected = {(x,y,0) for x in (-2,-1,1,2) for y in (0,1)}
    assert {tuple(v.co) for v in bm.verts} == expected
    assert len(bm.faces) == 2
    valid(bm)


def test_native_gizmo_face_symmetry():
    right = [(1,0,0),(1,1,0),(1,1,1),(1,0,1)]
    obj, bm = mesh(right + [(-x,y,z) for x,y,z in right], [],
                   [(0,1,2,3),(7,6,5,4)], 'FACE')
    obj.use_mesh_mirror_x = True
    select(bm, 'FACE', lambda f: f.calc_center_median().x > 0)
    result = bpy.ops.mesh.omni_outset_move('EXEC_DEFAULT',
                                         TRANSFORM_OT_translate={'value': (1,0,0)})
    assert result == {'FINISHED'}
    caps = [f for f in bm.faces if f.select]
    assert len(caps) == 2
    assert all(abs(abs(v.co.x)-2) < 1e-6 for f in caps for v in f.verts)
    assert sum(abs(abs(v.co.x)-1) < 1e-6 for v in bm.verts) == 8
    valid(bm)


for test in (test_wire, test_vertical_wire, test_face, test_connected_edges,
             test_cube, test_symmetry, test_multi_axis, test_mirror, test_mirror_object,
             test_scaled_face_and_uv, test_interior_edge, test_individual_faces,
             test_coincident_wires, test_face_symmetry_seam, test_both_sides_selected,
             test_registration, test_modal_cancel_and_numeric, test_mirror_split_vertices,
             test_mirror_split_faces, test_vertex_mode_edge_dispatch, test_vertex_mode_face_dispatch,
             test_xm_extrude_binding, test_native_gizmo_edge_symmetry,
             test_native_gizmo_face_symmetry):
    test()
    print('PASS', test.__name__, flush=True)
print('OMNI_EXTRUDE_TESTS_OK', flush=True)
