#!/usr/bin/env python3
"""iOS userland emulator prototype.

Runs an iOS app's own ARM code together with Apple's real dyld and system frameworks
(from a firmware root filesystem the user supplies) under a CPU emulator. Only the
kernel is imitated: BSD system calls, Mach traps and the commpage.

Usage: emu.py <rootfs> <app bundle dir> [max instructions]
"""
import bisect
import os
import struct
import sys
import time

from unicorn import (Uc, UcError, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_INTR, UC_HOOK_MEM_UNMAPPED,
                     UC_PROT_ALL, UC_PROT_READ, UC_PROT_WRITE, UC_PROT_EXEC, UC_HOOK_CODE)
from unicorn.arm_const import *
from sched import CLOCK

PAGE = 0x1000
STACK_TOP = 0x2f000000
STACK_SIZE = 0x100000 * 8
COMMPAGE = 0xffff1000      # iOS 6 ARM commpage base

def align_down(x): return x & ~(PAGE - 1)
def align_up(x): return (x + PAGE - 1) & ~(PAGE - 1)


class MachO:
    def __init__(self, data):
        self.data = data
        magic, self.cputype, self.cpusub, self.filetype, ncmds, _, self.flags = struct.unpack_from('<7I', data, 0)
        assert magic == 0xfeedface, hex(magic)
        self.segments, self.entry, self.dylinker = [], None, None
        o = 28
        for _ in range(ncmds):
            cmd, size = struct.unpack_from('<II', data, o)
            if cmd == 1:
                name = data[o + 8:o + 24].split(b'\0')[0].decode()
                va, vs, fo, fs, maxp, initp = struct.unpack_from('<6I', data, o + 24)
                self.segments.append((name, va, vs, fo, fs, initp))
            elif cmd == 5:
                self.entry = struct.unpack_from('<I', data, o + 16 + 15 * 4)[0]
            elif cmd == 0xe:
                off = struct.unpack_from('<I', data, o + 8)[0]
                self.dylinker = data[o + off:o + size].split(b'\0')[0].decode()
            o += size


WATCH_ADDR = int(os.environ['WATCH'], 16) if os.environ.get('WATCH') else None
CONTAINER = '/var/mobile/Applications/00000000-0000-0000-0000-000000000001'


def _random_file():
    # A fixed stream of random bytes, so runs repeat; PP_REALTIME uses the host's.
    if os.environ.get('PP_REALTIME'):
        return '/dev/urandom'
    import tempfile
    p = os.path.join(tempfile.gettempdir(), 'pocketpear-random.bin')
    if not os.path.exists(p):
        import random
        open(p, 'wb').write(random.Random(1).randbytes(1 << 20))
    return p


DEVICES = {'/dev/urandom': _random_file, '/dev/random': _random_file, '/dev/null': lambda: '/dev/null',
           '/dev/zero': lambda: '/dev/zero'}


