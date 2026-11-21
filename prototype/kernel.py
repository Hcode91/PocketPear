"""The imitated iOS 6 kernel: BSD system calls and Mach traps, entered by `svc #0x80`.

r12 holds the call number: positive for BSD calls, negative for Mach traps. A BSD call
reports failure by setting the carry flag with the errno in r0.
"""
import errno
import os
import stat as pystat
import struct
import time

from unicorn import UC_PROT_ALL, UC_PROT_READ, UC_PROT_WRITE
from unicorn.arm_const import *

CARRY = 1 << 29
ENOSYS = 78           # Darwin's ENOSYS
EBADF, ENOENT, EINVAL, ENOTSUP = 9, 2, 22, 45
EPERM = 1

# Darwin open() flags
O_ACCMODE, O_CREAT, O_TRUNC, O_EXCL, O_APPEND, O_NONBLOCK = 3, 0x200, 0x400, 0x800, 0x8, 0x4

# sysctlbyname: names with a fixed MIB, and others answered from NAMED_SYSCTL
SYSCTL_MIBS = {'kern.ostype': [1, 1], 'kern.osrelease': [1, 2], 'kern.version': [1, 4], 'kern.hostname': [1, 10],
               'kern.argmax': [1, 8], 'kern.osversion': [1, 65], 'kern.usrstack': [1, 35],
               'hw.machine': [6, 1], 'hw.model': [6, 2], 'hw.ncpu': [6, 3], 'hw.pagesize': [6, 7],
               'hw.physmem': [6, 5], 'hw.memsize': [6, 24], 'hw.availcpu': [6, 25], 'hw.cpufrequency': [6, 15],
               'hw.busfrequency': [6, 14], 'hw.cachelinesize': [6, 16]}
NAMED_MIB = 0x7770
NAMED_SYSCTL = {n: struct.pack('<i', v) if isinstance(v, int) else v for n, v in {
    'hw.physicalcpu': 2, 'hw.physicalcpu_max': 2, 'hw.logicalcpu': 2, 'hw.logicalcpu_max': 2, 'hw.activecpu': 2,
    'hw.cputype': 12, 'hw.cpusubtype': 11, 'hw.cpufamily': 0x1e2d6381, 'hw.cpu64bit_capable': 0,
    'hw.optional.neon': 1, 'hw.optional.neon_hpfp': 1, 'hw.optional.vfp': 1, 'hw.optional.armv7s': 1,
    'hw.l1icachesize': 32768, 'hw.l1dcachesize': 32768, 'hw.l2cachesize': 1048576,
    'hw.cpufrequency_max': 1300000000, 'hw.tbfrequency': 24000000, 'kern.maxfilesperproc': 10240,
    'kern.hv_vmm_present': 0, 'kern.secure_kernel': 1, 'security.mac.sandbox.sentinel': b'.sb-0\0',
    'kern.bootsessionuuid': b'00000000-0000-0000-0000-000000000000\0', 'hw.target': b'N42AP\0',
}.items()}

# Linux errno values that differ from Darwin's
DARWIN_ERRNO = {errno.ELOOP: 62, errno.ENAMETOOLONG: 63, errno.ENOTEMPTY: 66, errno.ENOSYS: 78,
                errno.EAGAIN: 35, errno.EINPROGRESS: 36, errno.EALREADY: 37, errno.ENOTSUP: 45,
                errno.ETIMEDOUT: 60, errno.ECONNREFUSED: 61}

TRACE_FROM = int(os.environ.get('PP_TRACE_FROM', 0)) or -1     # start TRACE_SYS at this call number

import json as _json
# Numbers and names come from libsystem_kernel's own stubs (bsdnums.json, made by
# tools/sysnums.py); the entries below only rename calls whose iOS 6 stub is the 64-bit-inode
# variant, or add numbers whose stubs that tool does not recognise.
def _data(name):
    """A data file beside this module: from disk, or through the loader when the module runs
    from an archive (the Android app's packaged Python)."""
    try:
        return open(os.path.join(os.path.dirname(__file__), name), 'rb').read()
    except OSError:
        import pkgutil
        return pkgutil.get_data(__name__, name)


BSD_NAMES = {int(k): v for k, v in _json.loads(_data('bsdnums.json')).items()}
BSD_NAMES.update({20: 'getpid', 188: 'stat', 189: 'fstat', 190: 'lstat', 338: 'stat64', 339: 'fstat64', 340: 'lstat64',
                  93: 'select', 197: 'mmap', 202: 'sysctl', 220: 'getattrlist', 221: 'setattrlist', 153: 'pread',
                  154: 'pwrite', 295: 'shared_region_map_np', 301: 'psynch_mutexwait', 302: 'psynch_mutexdrop',
                  303: 'psynch_cvbroad', 304: 'psynch_cvsignal', 305: 'psynch_cvwait', 306: 'psynch_rw_rdlock',
                  307: 'psynch_rw_wrlock', 308: 'psynch_rw_unlock', 309: 'psynch_rw_unlock2', 312: 'psynch_cvclrprepost',
                  336: 'proc_info', 360: 'bsdthread_create', 363: 'kevent', 366: 'bsdthread_register', 369: 'kevent64',
                  371: 'thread_selfid', 370: '__old_semwait_signal', 334: '__semwait_signal', 423: '__semwait_signal_nocancel'})

MACH_NAMES = {3: 'mach_absolute_time', 10: 'mach_vm_allocate', 12: 'mach_vm_deallocate', 14: 'mach_vm_protect',
              16: 'mach_port_allocate', 17: 'mach_port_destroy', 18: 'mach_port_deallocate', 19: 'mach_port_mod_refs',
              20: 'mach_port_move_member', 21: 'mach_port_insert_right', 22: 'mach_port_insert_member',
              23: 'mach_port_extract_member', 26: 'mach_reply_port', 27: 'thread_self', 28: 'task_self', 29: 'host_self',
              31: 'mach_msg', 32: 'mach_msg_overwrite', 33: 'semaphore_signal', 34: 'semaphore_signal_all', 36: 'semaphore_wait',
              37: 'semaphore_wait_signal', 38: 'semaphore_timedwait', 39: 'semaphore_timedwait_signal', 89: 'mach_timebase_info',
              90: 'mach_wait_until', 91: 'mk_timer_create', 92: 'mk_timer_destroy', 93: 'mk_timer_arm', 94: 'mk_timer_cancel', 58: 'pid_for_task',
              59: 'swtch_pri', 60: 'swtch', 61: 'thread_switch', 62: 'clock_sleep', 24: 'mach_port_construct?'}


