"""High-level stand-ins for libsystem_c's pthread mutexes, condition variables and rwlocks.

Their real implementation sits on the kernel's psynch calls, whose sequence-word protocol is
large. Since guest threads are scheduled by us, it is simpler and exact to implement the
pthread calls themselves: each function's first instructions are replaced with
`svc #(0x5050 + n); bx lr`, and the svc lands here.
"""
import struct

from unicorn.arm_const import *

SVC_BASE = 0x5050       # ARM svc immediates
THUMB_BASE = 0x40       # Thumb svc immediates (8 bits; the system call itself is 0x80)
EBUSY, EPERM, ETIMEDOUT, EINVAL, EDEADLK = 16, 1, 60, 22, 11
FUNCS = ['pthread_mutex_lock', 'pthread_mutex_trylock', 'pthread_mutex_unlock',
         'pthread_cond_wait', 'pthread_cond_wait$NOCANCEL', 'pthread_cond_timedwait',
         'pthread_cond_timedwait$NOCANCEL', 'pthread_cond_timedwait_relative_np',
         'pthread_cond_signal', 'pthread_cond_broadcast', 'pthread_cond_signal_thread_np',
         'pthread_rwlock_rdlock', 'pthread_rwlock_tryrdlock', 'pthread_rwlock_wrlock',
         'pthread_rwlock_trywrlock', 'pthread_rwlock_unlock']


class Mutex:
    def __init__(self):
        self.owner = None
        self.count = 0


class Cond:
    def __init__(self):
        self.waiters = []          # threads waiting, in order
        self.woken = set()


class RWLock:
    def __init__(self):
        self.readers = {}          # tid -> count
        self.writer = None
        self.wcount = 0