class Emulator:
    def __init__(self, rootfs, app_dir):
        self.rootfs = os.path.realpath(rootfs)
        self.app_dir = os.path.realpath(app_dir)    # a symlinked bundle would show the host path to lstat
        self.app_guest = CONTAINER + '/' + os.path.basename(self.app_dir)
        # The app's data container: Documents, Library and tmp live on the host, per app.
        name = os.path.basename(self.app_dir).rsplit('.', 1)[0]
        import plistlib
        try:
            info = plistlib.load(open(os.path.join(self.app_dir, 'Info.plist'), 'rb'))
        except Exception:
            info = {}
        self.bundle_id = info.get('CFBundleIdentifier')
        self.info = info
        # An iPhone 5 runs apps made before it (no 568-point launch image) letterboxed, on a
        # 320x480-point screen; their code may assume that size (a game that presents only once
        # its drawing has covered the screen, say).
        launch = [str(i.get('UILaunchImageSize', '')) for i in info.get('UILaunchImages') or []]
        self.tall = os.path.exists(os.path.join(self.app_dir, 'Default-568h@2x.png')) or any('568' in l for l in launch)
        self.screen_points = (320, 568 if self.tall else 480)
        self.container = os.path.join(os.environ.get('PP_DATA', os.path.expanduser('~/.pocketpear')), name)
        for d in ('Documents', 'Library/Preferences', 'Library/Caches', 'tmp'):
            os.makedirs(os.path.join(self.container, d), exist_ok=True)
        from cpu import make_cpu
        self.uc = make_cpu()
        self.mapped = []                      # (start, end) host bookkeeping for mmap
        self.next_mmap = 0x40000000           # anonymous mmap region search start
        self.trace_sys = bool(os.environ.get('TRACE_SYS'))
        self.trace = os.environ.get('TRACE')
        from kernel import Kernel
        self.kernel = Kernel(self)

    # -------------------------------------------------------------- memory
    # self.mapped is kept sorted by start and never overlaps, so a search walks the gaps
    def is_free(self, start, size):
        end = start + size
        i = bisect.bisect_right(self.mapped, (start, 1 << 64)) - 1
        if i >= 0 and self.mapped[i][1] > start:
            return False
        return i + 1 >= len(self.mapped) or self.mapped[i + 1][0] >= end

    def map(self, start, size, prot=UC_PROT_ALL):
        start, size = align_down(start), align_up(size + (start & (PAGE - 1)))
        end = start + size
        # map the parts of [start, end) that are not mapped yet
        holes, a = [], start
        i = max(0, bisect.bisect_right(self.mapped, (start, 1 << 64)) - 1)
        while a < end and i < len(self.mapped):
            s, e = self.mapped[i]
            if e <= a:
                i += 1; continue
            if s >= end:
                break
            if s > a:
                holes.append((a, s))
            a = max(a, e)
            i += 1
        if a < end:
            holes.append((a, end))
        for a, b in holes:
            self.uc.mem_map(a, b - a, prot)
            bisect.insort(self.mapped, (a, b))

    def unmap(self, start, size):
        start, end = align_down(start), align_up(start + size)
        if os.environ.get('WATCH') and start <= int(os.environ['WATCH'], 16) < end:
            print(f'[watch] unmap {start:#x}-{end:#x} lr={self.syms.name(self.uc.reg_read(UC_ARM_REG_LR))}')
            self.backtrace(8)
        new = []
        for s, e in self.mapped:
            if e <= start or s >= end:
                new.append((s, e)); continue
            lo, hi = max(s, start), min(e, end)
            self.uc.mem_unmap(lo, hi - lo)
            if s < lo: new.append((s, lo))
            if hi < e: new.append((hi, e))
        self.mapped = sorted(new)

    def find_free(self, size, hint=None):
        size = align_up(size)
        base = align_up(hint) if hint else self.next_mmap
        step = PAGE * 16                              # candidates stay on 64 KB steps from base
        a = base
        i = max(0, bisect.bisect_right(self.mapped, (a, 1 << 64)) - 1)
        while a + size < 0xf0000000:
            while i < len(self.mapped) and self.mapped[i][1] <= a:
                i += 1
            if i >= len(self.mapped) or self.mapped[i][0] >= a + size:
                return a
            e = self.mapped[i][1]
            a = base + -(-(e - base) // step) * step      # first step at or past this region
        raise MemoryError('out of guest address space')

    def read(self, a, n): return bytes(self.uc.mem_read(a, n))
    def write(self, a, b):
        w = WATCH_ADDR
        if w is not None and a <= w + 7 and w < a + len(b):
            print(f'[watch] host write {a:#x}+{len(b):#x} lr={self.syms.name(self.uc.reg_read(UC_ARM_REG_LR))} pc={self.syms.name(self.uc.reg_read(UC_ARM_REG_PC))}')
            self.backtrace(10)
        self.uc.mem_write(a, bytes(b))
    def r32(self, a): return struct.unpack('<I', self.read(a, 4))[0]
    def w32(self, a, v): self.write(a, struct.pack('<I', v & 0xffffffff))
    def cstr(self, a, limit=4096):
        out = bytearray()
        while len(out) < limit:
            chunk = self.read(a + len(out), 64)
            i = chunk.find(b'\0')
            if i >= 0:
                out += chunk[:i]; break
            out += chunk
        return out.decode('utf-8', 'replace')

    # -------------------------------------------------------------- paths
    def host_path_raw(self, guest):
        """The host path of a guest path without following its symlinks (for lstat and readlink)."""
        if guest.startswith('/private/var/mobile/Applications/'):
            guest = guest[len('/private'):]
        if guest.startswith(self.app_guest) or guest.startswith(CONTAINER) or guest in DEVICES:
            return self.host_path(guest)
        return self.rootfs + guest

    def host_path(self, guest):
        if guest.startswith('/private/var/mobile/Applications/'):
            guest = guest[len('/private'):]      # /var is a link to /private/var on the device
        if guest.startswith(self.app_guest):
            return self.app_dir + guest[len(self.app_guest):]
        if guest.startswith(CONTAINER):
            return self.container + guest[len(CONTAINER):]
        if guest in DEVICES:
            return DEVICES[guest]()
        p = guest
        for _ in range(16):   # resolve absolute symlinks inside the root ourselves
            host = self.rootfs + p
            parts = p.strip('/').split('/')
            changed = False
            for i in range(len(parts)):
                hp = self.rootfs + '/' + '/'.join(parts[:i + 1])
                if os.path.islink(hp):
                    target = os.readlink(hp)
                    if target.startswith('/'):
                        p = target + ('/' + '/'.join(parts[i + 1:]) if i + 1 < len(parts) else '')
                    else:
                        base = '/' + '/'.join(parts[:i])
                        p = os.path.normpath(os.path.join(base, target) + ('/' + '/'.join(parts[i + 1:]) if i + 1 < len(parts) else ''))
                    changed = True
                    break
            if not changed:
                return host
        return self.rootfs + p

    # -------------------------------------------------------------- loading
    def load_segments(self, macho, slide=0):
        for name, va, vs, fo, fs, initp in macho.segments:
            if vs == 0 or name == '__PAGEZERO':
                continue
            self.map(va + slide, vs)
            if fs:
                self.write(va + slide, macho.data[fo:fo + fs])

    def setup(self, argv, env):
        exe_host = os.path.join(self.app_dir, argv[0].split('/')[-1])
        exe = MachO(open(exe_host, 'rb').read())
        self.load_segments(exe)
        dyld = MachO(open(self.host_path(exe.dylinker), 'rb').read())
        self.load_segments(dyld)
        exe_mh = next(va for n, va, vs, fo, fs, ip in exe.segments if n == '__TEXT')

        # The shared cache is mapped up front, as the kernel's shared region is; dyld asks for
        # it with shared_region_check_np and uses it in place.
        cache_path = self.host_path('/System/Library/Caches/com.apple.dyld/dyld_shared_cache_armv7s')
        with open(cache_path, 'rb') as f:
            head = f.read(0x1000)
            mo, mc = struct.unpack_from('<II', head, 16)
            for i in range(mc):
                addr, size, fileoff, maxp, initp = struct.unpack_from('<QQQII', head, mo + 32 * i)
                self.map(addr, size)
                f.seek(fileoff)
                self.write(addr, f.read(size))
                if i == 0:
                    self.shared_cache_base = addr
        print(f'[emu] shared cache mapped at {self.shared_cache_base:#x}')
        from sym import Symbols
        self.syms = Symbols()
        with open(cache_path, 'rb') as f:
            self.syms.add_cache(f.read())
        system_defined = set(self.syms.defined)
        self.syms.add_macho(exe.data, image_name='app')
        self.syms.add_macho(dyld.data, image_name='dyld')
        self.syms.finish()
        by_name = {}
        for a, n in zip(self.syms.addrs, self.syms.names):
            if '(libsystem_c.dylib)' in n:
                by_name.setdefault(n.split(' (')[0], a)
        from hle import Pthreads, Stubs
        self.hle = Pthreads(self)
        self.stubs = Stubs(self)
        if not os.environ.get('PP_NO_HLE'):
            self.hle.install(by_name)
        gl_syms = {}
        for a, n in zip(self.syms.addrs, self.syms.names):
            if n.endswith(' (OpenGLES)'):
                gl_syms.setdefault(n[:-len(' (OpenGLES)')], a)
        from gles import GLES
        self.gles = GLES(self)
        self.gles.install(self.stubs, gl_syms)
        self.preset_screen()
        from displaylink import DisplayLinks
        self.objc_msgsend = next(a for a, n in zip(self.syms.addrs, self.syms.names) if n.startswith('_objc_msgSend (libobjc'))
        self.displaylinks = DisplayLinks(self)
        if not os.environ.get('PP_NO_DISPLAYLINK'):
            self.displaylinks.install(self.stubs)
        from imports import MissingImports
        self.missing_imports = MissingImports(self)
        self.missing_imports.install(exe, system_defined)
        from audio import AudioQueues
        self.audio = AudioQueues(self)
        self.audio.install(self.stubs)
        print(f'[emu] {len(self.syms.addrs)} symbols')

        # commpage
        self.map(COMMPAGE, PAGE, UC_PROT_READ)
        cp = bytearray(PAGE)
        cp[0:16] = b'commpage 32-bit\0'
        struct.pack_into('<H', cp, 0x1e, 3)          # version
        # cpu capabilities: 64-byte cache lines, fast TLS, NEON; the CPU count lives in bits 16-23
        struct.pack_into('<I', cp, 0x20, 0x8 | 0x20 | 0x100 | (2 << 16))
        struct.pack_into('<H', cp, 0x26, 64)         # cache line size
        cp[0x34] = cp[0x35] = cp[0x36] = 2           # active, physical and logical CPUs
        struct.pack_into('<Q', cp, 0x38, 1 << 30)    # memory size (1 GB, as an iPhone 5)
        self.write(COMMPAGE, cp)

        # stack: mach_header, argc, argv, 0, envp, 0, apple, 0, strings
        self.map(STACK_TOP - STACK_SIZE, STACK_SIZE, UC_PROT_READ | UC_PROT_WRITE)
        strings_area = STACK_TOP - 0x4000
        cursor = strings_area
        def put(s):
            nonlocal cursor
            b = s.encode() + b'\0'
            self.write(cursor, b)
            a = cursor
            cursor += len(b)
            return a
        argv_p = [put(a) for a in argv]
        env_p = [put(e) for e in env]
        apple_p = [put(argv[0])]           # iOS 6: apple[0] is the executable's path
        words = [exe_mh, len(argv)] + argv_p + [0] + env_p + [0] + apple_p + [0]
        sp = (strings_area - 4 * len(words) - 64) & ~0xf
        for i, w in enumerate(words):
            self.w32(sp + 4 * i, w)

        self.uc.reg_write(UC_ARM_REG_SP, sp)
        # enable VFP/NEON
        self.uc.reg_write(UC_ARM_REG_C1_C0_2, self.uc.reg_read(UC_ARM_REG_C1_C0_2) | (0xf << 20))
        self.uc.reg_write(UC_ARM_REG_FPEXC, 0x40000000)
        self.entry = dyld.entry
        self.uc.hook_add(UC_HOOK_INTR, self.kernel.on_interrupt)
        self.uc.hook_add(UC_HOOK_MEM_UNMAPPED, self.on_unmapped)
        from unicorn import UC_HOOK_MEM_READ
        self.commpage_reads = set()
        def on_cp(uc, access, address, size, value, data):
            off = address - COMMPAGE
            if off not in self.commpage_reads:
                self.commpage_reads.add(off)
                print(f'[commpage] read +{off:#x} ({size}) from {self.syms.name(uc.reg_read(UC_ARM_REG_PC))}')
        self.uc.hook_add(UC_HOOK_MEM_READ, on_cp, begin=COMMPAGE, end=COMMPAGE + PAGE - 1)
        if os.environ.get('WATCH'):
            from unicorn import UC_HOOK_MEM_WRITE
            wa = int(os.environ['WATCH'], 16)
            def on_w(uc, access, address, size, value, data):
                t = getattr(getattr(self, 'sched', None), 'cur', None)
                print(f'[watch] t{t.tid if t else 0} write {address:#x} size {size} value {value:#x} pc={self.syms.name(uc.reg_read(UC_ARM_REG_PC))} lr={self.syms.name(uc.reg_read(UC_ARM_REG_LR))}')
            self.uc.hook_add(UC_HOOK_MEM_WRITE, lambda uc, ac, ad, sz, v, d: on_w(uc, ac, ad, sz, v, d) if ad + sz > wa and ad < wa + 8 else None, begin=wa - 64, end=wa + 7)
        if os.environ.get('TRACK_CF'):
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools'))
            import trackcf
            trackcf.install(self)
        for b in filter(None, os.environ.get('BREAK', '').split(',')):
            a = int(b, 16)
            self.uc.hook_add(UC_HOOK_CODE, self.on_break, begin=a, end=a)
        print(f'[emu] exe {exe_host} at {exe_mh:#x}, dyld entry {dyld.entry:#x}, sp {sp:#x}')

    def next_external(self):
        """The next thing due from outside the guest: a display link tick or a scripted touch."""
        ds = [d for d in (self.displaylinks.next_due(), self.kernel.touches.next_due()) if d is not None]
        return min(ds) if ds else None

    def preset_screen(self):
        """GraphicsServices caches the main screen's pixel size, scale and orientation, filling
        them from MobileGestalt capabilities on first use. Fill the cache with an iPhone 5's
        (640x1136 pixels at 2x, portrait, or 640x960 for an app run letterboxed) so UIScreen, and
        every game that sizes its view from it, sees the right screen. The addresses are the iOS 6.1.4 (10B350) GraphicsServices' own."""
        pixels, scale = 0x3A31D450, 0x3A31EE6C
        try:
            if self.syms.name(0x34c97c88).startswith('_GSMainScreenPixelSize'):
                pw, ph = self.screen_points[0] * 2, self.screen_points[1] * 2
                self.write(pixels, struct.pack('<ii', pw, ph))
                self.write(scale, struct.pack('<fi', 2.0, 0))
                print(f'[emu] main screen {pw}x{ph} at 2x' + ('' if self.tall else ' (letterboxed, as an older app)'))
        except Exception as e:
            print('[emu] could not preset the screen size:', e)
        # UIScreen takes its metrics from CADisplay, which has no display list without a
        # render server. Keep its _bounds (ivar at +8) and _scale (+24) at the iPhone 5's
        # whenever they are read; -[UIScreen bounds] and friends only read those ivars.
        names = dict(zip(self.syms.names, self.syms.addrs))
        rect_methods = {names.get(m + ' (UIKit)') for m in (
            '-[UIScreen bounds]', '-[UIScreen _boundsInPixels]', '-[UIScreen _realDisplayBounds]',
            '-[UIScreen applicationFrame]', '-[UIScreen _applicationFrameForInterfaceOrientation:]')}

        def fix(uc, address, size, data):
            # methods returning a CGRect take the result pointer in r0 and self in r1
            obj = uc.reg_read(UC_ARM_REG_R1 if address in rect_methods else UC_ARM_REG_R0)
            if obj:
                self.write(obj + 8, struct.pack('<fffff', 0.0, 0.0, float(self.screen_points[0]),
                                                float(self.screen_points[1]), 2.0))
        # CAEAGLLayer's renderbuffer is its bounds times its contentsScale, which stays 1 unless
        # the app raises it (an app made before Retina never does).
        def on_scale(uc, address, size, data):
            self.gles.layer_scale[uc.reg_read(UC_ARM_REG_R0)] = struct.unpack('<f', struct.pack('<I', uc.reg_read(UC_ARM_REG_R2)))[0]
        a = names.get('-[CALayer setContentsScale:] (QuartzCore)')
        if a is not None:
            self.uc.hook_add(UC_HOOK_CODE, on_scale, begin=a, end=a)
        for m in ('-[UIScreen bounds]', '-[UIScreen scale]', '-[UIScreen _scale]', '-[UIScreen _boundsInPixels]',
                  '-[UIScreen _realDisplayBounds]', '-[UIScreen applicationFrame]',
                  '-[UIScreen _applicationFrameForInterfaceOrientation:]', '-[UIScreen currentMode]'):
            a = names.get(m + ' (UIKit)')
            if a is not None:
                self.uc.hook_add(UC_HOOK_CODE, fix, begin=a, end=a)

    def on_unmapped(self, uc, access, address, size, value, data):
        pc = uc.reg_read(UC_ARM_REG_PC)
        print(f'[emu] unmapped access type {access} at {address:#x} (size {size}) pc={self.syms.name(pc)} lr={self.syms.name(uc.reg_read(UC_ARM_REG_LR))}')
        self.backtrace()
        return False

    def on_break(self, uc, address, size, data):
        r = [uc.reg_read(UC_ARM_REG_R0 + i) for i in range(13)]
        print(f'[break] {self.syms.name(address)} ' + ' '.join(f'r{i}={v:#x}' for i, v in enumerate(r[:8])) + f' lr={self.syms.name(uc.reg_read(UC_ARM_REG_LR))}')
        if os.environ.get('BREAK_TRACE'):
            self.trace_sys = True
        if os.environ.get('BREAK_BT'):
            self._btn = getattr(self, '_btn', 0) + 1
            if self._btn % int(os.environ['BREAK_BT']) == 0:
                self.backtrace(16)
        for d in filter(None, os.environ.get('BREAK_DUMP', '').split(',')):
            a, _, n = d.partition(':')
            a, n = int(a, 16), int(n or 4)
            print(f'        [{a:#x}]', [hex(self.r32(a + 4 * i)) for i in range(n)])
        if os.environ.get('BREAK_CFSTR'):
            for i in range(3):
                print(f'        r{i}: {self.cfstring(r[i])!r}')
        if os.environ.get('BREAK_OBJ'):
            self._bhits = getattr(self, '_bhits', 0) + 1
            if self._bhits > int(os.environ['BREAK_OBJ']):
                return
            try:
                isa = self.r32(r[0])
                print(f'        r0 isa {isa:#x} = {self.syms.name(isa)}; words', [hex(self.r32(r[0] + 4 * i)) for i in range(4)])
            except Exception as e:
                print('        r0?', e)
        if os.environ.get('BREAK_VA'):
            try:
                va = r[2]
                print('        va:', [hex(self.r32(va + 4 * i)) for i in range(4)], self.read(self.r32(va), 64).split(b'\0')[0])
            except Exception as e:
                print('        va?', e)
        for v in r[:4]:
            try:
                b = self.read(v, 96)
                if b[:1].isascii() and b[:1] >= b' ' and b'\0' in b:
                    print('        string:', b.split(b'\0')[0])
            except Exception:
                pass

    def cfstring(self, a):
        """Best-effort text of a constant CFString (isa, info, bytes, length)."""
        try:
            if a == 0:
                return None
            isa, info, p, n = struct.unpack('<4I', self.read(a, 16))
            if n < 4096 and p:
                return self.read(p, n).decode('utf-8', 'replace')
        except Exception:
            pass
        return f'<{a:#x}>'

    def stack_strings(self, span=0x400):
        """Printable strings near the stack pointer: crash messages are often built there."""
        import re
        sp = self.uc.reg_read(UC_ARM_REG_SP)
        try:
            blob = self.read(sp, span)
        except Exception:
            return
        for m in re.finditer(rb'[\x20-\x7e]{12,}', blob):
            print(f'[emu] stack +{m.start():#x}: {m.group().decode()}')

    def describe_pc(self):
        pc = self.uc.reg_read(UC_ARM_REG_PC)
        thumb = bool(self.uc.reg_read(UC_ARM_REG_CPSR) & 0x20)
        try:
            from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB
        except ImportError:                     # the Android build has no disassembler
            try:
                print(f'[emu] at {self.syms.name(pc)} ({"Thumb" if thumb else "ARM"}): {self.read(pc, 8).hex(" ")}')
            except Exception:
                pass
            return
        md = Cs(CS_ARCH_ARM, CS_MODE_THUMB if thumb else CS_MODE_ARM)
        try:
            code = self.read(pc, 16)
        except Exception:
            return
        print(f'[emu] at {self.syms.name(pc)} ({"thumb" if thumb else "arm"}): ' + code[:4].hex())
        for i in list(md.disasm(code, pc))[:3]:
            print(f'      {i.address:#x} {i.mnemonic} {i.op_str}')

    def backtrace(self, limit=24):
        fp = self.uc.reg_read(UC_ARM_REG_R7)
        frames = [self.uc.reg_read(UC_ARM_REG_PC), self.uc.reg_read(UC_ARM_REG_LR)]
        for _ in range(1000000):
            try:
                nfp, lr = self.r32(fp), self.r32(fp + 4)
            except Exception:
                break
            if not lr:
                break
            frames.append(lr)
            if nfp <= fp:
                break
            fp = nfp
        print(f'[emu] backtrace ({len(frames)} frames):')
        if len(frames) > 1000:      # compress runs of one return address
            runs = []
            for a in frames:
                if runs and runs[-1][0] == a:
                    runs[-1][1] += 1
                else:
                    runs.append([a, 1])
            if len(runs) < 200:
                for a, n in runs:
                    print('   ', f'{a:#x}', self.syms.name(a), f'x{n}' if n > 1 else '')
                return
        tail = int(os.environ.get('BT_TAIL', 16))
        shown = frames if len(frames) <= limit + tail else frames[:limit] + [None] + frames[-tail:]
        for a in shown:
            print('   ', '...' if a is None else f'{a:#010x} {self.syms.name(a)}')

    def run(self, max_insns=0, slice_insns=int(os.environ.get("PP_SLICE", 0))):
        from sched import Scheduler, Thread
        self.sched = Scheduler(self)
        self.uc.reg_write(UC_ARM_REG_PC, self.entry & ~1)
        main = Thread(self.uc.context_save(), 'main', self.kernel.thread_port)
        self.sched.add(main)
        t0 = time.time()
        last_beat = t0
        executed = 0
        self.quit = False
        while self.kernel.exited is None and not self.quit:
            self.displaylinks.deliver()
            self.kernel.touches.deliver(CLOCK.now())
            t, state = self.sched.pick()
            if t is None and state == 'external':
                continue
            if t is None:
                print('[emu] every thread is blocked for ever:')
                for th in self.sched.threads:
                    print('     ', th)
                break
            self.sched.cur = t
            self.sched.wake_state(t, state)
            if t.ready is not None or t.dead:      # its completion blocked it again
                t.ctx = self.uc.context_save()
                continue
            pc = self.uc.reg_read(UC_ARM_REG_PC)
            thumb = (self.uc.reg_read(UC_ARM_REG_CPSR) >> 5) & 1
            try:
                started = time.time()
                self.uc.emu_start(pc | thumb, 0xffffffff, count=slice_insns,
                                  timeout=0 if slice_insns else int(float(os.environ.get('PP_WATCHDOG', 20)) * 1e6))
                if not slice_insns and t.ready is None and not t.dead and self.kernel.exited is None \
                        and time.time() - started >= float(os.environ.get('PP_WATCHDOG', 20)) * 0.95:
                    print(f'[emu] thread {t.tid} ({t.name}) ran {time.time() - started:.0f}s without a system call that waits:')
                    self.describe_pc()
                    self.backtrace()
                    for th in self.sched.threads:
                        print('     ', th)
                    break
            except UcError as e:
                regs = {n: self.uc.reg_read(r) for n, r in [('pc', UC_ARM_REG_PC), ('lr', UC_ARM_REG_LR), ('sp', UC_ARM_REG_SP),
                        ('r0', UC_ARM_REG_R0), ('r1', UC_ARM_REG_R1), ('r2', UC_ARM_REG_R2), ('r3', UC_ARM_REG_R3), ('r12', UC_ARM_REG_R12)]}
                print(f'[emu] thread {t.tid} ({t.name}) stopped:', e, ' '.join(f'{k}={v:#x}' for k, v in regs.items()))
                self.describe_pc()
                self.stack_strings()
                self.backtrace()
                break
            t.ctx = self.uc.context_save()
            t.pc = self.uc.reg_read(UC_ARM_REG_PC)
            if time.time() - last_beat > float(os.environ.get('PP_HEARTBEAT', 15)):
                last_beat = time.time()
                pass
                print(f'[emu] heartbeat: guest time {CLOCK.now():.2f}s, {self.kernel.calls} calls')
                for th in self.sched.threads:
                    if not th.dead:
                        print(f'      {th} at {self.syms.name(getattr(th, "pc", 0))}')
                        if os.environ.get('PP_BT_THREADS') and th.ctx is not None and th is not t:
                            self.uc.context_restore(th.ctx)
                            self.backtrace(int(os.environ['PP_BT_THREADS']))
                if os.environ.get('PP_BT_THREADS'):
                    self.uc.context_restore(t.ctx)
            pass
            CLOCK.tick(0.0005)
            executed += slice_insns
            if max_insns and executed >= max_insns:
                print('[emu] instruction limit reached')
                for th in self.sched.threads:
                    print('     ', th)
                break
        print(f'[emu] ran {time.time() - t0:.1f}s, exit={self.kernel.exited}, threads={len(self.sched.threads)} live')


def start(rootfs, app):
    """Load an app bundle over a root filesystem and set up its main thread; run() runs it."""
    emu = Emulator(rootfs, app)
    import plistlib
    try:
        exe_name = plistlib.load(open(os.path.join(app, 'Info.plist'), 'rb'))['CFBundleExecutable']
    except Exception:
        exe_name = os.path.basename(app.rstrip('/')).rsplit('.', 1)[0]
    emu.setup([emu.app_guest + '/' + exe_name], ['HOME=' + CONTAINER, 'CFFIXED_USER_HOME=' + CONTAINER, 'TMPDIR=' + CONTAINER + '/tmp/', 'IPHONE_SHARED_RESOURCES_DIRECTORY=/var/mobile', 'USER=mobile', 'LOGNAME=mobile', 'SHELL=/bin/sh', 'DYLD_PRINT_LIBRARIES=1' if os.environ.get('DYLD_LIBS') else 'X=1'])
    return emu


if __name__ == '__main__':
    emu = start(sys.argv[1], sys.argv[2])
    emu.run(int(sys.argv[3]) if len(sys.argv) > 3 else 0)
