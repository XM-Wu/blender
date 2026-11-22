# SPDX-License-Identifier: GPL-2.0-or-later

"""Binary key images and contour interpolation for the SDF editor.

Array row zero is the bottom of the UV tile. PNG scanlines run top to bottom.
"""

import base64
from functools import lru_cache
import os
import struct
import tempfile
import zlib

import numpy as np


def pack_bytes(raw):
    return base64.b85encode(zlib.compress(raw)).decode("ascii")


def unpack_bytes(text, expected_size):
    """Decode an existing .blend payload, bounded by its declared shape."""
    try:
        compressed = base64.b85decode(text)
        stream = zlib.decompressobj()
        raw = stream.decompress(compressed, expected_size + 1)
    except (ValueError, zlib.error) as ex:
        raise ValueError("Invalid compressed SDF data") from ex
    if len(raw) != expected_size or not stream.eof or stream.unused_data:
        raise ValueError("SDF data does not match the expected size")
    return raw


def encode(bits):
    return pack_bytes(np.packbits(bits).tobytes())


@lru_cache(maxsize=12)
def decode(data, size):
    if size <= 0:
        raise ValueError("Invalid SDF resolution")
    raw = np.frombuffer(unpack_bytes(data, (size * size + 7) // 8), dtype=np.uint8)
    bits = np.unpackbits(raw, count=size * size).reshape(size, size).view(bool)
    bits.flags.writeable = False
    return bits


def triangle_grid(size, uv):
    pts = np.asarray(uv, dtype=float) * size
    x0, y0 = np.maximum(0, np.ceil(pts.min(axis=0) - 0.5)).astype(int)
    x1, y1 = np.minimum(size, np.floor(pts.max(axis=0) - 0.5) + 1).astype(int)
    if x1 <= x0 or y1 <= y0:
        return
    y, x = np.ogrid[y0:y1, x0:x1]
    sides = []
    for a, b in zip(pts, np.roll(pts, -1, axis=0)):
        sides.append((b[0] - a[0]) * (y + 0.5 - a[1]) - (b[1] - a[1]) * (x + 0.5 - a[0]))
    a, b, c = sides
    area = (a + b + c)[0, 0]
    if abs(area) <= 1e-10:
        return
    inside = ((a >= 0) & (b >= 0) & (c >= 0)) | ((a <= 0) & (b <= 0) & (c <= 0))
    return (slice(y0, y1), slice(x0, x1)), inside, (b, c, a), area


def raster_triangle(canvas, uv, positive_side=None):
    grid = triangle_grid(len(canvas), uv)
    if grid is None:
        return
    region, inside, (b, c, a), area = grid
    if positive_side is not None:
        # Each edge function weights its opposite vertex. Classify the actual
        # texel center even when the triangle straddles local X = 0.
        x_a, x_b, x_c = positive_side
        inside &= ((b * x_a + c * x_b + a * x_c) * np.sign(area)) > 0
    canvas[region] |= inside


def flood_fill(bits, coverage, uv, white):
    x, y = np.floor(np.asarray(uv) * len(bits)).astype(int)
    if not (0 <= x < len(bits) and 0 <= y < len(bits)):
        return False
    pending = coverage & (bits != white)
    if not pending[y, x]:
        return False
    stack = [(x, y)]
    while stack:
        x, y = stack.pop()
        row = pending[y]
        if not row[x]:
            continue
        stops = np.flatnonzero(~row[:x])
        left = int(stops[-1]) + 1 if stops.size else 0
        stops = np.flatnonzero(~row[x+1:])
        right = int(stops[0]) + x + 1 if stops.size else len(bits)
        row[left:right] = False
        bits[y, left:right] = white
        for neighbor in (y-1, y+1):
            if 0 <= neighbor < len(bits):
                run = pending[neighbor, left:right]
                starts = np.flatnonzero(run & ~np.r_[False, run[:-1]])
                stack.extend((left + int(start), neighbor) for start in starts)
    return True


def stamp(bits, mask, uv, radius, white):
    x, y = np.asarray(uv) * len(bits)
    x0, y0 = max(0, int(x - radius)), max(0, int(y - radius))
    x1, y1 = min(len(bits), int(x + radius + 1)), min(len(bits), int(y + radius + 1))
    if x0 >= x1 or y0 >= y1:
        return False
    ys, xs = np.ogrid[y0:y1, x0:x1]
    brush = (xs + 0.5 - x) ** 2 + (ys + 0.5 - y) ** 2 <= radius ** 2
    covered = brush & mask[y0:y1, x0:x1]
    bits[y0:y1, x0:x1][covered] = white
    return bool(covered.any())


def stroke_segment(bits, mask, start, end, radius, white, start_radius=None):
    """Sweep a round brush between UV samples, including both end caps."""
    a, b = np.asarray(start) * len(bits), np.asarray(end) * len(bits)
    delta = b - a
    length_sq = delta @ delta
    r0 = radius if start_radius is None else start_radius
    dr = radius - r0
    if max(radius, r0) <= 0:
        return False
    if length_sq <= dr * dr:
        return stamp(bits, mask, start if r0 > radius else end, max(r0, radius), white)
    if dr == 0 and length_sq <= (.5 * radius) ** 2:
        return stamp(bits, mask, end, radius, white)
    extent = max(r0, radius)
    count = max(1, int(np.ceil(np.max(np.abs(delta)) / max(64, extent))))
    touched = False
    for i in range(count):
        p, q = a + delta * (i / count), a + delta * ((i + 1) / count)
        x0, y0 = np.maximum(0, np.floor(np.minimum(p, q) - extent)).astype(int)
        x1, y1 = np.minimum(len(bits), np.ceil(np.maximum(p, q) + extent)).astype(int)
        if x1 <= x0 or y1 <= y0:
            continue
        y, x = np.ogrid[y0:y1, x0:x1]
        dx, dy = x + .5 - a[0], y + .5 - a[1]
        # Minimize squared distance minus squared interpolated radius.
        t = np.clip((dx * delta[0] + dy * delta[1] + r0 * dr) / (length_sq - dr * dr),
                    i / count, (i + 1) / count)
        inside = (dx - t * delta[0]) ** 2 + (dy - t * delta[1]) ** 2 <= (r0 + t * dr) ** 2
        covered = inside & mask[y0:y1, x0:x1]
        bits[y0:y1, x0:x1][covered] = white
        touched = touched or bool(covered.any())
    return touched


def distance(seeds):
    """Two-pass 8-connected chamfer transform, vectorized along each row."""
    width = seeds.shape[1]
    d = np.where(seeds, 0, 2 * max(seeds.shape)).astype(np.float32)
    offsets = np.arange(width, dtype=np.float32)
    for rows in (d, d[::-1]):
        previous = None
        for row in rows:
            if previous is not None:
                np.minimum(row, previous + 1, out=row)
                np.minimum(row[1:], previous[:-1] + 2 ** 0.5, out=row[1:])
                np.minimum(row[:-1], previous[1:] + 2 ** 0.5, out=row[:-1])
            row[:] = np.minimum.accumulate(row - offsets) + offsets
            row[::-1] = np.minimum.accumulate(row[::-1] - offsets) + offsets
            previous = row
    return d


@lru_cache(maxsize=6)
def signed(data, coverage, size):
    bits = decode(data, size)
    mask = decode(coverage, size)
    # Pixels outside the islands are neither white nor black contour seeds.
    inside = distance(mask & ~bits)
    outside = distance(mask & bits)
    # With an empty/full key there is no contour. Scale its distance using
    # the other key below to grow/shrink from the interior of that shape.
    return np.where(bits, inside - 0.5, 0.5 - outside)


@lru_cache(maxsize=3)
def fields(a, b, coverage, size):
    da = signed(a, coverage, size)
    db = signed(b, coverage, size)
    mask = decode(coverage, size)
    white_a = decode(a, size)[mask]
    white_b = decode(b, size)[mask]
    # A uniform key has no boundary. Offset the adjoining contour until
    # its last white (or black) pixel disappears at the uniform key.
    def endpoint(white, other, own):
        if not white.any():
            return other - max(0.0, other[mask].max()) - 0.5
        if white.all():
            return other - min(0.0, other[mask].min()) + 0.5
        return own

    if white_a.min() == white_a.max() and white_b.min() == white_b.max():
        if white_a[0] == white_b[0]:
            level = np.float32(1 if white_a[0] else -1)
            return level, level
        # Two flat keys supply no contour. Start at the interior of the
        # selected UV islands and expand out to their boundary.
        inset = distance(~np.pad(mask, 1))[1:-1, 1:-1]
        peak = inset.max()
        crossing = (peak - inset + 0.5) / peak
        if white_a[0]:
            return 1 - crossing, -crossing
        return -crossing, 1 - crossing
    return endpoint(white_a, db, da), endpoint(white_b, da, db)



def key_at(mask, value):
    return next((k for k in mask.keys if abs(k.value - value) < 1e-6), None)


def key_images(mask):
    return tuple((k.value, k.bitmap) for k in mask.keys)


def preview(keys, value, coverage, size):
    keys = sorted(keys)
    if not keys:
        return np.zeros((size, size), dtype=bool)
    for i, (t, data) in enumerate(keys):
        if abs(value - t) < 1e-6 or (i == 0 and value < t):
            return decode(data, size) & decode(coverage, size)
        if value < t:
            start, before = keys[i - 1]
            a, b = fields(before, data, coverage, size)
            return ((t - value) * a + (value - start) * b >= 0) & decode(coverage, size)
    return decode(keys[-1][1], size) & decode(coverage, size)


class KeyConflict(ValueError):
    def __init__(self, start, end, count):
        self.start, self.end, self.count = start, end, count
        super().__init__(f"{count} pixels turn from white to black between keys {start:.4f} and {end:.4f}")


def check_keys(keys, coverage, size):
    ordered = sorted(keys)
    if not ordered:
        raise ValueError("Add a key before exporting")
    mask = decode(coverage, size)
    for (start, before), (end, after) in zip(ordered, ordered[1:]):
        count = np.count_nonzero(mask & decode(before, size) & ~decode(after, size))
        if count:
            raise KeyConflict(start, end, count)
    return ordered


def thresholds(keys, coverage, size):
    keys = check_keys(keys, coverage, size)
    mask = decode(coverage, size)
    result = np.ones(mask.shape, dtype=np.float32)
    result[decode(keys[0][1], size)] = 0
    for (start, before), (end, after) in zip(keys, keys[1:]):
        left, right = decode(before, size) & mask, decode(after, size) & mask
        changed = right & ~left
        if changed.any():
            a, b = fields(before, after, coverage, size)
            a = np.broadcast_to(a, mask.shape)[changed]
            b = np.broadcast_to(b, mask.shape)[changed]
            result[changed] = start + (end - start) * (-a / (b - a))
    result[~mask] = 0
    return result


def write_png(path, values):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    h, w = values.shape
    rows = np.rint(values[::-1] * 65535).astype(">u2")
    scanlines = b"".join(b"\0" + row.tobytes() for row in rows)
    with tempfile.NamedTemporaryFile(dir=os.path.dirname(os.path.abspath(path)), delete=False) as output:
        name = output.name
    try:
        with open(name, "wb") as output:
            output.write(b"\x89PNG\r\n\x1a\n")
            output.write(chunk(b"IHDR", struct.pack(">2I5B", w, h, 16, 0, 0, 0, 0)))
            output.write(chunk(b"IDAT", zlib.compress(scanlines)))
            output.write(chunk(b"IEND", b""))
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
