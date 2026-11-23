"""The CPU behind the emulator. By default Unicorn; with PP_CPU=dynarmic, libppcpu (core/ppcpu.cpp,
dynarmic's JIT) behind DynUc, which offers the part of Unicorn's interface the emulator uses."""
import ctypes
import os
import sys
import time

from unicorn import (Uc, UcError, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_INTR, UC_HOOK_MEM_UNMAPPED, UC_HOOK_CODE,
                     UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UC_ERR_READ_UNMAPPED, UC_ERR_WRITE_UNMAPPED,
                     UC_ERR_EXCEPTION, UC_ERR_INSN_INVALID, UC_MEM_READ_UNMAPPED, UC_MEM_WRITE_UNMAPPED)
from unicorn.arm_const import *

STOPPED, SVC, HOOK, FAULT, EXCEPTION, FALLBACK = range(6)
EXCEPTION_NAMES = {0: 'undefined instruction', 1: 'unpredictable instruction', 2: 'decode error',
                   3: 'supervisor call', 4: 'breakpoint'}

# Unicorn register id -> ppcpu register index
REGS = {UC_ARM_REG_R0 + i: i for i in range(13)}
REGS.update({UC_ARM_REG_SP: 13, UC_ARM_REG_LR: 14, UC_ARM_REG_PC: 15, UC_ARM_REG_CPSR: 16,
             UC_ARM_REG_FPSCR: 17, UC_ARM_REG_C13_C0_3: 18})
IGNORED = {UC_ARM_REG_C1_C0_2, UC_ARM_REG_FPEXC}   # VFP is always on in dynarmic


def library_path():
    p = os.environ.get('LIBPPCPU_PATH')
    if p:
        return p
    here = os.path.dirname(os.path.abspath(__file__))
    for c in (os.path.join(here, 'libppcpu.so'), os.path.join(here, '..', 'core', 'build', 'libppcpu.so')):
        if os.path.exists(c):
            return c
    return 'libppcpu.so'


_lib = None


def lib():
    global _lib
    if _lib is None:
        L = ctypes.CDLL(library_path())
        P, U32, U64, I = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint64, ctypes.c_int
        pu32 = ctypes.POINTER(U32)
        for name, res, args in [
            ('ppcpu_new', P, []), ('ppcpu_free', None, [P]),
            ('ppcpu_map', I, [P, U32, U32]), ('ppcpu_unmap', I, [P, U32, U32]),
            ('ppcpu_read', I, [P, U32, P, U32]), ('ppcpu_write', I, [P, U32, ctypes.c_char_p, U32]),
            ('ppcpu_host', P, [P, U32]),
            ('ppcpu_reg', U32, [P, I]), ('ppcpu_set_reg', None, [P, I, U32]),
            ('ppcpu_context_size', I, []), ('ppcpu_save', None, [P, P]), ('ppcpu_restore', None, [P, P]),
            ('ppcpu_add_hook', None, [P, U32]), ('ppcpu_invalidate', None, [P, U32, U32]),
            ('ppcpu_run', I, [P, U64, U64, pu32, pu32]), ('ppcpu_step', I, [P, pu32, pu32]),
            ('ppcpu_stop', None, [P])]:
            f = getattr(L, name)
            f.restype, f.argtypes = res, args
        # newer libraries: r0-r15 in place, and a PVRTC decoder (older ones lack them)
        for name, res, args in [('ppcpu_regs', pu32, [P]),
                                ('ppcpu_pvrtc', None, [ctypes.c_char_p, U32, I, I, I, P]),
                                ('ppcpu_mutex_svcs', None, [P, P]),
                                ('ppcpu_set_tid', None, [P, U32]),
                                ('ppcpu_mutex_get', None, [P, U32, P, P]),
                                ('ppcpu_mutex_set', None, [P, U32, U32, U32]),
                                ('ppcpu_fast_calls', ctypes.c_uint64, [P]),
                                ('ppcpu_gl_indexed', None, [P, U32, U32]),
                                ('ppcpu_gl_fn', None, [P, U32, P, I, ctypes.c_char_p, I, I]),
                                ('ppcpu_gl_tid', None, [P, U32]),
                                ('ppcpu_tex_target', U32, [P, U32]),
                                ('ppcpu_executed', ctypes.c_uint64, [P])]:
            f = getattr(L, name, None)
            if f is not None:
                f.restype, f.argtypes = res, args
        _lib = L
    return _lib


CTX_OFFSET = {i: 4 * i for i in range(17)}
CTX_OFFSET.update({17: 68, 18: 328, 19: 332})


