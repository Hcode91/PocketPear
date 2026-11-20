"""Disassemble the app binary: appdis.py <app executable> <address> <bytes> [arm]"""
import struct, sys
from capstone import *
d = open(sys.argv[1], 'rb').read()
segs = []
o = 28
for _ in range(struct.unpack_from('<I', d, 16)[0]):
    cmd, size = struct.unpack_from('<II', d, o)
    if cmd == 1:
        va, vs, fo_, fs = struct.unpack_from('<4I', d, o + 24)
        segs.append((va, vs, fo_))
    o += size
def fo(a):
    for va, vs, f in segs:
        if va <= a < va + vs:
            return a - va + f
def r32(a): return struct.unpack_from('<I', d, fo(a))[0]
def cstr(a):
    f = fo(a)
    return d[f:d.find(b'\0', f)][:60] if f is not None else None
a, n = int(sys.argv[2], 16), int(sys.argv[3], 16)
md = Cs(CS_ARCH_ARM, CS_MODE_ARM if len(sys.argv) > 4 else CS_MODE_THUMB)
regs = {}
for i in md.disasm(d[fo(a):fo(a) + n], a):
    line = f'  {i.address:#x} {i.mnemonic} {i.op_str}'
    if i.mnemonic == 'movw':
        regs[i.op_str.split(',')[0]] = int(i.op_str.split('#')[1], 16)
    elif i.mnemonic == 'movt':
        r = i.op_str.split(',')[0]; regs[r] = regs.get(r, 0) | (int(i.op_str.split('#')[1], 16) << 16)
    elif i.mnemonic == 'add' and i.op_str.endswith(', pc'):
        r = i.op_str.split(',')[0]
        if r in regs:
            va = (regs[r] + i.address + 4) & 0xffffffff
            st = cstr(va)
            line += f'   ; {va:#x} {st!r}'
    print(line)
