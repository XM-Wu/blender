# SPDX-License-Identifier: GPL-2.0-or-later
"""Tetrahedral gel dynamics in world space (meters, kilograms, seconds)."""

import math

import numpy as np


def contact(points, start, end):
    """Return the center-line parameter, outward normal and radial distance."""
    axis = end - start
    t = np.clip(np.sum((points - start) * axis, axis=1) / max(axis @ axis, 1e-20), 0.0, 1.0)
    delta = points - (start + t[:, None] * axis)
    distance = np.linalg.norm(delta, axis=1)
    normal = delta / np.maximum(distance[:, None], 1e-12)
    if np.any(distance < 1e-12):
        direction = axis / max(np.linalg.norm(axis), 1e-12)
        fallback = np.eye(3)[np.argmin(np.abs(direction))]
        fallback -= direction * (direction @ fallback)
        normal[distance < 1e-12] = fallback / np.linalg.norm(fallback)
    return t, normal, distance


def triangle_contacts(tri, start, end):
    """Closest points between a segment and triangles, including face crossings."""
    a, b, c = tri.transpose(1, 0, 2)
    axis = end - start
    length = axis @ axis
    ab, ac = b - a, c - a
    normal = np.cross(ab, ac)
    area2 = np.einsum("ij,ij->i", normal, normal)
    denom = np.maximum(area2, 1e-24)
    size = len(tri)
    best = np.full(size, np.inf)
    bary = np.zeros((size, 3))
    along = np.zeros(size)

    def consider(weights, t, valid):
        delta = np.einsum("ij,ijk->ik", weights, tri) - (start + t[:, None] * axis)
        dist2 = np.einsum("ij,ij->i", delta, delta)
        take = valid & (dist2 < best)
        best[take] = dist2[take]
        bary[take] = weights[take]
        along[take] = t[take]

    # An interior face minimum is either a projected endpoint, or the
    # intersection with the plane. Parallel coplanar segments also have edge minima.
    signed = np.sum((start - a) * normal, axis=1)
    slope = normal @ axis
    cross_t = np.divide(-signed, slope, out=np.zeros(size), where=np.abs(slope) > 1e-16)
    for t in (np.zeros(size), np.ones(size), np.clip(cross_t, 0., 1.)):
        d = start + t[:, None] * axis - a
        u = np.einsum("ij,ij->i", np.cross(d, ac), normal) / denom
        v = np.einsum("ij,ij->i", np.cross(ab, d), normal) / denom
        w = np.column_stack((1 - u - v, u, v))
        consider(w, t, (area2 > 1e-24) & np.all(w >= 0., axis=1))

    # Constrained segment/segment minimum for each of the triangle edges.
    for i, j in ((0, 1), (1, 2), (2, 0)):
        d = tri[:, i] - start
        e = tri[:, j] - tri[:, i]
        ee = np.maximum(np.einsum("ij,ij->i", e, e), 1e-24)
        ea = e @ axis
        ed = np.einsum("ij,ij->i", e, d)
        ad = d @ axis
        determinant = ee * length - ea * ea
        u = np.clip(np.divide(ea * ad - length * ed, determinant, out=np.zeros(size),
                              where=determinant > 1e-18), 0., 1.)
        t = np.clip((ad + ea * u) / max(length, 1e-24), 0., 1.)
        u = np.clip((ea * t - ed) / ee, 0., 1.)
        w = np.zeros((size, 3))
        w[:, i], w[:, j] = 1 - u, u
        consider(w, t, True)
    delta = np.einsum("ij,ijk->ik", bary, tri) - (start + along[:, None] * axis)
    return bary, along, delta, normal / np.sqrt(denom)[:, None]


def prism_tets(faces, vertex_count, layers):
    """Use matching diagonals on shared prism faces. Index zero is the center."""
    a, b, c = (np.sort(faces, axis=1) + 1).T
    blocks = [np.column_stack((np.zeros_like(a), a, b, c))]
    for _ in range(1, layers):
        A, B, C = a + vertex_count, b + vertex_count, c + vertex_count
        blocks.extend((np.column_stack((a, b, c, C)), np.column_stack((a, b, B, C)),
                       np.column_stack((a, A, B, C))))
        a, b, c = A, B, C
    return np.concatenate(blocks)


