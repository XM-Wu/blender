# SPDX-FileCopyrightText: 2026 Blender Authors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Incremental edit-mesh UV distortion cache. BMesh references live only during transforms."""
import bmesh
import numpy as np

CHUNK_SIZE = 1024


def triangle_shape_ratios(world, uv):
    """Singular-value ratio of the triangle Jacobian; invariant to UV similarity transforms."""
    world = np.asarray(world, dtype=np.float64).reshape((-1, 3, 3))
    uv = np.asarray(uv, dtype=np.float64).reshape((-1, 3, 2))
    e1, e2 = world[:, 1]-world[:, 0], world[:, 2]-world[:, 0]
    length = np.linalg.norm(e1, axis=1)
    cross = np.linalg.norm(np.cross(e1, e2), axis=1)
    valid = (length > 1e-20) & (cross > 1e-20)
    x = np.where(valid, length, 1.0)
    y = np.where(valid, cross/x, 1.0)
    projection = np.sum(e1*e2, axis=1)/x
    u, v = uv[:, 1]-uv[:, 0], uv[:, 2]-uv[:, 0]
    col0 = u/x[:, None]
    col1 = (v-col0*projection[:, None])/y[:, None]
    trace = np.sum(col0*col0 + col1*col1, axis=1)
    determinant = np.abs(col0[:, 0]*col1[:, 1]-col0[:, 1]*col1[:, 0])
    maximum_squared = .5*(trace + np.sqrt(np.maximum(0, trace*trace-4*determinant*determinant)))
    ratio = np.divide(maximum_squared, determinant, out=np.full(len(world), 1e6), where=determinant > 1e-30)
    return np.where(valid, np.clip(ratio, 1.0, 1e6), 1.0)


def shape_statistics(ratios, areas):
    valid = areas > 1e-20
    total = areas[valid].sum()
    weights = areas[valid]/total if total > 1e-20 else np.zeros(np.count_nonzero(valid))
    values = ratios[valid]
    indices = np.clip(((values-1)*64/3).astype(np.int64), 0, 63)
    return (np.bincount(indices, weights=weights, minlength=64).tolist(),
            float(weights[values > 2].sum()), float(weights[values > 4].sum()))


def island_area_values(areas, uv_areas, triangle_faces, face_islands):
    """Per-polygon relative linear scale, matching the tested RizomUV formula."""
    face_area = np.bincount(triangle_faces, weights=areas, minlength=len(face_islands))
    face_uv = np.bincount(triangle_faces, weights=uv_areas, minlength=len(face_islands))
    valid = face_area > 1e-20
    density = np.divide(face_uv, face_area, out=np.zeros_like(face_area), where=valid)
    island_area = np.bincount(face_islands, weights=np.where(valid, face_area, 0))
    island_uv = np.bincount(face_islands, weights=np.where(valid, face_uv, 0))
    norm = np.divide(island_area, island_uv, out=np.zeros_like(island_area), where=island_uv > 1e-20)
    ratios = np.sqrt(np.maximum(0, density * norm[face_islands]))
    return density[triangle_faces], norm, ratios[triangle_faces]


