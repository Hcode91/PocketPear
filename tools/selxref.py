"""Find code in one image that uses a selector: selxref.py <image> <selector>..."""
import sys, struct, re
exec(open(__file__.replace('selxref.py', 'objcsyms.py')).read())
image = sys.argv[1]
fun = sorted(a for a, n in zip(s.addrs, s.names) if n.endswith(f'({image})'))
lo, hi = fun[0], fun[-1]
# selector strings and the selrefs pointing at them (anywhere in the cache)
targets = {}
for sel in sys.argv[2:]:
    p = data.find(sel.encode() + b'\0')
    while p != -1:
        if data[p - 1] == 0:
            for addr, size, off, _, _ in maps:
                if off <= p < off + size:
                    targets[addr + p - off] = sel
        p = data.find(sel.encode() + b'\0', p + 1)
refs = {}
for t, sel in targets.items():
    pat = struct.pack('<I', t)
    p = data.find(pat)
    while p != -1:
        for addr, size, off, _, _ in maps:
            if off <= p < off + size and p % 4 == 0:
                refs[addr + p - off] = sel
        p = data.find(pat, p + 1)
md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
olo, ohi = fo(lo), fo(hi)
code = data[olo:ohi]
# scan thumb movw/movt pairs: decode T3/T1 movw and movt by bit pattern
hits = []
h = struct.unpack(f'<{len(code)//2}H', code[:len(code)//2*2])
regs = {}
for i in range(len(h) - 1):
    w1, w2 = h[i], h[i + 1]
    if (w1 & 0xfbf0) == 0xf240 or (w1 & 0xfbf0) == 0xf2c0:
        imm = ((w1 & 0xf) << 12) | (((w1 >> 10) & 1) << 11) | (((w2 >> 12) & 7) << 8) | (w2 & 0xff)
        rd = (w2 >> 8) & 0xf
        if (w1 & 0xfbf0) == 0xf240:
            regs[rd] = (imm, i)
        elif rd in regs and i - regs[rd][1] < 12:
            regs[rd] = ((regs[rd][0] | (imm << 16)), i, 'full')
    elif (w1 & 0xff78) == 0x4478:              # add rd, pc
        rd = (w1 & 7) | ((w1 >> 4) & 8)
        if rd in regs and len(regs[rd]) == 3 and i - regs[rd][1] < 12:
            va = lo + 2 * i
            t = (regs[rd][0] + va + 4) & 0xffffffff
            if t in refs:
                hits.append((va, refs[t]))
for va, sel in hits:
    print(hex(va), sel, s.name(va))