class DynContext:
    """A saved CPU state; like Unicorn's, its registers can be read and written."""
    __slots__ = ('buf',)

    def __init__(self, size):
        self.buf = ctypes.create_string_buffer(size)

    def _at(self, reg):
        i = REGS.get(reg)
        return None if i is None else CTX_OFFSET[i]

    def reg_read(self, reg):
        o = self._at(reg)
        return 0 if o is None else int.from_bytes(self.buf.raw[o:o + 4], 'little')

    def reg_write(self, reg, value):
        o = self._at(reg)
        if o is None:
            return
        value &= 0xffffffff
        if o == 60:
            cpsr = self.reg_read(UC_ARM_REG_CPSR)
            ctypes.memmove(ctypes.addressof(self.buf) + 64, ((cpsr & ~0x20) | ((value & 1) << 5)).to_bytes(4, 'little'), 4)
            value &= ~1
        ctypes.memmove(ctypes.addressof(self.buf) + o, value.to_bytes(4, 'little'), 4)


class DynUc:
    """Unicorn's interface (the subset PocketPear uses) over libppcpu."""

    def __init__(self):
        self.L = lib()
        self.h = self.L.ppcpu_new()
        self.ctx_size = self.L.ppcpu_context_size()
        self.intr_hooks = []
        self.unmapped_hooks = []
        self.code_hooks = {}       # address -> [(callback, user_data)]
        self.stopping = False
        self.i0, self.i1 = ctypes.c_uint32(), ctypes.c_uint32()
        self.warned = set()
        # r0-r14 read and written in place when the library allows (a ctypes call each is the
        # emulator's biggest single overhead after running guest code)
        self.regs = None
        if hasattr(self.L, 'ppcpu_regs'):
            self.regs = (ctypes.c_uint32 * 16).from_address(ctypes.addressof(self.L.ppcpu_regs(self.h).contents))
        # the core answers uncontended pthread mutex calls itself (hle.Pthreads); PP_SLOW_MUTEX=1 stops it
        self.native_mutexes = hasattr(self.L, 'ppcpu_mutex_get') and not os.environ.get('PP_SLOW_MUTEX')
        self._mo, self._mc = ctypes.c_uint32(), ctypes.c_uint32()
        self.native_gl = hasattr(self.L, 'ppcpu_gl_fn')

    def gl_indexed(self, thumb, arm):
        self.L.ppcpu_gl_indexed(self.h, thumb, arm)

    def gl_fn(self, n, fn, kind, spec, ret, comps):
        self.L.ppcpu_gl_fn(self.h, n, fn, kind, spec.encode(), ord(ret), comps)

    def executed(self):
        """Instructions run so far, or None from a library that does not count them."""
        f = getattr(self.L, 'ppcpu_executed', None)
        return f(self.h) if f is not None else None

    def tex_target(self, name):
        return self.L.ppcpu_tex_target(self.h, name)

    def gl_tid(self, tid):
        self.L.ppcpu_gl_tid(self.h, tid)

    def set_tid(self, tid):
        if self.native_mutexes or self.native_gl:
            self.L.ppcpu_set_tid(self.h, tid)

    def mutex_svcs(self, lock, trylock, unlock):
        """Each a (Thumb, ARM) pair of svc immediates."""
        self.L.ppcpu_mutex_svcs(self.h, (ctypes.c_uint32 * 6)(*lock, *trylock, *unlock))

    def mutex_get(self, addr):
        self.L.ppcpu_mutex_get(self.h, addr, ctypes.byref(self._mo), ctypes.byref(self._mc))
        return self._mo.value, self._mc.value

    def mutex_set(self, addr, owner, count):
        self.L.ppcpu_mutex_set(self.h, addr, owner, count)

    def _warn(self, what):
        if what not in self.warned:
            self.warned.add(what)
            print(f'[cpu] dynarmic: {what}')

    # memory
    def mem_map(self, address, size, perms=7):
        r = self.L.ppcpu_map(self.h, address, size)
        if r:
            raise UcError(UC_ERR_EXCEPTION if r != -2 else 11)   # 11: UC_ERR_MAP

    def mem_unmap(self, address, size):
        self.L.ppcpu_unmap(self.h, address, size)

    def mem_read(self, address, size):
        buf = ctypes.create_string_buffer(size)
        if self.L.ppcpu_read(self.h, address, buf, size):
            raise UcError(UC_ERR_READ_UNMAPPED)
        return bytearray(buf.raw)

    def mem_write(self, address, data):
        data = bytes(data)
        if self.L.ppcpu_write(self.h, address, data, len(data)):
            raise UcError(UC_ERR_WRITE_UNMAPPED)

    # registers
    def reg_read(self, reg):
        i = REGS.get(reg)
        if i is not None and i < 15 and self.regs is not None:
            return self.regs[i]
        if i is None:
            if reg not in IGNORED:
                self._warn(f'read of unsupported register {reg}')
            return 0
        return self.L.ppcpu_reg(self.h, i)

    def reg_write(self, reg, value):
        i = REGS.get(reg)
        if i is None:
            if reg not in IGNORED:
                self._warn(f'write of unsupported register {reg}')
            return
        value &= 0xffffffff
        if i < 15 and self.regs is not None:
            self.regs[i] = value
            return
        if i == 15:   # as Unicorn: bit 0 of a PC write selects Thumb
            cpsr = self.L.ppcpu_reg(self.h, 16)
            self.L.ppcpu_set_reg(self.h, 16, (cpsr & ~0x20) | ((value & 1) << 5))
            value &= ~1
        self.L.ppcpu_set_reg(self.h, i, value)

    def context_save(self):
        ctx = DynContext(self.ctx_size)
        self.L.ppcpu_save(self.h, ctx.buf)
        return ctx

    def context_restore(self, ctx):
        self.L.ppcpu_restore(self.h, ctx.buf)

    # hooks
    def hook_add(self, htype, callback, user_data=None, begin=1, end=0, *args):
        if htype == UC_HOOK_INTR:
            self.intr_hooks.append((callback, user_data))
        elif htype == UC_HOOK_MEM_UNMAPPED:
            self.unmapped_hooks.append((callback, user_data))
        elif htype == UC_HOOK_CODE and begin == end:
            self.code_hooks.setdefault(begin & ~1, []).append((callback, user_data))
            self.L.ppcpu_add_hook(self.h, begin & ~1)
        elif htype in (UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE):
            self._warn('memory access hooks are not supported (ignored)')
        else:
            self._warn(f'hook type {htype} over a range is not supported (ignored)')
        return 0

    def hook_del(self, h):
        pass

    # running
    def emu_stop(self):
        self.stopping = True

    def emu_start(self, begin, until, timeout=0, count=0):
        self.reg_write(UC_ARM_REG_PC, begin)
        self.stopping = False
        L, h, i0, i1 = self.L, self.h, self.i0, self.i1
        p0, p1 = ctypes.byref(i0), ctypes.byref(i1)
        deadline = time.monotonic() + timeout / 1e6 if timeout else None
        while not self.stopping:
            left = 0
            if deadline is not None:
                left = int((deadline - time.monotonic()) * 1e6)
                if left <= 0:
                    return
            r = L.ppcpu_run(h, count, left, p0, p1)
            if r == STOPPED:
                return
            if r == SVC:
                for cb, data in self.intr_hooks:
                    cb(self, 2, data)
            elif r == HOOK:
                pc = i0.value
                for cb, data in list(self.code_hooks.get(pc, ())):
                    cb(self, pc, 4, data)
                if self.stopping:
                    return
                if L.ppcpu_reg(h, 15) == pc:
                    r = L.ppcpu_step(h, p0, p1)
                    if r != STOPPED:
                        self._fail(r)
            else:
                self._fail(r)
            # an instruction count restarts after each stop (slices are only a debugging aid)

    def _fail(self, r):
        if r == FAULT:
            addr, write = self.i0.value, self.i1.value
            access = UC_MEM_WRITE_UNMAPPED if write else UC_MEM_READ_UNMAPPED
            for cb, data in self.unmapped_hooks:
                if cb(self, access, addr, 4, 0, data):
                    return
            raise UcError(UC_ERR_WRITE_UNMAPPED if write else UC_ERR_READ_UNMAPPED)
        if r == EXCEPTION:
            print(f'[cpu] {EXCEPTION_NAMES.get(self.i1.value, self.i1.value)} at {self.i0.value:#x}')
            raise UcError(UC_ERR_INSN_INVALID)
        if r == FALLBACK:
            print(f'[cpu] dynarmic cannot run the instruction at {self.i0.value:#x}')
            raise UcError(UC_ERR_INSN_INVALID)
        raise UcError(UC_ERR_EXCEPTION)


def make_cpu():
    if os.environ.get('PP_CPU', '').lower() == 'dynarmic':
        try:
            cpu = DynUc()
            print(f'[cpu] dynarmic ({library_path()})')
            return cpu
        except OSError as e:
            print(f'[cpu] dynarmic could not load ({e}); using Unicorn')
    return Uc(UC_ARCH_ARM, UC_MODE_ARM)
