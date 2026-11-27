# SPDX-License-Identifier: GPL-2.0-or-later
"""Finite triangle-mesh contact for gel. Coordinates are in world-space meters."""

import numpy as np

from _xblender_gel_native import project_scene_mesh


def dot(a, b):
    return np.einsum('ij,ij->i', a, b)


def box_hits(low, high, lo, hi):
    return np.all((high >= lo) & (low <= hi), axis=-1)


class TriangleTree:
    """Balanced hierarchy over swept triangle bounds; refit for moving vertices."""
    def __init__(self, vertices, faces):
        if (faces.dtype != np.int32 or faces.ndim != 2 or faces.shape[1] != 3 or
                max(len(vertices), len(faces)) >= 2**31 // 6 or faces.min() < 0 or faces.max() >= len(vertices)):
            raise ValueError("Invalid collision mesh topology")
        centers = vertices[faces].mean(axis=1)
        leaves = 1 << max(0, (max(1, len(faces) // 8)).bit_length() - 1)
        self.order = np.arange(len(faces), dtype=np.int32)
        self.ranges = np.empty((leaves, 2), dtype=np.int32)

        def split(node, begin, end):
            if node >= leaves:
                self.ranges[node - leaves] = begin, end
                return
            ids = self.order[begin:end]
            axis = np.ptp(centers[ids], axis=0).argmax()
            mid = (begin + end) // 2
            ids[:] = ids[np.argpartition(centers[ids, axis], mid - begin)]
            split(node * 2, begin, mid)
            split(node * 2 + 1, mid, end)

        split(1, 0, len(faces))
        self.faces = np.ascontiguousarray(faces[self.order], dtype=np.int32)
        self.low = np.empty((2 * leaves, 3))
        self.high = np.empty_like(self.low)

    def refit(self, old, new, margin):
        tri = new[self.faces]
        low, high = tri.min(axis=1), tri.max(axis=1)
        if old is not new:
            tri = old[self.faces]
            low = np.minimum(low, tri.min(axis=1))
            high = np.maximum(high, tri.max(axis=1))
        self.face_low = low - margin
        self.face_high = high + margin
        n = len(self.ranges)
        self.low[n:] = np.minimum.reduceat(self.face_low, self.ranges[:, 0], axis=0)
        self.high[n:] = np.maximum.reduceat(self.face_high, self.ranges[:, 0], axis=0)
        while n > 1:
            self.low[n // 2:n] = np.minimum(self.low[n:2*n:2], self.low[n+1:2*n:2])
            self.high[n // 2:n] = np.maximum(self.high[n:2*n:2], self.high[n+1:2*n:2])
            n //= 2

    def candidates(self, low, high, mobile):
        points = np.flatnonzero(mobile & box_hits(low, high, self.low[1], self.high[1]))
        nodes = np.ones(len(points), dtype=np.int32)
        while len(nodes) and nodes[0] < len(self.ranges):
            nodes = (2 * nodes[:, None] + (0, 1)).ravel()
            points = np.repeat(points, 2)
            keep = box_hits(low[points], high[points], self.low[nodes], self.high[nodes])
            nodes, points = nodes[keep], points[keep]
        if not len(points):
            return points, nodes
        first, last = self.ranges[nodes - len(self.ranges)].T
        sizes = last - first
        bases = np.cumsum(sizes) - sizes
        faces = np.arange(sizes.sum()) + np.repeat(first - bases, sizes)
        points = np.repeat(points, sizes)
        keep = box_hits(low[points], high[points], self.face_low[faces], self.face_high[faces])
        return points[keep], faces[keep]


def face_coordinates(p, a, ab, ac, normal, area):
    delta = p - a
    u = dot(np.cross(delta, ac), normal) / area
    v = dot(np.cross(ab, delta), normal) / area
    return np.column_stack((1 - u - v, u, v))


def face_normal(tri):
    ab, ac = tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]
    cross = np.cross(ab, ac)
    area = np.linalg.norm(cross, axis=1)
    return cross / np.maximum(area[:, None], 1e-20), area, ab, ac


class MeshContact:
    def __init__(self, vertices, faces):
        self.tree = TriangleTree(vertices, faces)
        self.old = self.new = vertices
        self.margin = -1.

    def update(self, old, new, margin):
        self.moving = old is not new
        dirty = self.moving or old is not self.old or new is not self.new or margin != self.margin
        self.old = np.ascontiguousarray(old, dtype=np.float64)
        self.new = np.ascontiguousarray(new, dtype=np.float64)
        self.margin = margin
        if dirty:
            self.tree.refit(self.old, self.new, margin)

    def project(self, previous, points, velocities, mobile, t0, t1, seconds, friction):
        tree = self.tree
        if not box_hits(np.minimum(points.min(0), previous.min(0)), np.maximum(points.max(0), previous.max(0)),
                        tree.low[1], tree.high[1]):
            return
        if project_scene_mesh(self, previous, points, velocities, mobile, t0, t1, seconds, friction):
            return
        # Bound temporary point/triangle arrays on detailed display meshes.
        for start in range(0, len(points), 512):
            chunk = slice(start, start + 512)
            self.project_numpy(previous[chunk], points[chunk], None if velocities is None else velocities[chunk],
                               mobile[chunk], t0, t1, seconds, friction)

    def project_numpy(self, previous, points, velocities, mobile, t0, t1, seconds, friction):
        tree = self.tree
        ids, fi = tree.candidates(np.minimum(previous, points), np.maximum(previous, points), mobile)
        if not len(ids):
            return
        faces = tree.faces[fi]
        tri = self.new[faces]
        if self.moving:
            motion = tri - self.old[faces]
            tri = self.old[faces] + motion * t1
            before = self.old[faces] + motion * t0
            old_normal, old_area, _, _ = face_normal(before)
        normal, area, ab, ac = face_normal(tri)
        if self.moving:
            d0 = dot(previous[ids] - before[:, 0], old_normal)
        else:
            d0 = dot(previous[ids] - tri[:, 0], normal)
        d1 = dot(points[ids] - tri[:, 0], normal)
        side = np.ones_like(d0) if self.culling else np.where(d0 < 0., -1., 1.)
        d0, d1 = d0 * side, d1 * side
        depth = self.margin - d1
        valid = (area > 1e-16) & (depth > 0.)
        if self.moving:
            valid &= old_area > 1e-16
        if self.culling:
            valid &= d0 >= -self.margin
        if not valid.any():
            return
        # Intersect the trajectory with the finite face, not its infinite supporting plane.
        toi = np.clip(np.divide(d0 - self.margin, d0 - d1, out=np.zeros_like(d0),
                                where=d0 > d1 + 1e-12), 0., 1.)
        hit = previous[ids] + toi[:, None] * (points[ids] - previous[ids])
        test_tri = before + (tri - before) * toi[:, None, None] if self.moving else tri
        hn, ha, hab, hac = face_normal(test_tri) if self.moving else (normal, area, ab, ac)
        bary = face_coordinates(hit, test_tri[:, 0], hab, hac, hn, np.maximum(ha, 1e-20))
        face_hit = np.all(bary >= -1e-8, axis=1)
        n = normal * side[:, None]
        # Points entering the collision shell around a finite edge or corner.
        if not face_hit.all():
            best = np.full(len(ids), self.margin ** 2)
            for j, k in ((0, 1), (1, 2), (2, 0)):
                edge = tri[:, k] - tri[:, j]
                along = np.clip(dot(points[ids] - tri[:, j], edge) / np.maximum(dot(edge, edge), 1e-20), 0., 1.)
                delta = points[ids] - (tri[:, j] + along[:, None] * edge)
                dist2 = dot(delta, delta)
                use = (~face_hit) & (dist2 < best) & (dist2 > 1e-24)
                best[use] = dist2[use]
                length = np.sqrt(dist2[use])
                n[use] = delta[use] / length[:, None]
                depth[use] = self.margin - length
                bary[use] = 0.
                bary[use, j], bary[use, k] = 1. - along[use], along[use]
            valid &= face_hit | (best < self.margin ** 2)
        indices = np.flatnonzero(valid)
        if not len(indices):
            return
        # Earliest face per point. Adjacent coplanar triangles must not apply the impulse twice.
        rank = indices[np.lexsort((-depth[indices], toi[indices], ids[indices]))]
        take = rank[np.r_[True, ids[rank[1:]] != ids[rank[:-1]]]]
        hit_ids, normals = ids[take], n[take]
        points[hit_ids] += depth[take, None] * normals
        if velocities is not None:
            surface_v = np.einsum('ij,ijk->ik', bary[take], motion[take]) / seconds if self.moving else 0.
            relative = velocities[hit_ids] - surface_v
            vn = np.sum(relative * normals, axis=1, keepdims=True)
            inward = np.minimum(vn, 0.)
            tangent = relative - vn * normals
            slip = np.minimum(1., -friction * self.friction * inward /
                              np.maximum(np.linalg.norm(tangent, axis=1, keepdims=True), 1e-12))
            velocities[hit_ids] -= inward * normals + slip * tangent


class SceneContacts:
    """Sample evaluated Collision meshes once per input pose, reusing unchanged BVHs."""
    def __init__(self, owner, meters):
        self.owner = owner
        self.meters = meters
        self.meshes = {}
        self.travel = 0.

    def sample(self, context, seconds, gel=None, interactive=False):
        graph = context.evaluated_depsgraph_get()
        active = {}
        self.travel = 0.
        for obj in context.view_layer.objects:
            if obj.type != 'MESH' or obj == self.owner or not any(
                    m.type == 'COLLISION' and m.show_viewport for m in obj.modifiers):
                continue
            ev = obj.evaluated_get(graph)
            if not ev.collision or not ev.collision.use:
                continue
            mesh = ev.data
            if mesh is None:
                continue
            mesh.calc_loop_triangles()
            if not mesh.loop_triangles:
                continue
            raw = np.empty(len(mesh.vertices) * 3, np.float32)
            mesh.vertices.foreach_get('co', raw)
            matrix = np.asarray(ev.matrix_world, dtype=np.float64)
            verts = (raw.reshape(-1, 3) @ matrix[:3, :3].T + matrix[:3, 3]) * self.meters
            verts = np.ascontiguousarray(verts, dtype=np.float64)
            indices = np.empty(len(mesh.loop_triangles) * 3, np.int32)
            mesh.loop_triangles.foreach_get('vertices', indices)
            faces = indices.reshape(-1, 3)
            if np.linalg.det(matrix[:3, :3]) < 0.:
                faces = faces[:, [0, 2, 1]].copy()
            entry = self.meshes.get(obj.session_uid)
            if entry is None or len(entry.new) != len(verts) or not np.array_equal(faces, entry.topology):
                entry = MeshContact(verts, faces)
                entry.topology = faces
            old = entry.new
            entry.target = verts
            entry.separated = False
            if np.array_equal(verts, old):
                verts = old
            elif gel is not None:
                travel = np.linalg.norm(verts - old, axis=1).max()
                pad = ev.collision.thickness_outer * self.meters + gel.min_altitude
                pad += seconds * np.linalg.norm(gel.v, axis=1).max()
                low, high = gel.x.min(0) - pad, gel.x.max(0) + pad
                entry.separated = not box_hits(verts.min(0), verts.max(0), low, high)
                if not box_hits(np.minimum(verts.min(0), old.min(0)), np.maximum(verts.max(0), old.max(0)), low, high):
                    old = verts
                    travel = 0.
                elif interactive and travel > gel.contact_speed * seconds:
                    verts = old + (verts - old) * (gel.contact_speed * seconds / travel)
                    travel = gel.contact_speed * seconds
                self.travel = max(self.travel, travel)
            entry.culling = ev.collision.use_culling
            entry.friction = max(0., ev.collision.cloth_friction / 5.)
            entry.update(old, verts, max(ev.collision.thickness_outer * self.meters, 1e-6 * self.meters))
            active[obj.session_uid] = entry
        self.meshes = active

    def advance_live(self, gel, advance):
        from _xblender_gel import _ContactStepRejected
        initial_x, initial_v = gel.x.copy(), gel.v.copy()
        moves = [(m, m.old, m.new) for m in self.meshes.values() if m.moving]
        travel = self.travel
        for fraction, refinement in ((1., 1), (.5, 2), (.125, 4), (0., 4)):
            if fraction != 1.:
                self.travel = travel * fraction
                for mesh, old, new in moves:
                    if fraction:
                        mesh.update(old, old + fraction * (new - old), mesh.margin)
                    else:
                        at = mesh.target if mesh.separated else old
                        mesh.update(at, at, mesh.margin)
            try:
                return advance(refinement)
            except (_ContactStepRejected, FloatingPointError):
                gel.x[:], gel.v[:] = initial_x, initial_v
                if fraction == 0.:
                    raise

    def residual(self, start, positions, mobile, t0, t1, seconds):
        projected = positions.copy()
        self.project(start, projected, None, mobile, t0, t1, seconds, 0.)
        return np.linalg.norm(projected - positions, axis=1).max()

    def project(self, previous, positions, velocity, mobile, t0, t1, seconds, friction):
        for mesh in self.meshes.values():
            mesh.project(previous, positions, velocity, mobile, t0, t1, seconds, friction)
