"""Find Thumb-2 'movw rN, #imm' sites inside one image of the cache: findimm.py <image> <imm>..."""
import sys, struct
exec(open(__file__.replace('findimm.py', 'objcsyms.py')).read())
image = sys.argv[1]
addrs = [a for a, n in zip(s.addrs, s.names) if n.endswith(f'({image})')]
lo, hi = min(addrs), max(addrs)
def enc(v):
    imm4, i, imm3, imm8 = v >> 12, (v >> 11) & 1, (v >> 8) & 7, v & 0xff
    return 0xF240 | (i << 10) | imm4, (imm3 << 12) | imm8
for arg in sys.argv[2:]:
    v = int(arg, 0)
    h1, h2 = enc(v)
    olo, ohi = fo(lo), fo(hi)
    pat = struct.pack('<H', h1)
    p = data.find(pat, olo, ohi)
    while p != -1:
        w2, = struct.unpack_from('<H', data, p + 2)
        if (w2 & 0x70ff) == h2:
            va = lo + (p - olo)
            print(f'{v:#x} at {va:#x} in {s.name(va)}')
        p = data.find(pat, p + 2, ohi)
