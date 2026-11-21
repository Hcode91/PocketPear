"""Imports the system does not have.

A game built with a newer SDK may import symbols that iOS 6 never had (an iOS 7 constant such as
NSMetadataUbiquitousItemDownloadingStatusKey). iOS 6's dyld refuses to start such an app. Before
dyld runs, every import that no system library defines is found and handled:

* a lazily bound function: its lazy pointer is pointed at a stand-in that returns 0, so dyld never
  looks it up (it binds a lazy pointer only on its first call, and this one is already filled);
* anything bound at launch: its bind entry is marked weak, so dyld binds 0 instead of stopping.
  Just before the game's own code first runs (its first initializer, or its entry point), a
  constant gets a real string whose value is its own name, which is what Apple's keys and
  notification names are, and a function gets the same stand-in. A class stays nil, as a class
  that does not exist yet does on the real device.
"""
import struct

BIND_WEAK = 0x1
LOAD_DYLIB_CMDS = {0xc, 0x80000018, 0x8000001f, 0x20, 0x80000023}
# Name endings that mean a data constant (an NSString/CFString key or name), not a function.
CONSTANT_ENDINGS = ('Key', 'Keys', 'Notification', 'Name', 'Domain', 'Identifier', 'Attribute',
                    'Option', 'Options', 'Constant', 'Scheme', 'Value', 'Type', 'Style', 'Mode',
                    'Category', 'Format', 'Error', 'Policy', 'Level', 'Hint', 'Property', 'Info')


def uleb(d, p):
    v = s = 0
    while True:
        b = d[p]; p += 1
        v |= (b & 0x7f) << s; s += 7
        if b < 0x80:
            return v, p


def sleb(d, p):
    v = s = 0
    while True:
        b = d[p]; p += 1
        v |= (b & 0x7f) << s; s += 7
        if b < 0x80:
            if b & 0x40:
                v -= 1 << s
            return v, p


def parse_binds(d, start, size, segments, lazy):
    """(symbol, flag_pos, address) for every bind; flag_pos is the offset in d of the opcode byte
    that carries the symbol's flags."""
    out, p, end = [], start, start + size
    sym, flag_pos, seg, off = None, None, 0, 0
    def at():
        return (segments[seg] + off) & 0xffffffff
    while p < end:
        b = d[p]; p += 1
        op, imm = b & 0xf0, b & 0x0f
        if op == 0x00:
            if not lazy:
                break
        elif op == 0x10 or op == 0x30 or op == 0x50:
            pass
        elif op == 0x20:
            _, p = uleb(d, p)
        elif op == 0x40:
            flag_pos = p - 1
            e = d.index(b'\0', p)
            sym = d[p:e].decode('utf-8', 'replace'); p = e + 1
        elif op == 0x60:
            _, p = sleb(d, p)
        elif op == 0x70:
            seg = imm; off, p = uleb(d, p)
        elif op == 0x80:
            v, p = uleb(d, p); off = (off + v) & 0xffffffff
        elif op == 0x90:
            out.append((sym, flag_pos, at())); off += 4
        elif op == 0xa0:
            out.append((sym, flag_pos, at())); v, p = uleb(d, p); off = (off + 4 + v) & 0xffffffff
        elif op == 0xb0:
            out.append((sym, flag_pos, at())); off += 4 + imm * 4
        elif op == 0xc0:
            n, p = uleb(d, p); skip, p = uleb(d, p)
            for _ in range(n):
                out.append((sym, flag_pos, at())); off += 4 + skip
        else:
            raise ValueError(f'bind opcode {b:#x}')
    return out


def is_constant(name):
    n = name.lstrip('_')
    return n.endswith(CONSTANT_ENDINGS) or (n[:1] == 'k' and n[1:2].isupper())


