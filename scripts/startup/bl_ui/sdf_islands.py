# SPDX-License-Identifier: GPL-2.0-or-later

"""Interactive mask selection in the SDF editor, in UV tile coordinates."""

import numpy as np


class IslandPicker:
    def __init__(self, objects, uv_name, *, saved=None):
        self.islands = []
        self.edges = []
        self.selected = set()
        self.partial = {}
        self.target = None
        self.fill_batch = None
        self.wire_batch = None
        self.objects = tuple(objects)
        for obj in self.objects:
            mesh = obj.data
            name, faces = (saved or {}).get(obj, (uv_name, set()))
            layer = mesh.uv_layers.get(name) if name else mesh.uv_layers.active
            if not layer:
                raise ValueError(f"{obj.name} has no matching UV map")
            self.add_mesh(obj, mesh, layer, faces)

    def add_mesh(self, obj, mesh, layer, saved_faces):
        # Two faces connect only across a mesh edge with matching UVs at both
        # ends. Stacked but disconnected islands must be separately pickable.
        parents = list(range(len(mesh.polygons)))

        def root(index):
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        uv = np.empty((len(mesh.loops), 2), dtype=np.float32)
        layer.uv.foreach_get("vector", uv.ravel())
        uvs = list(map(tuple, uv))
        connections = {}
        for face in mesh.polygons:
            if face.hide and face.index not in saved_faces:
                continue
            loops = face.loop_indices
            for a, b in zip(loops, (*loops[1:], loops[0])):
                edge = mesh.loops[a].edge_index
                ends = (uvs[a], uvs[b]) if mesh.loops[a].vertex_index < mesh.loops[b].vertex_index else (uvs[b], uvs[a])
                previous = connections.setdefault((edge, ends), face.index)
                parents[root(face.index)] = root(previous)

        groups = {}
        for face in mesh.polygons:
            if not face.hide or face.index in saved_faces:
                groups.setdefault(root(face.index), []).append(face.index)
        tris = {}
        for tri in mesh.loop_triangles:
            if not mesh.polygons[tri.polygon_index].hide or tri.polygon_index in saved_faces:
                tris.setdefault(root(tri.polygon_index), []).append(tri.loops[:])
        for number, (group, faces) in enumerate(groups.items(), 1):
            if group not in tris:
                continue
            triangles = uv[tris[group]]
            index = len(self.islands)
            included = saved_faces.intersection(faces)
            if included:
                self.selected.add(index)
                if len(included) != len(faces):
                    # A previously saved face selection can cover part of a current island.
                    subset = [t.loops[:] for t in mesh.loop_triangles if t.polygon_index in included]
                    self.partial[index] = sorted(included), uv[subset]
            self.islands.append((obj, layer.name, faces, triangles, f"{obj.name} · {number}"))

        self.edges.extend(ends for edge, ends in connections)

    def hits(self, uv):
        p = np.asarray(uv)
        hits = []
        for index, (_, _, _, tris, _) in enumerate(self.islands):
            a, b, c = tris.transpose(1, 0, 2)
            def side(start, end):
                return (end[:, 0] - start[:, 0]) * (p[1] - start[:, 1]) - (end[:, 1] - start[:, 1]) * (p[0] - start[:, 0])
            ab, bc, ca = side(a, b), side(b, c), side(c, a)
            inside = ((ab >= 0) & (bc >= 0) & (ca >= 0)) | ((ab <= 0) & (bc <= 0) & (ca <= 0))
            if np.any(inside & (abs(ab + bc + ca) > 1e-12)):
                hits.append(index)
        return hits

    def toggle(self, index):
        if index in self.partial:
            # First click extends a partial selection to the entire island.
            del self.partial[index]
        else:
            self.selected.symmetric_difference_update({index})
        self.fill_batch = None

    def draw(self, region):
        import gpu
        from gpu_extras.batch import batch_for_shader

        if not self.islands:
            return
        shader = gpu.shader.from_builtin('SMOOTH_COLOR')
        if self.fill_batch is None:
            # Draw selected islands last so overlapping selections remain visible.
            order = sorted(range(len(self.islands)), key=self.selected.__contains__)
            positions, colors = [], []
            for i in order:
                points = self.islands[i][3].reshape(-1, 2)
                color = (1.0, .45, .08, 1) if i in self.selected and i not in self.partial else (.20, .30, .40, 1)
                positions.append(points)
                colors.append(np.broadcast_to(np.float32(color), (len(points), 4)))
            for _, triangles in self.partial.values():
                points = triangles.reshape(-1, 2)
                positions.append(points)
                colors.append(np.broadcast_to(np.float32((1, .45, .08, 1)), (len(points), 4)))
            self.fill_batch = batch_for_shader(shader, 'TRIS', {
                'pos': np.concatenate(positions), 'color': np.concatenate(colors)})
        wire = gpu.shader.from_builtin('UNIFORM_COLOR')
        if self.wire_batch is None:
            self.wire_batch = batch_for_shader(wire, 'LINES', {'pos': np.asarray(self.edges).reshape(-1, 2)})
        origin = region.view2d.view_to_region(0, 0, clip=False)
        end = region.view2d.view_to_region(1, 1, clip=False)
        with gpu.matrix.push_pop():
            gpu.matrix.translate((*origin, 0))
            gpu.matrix.scale((end[0] - origin[0], end[1] - origin[1], 1))
            shader.bind()
            self.fill_batch.draw(shader)
            wire.bind()
            wire.uniform_float("color", (.07, .08, .10, 1))
            self.wire_batch.draw(wire)
