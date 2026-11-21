"""Firmware import: from an Apple disk image straight to a root folder, on the phone.

Takes what a firmware dump actually looks like, all read in place without unpacking first:
  - split archives (name.001, name.002, ...) given together, read as one file;
  - a .zip holding a .dmg (or holding the root folder itself);
  - a UDIF .dmg (zlib, ADC, bzip2 or raw chunks), or a bare HFS+/HFSX volume image.

The HFS+ reader follows the catalog B-tree and writes every folder, file (data fork, extents
overflow included), symlink and hard link out under the destination. Read-only, and only what
an iOS root filesystem uses.
"""
import bz2
import io
import os
import plistlib
import stat
import struct
import zlib


# ---------------------------------------------------------------------------- byte sources
class Source:
    """Random access over one or more files read as one (split archives), via os.pread."""

    def __init__(self, fds):
        self.parts = []                       # (start, size, fd)
        pos = 0
        for fd in fds:
            size = os.fstat(fd).st_size
            self.parts.append((pos, size, fd))
            pos += size
        self.size = pos

    def pread(self, n, off):
        out = []
        for start, size, fd in self.parts:
            if n <= 0:
                break
            if off < start + size and off + n > start:
                a = off - start
                k = min(n, size - a)
                out.append(os.pread(fd, k, a))
                off += k
                n -= k
        return b''.join(out)


class SourceFile(io.RawIOBase):
    """A Source as a seekable file (for zipfile)."""

    def __init__(self, src):
        self.src, self.pos = src, 0

    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.src.size + off
        return self.pos

    def readinto(self, b):
        data = self.src.pread(len(b), self.pos)
        b[:len(data)] = data
        self.pos += len(data)
        return len(data)


# ---------------------------------------------------------------------------- UDIF
def adc_decompress(data, out_len):
    out = bytearray()
    i = 0
    while i < len(data) and len(out) < out_len:
        c = data[i]
        if c & 0x80:                                   # literal run
            n = (c & 0x7f) + 1
            out += data[i + 1:i + 1 + n]
            i += 1 + n
        elif c & 0x40:                                 # 3-byte copy
            n = (c & 0x3f) + 4
            off = (data[i + 1] << 8 | data[i + 2]) + 1
            for _ in range(n):
                out.append(out[-off])
            i += 3
        else:                                          # 2-byte copy
            n = ((c >> 2) & 0xf) + 3
            off = ((c & 3) << 8 | data[i + 1]) + 1
            for _ in range(n):
                out.append(out[-off])
            i += 2
    return bytes(out)


class UDIF:
    """A UDIF (.dmg) partition as a random-access byte source, decompressing chunks on demand."""
    SECTOR = 512

    def __init__(self, src):
        self.src = src
        koly = src.pread(512, src.size - 512)
        if koly[:4] != b'koly':
            raise ValueError('not a UDIF disk image')
        xml_off, xml_len = struct.unpack_from('>QQ', koly, 0xD8)
        plist = plistlib.loads(src.pread(xml_len, xml_off))
        blkx = plist['resource-fork']['blkx']
        # the HFS partition (the largest one whose name says so)
        best = None
        for b in blkx:
            data = b['Data']
            if 'Apple_HFS' in b.get('Name', '') or 'Apple_HFS' in b.get('CFName', ''):
                if best is None or len(data) > len(best):
                    best = data
        if best is None:
            best = max((b['Data'] for b in blkx), key=len)
        if best[:4] != b'mish':
            raise ValueError('disk image has no block table')
        first, count, data_off = struct.unpack_from('>QQQ', best, 8)
        n = struct.unpack_from('>I', best, 0xC8)[0]
        self.chunks = []                              # (sector, sectors, type, offset, length)
        for i in range(n):
            t, _, sec, cnt, off, ln = struct.unpack_from('>IIQQQQ', best, 0xCC + 40 * i)
            if t in (0x7ffffffe, 0xffffffff) or cnt == 0:
                continue
            self.chunks.append((sec, cnt, t, data_off + off, ln))
        self.chunks.sort()
        self.starts = [c[0] for c in self.chunks]
        self.size = count * self.SECTOR
        self.cache = {}
        self.types = sorted({c[2] for c in self.chunks})

    def chunk(self, i):
        got = self.cache.get(i)
        if got is not None:
            return got
        sec, cnt, t, off, ln = self.chunks[i]
        size = cnt * self.SECTOR
        if t in (0, 2):
            data = bytes(size)
        elif t == 1:
            data = self.src.pread(ln, off)
        elif t == 0x80000005:
            data = zlib.decompress(self.src.pread(ln, off))
        elif t == 0x80000006:
            data = bz2.decompress(self.src.pread(ln, off))
        elif t == 0x80000004:
            data = adc_decompress(self.src.pread(ln, off), size)
        else:
            raise ValueError(f'disk image uses chunk type {t:#x} (LZFSE?), not supported')
        if len(self.cache) > 64:
            self.cache.clear()
        self.cache[i] = data
        return data

    def pread(self, n, off):
        import bisect
        out = []
        while n > 0 and off < self.size:
            sec = off // self.SECTOR
            i = bisect.bisect_right(self.starts, sec) - 1
            if i < 0:
                break
            cs, cc = self.chunks[i][0], self.chunks[i][1]
            if sec >= cs + cc:                         # a hole between chunks reads as zeros
                nxt = self.starts[i + 1] * self.SECTOR if i + 1 < len(self.starts) else self.size
                k = min(n, nxt - off)
                out.append(bytes(k))
            else:
                data = self.chunk(i)
                a = off - cs * self.SECTOR
                k = min(n, len(data) - a)
                out.append(data[a:a + k])
            off += k
            n -= k
        return b''.join(out)


