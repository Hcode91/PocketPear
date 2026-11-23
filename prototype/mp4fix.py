"""Movies whose video has an odd width or height, made playable by Android's decoders.

iOS plays a 1279x720 H.264 movie; every decoder on an Android phone refuses one (they take even
sizes only), so the player runs with a black picture (Bioshock's opening PlaneSequence.m4v on a
Galaxy S23: "not 1279x720" from all eight). The picture itself is coded in whole 16-pixel blocks
and cropped, so only the sizes the container states are odd. even_sized() returns a copy whose
track header and sample description say the next even size; the movie data is untouched.
"""
import os
import struct

CONTAINERS = {b'moov', b'trak', b'mdia', b'minf', b'stbl'}


def _boxes(d, o, end):
    while o + 8 <= end:
        size, typ = struct.unpack('>I4s', d[o:o + 8])
        head = 8
        if size == 1:
            size = struct.unpack('>Q', d[o + 8:o + 16])[0]
            head = 16
        elif size == 0:
            size = end - o
        if size < head or o + size > end:
            return
        yield o, head, size, typ
        o += size


def odd_size_fixes(d):
    """(offset, 2-byte or 4-byte, new value) patches that make every video track's stated size even."""
    fixes = []
    moov = next((b for b in _boxes(d, 0, len(d)) if b[3] == b'moov'), None)
    if moov is None:
        return fixes

    def walk(o, end, trak):
        for bo, head, size, typ in _boxes(d, o, end):
            if typ == b'trak':
                trak = {'video': False, 'tkhd': None, 'entries': []}
                walk(bo + head, bo + size, trak)
                if trak['video']:
                    if trak['tkhd'] is not None:
                        at = trak['tkhd']
                        w, h = struct.unpack('>II', d[at:at + 8])
                        for i, v in enumerate((w, h)):
                            if (v >> 16) & 1 and not v & 0xffff:
                                fixes.append((at + 4 * i, 4, v + 0x10000))
                    for at in trak['entries']:
                        w, h = struct.unpack('>HH', d[at:at + 4])
                        for i, v in enumerate((w, h)):
                            if v & 1:
                                fixes.append((at + 2 * i, 2, v + 1))
            elif typ in CONTAINERS:
                walk(bo + head, bo + size, trak)
            elif trak is not None and typ == b'tkhd':
                trak['tkhd'] = bo + size - 8
            elif trak is not None and typ == b'hdlr':
                trak['video'] = d[bo + head + 8:bo + head + 12] == b'vide'
            elif trak is not None and typ == b'stsd':
                count = struct.unpack('>I', d[bo + head + 4:bo + head + 8])[0]
                e = bo + head + 8
                for _ in range(count):
                    if e + 36 > bo + size:
                        break
                    trak['entries'].append(e + 32)
                    e += struct.unpack('>I', d[e:e + 4])[0] or (bo + size - e)
    walk(moov[0] + moov[1], moov[0] + moov[2], None)
    return fixes


def even_sized(path, cache_dir):
    """path, or a copy in cache_dir whose video sizes are even. Never raises."""
    try:
        st = os.stat(path)
        with open(path, 'rb') as f:
            d = f.read()
        fixes = odd_size_fixes(d)
        if not fixes:
            return path
        os.makedirs(cache_dir, exist_ok=True)
        out = os.path.join(cache_dir, f'{st.st_size:x}-{int(st.st_mtime):x}-' + os.path.basename(path))
        if not os.path.exists(out):
            b = bytearray(d)
            for at, n, v in fixes:
                b[at:at + n] = v.to_bytes(n, 'big')
            tmp = out + '.part'
            with open(tmp, 'wb') as f:
                f.write(b)
            os.replace(tmp, out)
        print(f'[movie] {os.path.basename(path)} has an odd video size; playing a copy that states it even')
        return out
    except Exception as e:
        print(f'[movie] could not check {os.path.basename(path)}: {e}')
        return path
