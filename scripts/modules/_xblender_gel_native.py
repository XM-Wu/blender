# SPDX-License-Identifier: GPL-2.0-or-later
"""Optional native numeric kernels for the interactive preview."""

import ctypes as ct
from functools import lru_cache
from pathlib import Path
import sys

import numpy as np


class _Mesh(ct.Structure):
    _fields_ = [('nodes', ct.c_int), ('tets', ct.c_int), ('faces', ct.c_int)] + [
        (name, ct.c_void_p) for name in ('tet', 'face', 'surface', 'inverse', 'volume', 'orientation', 'scratch')]


class _Contacts(ct.Structure):
    _fields_ = [(name, ct.c_double) for name in ('floor', 'radius', 'friction', 'altitude')] + [
        ('normal', ct.c_double * 3), ('offset', ct.c_double),
        ('start', ct.c_double * 3), ('end', ct.c_double * 3),
        ('speed0', ct.c_double * 3), ('speed1', ct.c_double * 3),
        ('inclined', ct.c_int), ('capsule', ct.c_int)]


@lru_cache(maxsize=1)
def _library():
    suffix = '.dll' if sys.platform == 'win32' else '.dylib' if sys.platform == 'darwin' else '.so'
    try:
        lib = ct.CDLL(str(Path(__file__).with_name('xblender_gel_kernels' + suffix)))
    except OSError:
        return None
    mesh, point, params = ct.POINTER(_Mesh), ct.c_void_p, ct.POINTER(_Contacts)
    signatures = (
        ('gel_forces_v1', None, [mesh, point, ct.c_double, ct.c_double, point]),
        ('gel_ratios_v1', None, [mesh, point, point]),
        ('gel_compression_v1', ct.c_int, [mesh, point, point, point, params, ct.c_double]),
        ('gel_contacts_v1', None, [mesh, point, point, point, params]),
        ('gel_depth_v1', ct.c_double, [mesh, point, params]),
    )
    for name, result, args in signatures:
        func = getattr(lib, name, None)
        if func is None:
            return None
        func.restype, func.argtypes = result, args
    relax = getattr(lib, 'gel_relax_skin_v1', None)
    if relax is not None:
        array = np.ctypeslib.ndpointer
        ints = array(dtype=np.int32, ndim=1, flags=('C_CONTIGUOUS', 'ALIGNED'))
        vectors = array(dtype=np.float64, ndim=2, flags=('C_CONTIGUOUS', 'ALIGNED', 'WRITEABLE'))
        relax.restype = ct.c_int
        relax.argtypes = [ct.c_int, ct.c_int, ints, ints, ct.c_double, ct.c_int, vectors, vectors]
    return lib


class _ColliderMesh(ct.Structure):
    _fields_ = [(n, ct.c_int) for n in ('leaves', 'moving', 'culling')] + [
        (n, ct.c_void_p) for n in ('ranges', 'faces', 'low', 'high', 'old_positions', 'positions')] + [
        ('margin', ct.c_double), ('friction', ct.c_double)]


@lru_cache(maxsize=1)
def mesh_contact_kernel():
    lib = _library()
    fn = getattr(lib, 'gel_scene_contacts_v1', None) if lib else None
    if fn is None:
        return None
    vectors = np.ctypeslib.ndpointer(dtype=np.float64, ndim=2, flags=('C_CONTIGUOUS', 'ALIGNED'))
    mask = np.ctypeslib.ndpointer(dtype=np.bool_, ndim=1, flags=('C_CONTIGUOUS', 'ALIGNED'))
    fn.argtypes = [ct.POINTER(_ColliderMesh), ct.c_int, vectors, vectors, ct.c_void_p, mask,
                   ct.c_double, ct.c_double, ct.c_double, ct.c_double]
    fn.restype = None
    return fn


def project_scene_mesh(entry, previous, points, velocities, mobile, t0, t1, seconds, friction):
    """Entry owns the packed, refitted hierarchy and both geometry samples."""
    fn = mesh_contact_kernel()
    if fn is None:
        return False
    n = len(points)
    if (n >= 2**31 // 3 or points.shape != (n, 3) or previous.shape != points.shape or mobile.shape != (n,) or
            not points.flags.writeable or (velocities is not None and
            (velocities.shape != points.shape or velocities.dtype != np.float64 or not velocities.flags.carray))):
        raise ValueError("Invalid scene contact buffers")
    t = entry.tree
    if (entry.new.shape != entry.old.shape or entry.new.ndim != 2 or entry.new.shape[1] != 3 or
            any(a.dtype != np.float64 or not a.flags.c_contiguous for a in (entry.new, entry.old, t.low, t.high))):
        raise ValueError("Invalid collision geometry buffers")
    mesh = _ColliderMesh(len(t.ranges), entry.moving, entry.culling, t.ranges.ctypes.data, t.faces.ctypes.data,
                         t.low.ctypes.data, t.high.ctypes.data, entry.old.ctypes.data, entry.new.ctypes.data,
                         entry.margin, entry.friction)
    fn(ct.byref(mesh), n, np.ascontiguousarray(previous), points,
       velocities.ctypes.data if velocities is not None else None, mobile, t0, t1, seconds, friction)
    return True


