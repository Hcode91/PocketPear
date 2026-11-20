"""Mach ports owned by the one guest task, and message passing between its threads.

Kernel objects (task, thread, host, clock, semaphore...) are ports whose messages are MIG
requests answered by the kernel (mach.py). Ports the task holds a receive right for get a
message queue; port sets gather them for one receive.
"""
import struct
import time

MACH_SEND_MSG, MACH_RCV_MSG = 0x1, 0x2
MACH_SEND_TIMEOUT, MACH_RCV_TIMEOUT = 0x10, 0x100
MACH_RCV_LARGE = 0x4
MACH_RCV_TIMED_OUT = 0x10004003
MACH_RCV_TOO_LARGE = 0x10004004
MACH_RCV_INVALID_NAME = 0x10004002
MACH_SEND_INVALID_DEST = 0x10000003
MACH_MSGH_BITS_COMPLEX = 0x80000000

# dispositions a sender names -> what the receiver gets
RECEIVED = {16: 16, 17: 17, 18: 18, 19: 17, 20: 17, 21: 18}   # MOVE_RECEIVE, MOVE_SEND, MOVE_SEND_ONCE, COPY_SEND, MAKE_SEND, MAKE_SEND_ONCE
TRAILER_SIZES = {0: 8, 1: 12, 2: 20, 3: 52, 4: 56, 5: 60, 6: 68, 7: 68}

# audit tokens: auid, euid, egid, ruid, rgid, pid, asid, pidversion
TOKEN_KERNEL = (0, 0, 0, 0, 0, 0, 0, 0)
TOKEN_LAUNCHD = (0xffffffff, 0, 0, 0, 0, 1, 100000, 0)
TOKEN_SYSTEM = (0xffffffff, 501, 501, 501, 501, 50, 100000, 0)     # a system daemon (mobile)
TOKEN_APP = (501, 501, 501, 501, 501, 1234, 100000, 0)


def trailer(option, seqno=0, token=TOKEN_KERNEL):
    """The trailer the receive option asks for: type 0, size, seqno, security token (uid, gid),
    audit token, then zeros for the context and labels."""
    size = TRAILER_SIZES.get((option >> 24) & 0xf, 8)
    t = struct.pack('<III', 0, size, seqno) + struct.pack('<II', token[1], token[2]) + struct.pack('<8I', *token)
    t += bytes(max(0, size - len(t)))
    return t[:size]


class Port:
    def __init__(self, name, kind):
        self.name = name
        self.kind = kind          # 'receive', 'set', or a kernel object kind
        self.queue = []           # received messages: (bytes, ool list)
        self.members = set()      # for a set
        self.sets = set()         # port sets this port belongs to (a port may be in several)
        self.context = 0
        self.value = 0            # semaphore count
        self.dead = False
        self.server = None        # a services.base.MigServer answering messages sent here
        self.timer_deadline = None  # an armed mk_timer's expiry, in guest seconds
        self.label = ''

    def __repr__(self):
        return f'{self.kind}:{self.name:#x}'


