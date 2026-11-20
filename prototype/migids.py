"""Build msgh_id -> routine name for the kernel's MIG subsystems, read from libsystem_kernel's
own client stubs (each loads 0x1513 as the header bits and then its msgh_id)."""
import json, sys
from capstone import *
from sym import Symbols
cache = sys.argv[1]
data = open(cache, 'rb').read()
s = Symbols(); s.add_cache(data); s.finish()
md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
ids = {}
for a, n in zip(s.addrs, s.names):
    if not n.endswith('(libsystem_kernel.dylib)') or not n.startswith('_'):
        continue
    off = (a & ~1) - 0x30000000
    seen_bits = False
    for i in md.disasm(data[off:off + 0x60], a & ~1):
        if i.mnemonic == 'movw':
            imm = int(i.op_str.split('#')[-1], 16)
            if imm == 0x1513 or imm == 0x80001513 & 0xffff:
                seen_bits = True
            elif seen_bits and 200 <= imm < 10000:
                ids.setdefault(imm, n.split(' ')[0][1:])
                break
        if i.mnemonic in ('pop', 'bx'):
            break
json.dump(ids, open('migids.json', 'w'), indent=0, sort_keys=True)
print(len(ids))
for k in (3229, 3218, 3404, 3409, 206, 3616, 3418):
    print(k, ids.get(k))