# ---------------------------------------------------------------------------- HFS+
class HFS:
    def __init__(self, src, offset=0):
        self.src, self.base = src, offset
        vh = src.pread(512, offset + 1024)
        sig = vh[:2]
        if sig not in (b'H+', b'HX'):
            raise ValueError('not an HFS+ volume')
        self.block = struct.unpack_from('>I', vh, 0x28)[0]
        self.ext_fork = self.fork(vh, 0xC0)
        self.cat_fork = self.fork(vh, 0x110)
        self.attr_fork = self.fork(vh, 0x160)
        self.overflow = {}                            # (fork type, file id) -> [(start block, count)]
        self.read_extents_overflow()

    @staticmethod
    def fork(buf, off):
        size = struct.unpack_from('>Q', buf, off)[0]
        ext = [struct.unpack_from('>II', buf, off + 16 + 8 * i) for i in range(8)]
        return size, [e for e in ext if e[1]]

    def read_fork(self, fork, fid=None, ftype=0, off=0, n=None):
        size, ext = fork
        if fid is not None and (ftype, fid) in self.overflow:
            ext = ext + self.overflow[(ftype, fid)]
        if n is None:
            n = size - off
        n = max(0, min(n, size - off))
        out, pos = [], 0
        for start, count in ext:
            length = count * self.block
            if n <= 0:
                break
            if off < pos + length:
                a = off - pos
                k = min(n, length - a)
                out.append(self.src.pread(k, self.base + start * self.block + a))
                off += k
                n -= k
            pos += length
        return b''.join(out)

    def btree_leaves(self, fork, fid):
        head = self.read_fork(fork, fid, 0, 0, 512)
        first_leaf = struct.unpack_from('>I', head, 14 + 10)[0]
        node_size = struct.unpack_from('>H', head, 14 + 18)[0]
        node = first_leaf
        while node:
            nd = self.read_fork(fork, fid, 0, node * node_size, node_size)
            flink, _, kind, _, nrec = struct.unpack_from('>IIbBH', nd, 0)
            offs = [struct.unpack_from('>H', nd, node_size - 2 * (i + 1))[0] for i in range(nrec + 1)]
            for i in range(nrec):
                yield nd[offs[i]:offs[i + 1]]
            node = flink

    def read_extents_overflow(self):
        if not self.ext_fork[1]:
            return
        for rec in self.btree_leaves(self.ext_fork, 3):
            klen, ftype, _, fid, start = struct.unpack_from('>HBBII', rec, 0)
            data = rec[2 + klen:]
            ext = [struct.unpack_from('>II', data, 8 * i) for i in range(8)]
            self.overflow.setdefault((ftype, fid), []).extend(e for e in ext if e[1])
        for v in self.overflow.values():
            v.sort()

    def decmpfs_attrs(self):
        """file id -> com.apple.decmpfs attribute bytes (HFS+ transparent compression)."""
        out = {}
        if not self.attr_fork[1]:
            return out
        for rec in self.btree_leaves(self.attr_fork, 8):
            klen, _, fid, _, nlen = struct.unpack_from('>HHIIH', rec, 0)
            name = rec[14:14 + 2 * nlen].decode('utf-16-be', 'replace')
            if name != 'com.apple.decmpfs':
                continue
            body = rec[2 + klen:]
            if struct.unpack_from('>I', body, 0)[0] != 0x10:
                continue
            size = struct.unpack_from('>I', body, 12)[0]
            out[fid] = body[16:16 + size]
        return out

    def decompress(self, attr, rfork, fid):
        magic, ctype, size = struct.unpack_from('<4sIQ', attr, 0)
        if magic != b'fpmc':
            raise ValueError('bad decmpfs header')
        if ctype == 3:                                   # data inline in the attribute
            data = attr[16:]
            if data[:1] and data[0] & 0x0f == 0x0f:
                return data[1:1 + size]
            return zlib.decompress(data)[:size]
        if ctype == 4:                                   # zlib blocks in the resource fork
            res = self.read_fork(rfork, fid, 0xff)
            data_off = struct.unpack_from('>I', res, 0)[0]
            table = data_off + 4
            count = struct.unpack_from('<I', res, table)[0]
            out = []
            for i in range(count):
                off, ln = struct.unpack_from('<II', res, table + 4 + 8 * i)
                blk = res[table + off:table + off + ln]
                out.append(blk[1:] if blk[:1] == b'\xff' else zlib.decompress(blk))
            return b''.join(out)[:size]
        raise ValueError(f'compression type {ctype} not supported')

    def catalog(self):
        """Yield (parent id, name, record bytes) for every folder and file record."""
        for rec in self.btree_leaves(self.cat_fork, 4):
            klen, parent, nlen = struct.unpack_from('>HIH', rec, 0)
            # HFS+ keeps the Mac separator ':' as '/' in names; a POSIX name shows it as ':'
            name = rec[8:8 + 2 * nlen].decode('utf-16-be', 'replace').replace('/', ':')
            body = rec[2 + klen:]
            kind = struct.unpack_from('>h', body, 0)[0]
            if kind in (1, 2):
                yield parent, name, kind, body


