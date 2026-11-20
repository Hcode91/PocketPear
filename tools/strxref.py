"""Find Thumb movw/movt + add pc references to a C string: strxref.py <image> <string>"""
import sys, struct
exec(open(__file__.replace('strxref.py', 'objcsyms.py')).read())
image, text = sys.argv[1], sys.argv[2].encode() + b'\0'
targets = set()
p = data.find(text)
while p != -1:
    for addr, size, off, _, _ in maps:
        if off <= p < off + size:
            targets.add(addr + p - off)
    p = data.find(text, p + 1)
fun = sorted(a for a, n in zip(s.addrs, s.names) if n.endswith(f'({image})'))
lo, hi = fun[0], fun[-1]
code = data[fo(lo):fo(hi)]
h = struct.unpack(f'<{len(code)//2}H', code[:len(code)//2*2])
regs = {}
for i in range(len(h) - 1):
    w1, w2 = h[i], h[i + 1]
    if (w1 & 0xfbf0) in (0xf240, 0xf2c0):
        imm = ((w1 & 0xf) << 12) | (((w1 >> 10) & 1) << 11) | (((w2 >> 12) & 7) << 8) | (w2 & 0xff)
        rd = (w2 >> 8) & 0xf
        if (w1 & 0xfbf0) == 0xf240:
            regs[rd] = [imm, i, False]
        elif rd in regs and i - regs[rd][1] < 12:
            regs[rd] = [regs[rd][0] | (imm << 16), i, True]
    elif (w1 & 0xff78) == 0x4478:
        rd = (w1 & 7) | ((w1 >> 4) & 8)
        if rd in regs and regs[rd][2] and i - regs[rd][1] < 12:
            va = lo + 2 * i
            if (regs[rd][0] + va + 4) & 0xffffffff in targets:
                print(hex(va), s.name(va))
