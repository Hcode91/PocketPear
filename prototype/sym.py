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
