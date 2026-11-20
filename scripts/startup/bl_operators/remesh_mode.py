# SPDX-License-Identifier: GPL-2.0-or-later
"""Surface-guided retopology tools for the 3D View."""
from contextlib import contextmanager

import bmesh
import bpy
from bpy.props import BoolProperty, IntProperty
from bpy.types import Menu, Operator, Panel
from bpy_extras import view3d_utils as view
from mathutils import Vector

TAG = "remesh_mode_output"
LOCK = "remesh_locked"
keymaps = []


def active(context):
    return context.mode == 'REMESH' and context.active_object is not None


def source_object(context):
    obj = context.active_object
    return obj.parent if obj.get(TAG) else obj


def output_object(context, *, create=False):
    obj = context.active_object
    if obj.get(TAG):
        return obj
    for child in obj.children:
        if child.type == 'MESH' and child.get(TAG) and child.visible_get(view_layer=context.view_layer):
            return child
    if create:
        mesh = bpy.data.meshes.new(obj.name + " Retopo")
        output = bpy.data.objects.new(mesh.name, mesh)
        context.collection.objects.link(output)
        output.parent = obj
        output[TAG] = True
        output.show_in_front = True
        output.show_wire = True
        output.show_all_edges = True
        if context.space_data.local_view:
            output.local_view_set(context.space_data, True)
        context.view_layer.update()
        return output
    return None


@contextmanager
def mesh_buffer(mesh):
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        yield bm
    finally:
        bm.free()


def store_mesh(bm, mesh):
    bm.to_mesh(mesh)
    mesh.update()


def select_row(bm, row):
    for face in bm.faces:
        face.select_set(False)
    for edge in bm.edges:
        edge.select_set(False)
    for vert in bm.verts:
        vert.select_set(False)
    for edge in row:
        edge.select_set(True)
    bm.select_history.clear()


def boundary_row(bm):
    edges = [e for e in bm.edges if e.select and not e.hide]
    if not edges or any(len(e.link_faces) > 1 for e in edges):
        return None
    neighbors = {}
    for edge in edges:
        a, b = edge.verts
        neighbors.setdefault(a, []).append(b)
        neighbors.setdefault(b, []).append(a)
    ends = [v for v, adj in neighbors.items() if len(adj) == 1]
    if len(ends) not in (0, 2) or any(len(adj) > 2 for adj in neighbors.values()):
        return None
    start = ends[0] if ends else next(iter(neighbors))
    row = [start]
    seen = {start}
    while True:
        nxt = next((v for v in neighbors[row[-1]] if v not in seen), None)
        if nxt is None:
            break
        row.append(nxt)
        seen.add(nxt)
    return (row, not ends) if len(seen) == len(neighbors) else None


def pairs(row, closed):
    return zip(row, row[1:] + (row[:1] if closed else []))


def commit_row(bm, coords, old, closed, normal):
    verts = [bm.verts.new(co) for co in coords]
    edges = [bm.edges.new(pair) for pair in pairs(verts, closed)]
    if old:
        for (a, b), (c, d) in zip(pairs(old, closed), pairs(verts, closed)):
            edge = bm.edges.get((a, b))
            loop = next(iter(edge.link_loops), None)
            face = bm.faces.new((a, b, d, c))
            face.normal_update()
            if (loop and loop.vert == a) or (not loop and face.normal.dot(normal) < 0):
                face.normal_flip()
    select_row(bm, edges)


class RemeshEdit:
    @classmethod
    def poll(cls, context):
        return bool(context.area and context.area.type == 'VIEW_3D' and active(context) and
                    source_object(context) and source_object(context).type == 'MESH')


