"""PVRTC1 (2 and 4 bits per pixel) to RGBA8, on the CPU, for hosts without PVRTC support.

Each 64-bit block holds a 32-bit modulation word and two colours (A and B). The colours of the
blocks around a pixel are interpolated bilinearly to give that pixel's A and B, and the
pixel's modulation value blends between them. Blocks are stored in Morton (twiddled) order.
"""
import numpy as np


def _twiddle_index(bx, by, nbx, nby):
    """Morton index of block (bx, by) in a texture of nbx x nby blocks (powers of two)."""
    idx = np.zeros_like(bx)
    shift = 0
    m = min(nbx, nby)
    bit = 1
    while bit < m:
        idx |= ((by & bit) != 0).astype(np.int64) << shift
        idx |= ((bx & bit) != 0).astype(np.int64) << (shift + 1)
        shift += 2
        bit <<= 1
    # the remaining high bits of the larger dimension follow unchanged
    if nbx > nby:
        idx |= (bx >> (shift // 2)) << shift
    elif nby > nbx:
        idx |= (by >> (shift // 2)) << shift
    return idx


def _colours(words):
    """Colours A and B of each block as float RGBA arrays (0-255)."""
    hi = words >> 32
    a16 = hi & 0xffff
    b16 = (hi >> 16) & 0xffff

    def expand(v, bits):
        return (v << (8 - bits)) | (v >> (2 * bits - 8)) if bits >= 4 else (v << (8 - bits)) | (v << (8 - 2 * bits)) | (v >> (3 * bits - 8) if 3 * bits >= 8 else 0)

    def unpack(c, is_b):
        opaque = (c >> 15) & 1
        r = np.where(opaque, (c >> 10) & 0x1f, ((c >> 8) & 0xf) << 1 | ((c >> 11) & 1))
        g = np.where(opaque, (c >> 5) & 0x1f, ((c >> 4) & 0xf) << 1 | ((c >> 7) & 1))
        if is_b:
            b = np.where(opaque, c & 0x1f, (c & 0xf) << 1 | ((c >> 3) & 1))
        else:
            b = np.where(opaque, ((c >> 1) & 0xf) << 1 | ((c >> 4) & 1), ((c >> 1) & 0x7) << 2 | ((c >> 2) & 3))
        a = np.where(opaque, 255, (((c >> 12) & 7) * 255) // 7)
        to8 = lambda v: (v << 3) | (v >> 2)
        return np.stack([to8(r), to8(g), to8(b), a], -1).astype(np.float32)
    return unpack(a16, False), unpack(b16, True)


def decode(data, width, height, bpp=4):
    bw = 4 if bpp == 4 else 8
    nbx, nby = max(width // bw, 2), max(height // 4, 2)
    count = nbx * nby
    raw = np.frombuffer(bytes(data).ljust(count * 8, b'\0'), dtype='<u8', count=count).astype(np.int64)
    byi, bxi = np.mgrid[0:nby, 0:nbx]
    order = _twiddle_index(bxi, byi, nbx, nby)
    words = raw[np.clip(order, 0, count - 1)]                      # [nby, nbx] in raster order
    ca, cb = _colours(words)

    W, H = nbx * bw, nby * 4
    ys, xs = np.mgrid[0:H, 0:W]
    # bilinear upscale of the block colours; sample points sit at block centres
    u = (xs - bw / 2) / bw
    v = (ys - 2) / 4
    x0, y0 = np.floor(u).astype(int), np.floor(v).astype(int)
    fx, fy = (u - x0)[..., None], (v - y0)[..., None]
    x1, y1 = (x0 + 1) % nbx, (y0 + 1) % nby
    x0, y0 = x0 % nbx, y0 % nby

    def up(c):
        return ((c[y0, x0] * (1 - fx) + c[y0, x1] * fx) * (1 - fy) +
                (c[y1, x0] * (1 - fx) + c[y1, x1] * fx) * fy)
    A, B = up(ca), up(cb)

    lo = (words & 0xffffffff)
    mode = (words >> 32) & 1
    bx, by = xs // bw, ys // 4
    px, py = xs % bw, ys % 4
    blo, bmode = lo[by, bx], mode[by, bx]
    if bpp == 4:
        m = (blo >> (2 * (py * 4 + px))) & 3
        weight = np.where(bmode == 0, np.choose(m, [0.0, 3 / 8, 5 / 8, 1.0]), np.choose(m, [0.0, 0.5, 0.5, 1.0]))
        punch = (bmode == 1) & (m == 2)
    else:
        m = (blo >> (py * 8 + px)) & 1
        weight = m.astype(np.float32)
        punch = np.zeros_like(m, dtype=bool)
    out = A * (1 - weight[..., None]) + B * weight[..., None]
    out[..., 3] = np.where(punch, 0, out[..., 3])
    out = np.clip(out, 0, 255).astype(np.uint8)
    return out[:height, :width].tobytes()
