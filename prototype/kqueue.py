"""kqueue / kevent / kevent64 over the guest's ports, timers and user events."""
import struct
import time
from sched import CLOCK

EVFILT_READ, EVFILT_WRITE, EVFILT_PROC, EVFILT_SIGNAL, EVFILT_TIMER, EVFILT_MACHPORT, EVFILT_USER, EVFILT_VM = -1, -2, -5, -6, -7, -8, -10, -12
EV_ADD, EV_DELETE, EV_ENABLE, EV_DISABLE, EV_ONESHOT, EV_CLEAR, EV_RECEIPT, EV_DISPATCH = 1, 2, 4, 8, 0x10, 0x20, 0x40, 0x80
EV_ERROR, EV_EOF = 0x4000, 0x8000
NOTE_TRIGGER, NOTE_FFCTRLMASK, NOTE_FFLAGSMASK = 0x01000000, 0xc0000000, 0x00ffffff
NOTE_SECONDS, NOTE_USECONDS, NOTE_NSECONDS, NOTE_ABSOLUTE = 1, 2, 4, 8
MACH_RCV_MSG = 2


class Knote:
    def __init__(self, ident, filt):
        self.ident, self.filter = ident, filt
        self.flags = self.fflags = self.data = self.udata = 0
        self.ext = [0, 0]
        self.enabled = True
        self.triggered = False      # user events
        self.deadline = None        # timers
        self.interval = 0
        self.fired = 0


class KQueue:
    def __init__(self, k):
        self.k = k
        self.notes = {}

    # ------------------------------------------------------------ registering
    def change(self, ident, filt, flags, fflags, data, udata, ext):
        key = (ident, filt)
        kn = self.notes.get(key)
        if flags & EV_DELETE:
            self.notes.pop(key, None)
            return 0 if kn else 2          # ENOENT
        if kn is None:
            if not flags & EV_ADD:
                return 2
            kn = self.notes[key] = Knote(ident, filt)
            # EV_ONESHOT, EV_CLEAR and EV_DISPATCH are fixed when the note is added; later
            # changes (a NOTE_TRIGGER, say) do not repeat them and must not remove them.
            kn.flags = flags & (EV_ONESHOT | EV_CLEAR | EV_DISPATCH)
        kn.udata = udata
        kn.ext = list(ext)
        if flags & EV_ENABLE or flags & EV_ADD:
            kn.enabled = True
        if flags & EV_DISABLE:
            kn.enabled = False
        if filt == EVFILT_USER:
            ctrl = fflags & NOTE_FFCTRLMASK
            v = fflags & NOTE_FFLAGSMASK
            if ctrl == 0x40000000: kn.fflags &= v
            elif ctrl == 0x80000000: kn.fflags |= v
            elif ctrl == 0xc0000000: kn.fflags = v
            if fflags & NOTE_TRIGGER:
                kn.triggered = True
        elif filt == EVFILT_TIMER:
            unit = 1e-3
            if fflags & NOTE_SECONDS: unit = 1
            elif fflags & NOTE_USECONDS: unit = 1e-6
            elif fflags & NOTE_NSECONDS: unit = 1e-9
            if data < 0: data += 1 << 64
            if fflags & NOTE_ABSOLUTE:
                # absolute time in the same unit since the epoch
                kn.deadline = CLOCK.now() + max(0, data * unit - CLOCK.wall())
                kn.interval = 0
            else:
                kn.interval = data * unit
                kn.deadline = CLOCK.now() + kn.interval
            kn.fflags = fflags
        else:
            kn.fflags = fflags
        return 0

    # ------------------------------------------------------------ readiness
    def check(self, kn, now):
        if not kn.enabled:
            return None
        if kn.filter == EVFILT_USER:
            return (kn.fflags, 0) if kn.triggered else None
        if kn.filter == EVFILT_TIMER:
            if kn.deadline is not None and now >= kn.deadline:
                n = 1
                if kn.interval:
                    n = int((now - kn.deadline) / kn.interval) + 1
                return (0, n)
            return None
        if kn.filter == EVFILT_MACHPORT:
            p = self.k.pr.pending(kn.ident)
            return (0, len(p.queue[0][0])) if p else None
        if kn.filter == EVFILT_READ:
            e = self.k.fds.get(kn.ident)
            if isinstance(e, tuple):
                import os
                try:
                    cur = os.lseek(e[0], 0, 1); end = os.fstat(e[0]).st_size
                    return (0, end - cur) if end > cur else None
                except OSError:
                    return (0, 0)
            return None
        if kn.filter == EVFILT_WRITE:
            return (0, 0x10000)
        return None

    def ready_any(self):
        now = CLOCK.now()
        return any(self.check(kn, now) for kn in self.notes.values())

    def next_deadline(self):
        ds = [kn.deadline for kn in self.notes.values() if kn.enabled and kn.filter == EVFILT_TIMER and kn.deadline is not None]
        return min(ds) if ds else None

    def collect(self, nevents):
        """Events ready now, consuming them as their flags say."""
        out = []
        now = CLOCK.now()
        for key, kn in list(self.notes.items()):
            if len(out) >= nevents:
                break
            r = self.check(kn, now)
            if not r:
                continue
            fflags, data = r
            ext = list(kn.ext)
            if kn.filter == EVFILT_MACHPORT and kn.fflags & MACH_RCV_MSG and kn.ext[1]:
                p = self.k.pr.pending(kn.ident)
                size = len(p.queue[0][0])
                fflags = self.k.pr.deliver(p, kn.ext[0], kn.ext[1], kn.fflags)
                ext = [kn.ext[0], size]
                data = size
            out.append((kn.ident, kn.filter, kn.flags, fflags, data, kn.udata, ext))
            if self.k.emu.trace_sys:
                print(f'[kq] event filter {kn.filter} ident {kn.ident:#x} flags {kn.flags:#x} fflags {fflags:#x} data {data} interval {kn.interval}')
            if kn.filter == EVFILT_TIMER:
                if kn.interval:
                    kn.deadline += kn.interval * data
                else:
                    kn.deadline = None
            if kn.filter == EVFILT_USER and kn.flags & EV_CLEAR:
                kn.triggered = False
            if kn.flags & EV_ONESHOT:
                del self.notes[key]
            elif kn.flags & EV_DISPATCH:
                kn.enabled = False
        return out