class Kernel:
    def __init__(self, emu):
        self.emu = emu
        self.uc = emu.uc
        self.fds = {0: None, 1: None, 2: None}
        self.cwd = '/'                   # an iOS app starts in /; chdir moves it
        self.exited = None
        self.unimplemented = {}
        self.t0 = time.monotonic()
        from ports import Ports
        self.pr = Ports(self)
        self.task_port = self.new_port('task')
        self.thread_port = self.new_port('thread')
        self.host_port = self.new_port('host')
        self.calls = 0
        self.semaphores = {}
        import kqueue
        kqueue.install(Kernel)
        from bootstrap import Bootstrap
        self.bootstrap = Bootstrap(self)
        from services.backboard import Launcher
        self.launcher = Launcher(self)
        from services.backboard import Touches
        self.touches = Touches(self, os.environ.get('PP_TOUCH', ''))
        from mach import MachMsg
        self.mach = MachMsg(self)

    def new_port(self, kind):
        return self.pr.new(kind)

    @property
    def cur(self):
        return self.emu.sched.cur

    # ------------------------------------------------------------ plumbing
    def reg(self, n): return self.uc.reg_read(UC_ARM_REG_R0 + n) if n < 13 else None
    def args(self, n): return [self.uc.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
                                                          UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R8)[:n]]

    def stack_args(self, n):
        """BSD calls take arguments 0-3 in r0-r3 and the rest from the user stack? No: libsystem
        loads r4-r6, r8 for args 4-7 before svc."""
        return self.args(n)

    def ret(self, value, error=False, value_hi=None):
        cpsr = self.uc.reg_read(UC_ARM_REG_CPSR)
        if error:
            self.uc.reg_write(UC_ARM_REG_CPSR, cpsr | CARRY)
            self.uc.reg_write(UC_ARM_REG_R0, value)
        else:
            self.uc.reg_write(UC_ARM_REG_CPSR, cpsr & ~CARRY)
            self.uc.reg_write(UC_ARM_REG_R0, value & 0xffffffff)
            if value_hi is not None:
                self.uc.reg_write(UC_ARM_REG_R1, value_hi & 0xffffffff)

    def on_interrupt(self, uc, intno, data):
        if intno != 2:
            print(f'[kernel] interrupt {intno} at pc={uc.reg_read(UC_ARM_REG_PC):#x}')
            uc.emu_stop()
            return
        pc = uc.reg_read(UC_ARM_REG_PC)
        if uc.reg_read(UC_ARM_REG_CPSR) & 0x20:
            imm = struct.unpack('<H', self.emu.read(pc - 2, 2))[0] & 0xff
            if imm != 0x80:
                from hle import THUMB_BASE, INDEXED_THUMB
                try:
                    if imm == INDEXED_THUMB:
                        self.emu.stubs.call(pc, True)
                    else:
                        self.emu.hle.call(imm - THUMB_BASE)
                except Exception:
                    import traceback
                    traceback.print_exc()
                    self.emu.backtrace(12)
                    self.exited = -1
                    uc.emu_stop()
                return
        else:
            imm = struct.unpack('<I', self.emu.read(pc - 4, 4))[0] & 0xffffff
            if imm != 0x80:
                from hle import SVC_BASE, INDEXED_ARM
                try:
                    if imm == INDEXED_ARM:
                        self.emu.stubs.call(pc, False)
                    else:
                        self.emu.hle.call(imm - SVC_BASE)
                except Exception:
                    import traceback
                    traceback.print_exc()
                    self.emu.backtrace(12)
                    self.exited = -1
                    uc.emu_stop()
                return
        num = uc.reg_read(UC_ARM_REG_R12)
        if num == 0x80000000:
            self.platform_call()
            return
        if num & 0x80000000:
            num -= 1 << 32
        self.calls += 1
        if self.calls == TRACE_FROM:
            self.emu.trace_sys = True
        if self.calls % 512 == 0 and len(self.emu.sched.threads) > 1 and self.emu.sched.cur.ready is None:
            self._yield_after = True
        if self.emu.trace_sys:
            self._last = (num, [hex(a) for a in self.args(4)])
        try:
            if num > 0:
                self.bsd(num)
            elif num < 0:
                self.mach_trap(-num)
            else:
                self.indirect()
            if getattr(self, '_yield_after', False):
                self._yield_after = False
                if self.emu.sched.cur.ready is None and not self.emu.sched.cur.dead and self.exited is None:
                    self.emu.sched.cur.ready = lambda: True
                    self.emu.sched.cur.why = 'preempted at a system call'
                    uc.emu_stop()
            if self.emu.trace_sys:
                n = getattr(self, "_last", (num, []))[0]
                nm = BSD_NAMES.get(n, '') if n > 0 else MACH_NAMES.get(-n, '')
                err = ' ERR' if n > 0 and uc.reg_read(UC_ARM_REG_CPSR) & CARRY else ''
                print(f'[sys] t{self.cur.tid} {n} {nm}{getattr(self, "_last", (0, []))[1]} -> {uc.reg_read(UC_ARM_REG_R0):#x}{err}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f'[kernel] host exception in call {num}: {e}')
            self.exited = -1
            uc.emu_stop()

    def platform_call(self):
        # ARM machine-dependent calls: r3 selects the routine.
        op = self.uc.reg_read(UC_ARM_REG_R3)
        if op == 2:       # thread_set_cthread_self: the TLS base, read back from TPIDRURO
            self.uc.reg_write(UC_ARM_REG_C13_C0_3, self.uc.reg_read(UC_ARM_REG_R0))
            self.mret(0)
        elif op == 3:     # thread_get_cthread_self
            self.mret(self.uc.reg_read(UC_ARM_REG_C13_C0_3))
        elif op in (0, 1):   # icache invalidate / dcache flush: nothing to do
            self.mret(0)
        else:
            print(f'[kernel] unknown platform call {op}')
            self.mret(0)

    def indirect(self):
        # syscall(number, ...): the number is in r0 and the arguments follow one register up.
        regs = (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R8)
        vals = [self.uc.reg_read(r) for r in regs]
        num = vals[0]
        for r, v in zip(regs, vals[1:] + [0]):
            self.uc.reg_write(r, v)
        self.bsd(num)

    def log(self, msg):
        if self.emu.trace:
            print('[sys]', msg)

    def unimpl(self, kind, num, name):
        key = (kind, num)
        self.unimplemented[key] = self.unimplemented.get(key, 0) + 1
        if self.unimplemented[key] <= 3:
            lr = self.uc.reg_read(UC_ARM_REG_LR)
            print(f'[kernel] unimplemented {kind} {num} ({name}) args={[hex(a) for a in self.args(4)]} lr={lr:#x}')

    # ------------------------------------------------------------ BSD
    def bsd(self, num):
        name = BSD_NAMES.get(num, '?')
        h = getattr(self, 'sys_' + name.replace('?', ''), None)
        if h is None:
            self.unimpl('bsd', num, name)
            self.ret(ENOSYS, True)
            return
        try:
            h()
        except OSError as e:
            self.log(f'{name}: host error {e}')
            self.ret(DARWIN_ERRNO.get(e.errno, e.errno), True)

    def sys_exit(self):
        self.exited = self.args(1)[0]
        print(f'[kernel] exit({self.exited})')
        self.uc.emu_stop()

    def sys_getpid(self): self.ret(1234)
    def sys_getppid(self): self.ret(1)
    def sys_getuid(self): self.ret(501)
    def sys_geteuid(self): self.ret(501)
    def sys_getgid(self): self.ret(501)
    def sys_getegid(self): self.ret(501)
    def sys_issetugid(self): self.ret(0)
    def sys_umask(self): self.ret(0o22)
    def sys_thread_selfid(self): self.ret(self.cur.tid, value_hi=0)
    def sys_csops(self): self.ret(0)

    def sys_sigaction(self): self.ret(0)
    def sys_sigprocmask(self):
        how, set_p, oset_p = self.args(3)
        if oset_p:
            self.emu.w32(oset_p, 0)
        self.ret(0)
    sys_pthread_sigmask = sys_sigprocmask

    def sys_gettid(self):
        uid_p, gid_p = self.args(2)
        self.emu.w32(uid_p, 501)
        self.emu.w32(gid_p, 501)
        self.ret(0)
    sys_pthread_sigmask = sys_sigprocmask
    def sys_disable_threadsignal(self): self.ret(0)
    def sys_bsdthread_register(self):
        self.thread_start, self.wq_start, self.pthsize = self.args(3)
        self.ret(0)

    def start_thread(self, name, pc, regs, sp):
        """A new guest thread whose first instruction is pc, with r0.. set from regs."""
        from sched import Thread
        saved = self.uc.context_save()
        for i, v in enumerate(regs):
            self.uc.reg_write((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
                               UC_ARM_REG_R4, UC_ARM_REG_R5)[i], v & 0xffffffff)
        self.uc.reg_write(UC_ARM_REG_SP, sp)
        self.uc.reg_write(UC_ARM_REG_LR, 0)
        self.uc.reg_write(UC_ARM_REG_PC, pc & ~1)
        cpsr = self.uc.reg_read(UC_ARM_REG_CPSR)
        self.uc.reg_write(UC_ARM_REG_CPSR, (cpsr | 0x20) if pc & 1 else (cpsr & ~0x20))
        t = Thread(self.uc.context_save(), name, self.new_port('thread'))
        self.uc.context_restore(saved)
        self.emu.sched.add(t)
        return t

    def sys_bsdthread_create(self):
        func, arg, stack, pthread, flags = self.args(5)
        PTHREAD_START_CUSTOM = 0x01000000
        if flags & PTHREAD_START_CUSTOM:
            th_stack, th_pthread = stack, pthread
        else:
            size = stack
            total = size + 0x1000 + ((self.pthsize + 0xfff) & ~0xfff)
            base = self.emu.find_free(total)
            self.emu.map(base, total)
            self.emu.write(base, bytes(total))
            th_stack = base + 0x1000 + size
            th_pthread = th_stack
        t = self.start_thread(f'pthread {func:#x}', self.thread_start,
                              [th_pthread, 0, func, arg, stack, flags], th_stack)
        t.pthread = th_pthread
        # the thread port goes in r1 once it exists
        saved = self.uc.context_save()
        self.uc.context_restore(t.ctx)
        self.uc.reg_write(UC_ARM_REG_R1, t.port)
        t.ctx = self.uc.context_save()
        self.uc.context_restore(saved)
        print(f'[kernel] thread {t.tid} created: start {self.emu.syms.name(func)}, flags {flags:#x}')
        self.ret(th_pthread)

    def sys_bsdthread_terminate(self):
        stack, size, port, sem = self.args(4)
        if sem:
            self.sem(sem).value += 1
        self.cur.dead = True
        print(f'[kernel] thread {self.cur.tid} ended')
        self.uc.emu_stop()

    def sys_psynch_mutexwait(self):
        m, mgen, ugen, tid, flags = self.args(5)
        print(f'[psynch] thread {self.cur.tid} waits on mutex {m:#x} mgen={mgen:#x} ugen={ugen:#x} owner={tid} flags={flags:#x}')
        print('   words', [hex(self.emu.r32(m + 4 * i)) for i in range(16)])
        self.emu.backtrace()
        self.exited = -2
        self.uc.emu_stop()

    def sys_pthread_kill(self):
        port, sig = self.args(2)
        print(f'[kernel] signal {sig} sent to thread port {port:#x} by thread {self.cur.tid}')
        if sig == 0:
            self.ret(0); return
        if sig in (6, 9, 10, 11):
            self.emu.backtrace(40)
            self.exited = 128 + sig
            self.uc.emu_stop()
            return
        self.ret(0)

    def sys_kill(self):
        pid, sig = self.args(2)
        if pid in (0, 1234) and sig in (6, 9):
            print(f'[kernel] process killed itself with signal {sig}')
            self.emu.backtrace(40)
            self.exited = 128 + sig
            self.uc.emu_stop()
            return
        self.ret(0)

    def sys_workq_open(self):
        self.ret(0)

    WQ_STACK, WQ_GUARD = 512 * 1024, 0x1000

    WQ_FLAG_THREAD_REUSE = 0x00020000

    def wq_thread(self, item, flags):
        # The kernel parks a worker when its item is done (op 4) and hands the next request to a
        # parked one, saying so with WQ_FLAG_THREAD_REUSE: libc then keeps its pthread_t, which
        # stays on libc's list of all threads. Starting a new thread per request instead mapped a
        # 516 KB stack each time (hundreds a minute), and none was ever freed.
        parked = self.__dict__.setdefault('wq_parked', [])
        if parked:
            t = parked.pop()
            base = t.wq_stack
            self_p = base + self.WQ_GUARD + self.WQ_STACK
            saved = self.uc.context_save()
            self.uc.context_restore(t.ctx)
            for r, v in zip((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_R4),
                            (self_p, t.port, base + self.WQ_GUARD, item, flags | self.WQ_FLAG_THREAD_REUSE)):
                self.uc.reg_write(r, v & 0xffffffff)
            self.uc.reg_write(UC_ARM_REG_SP, self_p - 16)
            self.uc.reg_write(UC_ARM_REG_LR, 0)
            self.uc.reg_write(UC_ARM_REG_PC, self.wq_start & ~1)
            cpsr = self.uc.reg_read(UC_ARM_REG_CPSR)
            self.uc.reg_write(UC_ARM_REG_CPSR, (cpsr | 0x20) if self.wq_start & 1 else (cpsr & ~0x20))
            t.ctx = self.uc.context_save()
            self.uc.context_restore(saved)
            t.ready = t.complete = t.deadline = None
            t.why = ''
            t.name = f'workq {item:#x}'
            return t
        total = self.WQ_GUARD + self.WQ_STACK + ((self.pthsize + 0xfff) & ~0xfff)
        base = self.emu.find_free(total)
        self.emu.map(base, total)
        self.emu.write(base, bytes(total))
        self_p = base + self.WQ_GUARD + self.WQ_STACK
        t = self.start_thread(f'workq {item:#x}', self.wq_start, [self_p, 0, base + self.WQ_GUARD, item, flags], self_p - 16)
        saved = self.uc.context_save()
        self.uc.context_restore(t.ctx)
        self.uc.reg_write(UC_ARM_REG_R1, t.port)
        t.ctx = self.uc.context_save()
        self.uc.context_restore(saved)
        t.pthread = self_p
        t.wq_stack = base
        return t

    def workq_op(self, op, item, affinity, prio):
        if op == 1:                       # WQOPS_QUEUE_ADD: run the item on a new worker
            t = self.wq_thread(item, prio & 0xffff)
            print(f'[kernel] workq thread {t.tid} for item {item:#x}')
            self.ret(0)
        elif op == 2:                     # WQOPS_QUEUE_REMOVE
            self.ret(0)
        elif op == 4:                     # WQOPS_THREAD_RETURN: the worker parks until reused
            t = self.cur
            if getattr(t, 'wq_stack', None) is None:
                t.dead = True
                self.uc.emu_stop()
                return
            self.__dict__.setdefault('wq_parked', []).append(t)
            t.name = 'workq (parked)'
            self.emu.sched.block('parked worker', lambda: False, lambda timed_out: None)
        elif op == 8:                     # WQOPS_THREAD_SETCONC
            self.ret(0)
        elif op == 0x10:                  # WQOPS_QUEUE_NEWSPISUPP: yes, libdispatch requires it
            self.ret(0)
        elif op == 0x20:                  # WQOPS_QUEUE_REQTHREADS: affinity = how many, prio = priority | overcommit
            self.reqthreads = getattr(self, 'reqthreads', 0) + 1
            if os.environ.get('BT_REQTHREADS') and self.reqthreads in (300, 301, 302):
                print(f'[kernel] REQTHREADS #{self.reqthreads} from thread {self.cur.tid}')
                self.emu.trace_sys = True
                self.emu.backtrace()
            for _ in range(max(1, affinity)):
                t = self.wq_thread(0, 0x40000 | (prio & 0x1ffff))
                if self.emu.trace_sys:
                    print(f'[kernel] workq thread {t.tid} (priority {prio & 0xffff}{", overcommit" if prio & 0x10000 else ""})')
            self.ret(0)
        else:
            self.ret(45, True)            # ENOTSUP

    def sys_workq_kernreturn(self):
        op, item, affinity, prio = self.args(4)
        if self.emu.trace_sys:
            print(f'[kernel] workq_kernreturn op={op:#x} item={item:#x} affinity={affinity} prio={prio}')
        self.workq_op(op, item, affinity, prio)

    def sys_gettimeofday(self):
        tv, tz = self.args(2)
        from sched import CLOCK
        CLOCK.nudge()
        now = CLOCK.wall()
        if tv:
            self.emu.write(tv, struct.pack('<ii', int(now), int((now % 1) * 1e6)))
        # libsystem's gettimeofday stub returns seconds/usecs in r0/r1 as well
        self.ret(int(now), value_hi=int((now % 1) * 1e6))

    def sys_getrlimit(self):
        res, p = self.args(2)
        res &= 0xff                              # the UNIX2003 wrappers add _RLIMIT_POSIX_FLAG (0x1000)
        limits = {8: (256, 10240), 3: (1 << 20, 1 << 20)}      # RLIMIT_NOFILE, RLIMIT_STACK
        cur, mx = limits.get(res, (0x7fffffffffffffff, 0x7fffffffffffffff))
        self.emu.write(p, struct.pack('<QQ', cur, mx))
        self.ret(0)
    def sys_setrlimit(self): self.ret(0)
    def sys_getdtablesize(self): self.ret(256)

    # --- files
    def guest_path(self, p):
        path = self.emu.cstr(p)
        if path and not path.startswith('/'):
            import posixpath
            path = posixpath.normpath(posixpath.join(self.cwd, path))
        return path

    def sys_chdir(self):
        path = self.guest_path(self.args(1)[0])
        host = self.emu.host_path(path)
        if not os.path.exists(host):
            self.ret(2, True); return                  # ENOENT
        if not os.path.isdir(host):
            self.ret(20, True); return                 # ENOTDIR
        self.cwd = path
        self.log(f'chdir({path!r})')
        self.ret(0)
    sys_pthread_chdir = sys_chdir                      # one working directory for every thread

    def sys_fchdir(self):
        entry = self.fds.get(self.args(1)[0])
        if not entry or not isinstance(entry[1], str) or not os.path.isdir(self.emu.host_path(entry[1])):
            self.ret(EBADF if not entry else 20, True); return
        self.cwd = entry[1]
        self.ret(0)
    sys_pthread_fchdir = sys_fchdir

    @staticmethod
    def writable(path):
        """The app may change files only in its container and the temporary directories."""
        return path.startswith('/var/mobile/Applications/') or path.startswith('/tmp') or path.startswith('/private/var/tmp')

    def path_call(self, n=1):
        """Guest paths of the first n arguments, and their host paths; None if one may not change."""
        args = self.args(n)
        guest = [self.guest_path(a) for a in args]
        if not all(self.writable(g) for g in guest):
            self.log(f'refused to change {guest}')
            self.ret(1, True)            # EPERM
            return None
        return guest, [self.emu.host_path(g) for g in guest]

    def sys_unlink(self):
        r = self.path_call(1)
        if r:
            os.unlink(r[1][0]); self.log(f'unlink({r[0][0]!r})'); self.ret(0)

    def sys_rename(self):
        r = self.path_call(2)
        if r:
            os.replace(r[1][0], r[1][1]); self.log(f'rename({r[0][0]!r}, {r[0][1]!r})'); self.ret(0)

    def sys_mkdir(self):
        path_p, mode = self.args(2)
        g = self.guest_path(path_p)
        if not self.writable(g):
            self.ret(1, True); return
        os.mkdir(self.emu.host_path(g), 0o755); self.log(f'mkdir({g!r})'); self.ret(0)

    def sys_rmdir(self):
        r = self.path_call(1)
        if r:
            os.rmdir(r[1][0]); self.ret(0)

    def sys_fsync(self):
        fd = self.hostfd(self.args(1)[0])
        if fd is None:
            self.ret(EBADF, True); return
        self.ret(0)
    sys_fsync_nocancel = sys_fsync
    sys_fdatasync = sys_fsync

    def sys_ftruncate(self):
        gfd = self.args(1)[0]
        length = self.uc.reg_read(UC_ARM_REG_R1) | (self.uc.reg_read(UC_ARM_REG_R2) << 32)
        fd = self.hostfd(gfd)
        if fd is None:
            self.ret(EBADF, True); return
        os.ftruncate(fd, length); self.ret(0)

    def sys_chmod(self): self.ret(0)
    def sys_fchmod(self): self.ret(0)
    def sys_chown(self): self.ret(0)
    def sys_fchown(self): self.ret(0)
    def sys_utimes(self): self.ret(0)
    def sys_futimes(self): self.ret(0)

    def sys_shm_open(self):
        """POSIX shared memory objects, kept as files in a per-run directory. SpringBoard's
        "GSCapabilities" object is created up front from capabilities.py."""
        name_p, flags, mode = self.args(3)
        name = self.emu.cstr(name_p).lstrip('/')
        d = getattr(self, 'shm_dir', None)
        if d is None:
            import tempfile
            from capabilities import plist_bytes
            d = self.shm_dir = tempfile.mkdtemp(prefix='pocketpear-shm-')
            with open(os.path.join(d, 'GSCapabilities'), 'wb') as f:
                f.write(plist_bytes(getattr(self.emu, 'tall', True)))
        host = os.path.join(d, name.replace('/', '_'))
        hflags = {0: os.O_RDONLY, 1: os.O_WRONLY, 2: os.O_RDWR}[flags & O_ACCMODE]
        if flags & O_CREAT: hflags |= os.O_CREAT
        if flags & O_EXCL: hflags |= os.O_EXCL
        if flags & O_TRUNC: hflags |= os.O_TRUNC
        try:
            fd = os.open(host, hflags, mode or 0o644)
        except OSError as e:
            self.ret(e.errno, True)
            return
        gfd = 3
        while gfd in self.fds:
            gfd += 1
        self.fds[gfd] = (fd, '/shm/' + name)
        self.log(f'shm_open({name!r}) = {gfd}')
        self.ret(gfd)

    def sys_flock(self):
        self.ret(0)                          # one process: advisory locks always succeed

    def sys_open(self):
        path_p, flags, mode = self.args(3)
        path = self.guest_path(path_p)
        host = self.emu.host_path(path)
        hflags = {0: os.O_RDONLY, 1: os.O_WRONLY, 2: os.O_RDWR}[flags & O_ACCMODE]
        if flags & O_CREAT: hflags |= os.O_CREAT
        if flags & O_TRUNC: hflags |= os.O_TRUNC
        if flags & O_EXCL: hflags |= os.O_EXCL
        if flags & O_APPEND: hflags |= os.O_APPEND
        writing = (flags & O_ACCMODE) != 0 or flags & O_CREAT
        if writing and not path.startswith('/var/mobile/Applications/') and not path.startswith('/tmp') and not path.startswith('/private/var/tmp'):
            self.log(f'open({path!r}) for writing refused')
            self.ret(EPERM, True)
            return
        try:
            if os.path.isdir(host):
                fd = os.open(host, os.O_RDONLY)
            else:
                if writing:
                    os.makedirs(os.path.dirname(host), exist_ok=True)
                # mode is read only with O_CREAT; without it the register holds whatever was there
                fd = os.open(host, hflags, (mode & 0o7777 or 0o644) if flags & O_CREAT else 0o644)
        except OSError as e:
            self.log(f'open({path!r}) -> errno {e.errno}')
            self.ret(e.errno, True)
            return
        gfd = 3
        while gfd in self.fds:
            gfd += 1
        self.fds[gfd] = (fd, path)
        self.log(f'open({path!r}, {flags:#x}) = {gfd}')
        self.ret(gfd)
    sys_open_nocancel = sys_open

    def sys_socket(self):
        """Only routing sockets: SCNetworkReachability opens one to hear about network changes
        and logs a complaint every time it cannot. The socket never has anything to read, so
        no change ever arrives, which is true here. Other kinds still fail (no networking)."""
        domain, stype, proto = self.args(3)
        if domain != 17:                       # PF_ROUTE
            self.unimpl('socket', domain, 'socket')
            self.ret(47, True)                 # EAFNOSUPPORT
            return
        gfd = 3
        while gfd in self.fds:
            gfd += 1
        self.fds[gfd] = None
        self.ret(gfd)

    def hostfd(self, gfd):
        e = self.fds.get(gfd)
        return e[0] if isinstance(e, tuple) else None

    def sys_close(self):
        gfd, = self.args(1)
        e = self.fds.pop(gfd, None)
        getattr(self, 'dirs', {}).pop(gfd, None)
        if isinstance(e, tuple):
            os.close(e[0])
        self.ret(0)
    sys_close_nocancel = sys_close

    def sys_read(self):
        gfd, buf, n = self.args(3)
        if gfd == 0:
            self.ret(0); return
        fd = self.hostfd(gfd)
        if fd is None:
            self.ret(EBADF, True); return
        data = os.read(fd, n)
        self.emu.write(buf, data)
        self.ret(len(data))
    sys_read_nocancel = sys_read

    def sys_pread(self):
        gfd, buf, n, off_lo, off_hi = self.args(5)
        fd = self.hostfd(gfd)
        if fd is None:
            self.ret(EBADF, True); return
        off = off_lo | (off_hi << 32)            # off_t in r3:r4 (Apple's ABI leaves it unaligned)
        data = os.pread(fd, n, off)
        self.emu.write(buf, data)
        self.ret(len(data))

    def sys_write(self):
        gfd, buf, n = self.args(3)
        data = self.emu.read(buf, n)
        if gfd in (1, 2):
            self.ret(self.write_bytes(gfd, data)); return
        fd = self.hostfd(gfd)
        if fd is None:
            self.ret(EBADF, True); return
        self.ret(os.write(fd, data))
    sys_write_nocancel = sys_write

    def write_bytes(self, gfd, data):
        if gfd in (1, 2):
            text = data.decode('utf-8', 'replace')
            for line in text.rstrip('\n').split('\n'):
                print(f'[guest {"out" if gfd == 1 else "err"}] {line}')
            if os.environ.get('BT_ON_TEXT') and os.environ['BT_ON_TEXT'] in text:
                self.emu.backtrace(30)
            return len(data)
        fd = self.hostfd(gfd)
        if fd is None:
            return None
        return os.write(fd, data)

    def sys_writev(self):
        gfd, iov, cnt = self.args(3)
        data = b''.join(self.emu.read(self.emu.r32(iov + 8 * i), self.emu.r32(iov + 8 * i + 4)) for i in range(cnt))
        n = self.write_bytes(gfd, data)
        if n is None:
            self.ret(EBADF, True)
        else:
            self.ret(n)
    sys_writev_nocancel = sys_writev

    def sys_readv(self):
        gfd, iov, cnt = self.args(3)
        fd = self.hostfd(gfd)
        if fd is None:
            self.ret(EBADF, True); return
        total = 0
        for i in range(cnt):
            base, ln = self.emu.r32(iov + 8 * i), self.emu.r32(iov + 8 * i + 4)
            data = os.read(fd, ln)
            self.emu.write(base, data)
            total += len(data)
            if len(data) < ln:
                break
        self.ret(total)
    sys_readv_nocancel = sys_readv

    def sys_lseek(self):
        gfd = self.args(1)[0]
        # Apple's armv7 ABI does not align 64-bit arguments to an even register pair: off_t
        # is r1:r2 and whence is r3 (and on the stack such arguments are only 4-aligned).
        off = self.uc.reg_read(UC_ARM_REG_R1) | (self.uc.reg_read(UC_ARM_REG_R2) << 32)
        whence = self.uc.reg_read(UC_ARM_REG_R3)
        fd = self.hostfd(gfd)
        if fd is None:
            self.ret(EBADF, True); return
        if off & (1 << 63): off -= 1 << 64
        r = os.lseek(fd, off, whence)
        self.ret(r & 0xffffffff, value_hi=r >> 32)

    def sys_fcntl(self):
        gfd, cmd, arg = self.args(3)
        fd = self.hostfd(gfd)
        if gfd not in self.fds:
            self.ret(EBADF, True); return
        if cmd == 1:      # F_GETFD
            self.ret(0)
        elif cmd in (2, 4):  # F_SETFD, F_SETFL
            self.ret(0)
        elif cmd == 3:    # F_GETFL
            self.ret(0)
        elif cmd == 59:   # F_ADDFILESIGS (code signature): accept
            self.ret(0)
        elif cmd == 50:   # F_GETPATH
            path = self.fds[gfd][1] if self.fds[gfd] else '/dev/tty'
            self.emu.write(arg, path.encode() + b'\0'); self.ret(0)
        elif cmd in (48, 49, 44, 45, 61, 62):   # F_NOCACHE, F_RDADVISE, F_RDAHEAD, ...
            self.ret(0)
        else:
            self.unimpl('fcntl', cmd, 'fcntl')
            self.ret(EINVAL, True)
    sys_fcntl_nocancel = sys_fcntl

    def stat_bytes(self, st, layout64):
        mode = st.st_mode
        if layout64:
            # dev, mode(16), nlink(16), ino(64), uid, gid, rdev, atime, mtime, ctime, birthtime, size, blocks, blksize, flags, gen, lspare, qspare[2]
            return struct.pack('<iHHQIIi' + 'ii' * 4 + 'qqiIIiqq', 1, mode & 0xffff, min(st.st_nlink, 0xffff), st.st_ino & 0xffffffffffffffff, 501, 501, 0,
                               int(st.st_atime), 0, int(st.st_mtime), 0, int(st.st_ctime), 0, int(st.st_mtime), 0,
                               st.st_size, (st.st_size + 511) // 512, 4096, 0, 0, 0, 0, 0)
        return struct.pack('<iIHHIIi' + 'ii' * 3 + 'qqiIIiqq', 1, st.st_ino & 0xffffffff, mode & 0xffff, min(st.st_nlink, 0xffff),
                           501, 501, 0, int(st.st_atime), 0, int(st.st_mtime), 0, int(st.st_ctime), 0,
                           st.st_size, (st.st_size + 511) // 512, 4096, 0, 0, 0, 0, 0)

    def do_stat(self, layout64, follow=True, fd=None):
        if fd is None:
            path_p, buf = self.args(2)
            path = self.guest_path(path_p)
            host = self.emu.host_path(path) if follow else self.emu.host_path_raw(path)
            try:
                st = os.stat(host) if follow else os.lstat(host)
            except OSError as e:
                self.log(f'stat({path!r}) -> errno {e.errno}')
                self.ret(e.errno, True); return
            self.log(f'stat({path!r}) ok')
        else:
            gfd, buf = self.args(2)
            if gfd in (0, 1, 2):
                st = os.stat_result((0o20666, 0, 0, 1, 0, 0, 0, 0, 0, 0))
            else:
                hfd = self.hostfd(gfd)
                if hfd is None:
                    self.ret(EBADF, True); return
                st = os.fstat(hfd)
        self.emu.write(buf, self.stat_bytes(st, layout64))
        self.ret(0)

    def sys_stat(self): self.do_stat(False)
    def sys_lstat(self): self.do_stat(False, follow=False)
    def sys_fstat(self): self.do_stat(False, fd=True)
    def sys_stat64(self): self.do_stat(True)
    def sys_lstat64(self): self.do_stat(True, follow=False)
    def sys_fstat64(self): self.do_stat(True, fd=True)

    # The _extended calls also return the file's ACL (xsecurity); there never is one, so its
    # size (the fourth argument, a size_t*) is answered 0.
    def extended(self, follow=True, fd=None):
        size_p = self.args(4)[3]
        if size_p:
            self.emu.w32(size_p, 0)
        self.do_stat(True, follow=follow, fd=fd)
    def sys_stat64_extended(self): self.extended()
    def sys_lstat64_extended(self): self.extended(follow=False)
    def sys_fstat64_extended(self): self.extended(fd=True)

    def sys_getattrlist(self):
        """getattrlist for the common attributes realpath and CFURL ask for: name, device,
        filesystem id, object type, tag and object ids. Anything else is ENOTSUP, which the
        callers treat as "use stat instead"."""
        path_p, al, buf, size, options = self.args(5)
        path = self.guest_path(path_p)
        bitmapcount, _, common, vol, dirattr, fileattr, fork = struct.unpack('<HHIIIII', self.emu.read(al, 24))
        if bitmapcount != 5 or vol or dirattr or fileattr or fork or common & ~0xff:
            self.log(f'getattrlist({path!r}) attrs {common:#x}/{vol:#x}/{dirattr:#x}/{fileattr:#x} unsupported')
            self.ret(45, True)                    # ENOTSUP
            return
        host = self.emu.host_path_raw(path) if options & 1 else self.emu.host_path(path)
        try:
            st = os.lstat(host) if options & 1 else os.stat(host)
        except OSError as e:
            self.ret(e.errno, True)
            return
        fixed, names = b'', []
        objtype = 2 if pystat.S_ISDIR(st.st_mode) else 5 if pystat.S_ISLNK(st.st_mode) else 1
        ino = st.st_ino & 0xffffffff
        for bit in range(8):
            if not common & (1 << bit):
                continue
            if bit == 0:                          # ATTR_CMN_NAME: attrreference, filled below
                names.append(len(fixed))
                fixed += b'\0' * 8
            elif bit == 1:                        # DEVID
                fixed += struct.pack('<I', 0x1000002)
            elif bit == 2:                        # FSID
                fixed += struct.pack('<II', 0x1000002, 17)
            elif bit == 3:                        # OBJTYPE
                fixed += struct.pack('<I', objtype)
            elif bit == 4:                        # OBJTAG: VT_HFS
                fixed += struct.pack('<I', 16)
            else:                                 # OBJID / OBJPERMANENTID / PAROBJID
                fixed += struct.pack('<II', ino if bit != 7 else 2, 0)
        name = (path.rstrip('/').rsplit('/', 1)[-1] or '/').encode() + b'\0'
        out = bytearray(4 + len(fixed))
        out[4:] = fixed
        for at in names:
            field = 4 + at
            struct.pack_into('<iI', out, field, len(out) - field, len(name))
            out += name + b'\0' * (-len(name) % 4)
        struct.pack_into('<I', out, 0, len(out))
        self.emu.write(buf, bytes(out[:size]))
        self.ret(0)

    def sys_access(self):
        path_p, mode = self.args(2)
        path = self.guest_path(path_p)
        ok = os.path.exists(self.emu.host_path(path))
        self.log(f'access({path!r}) = {ok}')
        if os.environ.get('BT_ON_PATH') and os.environ['BT_ON_PATH'] in path:
            self.emu.backtrace(20)
        self.ret(0) if ok else self.ret(ENOENT, True)

    def sys_readlink(self):
        path_p, buf, size = self.args(3)
        host = self.emu.host_path_raw(self.guest_path(path_p))
        self.log(f'readlink({self.guest_path(path_p)!r}) host {host!r}')
        if not os.path.lexists(host):
            self.ret(ENOENT, True); return
        if not os.path.islink(host):
            self.ret(EINVAL, True); return
        t = os.readlink(host).encode()[:size]
        self.emu.write(buf, t); self.ret(len(t))

    def sys_ioctl(self): self.ret(ENOTSUP if False else 25, True)   # ENOTTY

    # --- memory
    def sys_mmap(self):
        addr, length, prot, flags, gfd = self.args(5)
        off = self.uc.reg_read(UC_ARM_REG_R5) | (self.uc.reg_read(UC_ARM_REG_R6) << 32)   # fd r4, off_t r5:r6
        if gfd == 0xffffffff:
            gfd = -1
        MAP_FIXED, MAP_ANON = 0x10, 0x1000
        if flags & MAP_FIXED:
            a = addr
            self.emu.unmap(a, length)
        else:
            a = self.emu.find_free(length, addr or None)
        self.emu.map(a, length)
        if not (flags & MAP_ANON) and gfd >= 0:
            fd = self.hostfd(gfd)
            if fd is None:
                self.ret(EBADF, True); return
            data = os.pread(fd, length, off)
            self.emu.write(a, data)
        else:
            self.emu.write(a, bytes(((length + 0xfff) & ~0xfff)))
        self.log(f'mmap({addr:#x}, {length:#x}, prot={prot}, flags={flags:#x}, fd={gfd}, off={off:#x}) = {a:#x}')
        self.ret(a)

    def sys_munmap(self):
        a, n = self.args(2)
        self.emu.unmap(a, n)
        self.ret(0)

    def sys_mprotect(self): self.ret(0)
    def sys_madvise(self): self.ret(0)

    def sys_shared_region_check_np(self):
        p, = self.args(1)
        self.emu.write(p, struct.pack('<Q', self.emu.shared_cache_base))
        self.log(f'shared_region_check_np -> {self.emu.shared_cache_base:#x}')
        self.ret(0)

    # --- sysctl
    def sys_sysctl(self):
        name_p, namelen, old_p, oldlen_p, new_p, newlen = self.args(6)
        mib = [self.emu.r32(name_p + 4 * i) for i in range(namelen)]
        if mib[:2] == [0, 3]:               # sysctl.name2mib: newp holds the name
            name = self.emu.read(new_p, newlen).split(b'\0')[0].decode()
            known = SYSCTL_MIBS.get(name)
            if known is None and name in NAMED_SYSCTL:
                known = [NAMED_MIB, list(NAMED_SYSCTL).index(name)]
            if known is None:
                self.unimpl('sysctl name', 0, name)
                self.ret(ENOENT, True); return
            avail = self.emu.r32(oldlen_p) // 4
            for i, v in enumerate(known[:avail]):
                self.emu.w32(old_p + 4 * i, v)
            self.emu.w32(oldlen_p, 4 * len(known))
            self.ret(0); return
        val = self.sysctl_value(mib)
        if val is None:
            self.unimpl('sysctl', tuple(mib), 'sysctl')
            self.ret(ENOENT, True); return
        if oldlen_p:
            if old_p:
                avail = self.emu.r32(oldlen_p)
                self.emu.write(old_p, val[:avail])
            self.emu.w32(oldlen_p, len(val))
        self.ret(0)

    def sysctl_value(self, mib):
        CTL_KERN, CTL_HW = 1, 6
        i32 = lambda v: struct.pack('<i', v)
        s = lambda t: t.encode() + b'\0'
        table = {
            (1, 1): s('Darwin'), (1, 2): s('13.0.0'), (1, 4): s('Darwin Kernel Version 13.0.0'),
            (1, 10): s('localhost'), (1, 8): i32(262144), (1, 24): i32(1),      # argmax, securelevel?
            (1, 18): i32(1), (1, 59): i32(0),
            (6, 1): s('iPhone5,2'), (6, 2): s('N42AP'), (6, 3): i32(2), (6, 7): i32(4096), (6, 25): i32(2),
            (6, 24): struct.pack('<Q', 1 << 30), (6, 5): i32(1 << 30), (6, 15): i32(1300000000),
            (6, 26): i32(1300000000), (6, 16): i32(64), (6, 17): i32(64),
            (1, 21): i32(1), (1, 38): i32(1),
            (1, 65): i32(0),
        }
        table[(1, 65)] = s('10B350')        # kern.osversion: the build
        table[(1, 35)] = i32(0x2f000000)     # kern.usrstack
        key = tuple(mib[:2])
        if key in table:
            return table[key]
        if mib[0] == NAMED_MIB:
            return NAMED_SYSCTL[list(NAMED_SYSCTL)[mib[1]]]
        if len(mib) >= 5 and mib[0] == 4 and mib[1] == 17 and mib[4] in (3, 2):   # CTL_NET, PF_ROUTE, NET_RT_IFLIST
            return self.interface_list(mib[5] if len(mib) > 5 else 0)
        return None

    # The device's network interfaces: lo0 and en0 (Wi-Fi) with a fixed, locally administered
    # hardware address, as the routing socket lists them (an RTM_IFINFO message each).
    INTERFACES = [(1, 'lo0', 24, b''), (2, 'en0', 6, bytes([0x02, 0x00, 0x00, 0x00, 0x00, 0x01]))]

    def interface_list(self, only_index=0):
        out = b''
        for index, name, iftype, mac in self.INTERFACES:
            if only_index and index != only_index:
                continue
            nm = name.encode()
            sdl = struct.pack('<BBHBBBB', 0, 18, index, iftype, len(nm), len(mac), 0) + nm + mac
            sdl = sdl.ljust(max(20, (len(sdl) + 3) & ~3), b'\0')
            sdl = bytes([len(sdl)]) + sdl[1:]
            if_data = struct.pack('<8B', iftype, 0, 0, len(mac), 14 if mac else 0, 0, 0, 0) + struct.pack('<16I', 1500 if mac else 16384, *([0] * 15)) + bytes(8) + bytes(16)
            flags = 0x8863 if mac else 0x8049          # UP|BROADCAST|RUNNING|SIMPLEX|MULTICAST, or UP|LOOPBACK|RUNNING|MULTICAST
            hdr_len = 16 + len(if_data)
            msg = struct.pack('<HBBiiHH', hdr_len + len(sdl), 5, 0xe, 0x10, flags, index, 0) + if_data + sdl
            out += msg
        return out

    # ------------------------------------------------------------ file systems and directories
    def statfs_bytes(self):
        # A fixed 16 GB disk, as an iPhone 5, rather than the host's: Android's / is a read-only
        # system partition that can report its counts as "unlimited" (all ones), which Python
        # reads as -1, and the guest has no business seeing the host's disk anyway.
        # Free space stays under 2 GB: games of the time read NSFileSystemFreeSize with intValue,
        # and 8 GB truncates to exactly 0 (SvR 2010 then refuses to save).
        name = b'hfs'
        blocks = (16 << 30) // 4096
        free = (1536 << 20) // 4096
        return struct.pack('<IiQQQQQiiIIII16s1024s1024s32s', 4096, 1 << 20, blocks, free, free,
                           1 << 20, 1 << 19, 0x1000001, 0x1000001, 501, 0x11, 0x1000, 0, name,
                           b'/private/var', b'/dev/disk0s1s2', b'')

    def sys_fstatfs(self):
        gfd, buf = self.args(2)
        self.emu.write(buf, self.statfs_bytes())
        self.ret(0)

    def sys_statfs(self):
        path_p, buf = self.args(2)
        if not os.path.exists(self.emu.host_path(self.guest_path(path_p))):
            self.ret(ENOENT, True); return
        self.emu.write(buf, self.statfs_bytes())
        self.ret(0)

    def sys_getdirentries64(self):
        gfd, buf, size, basep = self.args(4)
        e = self.fds.get(gfd)
        if not isinstance(e, tuple):
            self.ret(EBADF, True); return
        if not hasattr(self, 'dirs'):
            self.dirs = {}
        host = self.emu.host_path(e[1])
        if gfd not in self.dirs:
            try:
                names = ['.', '..'] + sorted(os.listdir(host))
            except OSError as err:
                self.ret(err.errno, True); return
            self.dirs[gfd] = [names, 0]
        names, pos = self.dirs[gfd]
        out = bytearray()
        while pos < len(names):
            n = names[pos].encode()
            reclen = (21 + len(n) + 1 + 3) & ~3
            if len(out) + reclen > size:
                break
            full = os.path.join(host, names[pos])
            try:
                st = os.lstat(full)
                ino, mode = st.st_ino, st.st_mode
            except OSError:
                ino, mode = pos + 2, 0o100644
            dtype = 4 if pystat.S_ISDIR(mode) else 10 if pystat.S_ISLNK(mode) else 8
            rec = struct.pack('<QQHHB', ino & 0xffffffffffffffff, pos + 1, reclen, len(n), dtype) + n + b'\0'
            out += rec + bytes(reclen - len(rec))
            pos += 1
        self.dirs[gfd][1] = pos
        self.emu.write(buf, out)
        if basep:
            self.emu.write(basep, struct.pack('<Q', pos))
        self.log(f'getdirentries64({e[1]!r}) -> {len(out)} bytes')
        self.ret(len(out))

    def sys_proc_info(self):
        call, pid, flavor, arg_lo, arg_hi, buf, size = self.args(7)
        if call == 2 and flavor in (3, 13):      # PROC_PIDTBSDINFO / PROC_PIDT_SHORTBSDINFO
            comm = os.path.basename(self.emu.app_guest).rsplit(".", 1)[0][:15].encode()
            if flavor == 13:
                b = struct.pack('<IIII16sIIIIIIII', 1234, 1, 1234, 2, comm, 0, 501, 501, 501, 501, 501, 501, 0)
            else:
                b = struct.pack('<IIIII16s32sIIIIIIIIIIQQ', 0, 2, 0, 1234, 1, 501, comm, comm, 0, 501, 501, 501, 501, 501, 501, 0, 1234, 0, int(self.t0), 0)
            b = b[:size]
            self.emu.write(buf, b)
            self.ret(len(b))
        elif call == 2 and flavor == 12:         # PROC_PIDWORKQUEUEINFO
            n = sum(1 for t in self.emu.sched.threads if t.name.startswith('workq') and not t.dead)
            b = struct.pack('<IIII', n, n, 0, 0)[:size]
            self.emu.write(buf, b)
            self.ret(len(b))
        else:
            print(f'[kernel] proc_info call {call} flavor {flavor} not answered')
            self.ret(EINVAL, True)

    # ------------------------------------------------------------ Mach traps
    def mach_trap(self, num):
        name = MACH_NAMES.get(num, '?')
        h = getattr(self, 'trap_' + name.replace('?', ''), None)
        if h is None:
            self.unimpl('mach', -num, name)
            self.uc.reg_write(UC_ARM_REG_R0, 0x10000003)    # MACH_SEND_INVALID_DEST-ish failure
            return
        h()

    def mret(self, v): self.uc.reg_write(UC_ARM_REG_R0, v & 0xffffffff)

    def trap_task_self(self): self.mret(self.task_port)
    def trap_thread_self(self): self.mret(self.cur.port)
    def trap_host_self(self): self.mret(self.host_port)
    def trap_mach_reply_port(self): self.mret(self.new_port('receive'))

    def trap_mach_absolute_time(self):
        from sched import CLOCK
        CLOCK.nudge()
        t = int(CLOCK.now() * 24_000_000)    # 24 MHz timebase
        self.uc.reg_write(UC_ARM_REG_R0, t & 0xffffffff)
        self.uc.reg_write(UC_ARM_REG_R1, t >> 32)

    def trap_mach_timebase_info(self):
        p, = self.args(1)
        self.emu.write(p, struct.pack('<II', 125, 3))       # 24 MHz -> ns
        self.mret(0)

    def trap_mach_vm_allocate(self):
        task, addr_p, size_lo, size_hi, flags = self.args(5)
        # mach_vm_size_t is 64-bit: r2:r3, flags in r4
        size = size_lo | (size_hi << 32)
        anywhere = flags & 1
        a = self.emu.r32(addr_p)
        if anywhere or not a:
            a = self.emu.find_free(size)
        self.emu.map(a, size)
        self.emu.write(a, bytes(((size + 0xfff) & ~0xfff)))
        self.emu.write(addr_p, struct.pack('<Q', a))
        self.mret(0)

    def trap_mach_vm_deallocate(self):
        task = self.args(1)[0]
        addr = self.uc.reg_read(UC_ARM_REG_R2) | (self.uc.reg_read(UC_ARM_REG_R3) << 32)
        size = self.uc.reg_read(UC_ARM_REG_R4) | (self.uc.reg_read(UC_ARM_REG_R5) << 32)
        self.emu.unmap(addr, size)
        self.mret(0)

    def trap_mach_vm_protect(self): self.mret(0)

    def trap_mach_port_allocate(self):
        task, right, name_p = self.args(3)
        kind = {1: 'receive', 3: 'set', 4: 'dead'}.get(right, f'right{right}')
        self.emu.w32(name_p, self.new_port(kind))
        self.mret(0)

    def trap_mach_port_deallocate(self): self.mret(0)
    def trap_mach_port_mod_refs(self): self.mret(0)
    def trap_mach_port_insert_right(self): self.mret(0)
    def trap_mach_port_destroy(self): self.mret(0)
    def trap_mach_port_move_member(self):
        task, member, after = self.args(3)
        self.pr.move_member(member, after)
        self.mret(0)

    def trap_mach_port_insert_member(self):
        task, name, pset = self.args(3)
        self.pr.insert_member(name, pset)
        self.mret(0)

    def trap_mach_port_extract_member(self):
        task, name, pset = self.args(3)
        self.pr.extract_member(name, pset)
        self.mret(0)
    def trap_pid_for_task(self):
        t, p = self.args(2); self.emu.w32(p, 1234); self.mret(0)
    def yield_cpu(self):
        """Let the other threads run: the caller is runnable again straight away, but goes to
        the back of the round robin. Spin locks call this when they have spun for a while."""
        self.emu.sched.block('yield', lambda: True, lambda timed_out: self.mret(0))

    def trap_thread_switch(self):
        thread, option, ms = self.args(3)
        if option == 2 and ms:            # SWITCH_OPTION_WAIT: sleep for the time given
            self.emu.sched.block('thread_switch wait', lambda: False, lambda timed_out: self.mret(0), ms / 1000)
        else:
            self.yield_cpu()

    def trap_swtch_pri(self): self.yield_cpu()
    def trap_swtch(self): self.yield_cpu()
    def sem(self, name):
        p = self.pr.get(name)
        if p is None:
            p = self.pr.by_name.setdefault(name, __import__('ports').Port(name, 'semaphore'))
        return p

    def trap_semaphore_signal(self):
        self.sem(self.args(1)[0]).value += 1
        self.mret(0)

    def trap_semaphore_signal_all(self):
        p = self.sem(self.args(1)[0])
        waiters = sum(1 for t in self.emu.sched.threads if t.ready and getattr(t, 'sem', None) is p)
        p.value += waiters
        self.mret(0)

    def wait_on(self, p, timeout=None):
        if p.value > 0:
            p.value -= 1
            self.mret(0)
            return
        if timeout == 0:
            self.mret(49)          # KERN_OPERATION_TIMED_OUT
            return
        self.cur.sem = p
        def complete(timed_out):
            self.cur.sem = None
            if timed_out:
                self.mret(49)
            else:
                p.value -= 1
                self.mret(0)
        self.emu.sched.block(f'semaphore {p.name:#x}', lambda: p.value > 0, complete, timeout)

    def sys___semwait_signal(self):
        """nanosleep/usleep and the cancelable condition waits: wait on cond_sem until it is
        signalled or the timeout passes, after signalling mutex_sem. 64-bit tv_sec is r4:r5
        (Apple armv7 ABI), tv_nsec r6. Times out with ETIMEDOUT, as usleep expects."""
        from sched import CLOCK
        cond, mutex, has_timeout, relative, sec_lo, sec_hi, nsec = self.args(7)
        if mutex:
            self.sem(mutex).value += 1
        timeout = None
        if has_timeout:
            t = (sec_lo | (sec_hi << 32)) + nsec / 1e9
            timeout = t if relative else max(0.0, t - CLOCK.wall())
        p = self.sem(cond) if cond else None
        if p is not None and p.value > 0:
            p.value -= 1
            self.ret(0)
            return
        if timeout == 0:
            self.ret(60, True)
            return
        def complete(timed_out):
            if timed_out:
                self.ret(60, True)
            else:
                p.value -= 1
                self.ret(0)
        self.emu.sched.block(f'semwait {cond:#x}', (lambda: p.value > 0) if p else (lambda: False), complete, timeout)

    def trap_semaphore_wait(self):
        self.wait_on(self.sem(self.args(1)[0]))

    def trap_semaphore_wait_signal(self):
        wait, signal = self.args(2)
        self.sem(signal).value += 1
        self.wait_on(self.sem(wait))

    def trap_semaphore_timedwait(self):
        name, sec, nsec = self.args(3)
        self.wait_on(self.sem(name), sec + nsec / 1e9)

    def trap_semaphore_timedwait_signal(self):
        wait, signal, sec, nsec = self.args(4)
        self.sem(signal).value += 1
        self.wait_on(self.sem(wait), sec + nsec / 1e9)

    def trap_mk_timer_create(self): self.mret(self.new_port('receive'))

    def trap_mk_timer_destroy(self): self.mret(0)

    def trap_mk_timer_arm(self):
        name = self.uc.reg_read(UC_ARM_REG_R0)
        expire = self.uc.reg_read(UC_ARM_REG_R1) | (self.uc.reg_read(UC_ARM_REG_R2) << 32)   # uint64 in r1:r2 (Apple armv7 ABI: no even-pair alignment)
        p = self.pr.get(name)
        if p is None:
            self.mret(15); return
        p.timer_deadline = expire / 24_000_000
        self.mret(0)

    def trap_mk_timer_cancel(self):
        name, result_p = self.args(2)
        p = self.pr.get(name)
        if p is not None:
            if result_p:
                d = p.timer_deadline or 0
                self.emu.write(result_p, struct.pack('<Q', int(d * 24_000_000)))
            p.timer_deadline = None
        self.mret(0)

    def trap_mach_msg(self):
        from ports import MACH_SEND_MSG, MACH_RCV_MSG, MACH_RCV_TIMEOUT, MACH_RCV_TIMED_OUT, MACH_RCV_INVALID_NAME
        msg, option, send_size, rcv_size, rcv_name, timeout, notify = self.args(7)
        if option & MACH_SEND_MSG:
            remote = self.emu.r32(msg + 8)
            if self.emu.trace_sys:
                print(f'[ipc] t{self.cur.tid} send id {self.emu.r32(msg + 20)} to {remote:#x} '
                      f'(port {"known" if self.pr.get(remote) else "unknown"}, queue ok {self.pr.can_queue(remote)})')
            server = getattr(self.pr.get(remote), 'server', None)
            if server is not None:
                from services.base import Request
                req = Request(self.emu.read(msg, send_size))
                for r in server.handle(req):
                    if isinstance(r, tuple):
                        r, ool = r
                    else:
                        ool = []
                    dest = struct.unpack_from('<I', r, 12)[0]
                    if self.pr.get(dest) is not None:
                        self.pr.get(dest).queue.append((r, ool, server.token))
                if not option & MACH_RCV_MSG:
                    self.mret(0)
                    return
            elif self.pr.can_queue(remote):
                self.pr.send(msg, send_size)
                if self.emu.trace:
                    print(f'[ipc] thread {self.cur.tid} sent id {self.emu.r32(msg + 20)} to {remote:#x}')
                if not option & MACH_RCV_MSG:
                    self.mret(0)
                    return
            else:
                self.mret(self.mach.msg(msg, option, send_size, rcv_size, rcv_name, timeout, notify))
                return
        if not option & MACH_RCV_MSG:
            self.mret(0)
            return
        if self.pr.get(rcv_name) is None:
            self.mret(MACH_RCV_INVALID_NAME)
            return
        self.launcher.before_receive(rcv_name)
        ready = self.pr.pending(rcv_name)
        if ready:
            self.mret(self.pr.deliver(ready, msg, rcv_size, option))
            return
        limit = timeout / 1000 if option & MACH_RCV_TIMEOUT else None
        if limit == 0:
            self.mret(MACH_RCV_TIMED_OUT)
            return
        from sched import CLOCK
        end = CLOCK.now() + limit if limit is not None else None

        def wait():
            # Wake for a message, the receive timeout, or the next armed timer among the ports.
            cands = [x for x in (end, self.pr.next_timer(rcv_name)) if x is not None]
            when = min(cands) if cands else None
            self.emu.sched.block(f'mach_msg receive on {rcv_name:#x}', lambda: self.pr.pending(rcv_name) is not None,
                                 complete, None if when is None else max(0.0, when - CLOCK.now()))

        def complete(timed_out):
            ready = self.pr.pending(rcv_name)
            if ready is not None:
                self.mret(self.pr.deliver(ready, msg, rcv_size, option))
            elif end is not None and CLOCK.now() >= end:
                self.mret(MACH_RCV_TIMED_OUT)
            else:
                wait()
        wait()
    trap_mach_msg_overwrite = trap_mach_msg
