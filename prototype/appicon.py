"""An installed app's home screen icon, as an ordinary PNG the Android side can show.

iOS app bundles usually carry their icons as Apple's "crushed" PNGs (a CgBI chunk first,
raw deflate with no zlib header, pixels BGRA and premultiplied), which no other decoder
reads, so they are rewritten here. Ordinary PNGs are passed through unchanged."""
import os
import plistlib
import struct
import zlib


def _candidates(app_dir):
    try:
        info = plistlib.load(open(os.path.join(app_dir, 'Info.plist'), 'rb'))
    except Exception:
        info = {}
    names = []
    for key in ('CFBundleIcons', 'CFBundleIcons~ipad'):
        primary = (info.get(key) or {}).get('CFBundlePrimaryIcon') or {}
        names += primary.get('CFBundleIconFiles') or []
    names += info.get('CFBundleIconFiles') or []
    if info.get('CFBundleIconFile'):
        names.append(info['CFBundleIconFile'])
    names += ['Icon@2x.png', 'Icon.png', 'icon@2x.png', 'icon.png', 'Icon-72.png']
    files = {f.lower(): f for f in os.listdir(app_dir)}
    out = []
    for n in names:
        base = n[:-4] if n.lower().endswith('.png') else n
        for v in (base + '@2x.png', base + '.png', base + '@2x~iphone.png', base + '~iphone.png', base):
            f = files.get(v.lower())
            if f and f not in out:
                out.append(f)
    return [os.path.join(app_dir, f) for f in out]


def _chunks(d):
    p = 8
    while p + 8 <= len(d):
        n, kind = struct.unpack('>I4s', d[p:p + 8])
        yield kind, d[p + 8:p + 8 + n]
        p += 12 + n


def _size(d):
    for kind, body in _chunks(d):
        if kind == b'IHDR':
            return struct.unpack('>II', body[:8])
    return 0, 0


def _chunk(kind, body):
    return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff)


def _uncrush(d):
    ihdr, idat = None, b''
    for kind, body in _chunks(d):
        if kind == b'IHDR':
            ihdr = body
        elif kind == b'IDAT':
            idat += body
    w, h, depth, ctype, _c, _f, interlace = struct.unpack('>IIBBBBB', ihdr)
    if depth != 8 or ctype not in (2, 6) or interlace:
        return None
    bpp = 4 if ctype == 6 else 3
    raw = zlib.decompress(idat, -15)
    stride = w * bpp
    prev = bytearray(stride)
    rows = []
    p = 0
    for _ in range(h):
        ft = raw[p]
        line = bytearray(raw[p + 1:p + 1 + stride])
        p += 1 + stride
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if ft == 1:
                line[i] = (line[i] + a) & 255
            elif ft == 2:
                line[i] = (line[i] + b) & 255
            elif ft == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 255
            elif ft == 4:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        out = bytearray(stride)
        for i in range(0, stride, bpp):
            bl, g, r = line[i], line[i + 1], line[i + 2]
            if bpp == 4:
                al = line[i + 3]
                if al and al < 255:                    # undo the premultiplication
                    r, g, bl = min(255, r * 255 // al), min(255, g * 255 // al), min(255, bl * 255 // al)
                out[i:i + 4] = bytes((r, g, bl, al))
            else:
                out[i:i + 3] = bytes((r, g, bl))
        rows.append(b'\0' + bytes(out))
    return (b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', ihdr)
            + _chunk(b'IDAT', zlib.compress(b''.join(rows), 6)) + _chunk(b'IEND', b''))


def icon_png(app_dir, cache_dir):
    """Path of a PNG of the app's largest icon, made once and kept in cache_dir; None if it
    has none that can be read."""
    os.makedirs(cache_dir, exist_ok=True)
    out = os.path.join(cache_dir, os.path.basename(app_dir.rstrip('/')) + '.png')
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(app_dir):
        return out
    best = None
    for path in _candidates(app_dir):
        try:
            d = open(path, 'rb').read()
        except OSError:
            continue
        if d[:8] != b'\x89PNG\r\n\x1a\n':
            continue
        w, _h = _size(d)
        if best is None or w > best[0]:
            best = (w, d)
    if best is None:
        return None
    d = best[1]
    try:
        if any(kind == b'CgBI' for kind, _ in _chunks(d)):
            d = _uncrush(d)
            if d is None:
                return None
    except Exception:
        return None
    with open(out, 'wb') as f:
        f.write(d)
    return out