KEVENT32 = '<IhHIiI'          # 20 bytes
KEVENT64 = '<QhHIqQQQ'        # 48 bytes


def install(Kernel):
    def sys_kqueue(self):
        gfd = 3
        while gfd in self.fds:
            gfd += 1
        self.fds[gfd] = KQueue(self)
        self.ret(gfd)

    def do_kevent(self, kq_fd, changes, nchanges, events, nevents, ts_p, wide):
        kq = self.fds.get(kq_fd)
        if not isinstance(kq, KQueue):
            self.ret(9, True)
            return
        fmt = KEVENT64 if wide else KEVENT32
        size = struct.calcsize(fmt)
        errors = []
        for i in range(nchanges):
            v = struct.unpack(fmt, self.emu.read(changes + i * size, size))
            if wide:
                ident, filt, flags, fflags, data, udata, e0, e1 = v
                ext = (e0, e1)
            else:
                ident, filt, flags, fflags, data, udata = v
                ext = (0, 0)
            err = kq.change(ident, filt, flags, fflags, data, udata, ext)
            if err or flags & EV_RECEIPT:
                errors.append((ident, filt, flags | EV_ERROR, 0, err, udata, ext))
            if self.emu.trace or self.emu.trace_sys:
                print(f'[kq] {kq_fd}: filter {filt} ident {ident:#x} flags {flags:#x} fflags {fflags:#x} data {data} -> {err}')

        def write_out(evs):
            for i, (ident, filt, flags, fflags, data, udata, ext) in enumerate(evs):
                if wide:
                    b = struct.pack(fmt, ident, filt, flags, fflags & 0xffffffff, data, udata, ext[0], ext[1])
                else:
                    b = struct.pack(fmt, ident & 0xffffffff, filt, flags, fflags & 0xffffffff, data, udata & 0xffffffff)
                self.emu.write(events + i * size, b)
            self.ret(len(evs))

        if errors:
            write_out(errors[:nevents])
            return
        if nevents == 0:
            self.ret(0)
            return
        evs = kq.collect(nevents)
        if evs:
            write_out(evs)
            return
        timeout = None
        if ts_p:
            sec, nsec = struct.unpack('<ii', self.emu.read(ts_p, 8))
            timeout = sec + nsec / 1e9
            if timeout <= 0:
                self.ret(0)
                return
        end = CLOCK.now() + timeout if timeout is not None else None

        def limit():
            d = kq.next_deadline()
            cands = [x for x in (d, end) if x is not None]
            return max(0, min(cands) - CLOCK.now()) if cands else None

        def complete(timed_out):
            evs = kq.collect(nevents)
            if evs or end is not None and CLOCK.now() >= end:
                write_out(evs)
            else:
                # a timer's moment came but nothing to report: wait again
                self.emu.sched.block(f'kevent on kq {kq_fd}', kq.ready_any, complete, limit())
        self.emu.sched.block(f'kevent on kq {kq_fd}', kq.ready_any, complete, limit())

    def sys_kevent(self):
        kq, ch, nch, ev, nev, ts = self.args(6)
        self.do_kevent(kq, ch, nch, ev, nev, ts, False)

    def sys_kevent64(self):
        kq, ch, nch, ev, nev, flags, ts = self.args(7)
        self.do_kevent(kq, ch, nch, ev, nev, ts, True)

    Kernel.sys_kqueue = sys_kqueue
    Kernel.do_kevent = do_kevent
    Kernel.sys_kevent = sys_kevent
    Kernel.sys_kevent64 = sys_kevent64
