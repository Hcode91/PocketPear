"""Symbol names for addresses in the shared cache, dyld and the app, for backtraces."""
import bisect
import struct


class Symbols:
    def __init__(self):
        self.addrs, self.names = [], []
        self.thumb = set()        # addresses of functions defined as Thumb (N_ARM_THUMB_DEF)

    def add_macho(self, data, slide=0, file_base=0, image_name=''):
        """data is the whole file (or cache); the header is at file offset 0 for a plain
        Mach-O, or at the image's location for a cache image (file_base)."""
        magic, ct, cs, ft, ncmds, _, fl = struct.unpack_from('<7I', data, file_base)
        if magic != 0xfeedface:
            return
        o = file_base + 28
        for _ in range(ncmds):
            cmd, size = struct.unpack_from('<II', data, o)
            if cmd == 2:   # LC_SYMTAB
                symoff, nsyms, stroff, strsize = struct.unpack_from('<4I', data, o + 8)
                for i in range(nsyms):
                    strx, ntype, sect, desc, value = struct.unpack_from('<IBBhI', data, symoff + 12 * i)
                    if ntype & 0xe0 or (ntype & 0x0e) != 0x0e or not value:
                        continue
                    end = data.find(b'\0', stroff + strx)
                    name = data[stroff + strx:end].decode('utf-8', 'replace')
                    self.addrs.append(value + slide)
                    if desc & 0x8:
                        self.thumb.add(value + slide)
                    self.names.append(name + (f' ({image_name})' if image_name else ''))
            o += size

    def add_objc(self, read32, read_cstr, sections, image_name=''):
        """Name Objective-C method implementations: -[Class sel] / +[Class sel]. sections are
        (address, size) of each __objc_classlist; read32/read_cstr read virtual addresses."""
        n = 0
        for addr, size in sections:
            for i in range(size // 4):
                cls = read32(addr + 4 * i)
                for meta in (False, True):
                    c = read32(cls) if meta else cls          # the metaclass is the class's isa
                    try:
                        ro = read32(c + 16) & ~3
                        name = read_cstr(read32(ro + 16))
                        mlist = read32(ro + 20)
                    except Exception:
                        continue
                    if not mlist:
                        continue
                    entsize, count = read32(mlist) & ~3, read32(mlist + 4)
                    if entsize != 12 or count > 10000:
                        continue
                    for j in range(count):
                        e = mlist + 8 + 12 * j
                        try:
                            sel = read_cstr(read32(e))
                            imp = read32(e + 8)
                        except Exception:
                            continue
                        if imp:
                            self.addrs.append(imp & ~1)
                            if imp & 1:
                                self.thumb.add(imp & ~1)
                            self.names.append(f'{"+" if meta else "-"}[{name} {sel}]' + (f' ({image_name})' if image_name else ''))
                            n += 1
        return n

    @staticmethod
    def sections(data, file_base, want):
        """(addr, size) of the sections named want in a 32-bit Mach-O header at file_base."""
        out = []
        magic, ct, cs, ft, ncmds, _, fl = struct.unpack_from('<7I', data, file_base)
        o = file_base + 28
        for _ in range(ncmds):
            cmd, size = struct.unpack_from('<II', data, o)
            if cmd == 1:   # LC_SEGMENT
                nsects, = struct.unpack_from('<I', data, o + 48)
                for k in range(nsects):
                    so = o + 56 + 68 * k
                    sect = data[so:so + 16].split(b'\0')[0]
                    addr, sz = struct.unpack_from('<II', data, so + 32)
                    if sect == want:
                        out.append((addr, sz))
            o += size
        return out

    def add_cache(self, cache):
        mo, mc, io, ic = struct.unpack_from('<4I', cache, 16)
        maps = [struct.unpack_from('<QQQ', cache, mo + 32 * i) for i in range(mc)]
        def file_off(addr):
            for a, s, f in maps:
                if a <= addr < a + s:
                    return addr - a + f
        for i in range(ic):
            addr, mtime, inode, path_off, _ = struct.unpack_from('<QQQII', cache, io + 32 * i)
            path = cache[path_off:cache.find(b'\0', path_off)].decode()
            off = file_off(addr)
            if off is not None:
                self.add_macho(cache, 0, off, path.rsplit('/', 1)[-1])
                secs = self.sections(cache, off, b'__objc_classlist')
                if secs:
                    def r32(a):
                        fo = file_off(a)
                        if fo is None:
                            raise ValueError(a)
                        return struct.unpack_from('<I', cache, fo)[0]
                    def rstr(a):
                        fo = file_off(a)
                        if fo is None:
                            raise ValueError(a)
                        return cache[fo:cache.find(b'\0', fo)].decode('utf-8', 'replace')
                    self.add_objc(r32, rstr, secs, path.rsplit('/', 1)[-1])

    def finish(self):
        order = sorted(range(len(self.addrs)), key=self.addrs.__getitem__)
        self.addrs = [self.addrs[i] for i in order]
        self.names = [self.names[i] for i in order]

    def name(self, addr):
        addr &= ~1
        i = bisect.bisect_right(self.addrs, addr) - 1
        if i < 0:
            return f'{addr:#x}'
        return f'{self.names[i]}+{addr - self.addrs[i]:#x}'
