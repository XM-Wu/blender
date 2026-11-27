/* SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * CPU kernels for the interactive tetrahedral gel solver.
 * Arrays use contiguous row-major doubles and int32 indices. The Python
 * adapter checks their shapes before calling this private, versioned ABI.
 */
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>

#include <Eigen/Core>
#include <Eigen/Geometry>

#ifdef _WIN32
#  define GEL_EXPORT extern "C" __declspec(dllexport)
#else
#  define GEL_EXPORT extern "C" __attribute__((visibility("default")))
#endif

namespace {
using Vec = Eigen::Vector3d;
using Mat = Eigen::Matrix<double, 3, 3, Eigen::RowMajor>;
using ReadVec = Eigen::Map<const Vec>;
using WriteVec = Eigen::Map<Vec>;

struct Closest {
  Vec bary = Vec::Zero();
  Vec delta = Vec::Zero();
  double along = 0.0;
  double squared = std::numeric_limits<double>::infinity();

  void consider(const Vec &weights, const double t, const Vec &difference)
  {
    const double d2 = difference.squaredNorm();
    if (d2 < squared) {
      squared = d2;
      bary = weights;
      along = t;
      delta = difference;
    }
  }
};

Closest closest_triangle(const Vec (&p)[3], const Vec &start, const Vec &axis, Vec &normal)
{
  const Vec ab = p[1] - p[0], ac = p[2] - p[0];
  normal = ab.cross(ac);
  const double area2 = normal.squaredNorm(), length2 = axis.squaredNorm();
  Closest result;
  if (area2 > 1e-24) {
    const double slope = normal.dot(axis);
    const double crossing = std::abs(slope) > 1e-16 ? (p[0] - start).dot(normal) / slope : 0.0;
    const double samples[] = {0.0, 1.0, std::clamp(crossing, 0.0, 1.0)};
    for (const double t : samples) {
      const Vec d = start + t * axis - p[0];
      const double u = d.cross(ac).dot(normal) / area2;
      const double v = ab.cross(d).dot(normal) / area2;
      const Vec weights(1.0 - u - v, u, v);
      if (weights.minCoeff() >= 0.0) {
        result.consider(weights, t, u * ab + v * ac - d);
      }
    }
  }
  for (int i = 0; i < 3; i++) {
    const int j = (i + 1) % 3;
    const Vec d = p[i] - start, e = p[j] - p[i];
    const double ee = std::max(e.squaredNorm(), 1e-24);
    const double ea = e.dot(axis), ed = e.dot(d), ad = axis.dot(d);
    const double det = ee * length2 - ea * ea;
    double u = det > 1e-18 ? std::clamp((ea * ad - length2 * ed) / det, 0.0, 1.0) : 0.0;
    const double t = std::clamp((ad + ea * u) / std::max(length2, 1e-24), 0.0, 1.0);
    u = std::clamp((ea * t - ed) / ee, 0.0, 1.0);
    Vec weights = Vec::Zero();
    weights[i] = 1.0 - u;
    weights[j] = u;
    result.consider(weights, t, d + u * e - t * axis);
  }
  normal /= std::sqrt(std::max(area2, 1e-24));
  return result;
}
}  // namespace

namespace {
/* Mirrored in _xblender_gel_native.py. Topology and scratch storage belong to
 * one live session; positions, velocities and inverse masses are borrowed per call. */
struct Mesh {
  int nodes, tets, faces;
  const int32_t *tet, *face;
  const uint8_t *surface;
  const double *inverse, *volume, *orientation;
  double *scratch;
};
struct Contacts {
  double floor, radius, friction, altitude;
  double normal[3], offset;
  double start[3], end[3], speed0[3], speed1[3];
  int inclined, capsule;
};

Vec impulse(const Vec &relative, const Vec &normal, const double friction)
{
  const double vn = relative.dot(normal);
  const Vec tangent = relative - vn * normal;
  const double inward = std::min(0.0, vn);
  return -inward * normal -
         std::min(1.0, -friction * inward / std::max(1e-12, tangent.norm())) * tangent;
}

struct Capsule {
  Vec start, axis, low, high, fallback;
  double inv_length2;

