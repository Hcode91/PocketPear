"""List the app's Objective-C methods: appobjc.py <app executable> [filter]"""
import struct, sys
d = open(sys.argv[1], 'rb').read()
segs, sects = [], {}
o = 28
for _ in range(struct.unpack_from('<I', d, 16)[0]):
    cmd, size = struct.unpack_from('<II', d, o)
    if cmd == 1:
        va, vs, f, fs = struct.unpack_from('<4I', d, o + 24)
        segs.append((va, vs, f))
        ns = struct.unpack_from('<I', d, o + 48)[0]
        for k in range(ns):
            so = o + 56 + 68 * k
            name = d[so:so + 16].rstrip(b'\0').decode()
            addr, sz = struct.unpack_from('<II', d, so + 32)
            sects[name] = (addr, sz)
    o += size
def fo(a):
    for va, vs, f in segs:
        if va <= a < va + vs: return a - va + f
def r32(a): return struct.unpack_from('<I', d, fo(a))[0]
def cstr(a):
    f = fo(a); return d[f:d.find(b'\0', f)].decode('latin1')
flt = sys.argv[2] if len(sys.argv) > 2 else ''
a, sz = sects['__objc_classlist']
for i in range(sz // 4):
    for meta, cls in ((False, r32(a + 4 * i)),):
        for is_meta, c in ((False, cls), (True, r32(cls))):
            ro = r32(c + 16) & ~3
            name = cstr(r32(ro + 16))
            ml = r32(ro + 20)
            if not ml: continue
            es, cnt = r32(ml), r32(ml + 4)
            for j in range(cnt):
                e = ml + 8 + j * (es & 0xfffc)
                sel, imp = cstr(r32(e)), r32(e + 8)
                n = f"{'+-'[not is_meta]}[{name} {sel}]"
                if flt in n: print(hex(imp), n)