class EditCache:
    def __init__(self, context):
        self.sources = []
        self.owned_bmeshes = []
        triangle_faces, connections, connection_loops = [], [], []
        face_count = 0
        vertex_count = loop_count = 0
        triangles = []
        edge_masks = []
        edge_states = []
        vertex_indices = []
        objects = context.objects_in_mode_unique_data if context.mode == 'EDIT_MESH' else context.selected_objects
        for obj in objects:
            if obj.type != 'MESH' or not obj.visible_get():
                continue
            if obj.mode == 'EDIT':
                bm = bmesh.from_edit_mesh(obj.data)
            else:
                bm = bmesh.new()
                bm.from_mesh(obj.data)
                self.owned_bmeshes.append(bm)
            layer = bm.loops.layers.uv.active
            if layer is None:
                continue
            faces = {f: face_count+i for i,f in enumerate(f for f in bm.faces if not f.hide)}
            face_count += len(faces)
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
                triangle_faces.append(faces[tri[0].face])
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
            for edge in bm.edges:
                radial = [loop for loop in edge.link_loops if loop.face in faces]
                for ai, a in enumerate(radial):
                    for b in radial[ai+1:]:
                        if any(loop not in li for loop in (a, a.link_loop_next, b, b.link_loop_next)):
                            continue
                        bn = b.link_loop_next
                        an = a.link_loop_next
                        connection_loops.append((li[a], li[an], li[b if b.vert == a.vert else bn],
                                                 li[bn if b.vert == a.vert else b]))
                        connections.append((faces[a.face], faces[b.face]))
            self.sources.append((obj, bm, layer, verts, loops, (len(bm.verts), len(bm.edges), len(bm.faces))))
            vertex_count += len(verts)
            loop_count += len(loops)
        self.triangle_faces = np.asarray(triangle_faces, dtype=np.int32)
        self.face_count = face_count
        self.connections = np.asarray(connections, dtype=np.int32).reshape((-1, 2))
        self.connection_loops = np.asarray(connection_loops, dtype=np.int32).reshape((-1, 4))
        self.connected = None
        self.face_islands = np.arange(face_count, dtype=np.int32)
        self.face_density = np.zeros(len(triangles))
        self.island_norm = np.zeros(face_count)
        self.island_texture = None
        self.triangles = np.asarray(triangles, dtype=np.int32).reshape((-1, 3))
        self.edge_masks = np.asarray(edge_masks, dtype=np.float32).reshape((-1, 3))
        self.edge_states = np.asarray(edge_states, dtype=bool).reshape((-1, 3, 3))
        self.loop_vertices = np.asarray(vertex_indices, dtype=np.int32)
        self.uv = np.zeros((loop_count, 2), dtype=np.float64)
        self.world = np.zeros((vertex_count, 3), dtype=np.float64)
        self.area = np.zeros(len(triangles), dtype=np.float64)
        self.uv_area = self.area.copy()
        self.density = self.area.copy()
        self.shape = np.ones(len(triangles), dtype=np.float64)
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
        old_shape = self.shape.copy()
        self.shape[changed] = triangle_shape_ratios(world[self.loop_vertices[self.triangles[changed]]],
                                                    uv[self.triangles[changed]])
        connected = np.all(np.sum((uv[self.connection_loops[:, :2]] -
                                   uv[self.connection_loops[:, 2:]])**2, axis=2) <= 1e-12, axis=1)
        old_islands = self.face_islands.copy()
        if self.connected is None or not np.array_equal(connected, self.connected):
            parents = list(range(self.face_count))
            def root(i):
                while parents[i] != i:
                    parents[i] = parents[parents[i]]
                    i = parents[i]
                return i
            for a, b in self.connections[connected]:
                parents[root(int(b))] = root(int(a))
            _, self.face_islands = np.unique([root(i) for i in range(self.face_count)], return_inverse=True)
            self.connected = connected
        old_face_density = self.face_density.copy()
        norm_before = self.island_norm
        self.face_density, self.island_norm, self.distortion = island_area_values(
            self.area, self.uv_area, self.triangle_faces, self.face_islands)
        if not np.array_equal(norm_before, self.island_norm):
            self.island_texture = None
        density_changed = ((previous != self.density) | (old_shape != self.shape) |
                           (old_face_density != self.face_density) |
                           (old_islands[self.triangle_faces] != self.face_islands[self.triangle_faces]))
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
        values = self.distortion[valid]
        self.distortion_bins = np.bincount(np.clip((values*32).astype(np.int64), 0, 63),
                                           weights=weights, minlength=64).tolist()
        self.distortion_low = float(weights[values < .95].sum())
        self.distortion_high = float(weights[values > 1.05].sum())
        self.shape_bins, self.shape_over_two, self.shape_over_four = shape_statistics(self.shape, self.area)
        self.count = int(np.count_nonzero(valid))
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

    def chunk_island_values(self, chunk):
        sl = slice(chunk*CHUNK_SIZE, (chunk+1)*CHUNK_SIZE)
        return (np.repeat(self.face_density[sl].astype(np.float32), 3),
                np.repeat(self.face_islands[self.triangle_faces[sl]].astype(np.float32), 3))

    def normalization_texture(self):
        if self.island_texture is None:
            import gpu
            count = max(1, len(self.island_norm))
            width = min(1024, count)
            height = (count+width-1)//width
            values = np.zeros(width*height, dtype=np.float32)
            values[:len(self.island_norm)] = self.island_norm
            self.island_texture = gpu.types.GPUTexture((width, height), format='R32F',
                data=gpu.types.Buffer('FLOAT', len(values), values))
        return self.island_texture

    def __del__(self):
        for bm in self.owned_bmeshes:
            if bm.is_valid:
                bm.free()

    def chunk_shape(self, chunk):
        return np.repeat(self.shape[chunk*CHUNK_SIZE:(chunk+1)*CHUNK_SIZE].astype(np.float32), 3)

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