  explicit Capsule(const Contacts &p) : start(ReadVec(p.start)), axis(ReadVec(p.end) - start)
  {
    low = start.cwiseMin(ReadVec(p.end)).array() - p.radius;
    high = start.cwiseMax(ReadVec(p.end)).array() + p.radius;
    inv_length2 = 1.0 / std::max(1e-20, axis.squaredNorm());
    const Vec direction = axis / std::max(axis.norm(), 1e-12);
    Eigen::Index i;
    direction.cwiseAbs().minCoeff(&i);
    fallback = (Vec::Unit(i) - direction * direction[i]).normalized();
  }

  double radial(const Vec &point, double &t, Vec &normal) const
  {
    t = std::clamp((point - start).dot(axis) * inv_length2, 0.0, 1.0);
    const Vec delta = point - (start + t * axis);
    const double d = delta.norm();
    normal = d > 1e-12 ? Vec(delta / d) : fallback;
    return d;
  }

  bool near(const Vec (&p)[3]) const
  {
    return !(p[0].cwiseMin(p[1]).cwiseMin(p[2]).array() > high.array()).any() &&
           !(p[0].cwiseMax(p[1]).cwiseMax(p[2]).array() < low.array()).any();
  }
};

void accumulate(const Mesh &m, const int i, const Vec &dx, const Vec &dv, const double count)
{
  double *s = m.scratch + 7 * i;
  WriteVec(s + 0) += dx;
  WriteVec(s + 3) += dv;
  s[6] += count;
}

void apply(const Mesh &m, double *x, double *v)
{
  for (int i = 0; i < m.nodes; i++) {
    const double *s = m.scratch + 7 * i;
    if (s[6] > 0) {
      WriteVec(x + 3 * i) += ReadVec(s) / s[6];
      WriteVec(v + 3 * i) += ReadVec(s + 3) / s[6];
    }
  }
}

Mat basis_at(const double *x, const int32_t *ids)
{
  Mat D;
  const Vec origin = ReadVec(x + 3 * ids[0]);
  for (int j = 0; j < 3; j++) {
    D.col(j) = ReadVec(x + 3 * ids[j + 1]) - origin;
  }
  return D;
}
}  // namespace

GEL_EXPORT void gel_forces_v1(
    const Mesh &m, const double *x, double shear, double bulk, double *out)
{
  std::fill_n(out, 3 * m.nodes, 0.);
  for (int i = 0; i < m.tets; i++) {
    const int32_t *ids = m.tet + 4 * i;
    const Eigen::Map<const Mat> inv(m.inverse + 9 * i);
    const Mat F = basis_at(x, ids) * inv;
    Mat cof;
    cof.col(0) = F.col(1).cross(F.col(2));
    cof.col(1) = F.col(2).cross(F.col(0));
    cof.col(2) = F.col(0).cross(F.col(1));
    const double J = F.col(0).dot(cof.col(0));
    const Mat stress = shear * F + ((bulk + shear / 3) * (J - 1) - shear) * cof;
    Vec sum = Vec::Zero();
    for (int j = 0; j < 3; j++) {
      const Vec force = -m.volume[i] * (stress * inv.row(j).transpose());
      WriteVec(out + 3 * ids[j + 1]) += force;
      sum += force;
    }
    WriteVec(out + 3 * ids[0]) -= sum;
  }
}

GEL_EXPORT void gel_ratios_v1(const Mesh &m, const double *x, double *out)
{
  for (int i = 0; i < m.tets; i++) {
    out[i] = basis_at(x, m.tet + 4 * i).determinant() * m.orientation[i] / (6 * m.volume[i]);
  }
}