class VIEW3D_OT_remesh_stroke(RemeshEdit, Operator):
    bl_idname = "view3d.remesh_stroke"
    bl_label = "Draw Edge Flow"
    bl_options = {'UNDO', 'BLOCKING'}
    strip: BoolProperty(default=False, options={'SKIP_SAVE'})
    spacing: IntProperty(name="Sample Spacing (px)", default=20, min=4, max=120)

    def invoke(self, context, event):
        self.area = context.area
        self.region = (context.region if context.region.type == 'WINDOW' else
                       next(r for r in self.area.regions if r.type == 'WINDOW'))
        self.rv = context.region_data or context.space_data.region_3d
        self.owner = context.active_object
        self.obj = output_object(context)
        self.source = source_object(context).evaluated_get(context.evaluated_depsgraph_get())
        self.to_source = self.source.matrix_world.inverted_safe()
        self.to_output = (self.obj or self.owner).matrix_world.inverted_safe() @ self.source.matrix_world
        self.old = []
        self.closed = False
        if self.strip:
            if self.obj:
                with mesh_buffer(self.obj.data) as bm:
                    row = boundary_row(bm)
                    if row:
                        verts, self.closed = row
                        self.old = [v.index for v in verts]
            if not self.old:
                self.report({'WARNING'}, "Select one connected boundary chain or loop")
                return {'CANCELLED'}
        self.points = []
        self.trace = []
        self.valid = False
        self.start = None
        self.area.header_text_set("Remesh | Drag LMB to " +
                                  ("extrude selected boundary" if self.strip else "draw edge flow") +
                                  " | Release to apply | Esc to cancel")
        self.handler = bpy.types.SpaceView3D.draw_handler_add(self.draw_preview, (), 'WINDOW', 'POST_PIXEL')
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def hit(self, pixel):
        origin = view.region_2d_to_origin_3d(self.region, self.rv, pixel)
        direction = view.region_2d_to_vector_3d(self.region, self.rv, pixel)
        ok, co, normal, _ = self.source.ray_cast(
            self.to_source @ origin, self.to_source.to_3x3() @ direction)
        if ok:
            self.normal = self.to_output.to_3x3().inverted_safe().transposed() @ normal
            return self.to_output @ co
        return None

    def sample(self, pixel):
        if self.strip:
            self.points = []
            self.trace = []
            for index in self.old:
                vert = self.obj.data.vertices[index]
                p = view.location_3d_to_region_2d(self.region, self.rv, self.obj.matrix_world @ vert.co)
                if p is None:
                    return False
                p += pixel - self.start
                co = self.hit(p)
                if co is None or (co - vert.co).length_squared < 1e-16:
                    return False
                self.trace.append(p)
                self.points.append(co)
            return (pixel - self.start).length >= 3
        if self.trace:
            delta = pixel - self.trace[-1]
            steps = int(delta.length / self.spacing)
            direction = delta.normalized()
            samples = [self.trace[-1] + direction * (i * self.spacing) for i in range(1, steps + 1)]
        else:
            samples = [pixel]
        for point in samples:
            co = self.hit(point)
            if co is None:
                return False
            self.trace.append(point)
            self.points.append(co)
        return self.hit(pixel) is not None

    def draw_preview(self):
        if bpy.context.region != self.region or len(self.trace) < 2:
            return
        import gpu
        from gpu_extras.batch import batch_for_shader
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        coords = self.trace + (self.trace[:1] if self.closed else [])
        shader.bind()
        shader.uniform_float("color", (0.1, 1, 0.65, 1) if self.valid else (1, 0.2, 0.1, 1))
        batch_for_shader(shader, 'LINE_STRIP', {"pos": [(p.x, p.y, 0) for p in coords]}).draw(shader)

    def cancel(self, context):
        bpy.types.SpaceView3D.draw_handler_remove(self.handler, 'WINDOW')
        self.area.header_text_set(None)
        self.area.tag_redraw()

    def modal(self, context, event):
        if event.type in {'ESC', 'RIGHTMOUSE', 'WINDOW_DEACTIVATE'} or (
                not active(context) or context.active_object != self.owner):
            self.cancel(context)
            return {'CANCELLED'}
        pixel = Vector((event.mouse_x - self.region.x, event.mouse_y - self.region.y))
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            self.start = pixel
        if self.start is None or event.type not in {'LEFTMOUSE', 'MOUSEMOVE'}:
            return {'RUNNING_MODAL'}
        self.valid = self.sample(pixel)
        self.area.tag_redraw()
        if not self.valid and not self.strip:
            self.cancel(context)
            self.report({'INFO'}, "Stroke left the source surface")
            return {'CANCELLED'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            self.cancel(context)
            if not self.valid:
                return {'CANCELLED'}
            if not self.strip and (pixel - self.trace[-1]).length >= 3:
                self.points.append(self.hit(pixel))
            if len(self.points) < 2:
                return {'CANCELLED'}
            if not self.strip and len(self.points) >= 4 and (pixel - self.start).length <= self.spacing / 2:
                self.points.pop()
                self.closed = True
            obj = output_object(context, create=True)
            with mesh_buffer(obj.data) as bm:
                bm.verts.ensure_lookup_table()
                old = [bm.verts[i] for i in self.old]
                commit_row(bm, self.points, old, self.closed, self.normal)
                store_mesh(bm, obj.data)
            return {'FINISHED'}
        return {'RUNNING_MODAL'}


class MESH_OT_remesh_lock(RemeshEdit, Operator):
    bl_idname = "mesh.remesh_lock"
    bl_label = "Toggle Edge Lock"
    bl_options = {'UNDO'}

    def execute(self, context):
        obj = output_object(context)
        if obj is None:
            return {'CANCELLED'}
        with mesh_buffer(obj.data) as bm:
            layer = bm.edges.layers.int.get(LOCK) or bm.edges.layers.int.new(LOCK)
            edges = [e for e in bm.edges if e.select and not e.hide]
            if not edges:
                self.report({'WARNING'}, "Select edges to lock or unlock")
                return {'CANCELLED'}
            lock = not all(e[layer] for e in edges)
            for edge in edges:
                edge[layer] = int(lock)
            store_mesh(bm, obj.data)
        self.report({'INFO'}, "%s %d edges" % ("Locked" if lock else "Unlocked", len(edges)))
        return {'FINISHED'}


class MESH_OT_remesh_relax(RemeshEdit, Operator):
    bl_idname = "mesh.remesh_relax"
    bl_label = "Relax Selected Vertices"
    bl_options = {'UNDO'}

    def execute(self, context):
        obj = output_object(context)
        if obj is None:
            return {'CANCELLED'}
        with mesh_buffer(obj.data) as bm:
            return self.relax(context, obj, bm)

    def relax(self, context, obj, bm):
        layer = bm.edges.layers.int.get(LOCK)
        source = obj.parent.evaluated_get(context.evaluated_depsgraph_get())
        transform = source.matrix_world.inverted_safe() @ obj.matrix_world
        inverse = transform.inverted_safe()
        moves = {}
        for vert in bm.verts:
            if not vert.select or vert.hide or any(e[layer] for e in vert.link_edges if layer):
                continue
            adj = [e.other_vert(vert) for e in vert.link_edges]
            if len(adj) < 2:
                continue
            mean = sum((v.co for v in adj), Vector()) / len(adj)
            ok, co, _, _ = source.closest_point_on_mesh(transform @ vert.co.lerp(mean, 0.5))
            if ok:
                moves[vert] = inverse @ co
        if not moves:
            self.report({'INFO'}, "Select unlocked vertices with at least two neighbors")
            return {'CANCELLED'}
        for vert, co in moves.items():
            vert.co = co
        bm.normal_update()
        store_mesh(bm, obj.data)
        return {'FINISHED'}


class VIEW3D_PT_remesh_mode(Panel):
    bl_label = "Remesh Mode"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Remesh"

    @classmethod
    def poll(cls, context):
        return active(context)

    def draw(self, context):
        layout = self.layout
        layout.operator("view3d.remesh_stroke", text="Draw Edge Flow").strip = False
        layout.operator("view3d.remesh_stroke", text="Extrude Surface Strip").strip = True
        layout.operator("mesh.remesh_lock")
        layout.operator("mesh.remesh_relax")


class VIEW3D_OT_remesh_select(RemeshEdit, Operator):
    bl_idname = "view3d.remesh_select"
    bl_label = "Select Retopology Edge"
    bl_options = {'UNDO'}
    extend: BoolProperty(default=False)

    def invoke(self, context, event):
        obj = output_object(context)
        if obj is None:
            return {'CANCELLED'}
        pixel = Vector((event.mouse_region_x, event.mouse_region_y))
        with mesh_buffer(obj.data) as bm:
            closest = None
            distance = 12 * context.preferences.system.ui_scale
            screen = {v: view.location_3d_to_region_2d(context.region, context.region_data,
                                                       obj.matrix_world @ v.co) for v in bm.verts}
            for edge in bm.edges:
                a, b = (screen[v] for v in edge.verts)
                if a is None or b is None or edge.hide:
                    continue
                delta = b - a
                t = (pixel - a).dot(delta) / max(delta.length_squared, 1e-12)
                d = (a + delta * max(0, min(1, t)) - pixel).length
                if d < distance:
                    distance, closest = d, edge
            selected = {e for e in bm.edges if e.select} if self.extend else set()
            if closest:
                selected.symmetric_difference_update((closest,))
            select_row(bm, selected)
            store_mesh(bm, obj.data)
        return {'FINISHED'}


class MESH_OT_remesh_select_all(RemeshEdit, Operator):
    bl_idname = "mesh.remesh_select_all"
    bl_label = "Select Retopology Edges"
    bl_options = {'UNDO'}
    deselect: BoolProperty(default=False)

    def execute(self, context):
        obj = output_object(context)
        if obj is None:
            return {'CANCELLED'}
        with mesh_buffer(obj.data) as bm:
            select_row(bm, [] if self.deselect else [e for e in bm.edges if not e.hide])
            store_mesh(bm, obj.data)
        return {'FINISHED'}


class VIEW3D_MT_select_remesh(Menu):
    bl_label = "Select"

    def draw(self, context):
        self.layout.operator("mesh.remesh_select_all", text="All").deselect = False
        self.layout.operator("mesh.remesh_select_all", text="None").deselect = True


class VIEW3D_MT_remesh(Menu):
    bl_label = "Remesh"
    draw = VIEW3D_PT_remesh_mode.draw


def draw_selection():
    import gpu
    from gpu_extras.batch import batch_for_shader
    context = bpy.context
    if not active(context) or not context.space_data.overlay.show_overlays:
        return
    obj = output_object(context)
    if obj is None:
        return
    mesh = obj.data
    colors = ([], [], [])
    lock = mesh.attributes.get(LOCK)
    coords = [obj.matrix_world @ v.co for v in mesh.vertices]
    for e in mesh.edges:
        group = 2 if lock and lock.data[e.index].value else int(e.select)
        colors[group].extend(coords[i] for i in e.vertices)
    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    shader.bind()
    shader.uniform_float("viewportSize", gpu.state.viewport_get()[2:])
    shader.uniform_float("lineWidth", 2.0)
    for points, color in zip(colors, ((0.16, 0.5, 0.6, 1), (1, 0.6, 0.12, 1), (0.75, 0.25, 1, 1))):
        if points:
            shader.uniform_float("color", color)
            batch_for_shader(shader, 'LINES', {"pos": points}).draw(shader)


classes = (VIEW3D_OT_remesh_stroke, VIEW3D_OT_remesh_select, MESH_OT_remesh_lock,
           MESH_OT_remesh_relax, MESH_OT_remesh_select_all, VIEW3D_PT_remesh_mode,
           VIEW3D_MT_select_remesh, VIEW3D_MT_remesh)


def register():
    config = bpy.context.window_manager.keyconfigs.addon
    if config is None:
        return
    global draw_handle
    draw_handle = bpy.types.SpaceView3D.draw_handler_add(draw_selection, (), 'WINDOW', 'POST_VIEW')
    km = config.keymaps.new(name="Remesh Mode", space_type='EMPTY')
    for op, key in (("view3d.remesh_stroke", 'D'), ("view3d.remesh_stroke", 'E'),
                    ("mesh.remesh_lock", 'L'), ("mesh.remesh_relax", 'S')):
        item = km.keymap_items.new(op, key, 'PRESS')
        if op == "view3d.remesh_stroke":
            item.properties.strip = key == 'E'
        keymaps.append((km, item))
    for extend in (False, True):
        item = km.keymap_items.new("view3d.remesh_select", 'LEFTMOUSE', 'PRESS', shift=extend)
        item.properties.extend = extend
        keymaps.append((km, item))


def unregister():
    if keymaps:
        bpy.types.SpaceView3D.draw_handler_remove(draw_handle, 'WINDOW')
    for km, item in keymaps:
        km.keymap_items.remove(item)
    keymaps.clear()
