"""Calling guest functions from Python.

GuestCalls runs a sequence of calls on the main thread while its run loop waits (for work that
happens "between events", as a vsync or a notification would). InlineCalls runs them on whatever
thread is inside a Python-implemented guest function, and then returns from that function: a
replaced method can do its work with the app's own frameworks.

Both take a generator that yields (address, [args]) and receives (r0, r1) back. Arguments past
the fourth go on the stack; floats are passed as their bit patterns (iOS armv7 is softfp).
"""
import struct

from unicorn.arm_const import *

INDEXED_ARM = 0x507f


def fbits(x):
    return struct.unpack('<I', struct.pack('<f', x))[0]


def set_call(emu, call, sp, lr):
    uc = emu.uc
    addr, args = call
    args = [a & 0xffffffff for a in args]
    uc.reg_write(UC_ARM_REG_SP, sp)
    for i, a in enumerate(args[:4]):
        uc.reg_write(UC_ARM_REG_R0 + i, a)
    if len(args) > 4:
        emu.write(sp, struct.pack(f'<{len(args) - 4}I', *args[4:]))
    uc.reg_write(UC_ARM_REG_LR, lr)
    uc.reg_write(UC_ARM_REG_R12, addr | (1 if addr in emu.syms.thumb else 0))


class GuestCalls:
    """Runs a generator of guest function calls on the main thread while its run loop waits.

    The generator yields (address, [args]) and receives (r0, r1) back. Arguments past the
    fourth go on the stack; floats are passed as their bit patterns (iOS armv7 is softfp).
    """

    def __init__(self, emu, stubs):
        self.emu = emu
        self.uc = emu.uc
        self.saved = None
        self.gen = None
        self.sp = 0
        page = emu.find_free(0x1000)
        emu.map(page, 0x1000)
        n = stubs.replace(page, self.returned, False)
        # svc; bx r12; index: a call returns here, and the next one starts from here with
        # r12 holding its address and lr still pointing back at the stub.
        emu.write(page, struct.pack('<III', 0xEF000000 | INDEXED_ARM, 0xE12FFF1C, n))
        self.stub = page

    def busy(self):
        return self.saved is not None

    def start(self, gen, why):
        main = self.emu.sched.threads[0]
        try:
            call = next(gen)
        except StopIteration:
            return
        self.gen = gen
        self.saved = (main.ctx, main.ready, main.complete, main.deadline, main.why)
        self.uc.context_restore(main.ctx)
        self.sp = (self.uc.reg_read(UC_ARM_REG_SP) - 512) & ~7
        self.setup(call)
        addr = call[0]
        thumb = 1 if addr in self.emu.syms.thumb else 0
        cpsr = self.uc.reg_read(UC_ARM_REG_CPSR) & ~(1 << 5)
        self.uc.reg_write(UC_ARM_REG_CPSR, cpsr | (thumb << 5))
        self.uc.reg_write(UC_ARM_REG_PC, addr | thumb)
        main.ctx = self.uc.context_save()
        main.ready = main.complete = main.deadline = None
        main.why = why

    def setup(self, call):
        addr, args = call
        args = [a & 0xffffffff for a in args]
        self.uc.reg_write(UC_ARM_REG_SP, self.sp)
        for i, a in enumerate(args[:4]):
            self.uc.reg_write(UC_ARM_REG_R0 + i, a)
        if len(args) > 4:
            self.emu.write(self.sp, struct.pack(f'<{len(args) - 4}I', *args[4:]))
        self.uc.reg_write(UC_ARM_REG_LR, self.stub)
        self.uc.reg_write(UC_ARM_REG_R12, addr | (1 if addr in self.emu.syms.thumb else 0))

    def returned(self):
        r0, r1 = self.uc.reg_read(UC_ARM_REG_R0), self.uc.reg_read(UC_ARM_REG_R1)
        try:
            call = self.gen.send((r0, r1))
        except StopIteration:
            self.finish()
            return
        except Exception as e:
            print(f'[ui] composition failed: {type(e).__name__}: {e}')
            self.finish()
            return
        self.setup(call)          # bx r12 starts it

    def finish(self):
        main = self.emu.sched.threads[0]
        ctx, ready, complete, deadline, why = self.saved
        self.saved = None
        self.gen = None
        self.uc.context_restore(ctx)
        main.ready, main.complete, main.deadline, main.why = ready, complete, deadline, why
        self.uc.emu_stop()




class InlineCalls:
    """Guest calls made from inside a Python-implemented guest function, on its own thread."""

    def __init__(self, emu, stubs):
        self.emu = emu
        self.uc = emu.uc
        self.sessions = {}
        page = emu.find_free(0x1000)
        emu.map(page, 0x1000)
        n = stubs.replace(page + 4, self.returned, False)
        # T: blx r12 / svc / b T / index. A call is made by blx r12 and returns to the svc; the
        # branch makes the next call, and the last "call" is the jump back to the caller.
        emu.write(page, struct.pack('<IIII', 0xE12FFF3C, 0xEF000000 | INDEXED_ARM, 0xEAFFFFFC, n))
        self.tramp = page

    def run(self, gen):
        """Called from a stub handler: start gen; its return value becomes the function's
        result (an int for r0, or a pair for r0:r1)."""
        t = self.emu.sched.cur
        try:
            call = next(gen)
        except StopIteration as e:
            self.set_result(e.value)
            return
        lr = self.uc.reg_read(UC_ARM_REG_LR)
        sp0 = self.uc.reg_read(UC_ARM_REG_SP)
        sp = (sp0 - 512) & ~7
        # a stack per thread: a call made here can reach another replaced function that makes
        # calls of its own (a view controller's dealloc releasing the player it owns); the inner
        # one finishes before the outer resumes
        self.sessions.setdefault(t.tid, []).append((gen, lr, sp0, sp))
        set_call(self.emu, call, sp, self.tramp + 4)
        # the stub's own "bx lr" now goes to the trampoline, which calls r12
        self.uc.reg_write(UC_ARM_REG_LR, self.tramp)

    def set_result(self, v):
        if v is None:
            v = 0
        if isinstance(v, tuple):
            self.uc.reg_write(UC_ARM_REG_R0, v[0] & 0xffffffff)
            self.uc.reg_write(UC_ARM_REG_R1, v[1] & 0xffffffff)
        else:
            self.uc.reg_write(UC_ARM_REG_R0, v & 0xffffffff)

    def returned(self):
        t = self.emu.sched.cur
        gen, lr, sp0, sp = self.sessions[t.tid][-1]
        r = (self.uc.reg_read(UC_ARM_REG_R0), self.uc.reg_read(UC_ARM_REG_R1))
        try:
            call = gen.send(r)
        except StopIteration as e:
            self.sessions[t.tid].pop()
            if not self.sessions[t.tid]:
                del self.sessions[t.tid]
            self.set_result(e.value)
            self.uc.reg_write(UC_ARM_REG_SP, sp0)
            self.uc.reg_write(UC_ARM_REG_R12, lr)       # "b T; blx r12" returns to the caller
            return
        set_call(self.emu, call, sp, self.tramp + 4)
