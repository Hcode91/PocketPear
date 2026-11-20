#!/usr/bin/env python3
"""iOS userland emulator prototype.

Runs an iOS app's own ARM code together with Apple's real dyld and system frameworks
(from a firmware root filesystem the user supplies) under a CPU emulator. Only the
kernel is imitated: BSD system calls, Mach traps and the commpage.

Usage: emu.py <rootfs> <app bundle dir> [max instructions]
"""
import os
import struct
import sys
import time

from unicorn import (Uc, UcError, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_INTR, UC_HOOK_MEM_UNMAPPED,
                     UC_PROT_ALL, UC_PROT_READ, UC_PROT_WRITE, UC_PROT_EXEC, UC_HOOK_CODE)
from unicorn.arm_const import *

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


class Emulator:
    def __init__(self, rootfs, app_dir):
        self.rootfs = rootfs
        self.app_dir = os.path.abspath(app_dir)
        self.app_guest = '/var/mobile/Applications/00000000-0000-0000-0000-000000000001/' + os.path.basename(self.app_dir)
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        self.mapped = []                      # (start, end) host bookkeeping for mmap
        self.next_mmap = 0x40000000           # anonymous mmap region search start
        self.trace = os.environ.get('TRACE')
        from kernel import Kernel
        self.kernel = Kernel(self)

    # -------------------------------------------------------------- memory
    def is_free(self, start, size):
        end = start + size
        for s, e in self.mapped:
            if start < e and s < end:
                return False
        return True

    def map(self, start, size, prot=UC_PROT_ALL):
        start, size = align_down(start), align_up(size + (start & (PAGE - 1)))
        # map page ranges that are not mapped yet
        a = start
        while a < start + size:
            if self.is_free(a, PAGE):
                b = a
                while b < start + size and self.is_free(b, PAGE):
                    b += PAGE
                self.uc.mem_map(a, b - a, prot)
                self.mapped.append((a, b))
                a = b
            else:
                a += PAGE

    def unmap(self, start, size):
        start, end = align_down(start), align_up(start + size)
        new = []
        for s, e in self.mapped:
            if e <= start or s >= end:
                new.append((s, e)); continue
            lo, hi = max(s, start), min(e, end)
            self.uc.mem_unmap(lo, hi - lo)
            if s < lo: new.append((s, lo))
            if hi < e: new.append((hi, e))
        self.mapped = new

    def find_free(self, size, hint=None):
        size = align_up(size)
        a = align_up(hint) if hint else self.next_mmap
        while a + size < 0xf0000000:
            if self.is_free(a, size):
                return a
            a += PAGE * 16
        raise MemoryError('out of guest address space')

    def read(self, a, n): return bytes(self.uc.mem_read(a, n))
    def write(self, a, b): self.uc.mem_write(a, bytes(b))
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
    def host_path(self, guest):
        if guest.startswith(self.app_guest):
            return self.app_dir + guest[len(self.app_guest):]
        if guest.startswith('/var/mobile/Applications/00000000-0000-0000-0000-000000000001'):
            sandbox = os.path.join(os.path.dirname(self.app_dir), 'sandbox')
            return sandbox + guest[len('/var/mobile/Applications/00000000-0000-0000-0000-000000000001'):]
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
        self.syms.add_macho(exe.data, image_name='app')
        self.syms.add_macho(dyld.data, image_name='dyld')
        self.syms.finish()
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
        for b in filter(None, os.environ.get('BREAK', '').split(',')):
            a = int(b, 16)
            self.uc.hook_add(UC_HOOK_CODE, self.on_break, begin=a, end=a)
        print(f'[emu] exe {exe_host} at {exe_mh:#x}, dyld entry {dyld.entry:#x}, sp {sp:#x}')

    def on_unmapped(self, uc, access, address, size, value, data):
        pc = uc.reg_read(UC_ARM_REG_PC)
        print(f'[emu] unmapped access type {access} at {address:#x} (size {size}) pc={self.syms.name(pc)} lr={self.syms.name(uc.reg_read(UC_ARM_REG_LR))}')
        self.backtrace()
        return False

    def on_break(self, uc, address, size, data):
        r = [uc.reg_read(UC_ARM_REG_R0 + i) for i in range(13)]
        print(f'[break] {self.syms.name(address)} ' + ' '.join(f'r{i}={v:#x}' for i, v in enumerate(r[:8])) + f' lr={self.syms.name(uc.reg_read(UC_ARM_REG_LR))}')

    def describe_pc(self):
        from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB
        pc = self.uc.reg_read(UC_ARM_REG_PC)
        thumb = bool(self.uc.reg_read(UC_ARM_REG_CPSR) & 0x20)
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
        print('[emu] backtrace:')
        print('   ', self.syms.name(self.uc.reg_read(UC_ARM_REG_PC)))
        print('   ', self.syms.name(self.uc.reg_read(UC_ARM_REG_LR)))
        for _ in range(limit):
            try:
                nfp, lr = self.r32(fp), self.r32(fp + 4)
            except Exception:
                break
            if not lr:
                break
            print('   ', self.syms.name(lr))
            if nfp <= fp:
                break
            fp = nfp

    def run(self, max_insns=0):
        pc = self.entry
        thumb = pc & 1
        t0 = time.time()
        while True:
            try:
                self.uc.emu_start(pc | thumb, 0xffffffff, count=max_insns)
                break
            except UcError as e:
                if self.kernel.exited is not None:
                    break
                regs = {n: self.uc.reg_read(r) for n, r in [('pc', UC_ARM_REG_PC), ('lr', UC_ARM_REG_LR), ('sp', UC_ARM_REG_SP),
                        ('r0', UC_ARM_REG_R0), ('r1', UC_ARM_REG_R1), ('r2', UC_ARM_REG_R2), ('r3', UC_ARM_REG_R3), ('r12', UC_ARM_REG_R12)]}
                print('[emu] stopped:', e, ' '.join(f'{k}={v:#x}' for k, v in regs.items()))
                self.describe_pc()
                self.backtrace()
                break
        print(f'[emu] ran {time.time() - t0:.1f}s, exit={self.kernel.exited}')


if __name__ == '__main__':
    rootfs, app = sys.argv[1], sys.argv[2]
    emu = Emulator(rootfs, app)
    exe_name = os.path.basename(app).rsplit('.', 1)[0]
    emu.setup([emu.app_guest + '/' + exe_name], ['HOME=/var/mobile/Applications/00000000-0000-0000-0000-000000000001', 'DYLD_PRINT_LIBRARIES=1' if os.environ.get('DYLD_LIBS') else 'X=1'])
    emu.run(int(sys.argv[3]) if len(sys.argv) > 3 else 0)
