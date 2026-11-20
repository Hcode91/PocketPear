"""Shared: load symbols including Objective-C method names (slow: ~5 s)."""
import os, sys, struct
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'prototype'))
from sym import Symbols
from capstone import *
path = os.environ.get('PP_CACHE', '/tmp/fw/root/BrightonMaps10B350.N42OS/System/Library/Caches/com.apple.dyld/dyld_shared_cache_armv7s')
data = open(path, 'rb').read()
s = Symbols(); s.add_cache(data); s.finish()
inv = {}
for a, n in zip(s.addrs, s.names):
    inv.setdefault(n.split(' (')[0], a)
mo, mc = struct.unpack_from('<II', data, 0x10)
maps = [struct.unpack_from('<QQQII', data, mo + 32 * i) for i in range(mc)]
def fo(a):
    for addr, size, off, _, _ in maps:
        if addr <= a < addr + size:
            return off + a - addr
def dis(a, n, thumb=None):
    if thumb is None:
        thumb = a in s.thumb
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB if thumb else CS_MODE_ARM)
    out = []
    for i in md.disasm(data[fo(a):fo(a) + n], a):
        extra = ''
        if i.mnemonic in ('bl', 'blx', 'b.w', 'b') and i.op_str.startswith('#'):
            t = int(i.op_str[1:], 16)
            if not (a <= t < a + n):
                extra = s.name(t)
        out.append(f'  {i.address:#x} {i.mnemonic} {i.op_str} {extra}')
    return out
