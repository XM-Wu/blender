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


class _ContactStepRejected(ValueError):
    """A trial step cannot satisfy contact without folding an element."""


class Gel:
    def __init__(self, rest, tets, pinned, density, shear, bulk, damping, friction, surface=None):
        self.x = np.array(rest, dtype=float, order='C', copy=True)
        self.v = np.zeros_like(self.x)
        self.kernels = None
        self.mesh_contacts = None
        self.mesh_phase = None
        self.tets = tets
        self.pinned = np.array(pinned, dtype=bool)
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
        self.nodal_volume = np.bincount(self.indices, np.repeat(self.volume / 4, 4))
        self.surface = np.unique(surface) if surface is not None else np.arange(len(rest))
        self.surface_mask = np.zeros(len(rest), dtype=bool)
        self.surface_mask[self.surface] = True
        self.contact_faces = np.empty((0, 3), dtype=np.int32) if surface is None else surface
        self.rest = self.x.copy()

        # The altitude opposite vertex i is 1 / |grad N_i|. Bound the explicit
        # timestep by the smallest altitude and the longitudinal wave speed.
        self.min_altitude = 1.0 / np.linalg.norm(self.grad, axis=2).max()
        self.set_material(density, shear, bulk, damping, friction)

    def set_material(self, density, shear, bulk, damping, friction):
        self.shear, self.bulk = shear, bulk
        self.damping, self.friction = damping, friction
        self.mass = self.nodal_volume * density
        self.inverse_mass = np.where(self.pinned, 0., 1. / self.mass)
        self.contact_speed = 2.5 * math.sqrt(shear / density)
        wave_speed = math.sqrt((bulk + 4 * shear / 3) / density)
        self.stable_dt = .30 * self.min_altitude / wave_speed
        if self.kernels:
            self.kernels.material(self)

    def elastic_forces(self):
        if self.kernels:
            return self.kernels.elastic_forces(self.shear, self.bulk)
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

    def _faces_near_capsule(self, ends, radius):
        triangles = self.x[self.contact_faces]
        low, high = ends.min(axis=0) - radius, ends.max(axis=0) + radius
        near = np.all((triangles.max(axis=1) >= low) & (triangles.min(axis=1) <= high), axis=1)
        return self.contact_faces[near]

    def _surface_contact(self, ends, tool_v, radius):
        # Test against triangles as well as nodes: a slender tool can pass
        # through the middle of a stretched face without touching its vertices.
        ids = self._faces_near_capsule(ends, radius)
        if not len(ids):
            return
        bary, t, delta, face_n = triangle_contacts(self.x[ids], *ends)
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

    def _limit_compression(self, threshold=.15, floor=None, incline=None, ends=None, radius=0.):
        if self.kernels:
            return self.kernels.limit_compression(threshold, floor, incline, ends, radius)
        q = self.x[self.tets]
        six_volume = self._signed_six_volumes(q)
        minimum = .15 * 6 * self.volume
        bad = six_volume < threshold * 6 * self.volume
        if not bad.any():
            return False
        ids = self.tets[bad]
        active = q[bad]
        u, v, w = (active[:, 1:] - active[:, :1]).transpose(1, 0, 2)
        faces = np.stack((np.cross(v, w), np.cross(w, u), np.cross(u, v)), axis=1)
        grad = np.concatenate((-faces.sum(axis=1, keepdims=True), faces), axis=1)
        grad *= self.orientation[bad, None, None]
        weights = self.inverse_mass[ids]
        direction = grad.copy() if floor is not None or incline is not None or ends is not None else grad
        if floor is not None:
            # A node in contact cannot open a compressed tet by going through
            # the floor. Give that part of the correction to the mobile nodes.
            touching = self.x[ids, 2] <= floor + 1e-8 * self.min_altitude
            direction[:, :, 2] -= touching * np.minimum(direction[:, :, 2], 0.)
        if incline is not None:
            normal, offset = incline
            touching = self.x[ids] @ normal <= offset + 1e-8 * self.min_altitude
            direction -= (touching * np.minimum(direction @ normal, 0.))[:, :, None] * normal
        if ends is not None:
            _, normals, distance = contact(self.x[ids].reshape(-1, 3), *ends)
            normals = normals.reshape(grad.shape)
            touching = self.surface_mask[ids] & (distance.reshape(ids.shape) <= radius + .1 * self.min_altitude)
            radial = np.einsum('ijk,ijk->ij', direction, normals)
            direction -= (touching * np.minimum(radial, 0.))[:, :, None] * normals
        weighted_grad = weights[:, :, None] * direction
        inv_mass = np.einsum('ij,ijk,ijk->i', weights, grad, direction)
        denom = np.where(inv_mass > 1e-20, inv_mass, np.inf)
        correction = (minimum[bad] - six_volume[bad]) / denom
        speed = np.einsum('ijk,ijk->i', grad, self.v[ids])
        impulse = -np.minimum(speed, 0.) / denom
        count = np.maximum(np.bincount(ids.ravel(), minlength=len(self.x)), 1)[:, None]
        for field, scale in ((self.x, correction), (self.v, impulse)):
            adjustment = np.zeros_like(field)
            np.add.at(adjustment, ids.ravel(), (weighted_grad * scale[:, None, None]).reshape(-1, 3))
            field += adjustment / count
        return True

    def _project_contacts(self, floor, incline, ends, tool_v, radius):
        self._project_analytic_contacts(floor, incline, ends, tool_v, radius)
        if self.mesh_phase is not None:
            start, t0, t1, seconds = self.mesh_phase
            self.mesh_contacts.project(start, self.x, self.v, ~self.pinned, t0, t1, seconds, self.friction)

    def _project_analytic_contacts(self, floor, incline, ends, tool_v, radius):
        if self.kernels:
            self.kernels.project_contacts(floor, incline, ends, tool_v, radius)
            return
        if ends is not None:
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

    def _signed_six_volumes(self, q):
        a, b, c = (q[:, 1:] - q[:, :1]).transpose(1, 0, 2)
        return np.einsum('ij,ij->i', a, np.cross(b, c)) * self.orientation

    def _volume_ratios(self):
        if self.kernels:
            return self.kernels.volume_ratios()
        return self._signed_six_volumes(self.x[self.tets]) / (6 * self.volume)

    def _contact_depth(self, ends, radius):
        if self.kernels:
            return self.kernels.contact_depth(ends, radius)
        if not len(self.contact_faces):
            return max(0., radius - contact(self.x[self.surface], *ends)[2].min())
        ids = self._faces_near_capsule(ends, radius)
        if not len(ids):
            return 0.
        delta = triangle_contacts(self.x[ids], *ends)[2]
        return max(0., radius - np.linalg.norm(delta, axis=1).min())

    def _backtrack(self, start, floor, incline, ends, previous_ends, radius):
        ratios = self._volume_ratios()
        bad = ratios < .04
        if not bad.any():
            return
        proposed = self.x.copy()
        motion = proposed - start
        velocity = self.v.copy()
        if ends is not None:
            # Full face/vertex projections are not idempotent. Measure the
            # accepted contact residual at the *previous* pose and allow a
            # small contact tolerance, rather than reprojecting and folding
            # the old configuration even on a zero-length retry.
            self.x[:] = start
            allowed_depth = max(.04 * radius, self._contact_depth(previous_ends, radius) + 1e-10 * radius)
        if self.mesh_phase is not None:
            _, t0, t1, seconds = self.mesh_phase
            mobile_mask = ~self.pinned
            mesh_allowance = self.mesh_contacts.residual(start, start, mobile_mask, t0, t0, seconds)
            mesh_allowance += 1e-7 * self.min_altitude
        # First damp only the free nodes of the problematic tets. Neighbors can
        # continue to move and relieve the stress instead of freezing the body.
        local = np.unique(self.tets[bad])
        for mobile in (local[~self.pinned[local]], np.flatnonzero(~self.pinned)):
            for fraction in (.5, .25, .125, .0625, .03125, 0.):
                self.x[:] = proposed
                self.x[mobile] = start[mobile] + fraction * motion[mobile]
                self.v[:] = velocity
                self.v[mobile] *= fraction
                self._project_analytic_contacts(floor, incline, None, None, radius)
                if self.mesh_phase is not None and self.mesh_contacts.residual(
                        start, self.x, mobile_mask, t0, t1, seconds) > mesh_allowance:
                    continue
                if self._volume_ratios().min() >= .04:
                    if ends is None or self._contact_depth(ends, radius) <= allowed_depth:
                        return
        raise _ContactStepRejected("Gel contact and pin positions could not be resolved in this substep")

    def advance_live_collider(self, seconds, gravity, floor, previous, target, radius, pins, incline, grab, mesh_refinement=1):
        """Advance against a hand-driven tool, returning its accepted collision pose."""
        # The capsule is symmetric. Use corresponding ends even after a viewport
        # rotation flips its axis between samples.
        if (previous[1] - previous[0]) @ (target[1] - target[0]) < 0:
            target = target[::-1]
        delta = target - previous
        travel = np.linalg.norm(delta, axis=1).max()
        # Fast-forward across empty space. Near the gel, a viewport
        # event can imply hundreds of m/s; meter contact travel using
        # the material shear-wave speed, independently of event frequency.
        margin = radius + self.min_altitude + seconds * (
            np.linalg.norm(self.v, axis=1).max() + seconds * np.linalg.norm(gravity))
        low, high = self.x.min(axis=0), self.x.max(axis=0)
        if pins is not None:
            low, high = np.minimum(low, pins.min(axis=0)), np.maximum(high, pins.max(axis=0))
        low, high = low - margin, high + margin
        clear = np.maximum(low - previous.max(axis=0), previous.min(axis=0) - high)
        if travel:
            if np.any((np.maximum(previous, target).max(axis=0) < low) |
                      (np.minimum(previous, target).min(axis=0) > high)):
                previous = target
            else:
                previous = previous + min(1., np.linalg.norm(np.maximum(clear, 0.)) / travel) * delta

        delta = target - previous
        travel = np.linalg.norm(delta, axis=1).max()
        if travel > self.contact_speed * seconds:
            delta = delta * ((self.contact_speed * seconds) / travel)
        start_x, start_v = self.x.copy(), self.v.copy()
        for fraction, refinement in ((1., 1), (.5, 2), (.125, 4), (0., 4)):
            candidate = previous + fraction * delta
            try:
                if fraction == 0. and np.any((target.max(axis=0) < low) | (target.min(axis=0) > high)):
                    # If withdrawal was blocked, release the stale contact
                    # once the requested tool is entirely outside the body.
                    self.advance(seconds, gravity, floor, None, None, radius, pins, incline, grab, interactive=True)
                    return target
                self.advance(seconds, gravity, floor, previous, candidate, radius, pins, incline,
                             grab, interactive=True, refinement=refinement * mesh_refinement)
                return candidate
            except (_ContactStepRejected, FloatingPointError):
                # Retrying a failed sweep must start with its original momentum
                # and tool pose. Never publish a partially inverted trial.
                self.x[:] = start_x
                self.v[:] = start_v
                if fraction == 0.:
                    raise

    def advance(self, seconds, gravity, floor, previous, current, radius, pin_positions=None, incline=None,
                grab=None, interactive=False, refinement=1):
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
        meshes = self.mesh_contacts
        if meshes and meshes.travel > 0:
            dt_limit = min(dt_limit, seconds * .35 * self.min_altitude / meshes.travel)
        steps = max(1, math.ceil(seconds / dt_limit)) * refinement
        if steps > 4096:
            raise ValueError("Simulation needs too many substeps: check scale, stiffness and collider motion")
        dt = seconds / steps
        drag = math.exp(-self.damping * dt)
        tool_v = (current - previous) / seconds if current is not None else None
        if grab is not None:
            indices, goals, weight = grab
            # A small patch may support the entire body. Size the spring for that load
            # so even a single picked node can hold it with little static handle lag.
            patch_mass = self.mass[indices] @ weight
            extent = np.ptp(self.rest, axis=0).max()
            response = max(100., np.linalg.norm(gravity) / (.025 * extent))
            stiffness = max(42. ** 2, response * self.mass.sum() / patch_mass)
            omega_dt = dt * np.sqrt(stiffness * weight[:, None])
            spring_gain = omega_dt ** 2 / dt
            spring_denom = (1. + omega_dt) ** 2
        for i in range(steps):
            if interactive or meshes:
                start = self.x.copy()
            self.mesh_phase = (start, i / steps, (i + 1) / steps, seconds) if meshes else None
            self.v += dt * (self.elastic_forces() / self.mass[:, None] + gravity)
            self.v *= drag
            if grab is not None:
                # Implicit damped spring on free surface nodes. Releasing the
                # handle removes this force while preserving their velocities.
                self.v[indices] = (self.v[indices] + spring_gain * (goals - self.x[indices])) / spring_denom
            self.x += dt * self.v
            if pin_positions is not None:
                # Prescribe both position and velocity before strain/contact projection.
                self.x[self.pinned] = pin_start + pin_motion * ((i + 1) / steps)
                self.v[self.pinned] = pin_velocity
            elif interactive:
                self.x[self.pinned] = self.rest[self.pinned]
                self.v[self.pinned] = 0.
            self._limit_compression()
            ends = previous + (current - previous) * ((i + 1) / steps) if current is not None else None
            self._project_contacts(floor, incline, ends, tool_v, radius)
            if interactive:
                # Contact projection follows the elastic integration and can
                # compress a one-node grab again. Reconcile both constraints.
                for _ in range(6):
                    if not self._limit_compression(.08, floor, incline, ends, radius):
                        break
                    self._project_contacts(floor, incline, ends, tool_v, radius)
                else:
                    self._backtrack(start, floor, incline, ends,
                                    ends - dt * tool_v if ends is not None else None, radius)
            if pin_positions is None:
                self.x[self.pinned] = self.rest[self.pinned]
                self.v[self.pinned] = 0.
            if not np.isfinite(self.x).all():
                raise _ContactStepRejected("Gel simulation diverged: check the mesh and collider motion")
        self.mesh_phase = None
        if np.any(self._volume_ratios() <= 0):
            raise _ContactStepRejected("A gel element inverted: reduce impact depth or increase simulation resolution")
