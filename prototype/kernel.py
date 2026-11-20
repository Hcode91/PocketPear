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

BSD_NAMES = {1: 'exit', 3: 'read', 4: 'write', 5: 'open', 6: 'close', 20: 'getpid', 24: 'getuid', 25: 'geteuid',
             33: 'access', 43: 'getegid', 46: 'sigaction', 47: 'getgid', 48: 'sigprocmask', 54: 'ioctl', 73: 'munmap',
             74: 'mprotect', 75: 'madvise', 92: 'fcntl', 116: 'gettimeofday', 153: 'pread', 188: 'stat', 189: 'fstat',
             190: 'lstat', 194: 'getrlimit', 195: 'setrlimit', 197: 'mmap', 199: 'lseek', 202: 'sysctl', 294: 'shared_region_check_np',
             295: 'shared_region_map_np', 327: 'issetugid', 336: 'proc_info', 338: 'stat64', 339: 'fstat64', 340: 'lstat64',
             344: 'getdirentries64', 360: 'bsdthread_create', 366: 'bsdthread_register', 372: 'thread_selfid', 381: 'sandbox_ms',
             301: 'psynch_mutexwait', 302: 'psynch_mutexdrop', 303: 'psynch_cvbroad', 304: 'psynch_cvsignal', 305: 'psynch_cvwait',
             286: 'pthread_sigmask', 329: '__pthread_kill', 330: '__pthread_sigmask', 331: '__sigwait', 332: '__disable_threadsignal',
             333: '__pthread_markcancel', 334: '__pthread_canceled', 362: 'kqueue', 363: 'kevent', 357: 'getaudit_addr',
             340: 'lstat64', 396: 'read_nocancel', 397: 'write_nocancel', 398: 'open_nocancel', 399: 'close_nocancel',
             406: 'fcntl_nocancel', 159: 'unmount', 93: 'select', 97: 'socket', 98: 'connect', 137: 'rmdir', 136: 'mkdir',
             10: 'unlink', 128: 'rename', 58: 'readlink', 101: '__sysctl?', 220: 'getattrlist', 37: 'kill', 39: 'getppid',
             50: 'getlogin', 60: 'umask', 147: 'setsid', 169: 'csops', 323: 'getsgroups?', 274: 'sysctlbyname', 371: 'thread_selfid?'}

MACH_NAMES = {3: 'mach_absolute_time', 10: 'mach_vm_allocate', 12: 'mach_vm_deallocate', 14: 'mach_vm_protect',
              16: 'mach_port_allocate', 17: 'mach_port_destroy', 18: 'mach_port_deallocate', 19: 'mach_port_mod_refs',
              20: 'mach_port_move_member', 21: 'mach_port_insert_right', 22: 'mach_port_insert_member',
              23: 'mach_port_extract_member', 26: 'mach_reply_port', 27: 'thread_self', 28: 'task_self', 29: 'host_self',
              31: 'mach_msg', 32: 'mach_msg_overwrite', 33: 'semaphore_signal', 36: 'semaphore_wait', 89: 'mach_timebase_info',
              90: 'mach_wait_until', 91: 'mk_timer_create', 93: 'mk_timer_arm', 94: 'mk_timer_cancel', 58: 'pid_for_task',
              59: 'macx_swapon?', 61: 'thread_switch', 62: 'clock_sleep', 24: 'mach_port_construct?'}