GEL_EXPORT int gel_compression_v1(const Mesh &m,
                                  double *x,
                                  double *v,
                                  const double *inv_mass,
                                  const Contacts &p,
                                  double threshold)
{
  bool changed = false;
  const Capsule cap(p);
  const Vec normal = ReadVec(p.normal);
  for (int i = 0; i < m.tets; i++) {
    const int32_t *ids = m.tet + 4 * i;
    const Mat D = basis_at(x, ids);
    const double six = D.determinant() * m.orientation[i];
    if (six >= threshold * 6 * m.volume[i]) {
      continue;
    }
    if (!changed) {
      std::fill_n(m.scratch, 7 * m.nodes, 0.);
      changed = true;
    }
    Vec grad[4], weighted[4];
    grad[1] = m.orientation[i] * D.col(1).cross(D.col(2));
    grad[2] = m.orientation[i] * D.col(2).cross(D.col(0));
    grad[3] = m.orientation[i] * D.col(0).cross(D.col(1));
    grad[0] = -grad[1] - grad[2] - grad[3];
    double denom = 0., speed = 0.;
    for (int j = 0; j < 4; j++) {
      const int n = ids[j];
      const Vec point = ReadVec(x + 3 * n);
      Vec dir = grad[j];
      if (point.z() <= p.floor + 1e-8 * p.altitude) {
        dir.z() = std::max(0., dir.z());
      }
      if (p.inclined && point.dot(normal) <= p.offset + 1e-8 * p.altitude) {
        dir -= std::min(0., dir.dot(normal)) * normal;
      }
      if (p.capsule && m.surface[n]) {
        double t;
        Vec radial;
        if (cap.radial(point, t, radial) <= p.radius + .1 * p.altitude) {
          dir -= std::min(0., dir.dot(radial)) * radial;
        }
      }
      weighted[j] = inv_mass[n] * dir;
      denom += grad[j].dot(weighted[j]);
      speed += grad[j].dot(ReadVec(v + 3 * n));
    }
    const double inv = denom > 1e-20 ? 1. / denom : 0.;
    for (int j = 0; j < 4; j++) {
      accumulate(m,
                 ids[j],
                 ((.9 * m.volume[i] - six) * inv) * weighted[j],
                 (-std::min(speed, 0.) * inv) * weighted[j],
                 1.);
    }
  }
  if (changed) {
    apply(m, x, v);
  }
  return changed;
}

/* A single broad/narrow phase serves both projection and the line-search residual. */
static double surface_contacts(const Mesh &m,
                               const double *x,
                               const double *v,
                               const double *inv_mass,
                               const Contacts &p,
                               const Capsule &cap,
                               bool project,
                               bool &changed)
{
  double depth = 0.;
  for (int i = 0; i < m.faces; i++) {
    const int32_t *ids = m.face + 3 * i;
    const Vec tri[3] = {ReadVec(x + 3 * ids[0]), ReadVec(x + 3 * ids[1]), ReadVec(x + 3 * ids[2])};
    if (!cap.near(tri)) {
      continue;
    }
    Vec face_normal;
    const Closest hit = closest_triangle(tri, cap.start, cap.axis, face_normal);
    if (hit.squared >= p.radius * p.radius) {
      continue;
    }
    const double d = std::sqrt(hit.squared);
    depth = std::max(depth, p.radius - d);
    if (!project) {
      continue;
    }
    if (!changed) {
      std::fill_n(m.scratch, 7 * m.nodes, 0.);
      changed = true;
    }
    const Vec n = d < 1e-8 ? Vec(-face_normal) : Vec(hit.delta / d);
    Vec weights, relative = -(1 - hit.along) * ReadVec(p.speed0) - hit.along * ReadVec(p.speed1);
    for (int j = 0; j < 3; j++) {
      weights[j] = inv_mass[ids[j]] * hit.bary[j];
      relative += hit.bary[j] * ReadVec(v + 3 * ids[j]);
    }
    weights /= std::max(weights.dot(hit.bary), 1e-20);
    const Vec dx = (p.radius - d) * n, dv = impulse(relative, n, p.friction);
    for (int j = 0; j < 3; j++) {
      accumulate(m, ids[j], weights[j] * dx, weights[j] * dv, weights[j] > 0. ? 1. : 0.);
    }
  }
  return depth;
}

GEL_EXPORT double gel_depth_v1(const Mesh &m, const double *x, const Contacts &p)
{
  const Capsule cap(p);
  bool unused = false;
  if (m.faces) {
    return surface_contacts(m, x, nullptr, nullptr, p, cap, false, unused);
  }
  double depth = 0.;
  for (int i = 0; i < m.nodes; i++) {
    if (m.surface[i]) {
      double t;
      Vec n;
      depth = std::max(depth, p.radius - cap.radial(ReadVec(x + 3 * i), t, n));
    }
  }
  return depth;
}

