"""Build the BSD syscall number table from libsystem_kernel's stubs in the shared cache."""
import os, sys, json, struct
sys.argv = ['x', '0']
exec(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'disas.py')).read())
md = Cs(CS_ARCH_ARM, CS_MODE_ARM)
out = {}
for a, n in zip(s.addrs, s.names):
    if '(libsystem_kernel.dylib)' not in n or a & 1:
        continue
    name = n.split(' (')[0]
    off = fo(a)
    if off is None:
        continue
    ip = None
    for i in md.disasm(data[off:off + 24], a):
        if i.mnemonic == 'mov' and i.op_str.startswith('ip, #'):
            ip = int(i.op_str[5:], 0)
        elif i.mnemonic == 'orr' and i.op_str.startswith('ip, ip, #') and ip is not None:
            ip |= int(i.op_str[9:], 0)
        elif i.mnemonic == 'mvn' and i.op_str.startswith('ip, #'):
            ip = ~int(i.op_str[5:], 0)
        elif i.mnemonic == 'svc':
            if ip is not None and ip > 0:
                out.setdefault(ip, name.lstrip('_'))
            break
        elif i.mnemonic not in ('mov', 'orr', 'mvn'):
            break
print(json.dumps({k: out[k] for k in sorted(out)}, indent=0))