class Kernel:
    def __init__(self, emu):
        self.emu = emu
        self.uc = emu.uc
        self.fds = {0: None, 1: None, 2: None}
        self.exited = None
        self.unimplemented = {}
        self.t0 = time.monotonic()
        self.ports = {}            # name -> description
        self.next_port = 0x103
        self.task_port = self.new_port('task')
        self.thread_port = self.new_port('thread')
        self.host_port = self.new_port('host')
        self.calls = 0
        self.semaphores = {}
        from mach import MachMsg
        self.mach = MachMsg(self)

    def new_port(self, what):
        p = self.next_port
        self.next_port += 0x100
        self.ports[p] = what
        return p

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
        num = uc.reg_read(UC_ARM_REG_R12)
        if num == 0x80000000:
            self.platform_call()
            return
        if num & 0x80000000:
            num -= 1 << 32
        self.calls += 1
        try:
            if num > 0:
                self.bsd(num)
            elif num < 0:
                self.mach_trap(-num)
            else:
                self.indirect()
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
        h()

    def sys_kill(self):
        pid, sig = self.args(2)
        print(f'[kernel] kill({pid}, {sig}) - the guest aborted')
        self.emu.backtrace()
        self.exited = 128 + sig
        self.uc.emu_stop()

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
    def sys_thread_selfid(self): self.ret(1)
    def sys_csops(self): self.ret(0)

    def sys_sigaction(self): self.ret(0)
    def sys_sigprocmask(self):
        how, set_p, oset_p = self.args(3)
        if oset_p:
            self.emu.w32(oset_p, 0)
        self.ret(0)
    sys___pthread_sigmask = sys_sigprocmask
    sys_pthread_sigmask = sys_sigprocmask
    def sys___disable_threadsignal(self): self.ret(0)
    def sys_bsdthread_register(self):
        self.thread_start, self.wq_start = self.args(2)
        self.ret(0)

    def sys_gettimeofday(self):
        tv, tz = self.args(2)
        now = time.time()
        if tv:
            self.emu.write(tv, struct.pack('<ii', int(now), int((now % 1) * 1e6)))
        # libsystem's gettimeofday stub returns seconds/usecs in r0/r1 as well
        self.ret(int(now), value_hi=int((now % 1) * 1e6))

    def sys_getrlimit(self):
        res, p = self.args(2)
        self.emu.write(p, struct.pack('<QQ', 0x7fffffffffffffff, 0x7fffffffffffffff) if res != 8 else struct.pack('<QQ', 256, 10240))
        self.ret(0)
    def sys_setrlimit(self): self.ret(0)

    # --- files
    def guest_path(self, p):
        return self.emu.cstr(p)

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
                fd = os.open(host, hflags, mode or 0o644)
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

    def hostfd(self, gfd):
        e = self.fds.get(gfd)
        return e[0] if e else None

    def sys_close(self):
        gfd, = self.args(1)
        e = self.fds.pop(gfd, None)
        if e:
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
        # 64-bit offset is passed in r4/r5 (aligned pair after the 3 args: r3 is padding)
        off = off_lo | (off_hi << 32) if False else (self.uc.reg_read(UC_ARM_REG_R4) | (self.uc.reg_read(UC_ARM_REG_R5) << 32))
        data = os.pread(fd, n, off)
        self.emu.write(buf, data)
        self.ret(len(data))

    def sys_write(self):
        gfd, buf, n = self.args(3)
        data = self.emu.read(buf, n)
        if gfd in (1, 2):
            text = data.decode('utf-8', 'replace')
            for line in text.rstrip('\n').split('\n'):
                print(f'[guest {"out" if gfd == 1 else "err"}] {line}')
            self.ret(n); return
        fd = self.hostfd(gfd)
        if fd is None:
            self.ret(EBADF, True); return
        self.ret(os.write(fd, data))
    sys_write_nocancel = sys_write

    def sys_lseek(self):
        gfd = self.args(1)[0]
        off = self.uc.reg_read(UC_ARM_REG_R2) | (self.uc.reg_read(UC_ARM_REG_R3) << 32)
        whence = self.uc.reg_read(UC_ARM_REG_R4)
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
            return struct.pack('<iHHQIIi' + 'ii' * 4 + 'qqiIIiqq', 1, mode & 0xffff, min(st.st_nlink, 0xffff), st.st_ino, 501, 501, 0,
                               int(st.st_atime), 0, int(st.st_mtime), 0, int(st.st_ctime), 0, int(st.st_mtime), 0,
                               st.st_size, (st.st_size + 511) // 512, 4096, 0, 0, 0, 0, 0)
        return struct.pack('<iIHHIIi' + 'ii' * 3 + 'qqiIIiqq', 1, st.st_ino & 0xffffffff, mode & 0xffff, min(st.st_nlink, 0xffff),
                           501, 501, 0, int(st.st_atime), 0, int(st.st_mtime), 0, int(st.st_ctime), 0,
                           st.st_size, (st.st_size + 511) // 512, 4096, 0, 0, 0, 0, 0)

    def do_stat(self, layout64, follow=True, fd=None):
        if fd is None:
            path_p, buf = self.args(2)
            path = self.guest_path(path_p)
            host = self.emu.host_path(path)
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

    def sys_access(self):
        path_p, mode = self.args(2)
        path = self.guest_path(path_p)
        ok = os.path.exists(self.emu.host_path(path))
        self.log(f'access({path!r}) = {ok}')
        self.ret(0) if ok else self.ret(ENOENT, True)

    def sys_readlink(self):
        path_p, buf, size = self.args(3)
        host = self.emu.host_path(self.guest_path(path_p))
        if not os.path.islink(host):
            self.ret(EINVAL, True); return
        t = os.readlink(host).encode()[:size]
        self.emu.write(buf, t); self.ret(len(t))

    def sys_ioctl(self): self.ret(ENOTSUP if False else 25, True)   # ENOTTY

    # --- memory
    def sys_mmap(self):
        addr, length, prot, flags, gfd = self.args(5)
        off = self.uc.reg_read(UC_ARM_REG_R6) | (self.uc.reg_read(UC_ARM_REG_R8) << 32)
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
        key = tuple(mib[:2])
        if key in table:
            return table[key]
        if mib[:2] == [0, 3]:
            return None       # name2mib, handled by sysctlbyname path later
        return None

    def sys_proc_info(self): self.ret(EINVAL, True)
    def sys_kqueue(self):
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
    def trap_thread_self(self): self.mret(self.thread_port)
    def trap_host_self(self): self.mret(self.host_port)
    def trap_mach_reply_port(self): self.mret(self.new_port('reply'))

    def trap_mach_absolute_time(self):
        t = int((time.monotonic() - self.t0) * 24_000_000)    # 24 MHz timebase
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
        self.emu.w32(name_p, self.new_port(f'right{right}'))
        self.mret(0)

    def trap_mach_port_deallocate(self): self.mret(0)
    def trap_mach_port_mod_refs(self): self.mret(0)
    def trap_mach_port_insert_right(self): self.mret(0)
    def trap_mach_port_destroy(self): self.mret(0)
    def trap_mach_port_move_member(self): self.mret(0)
    def trap_mach_port_insert_member(self): self.mret(0)
    def trap_mach_port_extract_member(self): self.mret(0)
    def trap_pid_for_task(self):
        t, p = self.args(2); self.emu.w32(p, 1234); self.mret(0)
    def trap_thread_switch(self): self.mret(0)
    def trap_semaphore_signal(self):
        p = self.args(1)[0]
        self.semaphores[p] = self.semaphores.get(p, 0) + 1
        self.mret(0)

    def trap_semaphore_wait(self):
        # Single-threaded for now: waiting on an empty semaphore would block for ever.
        p = self.args(1)[0]
        if self.semaphores.get(p, 0) > 0:
            self.semaphores[p] -= 1
            self.mret(0)
        else:
            print(f'[kernel] semaphore_wait on empty semaphore {p:#x} (would block)')
            self.emu.backtrace()
            self.mret(0)
    def trap_mk_timer_create(self): self.mret(self.new_port('timer'))
    def trap_mk_timer_arm(self): self.mret(0)
    def trap_mk_timer_cancel(self): self.mret(0)

    def trap_mach_msg(self):
        self.mret(self.mach.msg(*self.args(7)))
    trap_mach_msg_overwrite = trap_mach_msg