class Pthreads:
    def __init__(self, emu):
        self.emu = emu
        self.uc = emu.uc
        self.mutexes = {}
        self.conds = {}
        self.rwlocks = {}

    def install(self, syms_by_name):
        n = 0
        for i, f in enumerate(FUNCS):
            a = syms_by_name.get('_' + f)
            if a is None:
                print(f'[hle] {f} not found')
                continue
            if a in self.emu.syms.thumb:
                self.emu.write(a, struct.pack('<HH', 0xDF00 | (THUMB_BASE + i), 0x4770))    # svc #n; bx lr
            else:
                self.emu.write(a, struct.pack('<II', 0xEF000000 | (SVC_BASE + i), 0xE12FFF1E))
            n += 1
        print(f'[hle] {n} pthread functions replaced')

    # ------------------------------------------------------------ plumbing
    def args(self, n):
        return [self.uc.reg_read(UC_ARM_REG_R0 + i) for i in range(n)]

    def ret(self, v):
        self.uc.reg_write(UC_ARM_REG_R0, v & 0xffffffff)

    @property
    def sched(self):
        return self.emu.sched

    def call(self, index):
        name = FUNCS[index].split('$')[0]
        getattr(self, name)()

    def mutex(self, a):
        m = self.mutexes.get(a)
        if m is None:
            m = self.mutexes[a] = Mutex()
        return m

    def mutex_type(self, a):
        """0 normal, 1 error-checking, 2 recursive: bits 2-3 of the options word."""
        try:
            sig, _, opts = struct.unpack('<III', self.emu.read(a, 12))
        except Exception:
            return 0
        if sig == 0x32AAABA2:                     # PTHREAD_RECURSIVE_MUTEX_INITIALIZER
            return 2
        if sig == 0x32AAABA1:                     # PTHREAD_ERRORCHECK_MUTEX_INITIALIZER
            return 1
        return (opts >> 2) & 3

    def recursive(self, a):
        return self.mutex_type(a) == 2

    # ------------------------------------------------------------ mutexes
    def take(self, m):
        m.owner, m.count = self.sched.cur.tid, 1

    def pthread_mutex_lock(self):
        a, = self.args(1)
        m = self.mutex(a)
        me = self.sched.cur.tid
        if m.owner is None:
            self.take(m); self.ret(0); return
        if m.owner == me:
            kind = self.mutex_type(a)
            if kind == 2:                         # recursive
                m.count += 1; self.ret(0); return
            if kind != 1:                         # a normal mutex would deadlock here
                print(f'[hle] thread {me} relocks normal mutex {a:#x}; answering EDEADLK')
            self.ret(EDEADLK); return

        def complete(timed_out):
            if m.owner is None:
                self.take(m); self.ret(0)
            else:
                self.sched.block(f'mutex {a:#x}', lambda: m.owner is None, complete)
        self.sched.block(f'mutex {a:#x}', lambda: m.owner is None, complete)

    def pthread_mutex_trylock(self):
        a, = self.args(1)
        m = self.mutex(a)
        me = self.sched.cur.tid
        if m.owner is None:
            self.take(m); self.ret(0)
        elif m.owner == me and self.recursive(a):
            m.count += 1; self.ret(0)
        else:
            self.ret(EBUSY)

    def pthread_mutex_unlock(self):
        a, = self.args(1)
        m = self.mutex(a)
        if m.owner != self.sched.cur.tid:
            self.ret(EPERM); return
        m.count -= 1
        if m.count <= 0:
            m.owner, m.count = None, 0
        self.ret(0)

    # ------------------------------------------------------------ condition variables
    def wait(self, c_addr, m_addr, deadline):
        from sched import CLOCK
        c = self.conds.setdefault(c_addr, Cond())
        m = self.mutex(m_addr)
        t = self.sched.cur
        saved = m.count if m.owner == t.tid else 1
        m.owner, m.count = None, 0
        c.waiters.append(t)
        timeout = None if deadline is None else max(0.0, deadline - CLOCK.now())

        def relock(result):
            def done(_):
                if m.owner is None:
                    m.owner, m.count = t.tid, saved
                    self.ret(result)
                else:
                    self.sched.block(f'mutex {m_addr:#x} after cond', lambda: m.owner is None, done)
            done(False)

        def complete(timed_out):
            if t in c.waiters:
                c.waiters.remove(t)
            woke = t.tid in c.woken
            c.woken.discard(t.tid)
            relock(0 if woke or not timed_out else ETIMEDOUT)
        self.sched.block(f'cond {c_addr:#x}', lambda: t.tid in c.woken, complete, timeout)

    def pthread_cond_wait(self):
        c, m = self.args(2)
        self.wait(c, m, None)

    def pthread_cond_timedwait(self):
        from sched import CLOCK
        c, m, ts = self.args(3)
        sec, nsec = struct.unpack('<ii', self.emu.read(ts, 8))
        self.wait(c, m, CLOCK.now() + (sec + nsec / 1e9 - CLOCK.wall()))

    def pthread_cond_timedwait_relative_np(self):
        from sched import CLOCK
        c, m, ts = self.args(3)
        sec, nsec = struct.unpack('<ii', self.emu.read(ts, 8))
        self.wait(c, m, CLOCK.now() + sec + nsec / 1e9)

    def pthread_cond_signal(self):
        a, = self.args(1)
        c = self.conds.get(a)
        if c:
            for t in c.waiters:
                if t.tid not in c.woken:
                    c.woken.add(t.tid)
                    break
        self.ret(0)
    pthread_cond_signal_thread_np = pthread_cond_signal

    def pthread_cond_broadcast(self):
        a, = self.args(1)
        c = self.conds.get(a)
        if c:
            for t in c.waiters:
                c.woken.add(t.tid)
        self.ret(0)

    # ------------------------------------------------------------ rwlocks
    def rw(self, a):
        return self.rwlocks.setdefault(a, RWLock())

    def pthread_rwlock_rdlock(self):
        a, = self.args(1)
        l, me = self.rw(a), self.sched.cur.tid
        def ok(): return l.writer is None or l.writer == me
        def complete(_):
            if ok():
                l.readers[me] = l.readers.get(me, 0) + 1; self.ret(0)
            else:
                self.sched.block(f'rwlock {a:#x} read', ok, complete)
        complete(False)

    def pthread_rwlock_tryrdlock(self):
        a, = self.args(1)
        l, me = self.rw(a), self.sched.cur.tid
        if l.writer is None or l.writer == me:
            l.readers[me] = l.readers.get(me, 0) + 1; self.ret(0)
        else:
            self.ret(EBUSY)

    def pthread_rwlock_wrlock(self):
        a, = self.args(1)
        l, me = self.rw(a), self.sched.cur.tid
        def ok(): return (l.writer is None or l.writer == me) and not any(k != me for k in l.readers)
        def complete(_):
            if ok():
                l.writer = me; l.wcount += 1; self.ret(0)
            else:
                self.sched.block(f'rwlock {a:#x} write', ok, complete)
        complete(False)

    def pthread_rwlock_trywrlock(self):
        a, = self.args(1)
        l, me = self.rw(a), self.sched.cur.tid
        if (l.writer is None or l.writer == me) and not any(k != me for k in l.readers):
            l.writer = me; l.wcount += 1; self.ret(0)
        else:
            self.ret(EBUSY)

    def pthread_rwlock_unlock(self):
        a, = self.args(1)
        l, me = self.rw(a), self.sched.cur.tid
        if l.writer == me:
            l.wcount -= 1
            if l.wcount <= 0:
                l.writer, l.wcount = None, 0
        elif me in l.readers:
            l.readers[me] -= 1
            if l.readers[me] <= 0:
                del l.readers[me]
        else:
            self.ret(EPERM); return
        self.ret(0)
