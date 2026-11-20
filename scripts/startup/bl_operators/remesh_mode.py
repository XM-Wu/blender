# SPDX-License-Identifier: GPL-2.0-or-later
"""Surface-guided retopology tools for the 3D View."""
import bmesh
import bpy
from bpy.props import BoolProperty, IntProperty
from bpy.types import Operator, Panel
from bpy_extras import view3d_utils as view
from mathutils import Vector

TAG = "remesh_mode_output"
LOCK = "remesh_locked"
keymaps = []


def active(context):
    obj = context.edit_object
    return (obj is not None and obj.type == 'MESH' and obj.get(TAG) and
            obj.parent is not None and obj.parent.type == 'MESH' and
            len(context.objects_in_mode) == 1)


class VIEW3D_OT_remesh_toggle(Operator):
    bl_idname = "view3d.remesh_mode_toggle"
    bl_label = "Toggle Remesh Mode"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.area and context.area.type == 'VIEW_3D' and
                (active(context) or (context.mode == 'OBJECT' and
                 context.object and context.object.type == 'MESH'))) or False

    def execute(self, context):
        if active(context):
            bpy.ops.object.mode_set(mode='OBJECT')
            return {'FINISHED'}
        obj = context.object
        if obj.get(TAG):
            if not obj.parent or obj.parent.type != 'MESH':
                self.report({'WARNING'}, "Output has no source mesh parent")
                return {'CANCELLED'}
            output = obj
        else:
            output = next((c for c in obj.children if c.get(TAG) and
                           c.name in context.view_layer.objects and c.visible_get()), None)
            if output is None:
                mesh = bpy.data.meshes.new(obj.name + " Retopo")
                output = bpy.data.objects.new(mesh.name, mesh)
                context.collection.objects.link(output)
                output.parent = obj
                output[TAG] = True
                output.show_in_front = True
                if context.space_data.local_view:
                    output.local_view_set(context.space_data, True)
        for selected in context.selected_objects:
            selected.select_set(False)
        output.select_set(True)
        context.view_layer.objects.active = output
        bpy.ops.object.mode_set(mode='EDIT')
        context.tool_settings.mesh_select_mode = (False, True, False)
        return {'FINISHED'}


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
        return bool(context.area and context.area.type == 'VIEW_3D' and active(context))


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
        self.obj = context.edit_object
        self.source = self.obj.parent.evaluated_get(context.evaluated_depsgraph_get())
        self.to_source = self.source.matrix_world.inverted_safe()
        self.to_output = self.obj.matrix_world.inverted_safe() @ self.source.matrix_world
        self.old = []
        self.closed = False
        if self.strip:
            row = boundary_row(bmesh.from_edit_mesh(self.obj.data))
            if not row:
                self.report({'WARNING'}, "Select one connected boundary chain or loop")
                return {'CANCELLED'}
            self.old, self.closed = row
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
            for vert in self.old:
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
        if event.type in {'ESC', 'RIGHTMOUSE', 'WINDOW_DEACTIVATE'} or context.edit_object != self.obj:
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
            bm = bmesh.from_edit_mesh(self.obj.data)
            commit_row(bm, self.points, self.old, self.closed, self.normal)
            bmesh.update_edit_mesh(self.obj.data)
            return {'FINISHED'}
        return {'RUNNING_MODAL'}


class MESH_OT_remesh_lock(RemeshEdit, Operator):
    bl_idname = "mesh.remesh_lock"
    bl_label = "Toggle Edge Lock"
    bl_options = {'UNDO'}

    def execute(self, context):
        mesh = context.edit_object.data
        bm = bmesh.from_edit_mesh(mesh)
        edges = [e for e in bm.edges if e.select and not e.hide]
        if not edges:
            self.report({'WARNING'}, "Select edges to lock or unlock")
            return {'CANCELLED'}
        layer = bm.edges.layers.int.get(LOCK) or bm.edges.layers.int.new(LOCK)
        edges = [e for e in bm.edges if e.select and not e.hide]
        lock = not all(e[layer] for e in edges)
        for edge in edges:
            edge[layer] = int(lock)
        bmesh.update_edit_mesh(mesh, destructive=False)
        self.report({'INFO'}, "%s %d edges" % ("Locked" if lock else "Unlocked", len(edges)))
        return {'FINISHED'}


class MESH_OT_remesh_relax(RemeshEdit, Operator):
    bl_idname = "mesh.remesh_relax"
    bl_label = "Relax Selected Vertices"
    bl_options = {'UNDO'}

    def execute(self, context):
        obj = context.edit_object
        bm = bmesh.from_edit_mesh(obj.data)
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
        bmesh.update_edit_mesh(obj.data, destructive=False)
        return {'FINISHED'}


class VIEW3D_PT_remesh_mode(Panel):
    bl_label = "Remesh Mode"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Remesh"

    def draw(self, context):
        layout = self.layout
        editing = active(context)
        layout.operator("view3d.remesh_mode_toggle", text="Exit Remesh Mode" if editing else "Enter Remesh Mode")
        if editing:
            layout.label(text="Source: " + context.edit_object.parent.name)
            layout.operator("view3d.remesh_stroke", text="Draw Edge Flow").strip = False
            layout.operator("view3d.remesh_stroke", text="Extrude Surface Strip").strip = True
            layout.operator("mesh.remesh_lock")
            layout.operator("mesh.remesh_relax")
            layout.label(text="Drag LMB to draw or extrude.")


classes = (VIEW3D_OT_remesh_toggle, VIEW3D_OT_remesh_stroke, MESH_OT_remesh_lock,
           MESH_OT_remesh_relax, VIEW3D_PT_remesh_mode)


def register():
    config = bpy.context.window_manager.keyconfigs.addon
    if config is None:
        return
    for name, space, op, key, strip in (
        ('3D View', 'VIEW_3D', "view3d.remesh_mode_toggle", 'R', None),
        ('Mesh', 'EMPTY', "view3d.remesh_stroke", 'D', False),
        ('Mesh', 'EMPTY', "view3d.remesh_stroke", 'E', True),
        ('Mesh', 'EMPTY', "mesh.remesh_lock", 'L', None),
        ('Mesh', 'EMPTY', "mesh.remesh_relax", 'S', None),
    ):
        km = config.keymaps.new(name=name, space_type=space)
        kmi = km.keymap_items.new(op, key, 'PRESS', ctrl=True, alt=True, shift=True)
        if strip is not None:
            kmi.properties.strip = strip
        keymaps.append((km, kmi))


def unregister():
    for km, item in keymaps:
        km.keymap_items.remove(item)
    keymaps.clear()