class Gel:
    def __init__(self, rest, tets, pinned, density, shear, bulk, damping, friction, surface=None):
        self.x = np.array(rest, dtype=float, copy=True)
        self.v = np.zeros_like(self.x)
        self.tets = tets
        self.pinned = np.array(pinned, dtype=bool)
        self.shear = shear
        self.bulk = bulk
        self.damping = damping
        self.friction = friction
        q = self.x[tets]
        basis = (q[:, 1:] - q[:, :1]).transpose(0, 2, 1)
        determinant = np.linalg.det(basis)
        self.volume = np.abs(determinant) / 6
        if self.volume.min() < self.volume.sum() * 1e-12:
            raise ValueError("Degenerate tetrahedron in the simulation mesh")
        self.inverse = np.linalg.inv(basis)
        self.grad = np.concatenate((-self.inverse.sum(axis=1, keepdims=True), self.inverse), axis=1)
        self.orientation = np.sign(determinant)
        self.indices = tets.ravel()
        self.mass = np.bincount(self.indices, np.repeat(self.volume * density / 4, 4))
        self.inverse_mass = np.where(self.pinned, 0., 1. / self.mass)
        self.surface = np.unique(surface) if surface is not None else np.arange(len(rest))
        self.contact_faces = np.empty((0, 3), dtype=np.int32) if surface is None else surface
        self.rest = self.x.copy()

        # The altitude opposite vertex i is 1 / |grad N_i|. Bound the explicit
        # timestep by the smallest altitude and the longitudinal wave speed.
        altitude = 1.0 / np.linalg.norm(self.grad, axis=2).max()
        wave_speed = math.sqrt((bulk + 4 * shear / 3) / density)
        self.stable_dt = .30 * altitude / wave_speed
        self.min_altitude = altitude

    def elastic_forces(self):
        q = self.x[self.tets]
        F = (q[:, 1:] - q[:, :1]).transpose(0, 2, 1) @ self.inverse
        a, b, c = F.transpose(2, 0, 1)
        cof = np.stack((np.cross(b, c), np.cross(c, a), np.cross(a, b)), axis=2)
        J = np.sum(a * cof[:, :, 0], axis=1)
        # Polynomial Neo-Hookean stress (Smith et al., Stable Neo-Hookean Flesh
        # Simulation, supplement, Eq. 2). This coefficient gives linearized shear
        # modulus mu and bulk modulus K at the stress-free reference shape.
        pressure = (self.bulk + self.shear / 3) * (J - 1) - self.shear
        stress = self.shear * F + pressure[:, None, None] * cof
        forces = -self.volume[:, None, None] * (self.grad @ stress.transpose(0, 2, 1))
        return np.column_stack([np.bincount(self.indices, forces[:, :, i].ravel())
                                for i in range(3)])

    def _impulse(self, mask, normal, surface_v):
        vel = self.v[mask] - surface_v
        inward = np.minimum(np.sum(vel * normal, axis=1, keepdims=True), 0)
        tangent = vel - np.sum(vel * normal, axis=1, keepdims=True) * normal
        slip = np.minimum(1, -self.friction * inward / np.maximum(np.linalg.norm(
            tangent, axis=1, keepdims=True), 1e-12))
        self.v[mask] -= inward * normal + slip * tangent

    def _surface_contact(self, ends, tool_v, radius):
        # Test against triangles as well as nodes: a slender tool can pass
        # through the middle of a stretched face without touching its vertices.
        ids = self.contact_faces
        if not len(ids):
            return
        triangles = self.x[ids]
        low = ends.min(axis=0) - radius
        high = ends.max(axis=0) + radius
        near = np.all((triangles.max(axis=1) >= low) & (triangles.min(axis=1) <= high), axis=1)
        if not near.any():
            return
        ids = ids[near]
        bary, t, delta, face_n = triangle_contacts(triangles[near], *ends)
        distance = np.linalg.norm(delta, axis=1)
        hit = distance < radius
        if not hit.any():
            return
        ids, bary, t = ids[hit], bary[hit], t[hit]
        delta, face_n, distance = delta[hit], face_n[hit], distance[hit]
        n = delta / np.maximum(distance[:, None], 1e-12)
        # If the axis crosses the face, push the membrane toward its interior.
        # Picking an arbitrary radial normal here can thread the rod through the body.
        crossed = distance < 1e-8
        n[crossed] = -face_n[crossed]
        w = self.inverse_mass[ids] * bary
        scale = w / np.maximum((w * bary).sum(axis=1, keepdims=True), 1e-20)
        correction = (radius - distance)[:, None] * n
        motion = tool_v[0] + t[:, None] * (tool_v[1] - tool_v[0])
        rel = np.einsum("ij,ijk->ik", bary, self.v[ids]) - motion
        vn = (rel * n).sum(axis=1, keepdims=True)
        tangent = rel - vn * n
        inward = np.minimum(vn, 0.)
        slip = np.minimum(1., -self.friction * inward / np.maximum(
            np.linalg.norm(tangent, axis=1, keepdims=True), 1e-12))
        impulse = -inward * n - slip * tangent
        count = np.maximum(np.bincount(ids.ravel(), (scale > 0).ravel(), minlength=len(self.x)), 1)[:, None]
        for field, value in ((self.x, correction), (self.v, impulse)):
            adjustment = np.zeros_like(field)
            np.add.at(adjustment, ids.ravel(), (scale[:, :, None] * value[:, None]).reshape(-1, 3))
            field += adjustment / count

    def _limit_compression(self):
        q = self.x[self.tets]
        u, v, w = (q[:, 1:] - q[:, :1]).transpose(1, 0, 2)
        six_volume = np.einsum('ij,ij->i', u, np.cross(v, w)) * self.orientation
        minimum = .15 * 6 * self.volume
        bad = six_volume < minimum
        if not bad.any():
            return
        ids = self.tets[bad]
        u, v, w = u[bad], v[bad], w[bad]
        faces = np.stack((np.cross(v, w), np.cross(w, u), np.cross(u, v)), axis=1)
        grad = np.concatenate((-faces.sum(axis=1, keepdims=True), faces), axis=1)
        grad *= self.orientation[bad, None, None]
        weights = self.inverse_mass[ids]
        inv_mass = np.einsum('ij,ijk,ijk->i', weights, grad, grad)
        correction = (minimum[bad] - six_volume[bad]) / np.maximum(inv_mass, 1e-20)
        speed = np.einsum('ijk,ijk->i', grad, self.v[ids])
        impulse = -np.minimum(speed, 0.) / np.maximum(inv_mass, 1e-20)
        weighted_grad = weights[:, :, None] * grad
        count = np.maximum(np.bincount(ids.ravel(), minlength=len(self.x)), 1)[:, None]
        for field, scale in ((self.x, correction), (self.v, impulse)):
            adjustment = np.zeros_like(field)
            np.add.at(adjustment, ids.ravel(), (weighted_grad * scale[:, None, None]).reshape(-1, 3))
            field += adjustment / count

    def advance(self, seconds, gravity, floor, previous, current, radius, pin_positions=None, incline=None):
        dt_limit = self.stable_dt
        if current is not None:
            travel = np.linalg.norm(current - previous, axis=1).max()
            if travel > 0:
                dt_limit = min(dt_limit, seconds * .35 * min(self.min_altitude, radius) / travel)
        if pin_positions is not None:
            pin_start = self.x[self.pinned].copy()
            pin_motion = pin_positions - pin_start
            pin_velocity = pin_motion / seconds
            pin_travel = np.linalg.norm(pin_motion, axis=1).max()
            if pin_travel > 0:
                dt_limit = min(dt_limit, seconds * .35 * self.min_altitude / pin_travel)
        steps = max(1, math.ceil(seconds / dt_limit))
        if steps > 4096:
            raise ValueError("Simulation needs too many substeps: check scale, stiffness and collider motion")
        dt = seconds / steps
        drag = math.exp(-self.damping * dt)
        if current is not None:
            tool_v = (current - previous) / seconds
        for i in range(steps):
            self.v += dt * (self.elastic_forces() / self.mass[:, None] + gravity)
            self.v *= drag
            self.x += dt * self.v
            if pin_positions is not None:
                # Prescribe both position and velocity before strain/contact projection.
                self.x[self.pinned] = pin_start + pin_motion * ((i + 1) / steps)
                self.v[self.pinned] = pin_velocity
            self._limit_compression()
            if current is not None:
                ends = previous + (current - previous) * ((i + 1) / steps)
                self._surface_contact(ends, tool_v, radius)
                t, n, d = contact(self.x[self.surface], *ends)
                hit = (d < radius) & ~self.pinned[self.surface]
                if hit.any():
                    self.x[self.surface[hit]] += (radius - d[hit, None]) * n[hit]
                    self._impulse(self.surface[hit], n[hit], tool_v[0] + t[hit, None] * (tool_v[1] - tool_v[0]))
            hit = (self.x[:, 2] < floor) & ~self.pinned
            if hit.any():
                self.x[hit, 2] = floor
                self._impulse(hit, np.array((0., 0., 1.)), 0.)
            if incline is not None:
                normal, offset = incline
                depth = offset - self.x @ normal
                hit = (depth > 0) & ~self.pinned
                if hit.any():
                    self.x[hit] += depth[hit, None] * normal
                    self._impulse(hit, normal, 0.)
            if pin_positions is None:
                self.x[self.pinned] = self.rest[self.pinned]
                self.v[self.pinned] = 0.
            if not np.isfinite(self.x).all():
                raise ValueError("Gel simulation diverged: check the mesh and collider motion")
        q = self.x[self.tets]
        if np.any(np.linalg.det((q[:, 1:] - q[:, :1]).transpose(0, 2, 1)) * self.orientation <= 0):
            raise ValueError("A gel element inverted: reduce impact depth or increase simulation resolution")