class MissingImports:
    def __init__(self, emu):
        self.emu = emu
        self.slots = []            # (symbol, address) bound to 0 at launch, filled before game code
        self.stub = None
        self.filled = False
        self.arena = self.arena_end = 0
        self.warned = set()

    def install(self, exe, system_defined):
        d = exe.data
        segs = [s[1] for s in exe.segments]
        linkedit = next(((va, fo) for n, va, vs, fo, fs, ip in exe.segments if n == '__LINKEDIT'), None)
        info, inits, main_off, o = None, [], None, 28
        ncmds = struct.unpack_from('<I', d, 16)[0]
        for _ in range(ncmds):
            cmd, size = struct.unpack_from('<II', d, o)
            if cmd in (0x22, 0x80000022):
                info = struct.unpack_from('<10I', d, o + 8)
            elif cmd == 0x80000028:
                main_off = struct.unpack_from('<Q', d, o + 8)[0]
            elif cmd == 1:
                nsects = struct.unpack_from('<I', d, o + 48)[0]
                for i in range(nsects):
                    so = o + 56 + 68 * i
                    addr, sz, foff = struct.unpack_from('<3I', d, so + 32)
                    flags = struct.unpack_from('<I', d, so + 56)[0]
                    if flags & 0xff == 0x9:                     # S_MOD_INIT_FUNC_POINTERS
                        inits += [x & ~1 for x in struct.unpack_from(f'<{sz // 4}I', d, foff) if x]
            o += size
        if not info or not linkedit:
            return
        bind_off, bind_size, _, _, lazy_off, lazy_size = info[2:8]
        def missing(name):
            return name and name not in system_defined
        binds = [b for b in parse_binds(d, bind_off, bind_size, segs, False) if missing(b[0])]
        lazies = [b for b in parse_binds(d, lazy_off, lazy_size, segs, True) if missing(b[0])]
        if not binds and not lazies:
            return
        names = sorted({b[0] for b in binds + lazies})
        print(f'[imports] {len(names)} import(s) iOS 6 does not have: {", ".join(n.lstrip("_") for n in names[:12])}'
              + (' ...' if len(names) > 12 else ''))
        self.stub = self.alloc(16)
        self.emu.stubs.replace(self.stub, self.stand_in, False)
        for sym, _, addr in lazies:
            self.emu.w32(addr, self.stub)
        le_va, le_fo = linkedit
        for pos in {b[1] for b in binds}:
            a = le_va + pos - le_fo
            self.emu.write(a, bytes([self.emu.read(a, 1)[0] | BIND_WEAK]))
        self.slots = [(sym, addr) for sym, _, addr in binds]
        if self.slots:
            from unicorn import UC_HOOK_CODE
            text = next(va for n, va, vs, fo, fs, ip in exe.segments if n == '__TEXT')
            entry = text + main_off if main_off is not None else exe.entry
            for a in set(inits + ([entry & ~1] if entry else [])):
                self.emu.uc.hook_add(UC_HOOK_CODE, self.fill, begin=a, end=a)

    def alloc(self, size):
        size = (size + 15) & ~15
        if self.arena + size > self.arena_end:
            n = max(0x4000, (size + 0xfff) & ~0xfff)
            self.arena = self.emu.find_free(n)
            self.emu.map(self.arena, n)
            self.arena_end = self.arena + n
        a = self.arena
        self.arena += size
        return a

    def constant(self, name):
        """A CFString constant whose value is the symbol's own name, and the variable holding it."""
        isa = getattr(self, 'cfstring_isa', None)
        if isa is None:
            isa = self.cfstring_isa = next((a for a, n in zip(self.emu.syms.addrs, self.emu.syms.names)
                                            if n.startswith('___CFConstantStringClassReference (')), 0)
        text = name.lstrip('_').encode()
        cstr = self.alloc(len(text) + 1)
        self.emu.write(cstr, text + b'\0')
        cf = self.alloc(16)
        self.emu.write(cf, struct.pack('<4I', isa, 0x7c8, cstr, len(text)))
        var = self.alloc(4)
        self.emu.w32(var, cf)
        return var

    def fill(self, uc, address, size, data):
        if self.filled:
            return
        self.filled = True
        made = {}
        for sym, addr in self.slots:
            if sym.startswith(('_OBJC_CLASS_$_', '_OBJC_METACLASS_$_', '_OBJC_IVAR_$_')):
                continue
            if sym not in made:
                made[sym] = self.constant(sym) if is_constant(sym) else self.stub
            self.emu.w32(addr, made[sym])
        print(f'[imports] filled {len(self.slots)} launch-time slot(s) before the game\'s own code')

    def stand_in(self):
        from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_LR
        lr = self.emu.uc.reg_read(UC_ARM_REG_LR)
        if lr not in self.warned and len(self.warned) < 50:
            self.warned.add(lr)
            print(f'[imports] a function iOS 6 does not have was called from {self.emu.syms.name(lr)}; returned 0')
        self.emu.uc.reg_write(UC_ARM_REG_R0, 0)
        self.emu.uc.reg_write(UC_ARM_REG_R1, 0)
