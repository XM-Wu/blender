# SPDX-FileCopyrightText: 2026 Blender Authors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Incremental edit-mesh UV distortion cache. BMesh references live only during transforms."""
import bmesh
import numpy as np

CHUNK_SIZE = 1024


class EditCache:
    def __init__(self, context):
        self.sources = []
        vertex_count = loop_count = 0
        triangles = []
        edge_masks = []
        edge_states = []
        vertex_indices = []
        for obj in context.objects_in_mode_unique_data:
            if obj.type != 'MESH' or not obj.visible_get():
                continue
            bm = bmesh.from_edit_mesh(obj.data)
            layer = bm.loops.layers.uv.active
            if layer is None:
                continue
            verts = list(bm.verts)
            vi = {v: vertex_count + i for i, v in enumerate(verts)}
            loops = []
            li = {}
            for tri in bm.calc_loop_triangles():
                if tri[0].face.hide:
                    continue
                for loop in tri:
                    if loop not in li:
                        li[loop] = loop_count + len(loops)
                        loops.append(loop)
                        vertex_indices.append(vi[loop.vert])
                triangles.append([li[loop] for loop in tri])
                edge_masks.append([float(tri[(i+1)%3].link_loop_next == tri[(i+2)%3]
                                         or tri[(i+2)%3].link_loop_next == tri[(i+1)%3]) for i in range(3)])
                states = []
                for i in range(3):
                    a, b = tri[(i+1)%3], tri[(i+2)%3]
                    edge_loop = a if a.link_loop_next == b else b
                    uv_selected = (edge_loop.edge.select if context.scene.tool_settings.use_uv_select_sync
                                   else edge_loop[layer].select_edge)
                    states.append((edge_loop.edge.select, uv_selected, edge_loop.edge.seam))
                edge_states.append(states)
            self.sources.append((obj, bm, layer, verts, loops, (len(bm.verts), len(bm.edges), len(bm.faces))))
            vertex_count += len(verts)
            loop_count += len(loops)
        self.triangles = np.asarray(triangles, dtype=np.int32).reshape((-1, 3))
        self.edge_masks = np.asarray(edge_masks, dtype=np.float32).reshape((-1, 3))
        self.edge_states = np.asarray(edge_states, dtype=bool).reshape((-1, 3, 3))
        self.loop_vertices = np.asarray(vertex_indices, dtype=np.int32)
        self.uv = np.zeros((loop_count, 2), dtype=np.float64)
        self.world = np.zeros((vertex_count, 3), dtype=np.float64)
        self.area = np.zeros(len(triangles), dtype=np.float64)
        self.uv_area = self.area.copy()
        self.density = self.area.copy()
        self.batches = {'positions': {}, 'uvs': {}}
        self.generation = 0
        self.update(force=True)

    def update(self, force=False):
        world, uv = [], []
        for obj, bm, layer, verts, loops, counts in self.sources:
            if not bm.is_valid or counts != (len(bm.verts), len(bm.edges), len(bm.faces)):
                raise ValueError('Topology changed')
            coords = np.asarray([tuple(v.co) for v in verts], dtype=np.float64).reshape((-1, 3))
            matrix = np.asarray(obj.matrix_world, dtype=np.float64)
            world.append(coords @ matrix[:3, :3].T + matrix[:3, 3])
            uv.extend(tuple(loop[layer].uv) for loop in loops)
        world = np.concatenate(world) if world else self.world.copy()
        uv = np.asarray(uv, dtype=np.float64).reshape((-1, 2))
        moved_vertices = np.any(world != self.world, axis=1)
        moved_loops = np.any(uv != self.uv, axis=1)
        geom_changed = np.any(moved_vertices[self.loop_vertices[self.triangles]], axis=1)
        uv_changed = np.any(moved_loops[self.triangles], axis=1)
        if force:
            geom_changed[:] = True
            uv_changed[:] = True
        changed = geom_changed | uv_changed
        self.world, self.uv = world, uv
        if not np.any(changed) and not force:
            return False
        idx = self.loop_vertices[self.triangles[geom_changed]]
        p = world[idx]
        self.area[geom_changed] = np.linalg.norm(np.cross(p[:, 1]-p[:, 0], p[:, 2]-p[:, 0]), axis=1)*.5
        p = uv[self.triangles[uv_changed]]
        a, b = p[:, 1]-p[:, 0], p[:, 2]-p[:, 0]
        self.uv_area[uv_changed] = np.abs(a[:, 0]*b[:, 1]-a[:, 1]*b[:, 0])*.5
        valid = self.area > 1e-20
        previous = self.density.copy()
        self.density[changed] = np.divide(self.uv_area[changed], self.area[changed],
                                         out=np.zeros(np.count_nonzero(changed)), where=valid[changed])
        density_changed = previous != self.density
        for name, dirty in (('positions', geom_changed | density_changed), ('uvs', uv_changed | density_changed)):
            for chunk in np.unique(np.flatnonzero(dirty) // CHUNK_SIZE):
                self.batches[name].pop(int(chunk), None)
        total = self.area[valid].sum()
        uv_total = self.uv_area[valid].sum()
        self.normalization = total/uv_total if uv_total > 1e-20 and total > 1e-20 else 0.0
        ratios = self.density[valid]*self.normalization
        weights = self.area[valid]/total if total > 1e-20 else np.zeros_like(ratios)
        bins = np.clip((ratios*32).astype(np.int64), 0, 63)
        self.bins = np.bincount(bins, weights=weights, minlength=64).tolist() if self.normalization else [0.0]*64
        self.compressed = float(weights[ratios < .95].sum()) if self.normalization else 0.0
        self.stretched = float(weights[ratios > 1.05].sum()) if self.normalization else 0.0
        self.count = int(np.count_nonzero(valid)) if self.normalization else 0
        self.generation += 1
        self.changed_triangles = int(np.count_nonzero(changed))
        return True

    def chunk_arrays(self, name, chunk):
        sl = slice(chunk*CHUNK_SIZE, (chunk+1)*CHUNK_SIZE)
        tri = self.triangles[sl]
        if name == 'positions':
            positions = self.world[self.loop_vertices[tri]].reshape((-1, 3))
        else:
            uvs = self.uv[tri].reshape((-1, 2))
            positions = np.column_stack((uvs, np.zeros(len(uvs))))
        density = np.where(self.area[sl] > 1e-20, self.density[sl], -1.0)
        return np.asarray(positions, dtype=np.float32), np.repeat(density.astype(np.float32), 3)

    def edge_attributes(self, chunk):
        sl = slice(chunk*CHUNK_SIZE, (chunk+1)*CHUNK_SIZE)
        masks = self.edge_masks[sl]
        return np.tile(np.eye(3, dtype=np.float32), (len(masks), 1)), np.repeat(masks, 3, axis=0)

    def wire_arrays(self, name, chunk, normal, selected, seam):
        sl = slice(chunk*CHUNK_SIZE, (chunk+1)*CHUNK_SIZE)
        positions, _ = self.chunk_arrays(name, chunk)
        triangles = positions.reshape((-1, 3, 3))
        endpoints = triangles[:, ((1, 2), (2, 0), (0, 1)), :]
        mask = self.edge_masks[sl].astype(bool)
        states = self.edge_states[sl][mask]
        colors = np.tile(np.asarray(normal, dtype=np.float32), (len(states), 1))
        if name == 'positions':
            colors[states[:, 2]] = seam
        colors[states[:, 0 if name == 'positions' else 1]] = selected
        return endpoints[mask].reshape((-1, 3)), np.repeat(colors, 2, axis=0)