# ---------------------------------------------------------------------------- extraction
PRIVATE = '\x00\x00\x00\x00HFS+ Private Data'


def extract_hfs(hfs, dest, progress=None, label='Firmware'):
    folders = {1: ('', None)}                         # id -> (name, parent)
    files = []
    private_id = None
    for parent, name, kind, body in hfs.catalog():
        if kind == 1:
            fid = struct.unpack_from('>I', body, 8)[0]
            folders[fid] = (name, parent)
            if parent == 2 and name == PRIVATE:
                private_id = fid
        else:
            files.append((parent, name, body))

    paths = {}

    def path_of(fid):
        if fid in paths:
            return paths[fid]
        if fid in (1, 2):
            return ''
        name, parent = folders.get(fid, ('?', 1))
        p = path_of(parent) + '/' + name
        paths[fid] = p
        return p

    for fid, (name, parent) in folders.items():
        if fid > 2 and parent != private_id and fid != private_id:
            os.makedirs(dest + path_of(fid), exist_ok=True)

    compressed = hfs.decmpfs_attrs()
    inodes = {}
    if private_id is not None:
        for parent, name, body in files:
            if parent == private_id and name.startswith('iNode'):
                inodes[int(name[5:])] = body

    total = sum(struct.unpack_from('>Q', b, 88)[0] for p, n, b in files if p != private_id) or 1
    done, last = 0, -1
    for parent, name, body in files:
        if parent == private_id or (parent in folders and folders[parent][1] == private_id):
            continue
        fid = struct.unpack_from('>I', body, 8)[0]
        mode = struct.unpack_from('>H', body, 42)[0]
        ftype, creator = body[48:52], body[52:56]
        if ftype == b'hlnk' and creator == b'hfs+':     # hard link: the data lives in an iNode file
            inode = struct.unpack_from('>I', body, 44)[0]
            target = inodes.get(inode)
            if target is not None:
                body = target
                fid = struct.unpack_from('>I', body, 8)[0]
                mode = struct.unpack_from('>H', body, 42)[0]
        out = dest + path_of(parent) + '/' + name
        fork = hfs.fork(body, 88)
        if stat.S_ISLNK(mode):
            target = hfs.read_fork(fork, fid).decode('utf-8', 'replace')
            if os.path.lexists(out):
                os.remove(out)
            os.symlink(target, out)
        elif body[41] & 0x20 and fid in compressed:      # UF_COMPRESSED
            with open(out, 'wb') as f:
                f.write(hfs.decompress(compressed[fid], hfs.fork(body, 168), fid))
            if mode & 0o111:
                os.chmod(out, 0o755)
        else:
            with open(out, 'wb') as f:
                pos, size = 0, fork[0]
                while pos < size:
                    k = min(8 << 20, size - pos)
                    f.write(hfs.read_fork(fork, fid, 0, pos, k))
                    pos += k
            if mode & 0o111:
                os.chmod(out, 0o755)
        done += fork[0]
        pct = done * 100 // total
        if progress is not None and pct != last:
            last = pct
            progress.onProgress(label, pct)
    return len(files)


def open_volume(src):
    """A byte source holding an HFS+ volume: a bare image, or the HFS partition of a .dmg."""
    head = src.pread(1026, 0)
    if head[1024:1026] in (b'H+', b'HX'):
        return HFS(src)
    return HFS(UDIF(src))
