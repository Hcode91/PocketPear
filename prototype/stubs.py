"""Resolve an ARM stub (ldr ip,[pc,#4]; add ip,pc,ip; ldr pc,[ip]) to the function it calls."""
import struct, sys
sys.path.insert(0, '.')
from sym import Symbols
cache = '/tmp/fw/root/BrightonMaps10B350.N42OS/System/Library/Caches/com.apple.dyld/dyld_shared_cache_armv7s'
data = open(cache, 'rb').read()
mo, mc = struct.unpack_from('<II', data, 16)
maps = [struct.unpack_from('<QQQII', data, mo + 32 * i)[:3] for i in range(mc)]
def fo(a):
    for ad, sz, f in maps:
        if ad <= a < ad + sz: return a - ad + f
s = Symbols(); s.add_cache(data); s.finish()
for arg in sys.argv[1:]:
    stub = int(arg, 16)
    off = fo(stub)
    lit = struct.unpack_from('<I', data, off + 12)[0]
    ptr = (stub + 4 + 8 + lit) & 0xffffffff
    target = struct.unpack_from('<I', data, fo(ptr))[0]
    print(hex(stub), '->', hex(target), s.name(target))