GEL_EXPORT void gel_contacts_v1(
    const Mesh &m, double *x, double *v, const double *inv_mass, const Contacts &p)
{
  const Capsule cap(p);
  const Vec plane = ReadVec(p.normal);
  if (p.capsule) {
    bool changed = false;
    surface_contacts(m, x, v, inv_mass, p, cap, true, changed);
    if (changed) {
      apply(m, x, v);
    }
  }
  for (int i = 0; i < m.nodes; i++) {
    if (inv_mass[i] == 0.) {
      continue;
    }
    WriteVec point(x + 3 * i), vel(v + 3 * i);
    if (p.capsule && m.surface[i]) {
      double t;
      Vec n;
      const double d = cap.radial(point, t, n);
      if (d < p.radius) {
        point += (p.radius - d) * n;
        const Vec relative = vel - ((1 - t) * ReadVec(p.speed0) + t * ReadVec(p.speed1));
        vel += impulse(relative, n, p.friction);
      }
    }
    if (point.z() < p.floor) {
      point.z() = p.floor;
      vel += impulse(vel, Vec::UnitZ(), p.friction);
    }
    if (p.inclined) {
      const double depth = p.offset - point.dot(plane);
      if (depth > 0.) {
        point += depth * plane;
        vel += impulse(vel, plane, p.friction);
      }
    }
  }
}

/* Relax non-affine skin displacement on the render mesh. Jacobi reads one
 * buffer for an entire pass, so the result is independent of vertex order. */
GEL_EXPORT int gel_relax_skin_v1(const int vertices,
                                 const int entries,
                                 const int32_t *offsets,
                                 const int32_t *neighbors,
                                 const double amount,
                                 const int passes,
                                 double *detail,
                                 double *scratch)
{
  if (vertices <= 0 || vertices > std::numeric_limits<int>::max() / 3 || offsets[0] != 0 ||
      offsets[vertices] != entries || passes < 0)
  {
    return 0;
  }
  for (int i = 0; i < vertices; ++i) {
    if (offsets[i] >= offsets[i + 1] || offsets[i + 1] > entries) {
      return 0;
    }
  }
  for (int j = 0; j < entries; ++j) {
    if (neighbors[j] < 0 || neighbors[j] >= vertices) {
      return 0;
    }
  }
  double *src = detail;
  double *dst = scratch;
  for (int step = 0; step < passes; ++step) {
    for (int i = 0; i < vertices; ++i) {
      Vec sum = Vec::Zero();
      for (int j = offsets[i]; j < offsets[i + 1]; ++j) {
        sum += ReadVec(src + 3 * neighbors[j]);
      }
      const Vec point = ReadVec(src + 3 * i);
      WriteVec(dst + 3 * i) = point + amount * (sum / (offsets[i + 1] - offsets[i]) - point);
    }
    std::swap(src, dst);
  }
  if (src != detail) {
    std::copy_n(src, 3 * vertices, detail);
  }
  return 1;
}

/* Swept Collision modifier triangle meshes. Arrays and the balanced tree are
 * owned by the Python sampler. Use double precision throughout in world meters. */
