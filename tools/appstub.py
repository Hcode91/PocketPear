"""Name the imported function an app stub calls: appstub.py <app executable> <stub address>..."""
import struct, sys
d = open(sys.argv[1], 'rb').read()
ncmds = struct.unpack_from('<I', d, 16)[0]
o = 28
sections = []
symoff = stroff = indoff = None
for _ in range(ncmds):
    cmd, size = struct.unpack_from('<II', d, o)
    if cmd == 1:
        nsects = struct.unpack_from('<I', d, o + 48)[0]
        for k in range(nsects):
            so = o + 56 + 68 * k
            name = d[so:so + 16].split(b'\0')[0]
            addr, sz, off, align, reloff, nreloc, flags, r1, r2 = struct.unpack_from('<9I', d, so + 32)
            sections.append((name, addr, sz, flags, r1, r2))
    elif cmd == 2:
        symoff, nsyms, stroff, strsize = struct.unpack_from('<4I', d, o + 8)
    elif cmd == 0xb:
        indoff, nind = struct.unpack_from('<II', d, o + 56)
    o += size
for arg in sys.argv[2:]:
    a = int(arg, 16)
    for name, addr, sz, flags, r1, r2 in sections:
        if addr <= a < addr + sz and r2:
            idx = r1 + (a - addr) // r2
            sym = struct.unpack_from('<I', d, indoff + 4 * idx)[0]
            strx = struct.unpack_from('<I', d, symoff + 12 * sym)[0]
            print(hex(a), name.decode(), d[stroff + strx:d.find(b'\0', stroff + strx)].decode())