class _BoundMesh:
    def __init__(self, lib, gel):
        self.lib = lib
        n, t, f = len(gel.x), len(gel.tets), len(gel.contact_faces)
        if max(n, t, f) > (2**31 - 1) // 12:
            raise ValueError("Gel mesh exceeds the native index range")
        for ids in (gel.tets, gel.contact_faces):
            if ids.size and (ids.dtype.kind not in 'iu' or ids.min() < 0 or ids.max() >= n):
                raise ValueError("Invalid gel topology")
        arrays = ((gel.tets, np.int32, (t, 4)), (gel.contact_faces, np.int32, (f, 3)),
                  (gel.surface_mask, np.uint8, (n,)), (gel.inverse, np.float64, (t, 3, 3)),
                  (gel.volume, np.float64, (t,)), (gel.orientation, np.float64, (t,)))
        self.storage = []
        for array, dtype, shape in arrays:
            if array.shape != shape:
                raise ValueError("Invalid gel kernel buffer shape")
            self.storage.append(np.array(array, dtype=dtype, order='C', copy=True))
        self.storage.append(np.empty((n, 7)))
        self.mesh = _Mesh(n, t, f, *(a.ctypes.data for a in self.storage))
        self.params = _Contacts()
        self.params.altitude = gel.min_altitude
        self.positions = self.buffer(gel.x, (n, 3))
        self.velocities = self.buffer(gel.v, (n, 3))

    @staticmethod
    def buffer(array, shape):
        if array.shape != shape or array.dtype != np.float64 or not array.flags.carray:
            raise ValueError("Invalid mutable gel buffer")
        # data_as retains the Python array for the lifetime of the pointer.
        return array.ctypes.data_as(ct.c_void_p)

    def relax_skin(self, detail, offsets, adjacent, amount, passes):
        relax = getattr(self.lib, 'gel_relax_skin_v1', None)
        if relax is None:
            return False
        n = len(offsets) - 1
        if detail.shape != (n, 3) or max(n, len(adjacent)) >= 2 ** 31:
            raise ValueError("Invalid skin relaxation buffer")
        if not relax(n, len(adjacent), offsets, adjacent, amount, passes, detail, np.empty_like(detail)):
            raise ValueError("Invalid skin relaxation topology")
        return True

    def material(self, gel):
        self.inv_mass = self.buffer(gel.inverse_mass, (self.mesh.nodes,))
        self.params.friction = gel.friction

    def set_contacts(self, floor, incline, ends, tool_v, radius):
        p = self.params
        p.floor = -float('inf') if floor is None else floor
        p.radius = radius
        p.inclined = incline is not None
        if p.inclined:
            p.normal[:], p.offset = incline
        p.capsule = ends is not None
        if p.capsule:
            p.start[:], p.end[:] = ends
        if tool_v is not None:
            p.speed0[:], p.speed1[:] = tool_v
        return ct.byref(p)

    def elastic_forces(self, shear, bulk):
        result = np.empty((self.mesh.nodes, 3))
        self.lib.gel_forces_v1(ct.byref(self.mesh), self.positions, shear, bulk, result.ctypes.data)
        return result

    def volume_ratios(self):
        result = np.empty(self.mesh.tets)
        self.lib.gel_ratios_v1(ct.byref(self.mesh), self.positions, result.ctypes.data)
        return result

    def limit_compression(self, threshold, floor, incline, ends, radius):
        p = self.set_contacts(floor, incline, ends, None, radius)
        return bool(self.lib.gel_compression_v1(ct.byref(self.mesh), self.positions, self.velocities,
                                                self.inv_mass, p, threshold))

    def project_contacts(self, floor, incline, ends, tool_v, radius):
        p = self.set_contacts(floor, incline, ends, tool_v, radius)
        self.lib.gel_contacts_v1(ct.byref(self.mesh), self.positions, self.velocities, self.inv_mass, p)

    def contact_depth(self, ends, radius):
        p = self.set_contacts(None, None, ends, None, radius)
        return self.lib.gel_depth_v1(ct.byref(self.mesh), self.positions, p)


def live_kernels(gel):
    lib = _library()
    if lib is None:
        return None
    mesh = _BoundMesh(lib, gel)
    mesh.material(gel)
    return mesh
