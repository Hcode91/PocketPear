"""Guest threads and a cooperative scheduler.

Every guest thread is a saved Unicorn register context. The run loop restores one, runs it for
a time slice (an instruction count) or until it blocks in a system call, saves it, and picks the
next runnable thread. A blocking call marks the thread blocked with a wake condition and a
completion that writes the call's return registers once it can go on.
"""
import os
import time

EPOCH = 1384000000.0          # the guest's wall clock starts here (November 2013)


class Clock:
    """Guest time. By default it is virtual: each time slice advances it a little and an idle
    machine jumps straight to its next deadline, so a run is the same every time. With
    PP_REALTIME set it follows the host clock instead."""
    def __init__(self):
        self.real = bool(os.environ.get('PP_REALTIME'))
        self.t = 0.0
        self.base = time.monotonic()

    def now(self):
        if self.real:
            return time.monotonic() - self.base
        return self.t

    def tick(self, dt):
        if not self.real:
            self.t += dt
        else:
            pass

    def wall(self):
        return EPOCH + self.now()

    def advance_to(self, when):
        if self.real:
            time.sleep(max(0, min(when - self.now(), 0.05)))
        else:
            self.t = max(self.t, when)

    def nudge(self):
        """Each time query moves virtual time on a little so loops reading it see it move."""
        if not self.real:
            self.t += 1e-6


CLOCK = Clock()


class Thread:
    _next = 1

    def __init__(self, ctx, name, port):
        self.tid = Thread._next
        Thread._next += 1
        self.ctx = ctx
        self.name = name
        self.port = port            # its Mach thread port name
        self.ready = None           # None when runnable, else a function -> bool
        self.deadline = None        # monotonic time at which a timed wait gives up
        self.complete = None        # called on wake: complete(timed_out)
        self.why = ''
        self.dead = False
        self.pthread = 0

    def __repr__(self):
        return f'<thread {self.tid} {self.name}{" blocked: " + self.why if self.ready else ""}>'


class Scheduler:
    def __init__(self, emu):
        self.emu = emu
        self.threads = []
        self.cur = None
        self.rr = 0

    def add(self, t):
        self.threads.append(t)

    def block(self, why, ready, complete, timeout=None):
        """Called from a system call handler: stop the current thread until ready() is true
        or the timeout (seconds) passes; complete(timed_out) then writes the return value."""
        t = self.cur
        t.ready, t.complete, t.why = ready, complete, why
        t.deadline = (CLOCK.now() + timeout) if timeout is not None else None
        self.emu.uc.emu_stop()

    def runnable(self, t, now):
        if t.dead or getattr(t, 'suspended', 0):
            return False
        if t.ready is None:
            return True
        if t.ready():
            return 'ready'
        if t.deadline is not None and now >= t.deadline:
            return 'timeout'
        return False

    def pick(self):
        """The next thread to run (round robin), waking it if its wait is over. Sleeps when
        every thread waits on a deadline; returns None when nothing can ever run again."""
        while True:
            now = CLOCK.now()
            n = len(self.threads)
            for i in range(n):
                t = self.threads[(self.rr + i) % n]
                state = self.runnable(t, now)
                if state:
                    self.rr = (self.rr + i + 1) % n
                    return t, state
            deadlines = [t.deadline for t in self.threads if not t.dead and t.ready and t.deadline is not None]
            ext = self.emu.next_external() if hasattr(self.emu, 'displaylinks') else None
            if ext is not None and (not deadlines or ext <= min(deadlines)):
                CLOCK.advance_to(ext)          # a display link tick or a touch is next
                return None, 'external'
            if not deadlines:
                return None, None
            CLOCK.advance_to(min(deadlines))

    def wake_state(self, t, state):
        """Restore t's registers and finish the call it was blocked in."""
        self.emu.uc.context_restore(t.ctx)
        if t.ready is not None:
            complete = t.complete
            t.ready = t.complete = t.deadline = None
            t.why = ''
            if complete:
                complete(state == 'timeout')