class Ports:
    def __init__(self, kernel):
        self.k = kernel
        self.emu = kernel.emu
        self.by_name = {}
        self.next = 0x103

    def new(self, kind):
        name = self.next
        self.next += 0x100
        self.by_name[name] = Port(name, kind)
        return name

    def get(self, name):
        return self.by_name.get(name)

    def kind(self, name):
        p = self.by_name.get(name)
        return p.kind if p else None

    # ------------------------------------------------------------ sets
    def move_member(self, member, set_name):
        """mach_port_move_member: leave every set, then join set_name (0 = none)."""
        p = self.by_name.get(member)
        if not p:
            return
        for old in list(p.sets):
            self.extract_member(member, old)
        if set_name:
            self.insert_member(member, set_name)

    def insert_member(self, member, set_name):
        """mach_port_insert_member: join one more set. CFRunLoop puts its wake-up port in the
        port set of every mode, so membership in several sets is the normal case."""
        p, ps = self.by_name.get(member), self.by_name.get(set_name)
        if p and ps:
            p.sets.add(set_name)
            ps.members.add(member)

    def extract_member(self, member, set_name):
        p, ps = self.by_name.get(member), self.by_name.get(set_name)
        if p:
            p.sets.discard(set_name)
        if ps:
            ps.members.discard(member)

    def fire_timer(self, p):
        """An armed mk_timer whose time has come queues its expiry message."""
        if p.timer_deadline is not None:
            from sched import CLOCK
            if CLOCK.now() >= p.timer_deadline:
                p.timer_deadline = None
                msg = struct.pack('<6I', 0x12, 48, 0, p.name, 0, 0) + bytes(24)
                p.queue.append((msg, [], TOKEN_KERNEL))

    def pending(self, name):
        """The port (itself or a set member) with a message waiting, or None."""
        p = self.by_name.get(name)
        if not p:
            return None
        if p.kind == 'set':
            for m in sorted(p.members):
                q = self.by_name.get(m)
                if q:
                    self.fire_timer(q)
                    if q.queue:
                        return q
            return None
        self.fire_timer(p)
        return p if p.queue else None

    def next_timer(self, name):
        """The earliest armed timer deadline among a port or a set's members."""
        p = self.by_name.get(name)
        if not p:
            return None
        ports = [self.by_name.get(m) for m in p.members] if p.kind == 'set' else [p]
        ds = [q.timer_deadline for q in ports if q is not None and q.timer_deadline is not None]
        return min(ds) if ds else None

    # ------------------------------------------------------------ send / receive
    def can_queue(self, name):
        p = self.by_name.get(name)
        return p is not None and p.kind in ('receive', 'reply')

    def send(self, msg_p, size):
        """Copy a message out of guest memory onto its destination port's queue."""
        raw = bytearray(self.emu.read(msg_p, size))
        bits, size_, remote, local, voucher, mid = struct.unpack_from('<6I', raw, 0)
        dest = self.by_name[remote]
        ool = []
        if bits & MACH_MSGH_BITS_COMPLEX:
            count, = struct.unpack_from('<I', raw, 24)
            o = 28
            for _ in range(count):
                w0, w1, w2 = struct.unpack_from('<III', raw, o)
                dtype = w2 >> 24
                if dtype == 0:                       # port: disposition becomes what the receiver holds
                    disp = (w2 >> 16) & 0xff
                    w2 = (w2 & 0xff00ffff) | (RECEIVED.get(disp, disp) << 16)
                    struct.pack_into('<I', raw, o + 8, w2)
                    o += 12
                elif dtype in (1, 3):                # out-of-line memory: copy now, place on receive
                    ool.append((o, self.emu.read(w0, w1) if w1 else b''))
                    o += 12
                elif dtype == 2:                     # out-of-line ports
                    ool.append((o, self.emu.read(w0, 4 * w1) if w1 else b''))
                    o += 12
                else:
                    o += 12
        rdisp, ldisp = bits & 0xff, (bits >> 8) & 0xff
        new_bits = (bits & MACH_MSGH_BITS_COMPLEX) | RECEIVED.get(ldisp, 0) | (RECEIVED.get(rdisp, rdisp) << 8)
        # The receiver sees the reply port as remote and its own port as local.
        struct.pack_into('<6I', raw, 0, new_bits, size_, local, remote, 0, mid)
        dest.queue.append((bytes(raw), ool, TOKEN_APP))

    def deliver(self, port, buf_p, buf_size, option):
        """Write the first message on port into the receive buffer. Returns a kern_return."""
        raw, ool, token = port.queue[0]
        tr = trailer(option, token=token)
        if len(raw) + len(tr) > buf_size:
            if option & MACH_RCV_LARGE:
                self.emu.w32(buf_p + 4, len(raw) + len(tr))
                return MACH_RCV_TOO_LARGE
            port.queue.pop(0)
            return MACH_RCV_TOO_LARGE
        port.queue.pop(0)
        raw = bytearray(raw)
        for off, data in ool:
            if data:
                a = self.emu.find_free(len(data))
                self.emu.map(a, len(data))
                self.emu.write(a, data)
            else:
                a = 0
            struct.pack_into('<I', raw, off, a)
        self.emu.write(buf_p, raw)
        self.emu.write(buf_p + len(raw), tr)
        return 0