namespace {
struct ColliderMesh {
  int leaves, moving, culling;
  const int32_t *ranges, *faces;
  const double *low, *high, *old_positions, *positions;
  double margin, friction;
};

struct MeshHit {
  double toi = 2.0, depth = 0.0;
  Vec normal = Vec::Zero(), speed = Vec::Zero();
};

bool inside_face(const Vec &p, const Vec (&tri)[3], Vec &weights)
{
  const Vec ab = tri[1] - tri[0], ac = tri[2] - tri[0], d = p - tri[0];
  const Vec n = ab.cross(ac);
  const double a2 = n.squaredNorm();
  if (a2 < 1e-32) {
    return false;
  }
  weights[1] = d.cross(ac).dot(n) / a2;
  weights[2] = ab.cross(d).dot(n) / a2;
  weights[0] = 1. - weights[1] - weights[2];
  return weights.minCoeff() >= -1e-8;
}

void swept_face(const ColliderMesh &m,
                const int face,
                const Vec &start,
                const Vec &p,
                const double t0,
                const double t1,
                const double seconds,
                MeshHit &best)
{
  Vec tri[3], old[3], motion[3];
  for (int j = 0; j < 3; ++j) {
    const int id = m.faces[3 * face + j];
    tri[j] = ReadVec(m.positions + 3 * id);
    if (m.moving) {
      const Vec ref = ReadVec(m.old_positions + 3 * id);
      motion[j] = tri[j] - ref;
      old[j] = ref + t0 * motion[j];
      tri[j] = ref + t1 * motion[j];
    }
  }
  Vec normal = (tri[1] - tri[0]).cross(tri[2] - tri[0]);
  const double area = normal.norm();
  if (area <= 1e-16)
    return;
  normal /= area;
  double d0;
  if (m.moving) {
    Vec n = (old[1] - old[0]).cross(old[2] - old[0]);
    const double area0 = n.norm();
    if (area0 <= 1e-16)
      return;
    d0 = (start - old[0]).dot(n) / area0;
  }
  else {
    d0 = (start - tri[0]).dot(normal);
  }
  if (m.culling && d0 < -m.margin)
    return;
  if (!m.culling && d0 < 0.) {
    d0 = -d0;
    normal = -normal;
  }
  const double d1 = (p - tri[0]).dot(normal);
  double depth = m.margin - d1;
  if (depth <= 0.)
    return;
  const double toi = d0 > d1 + 1e-12 ? std::clamp((d0 - m.margin) / (d0 - d1), 0., 1.) : 0.;
  if (toi > best.toi)
    return;
  Vec weights;
  bool inside;
  if (m.moving) {
    Vec at[3];
    for (int j = 0; j < 3; j++)
      at[j] = old[j] + toi * (tri[j] - old[j]);
    inside = inside_face(start + toi * (p - start), at, weights);
  }
  else {
    inside = inside_face(start + toi * (p - start), tri, weights);
  }
  if (!inside) {
    double nearest = m.margin * m.margin;
    bool edge_hit = false;
    for (int j = 0; j < 3; ++j) {
      const int k = (j + 1) % 3;
      const Vec edge = tri[k] - tri[j];
      const double u = std::clamp(
          (p - tri[j]).dot(edge) / std::max(edge.squaredNorm(), 1e-20), 0., 1.);
      const Vec delta = p - (tri[j] + u * edge);
      const double dd = delta.squaredNorm();
      if (dd < nearest && dd > 1e-24) {
        edge_hit = true;
        nearest = dd;
        const double d = std::sqrt(dd);
        normal = delta / d;
        depth = m.margin - d;
        weights.setZero();
        weights[j] = 1. - u;
        weights[k] = u;
      }
    }
    if (!edge_hit)
      return;
  }
  if (toi == best.toi && depth <= best.depth)
    return;
  best.toi = toi;
  best.depth = depth;
  best.normal = normal;
  if (m.moving)
    best.speed = (weights[0] * motion[0] + weights[1] * motion[1] + weights[2] * motion[2]) /
                 seconds;
}
}  // namespace

GEL_EXPORT void gel_scene_contacts_v1(const ColliderMesh &m,
                                      int count,
                                      const double *start,
                                      double *positions,
                                      double *velocities,
                                      const uint8_t *mobile,
                                      double t0,
                                      double t1,
                                      double seconds,
                                      double friction)
{
  for (int i = 0; i < count; ++i) {
    if (!mobile[i])
      continue;
    const Vec a = ReadVec(start + 3 * i);
    WriteVec p(positions + 3 * i);
    const Vec lo = a.cwiseMin(p), hi = a.cwiseMax(p);
    int stack[64] = {1}, top = 1;
    MeshHit best;
    while (top) {
      const int cell = stack[--top];
      if ((lo.array() > ReadVec(m.high + 3 * cell).array()).any() ||
          (hi.array() < ReadVec(m.low + 3 * cell).array()).any())
        continue;
      if (cell < m.leaves) {
        stack[top++] = cell * 2 + 1;
        stack[top++] = cell * 2;
      }
      else {
        const int *range = m.ranges + 2 * (cell - m.leaves);
        for (int face = range[0]; face < range[1]; face++)
          swept_face(m, face, a, p, t0, t1, seconds, best);
      }
    }
    if (best.depth > 0.) {
      p += best.depth * best.normal;
      if (velocities) {
        WriteVec v(velocities + 3 * i);
        v += impulse(v - best.speed, best.normal, friction * m.friction);
      }
    }
  }
}
