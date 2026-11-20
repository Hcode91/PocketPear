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


class Port:
    def __init__(self, name, kind):
        self.name = name
        self.kind = kind          # 'receive', 'set', or a kernel object kind
        self.queue = []           # received messages: (bytes, ool list)
        self.members = set()      # for a set
        self.in_set = None
        self.context = 0
        self.value = 0            # semaphore count
        self.dead = False

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
        p = self.by_name.get(member)
        if not p:
            return
        if p.in_set is not None and p.in_set in self.by_name:
            self.by_name[p.in_set].members.discard(member)
        p.in_set = set_name or None
        if set_name and set_name in self.by_name:
            self.by_name[set_name].members.add(member)

    def pending(self, name):
        """The port (itself or a set member) with a message waiting, or None."""
        p = self.by_name.get(name)
        if not p:
            return None
        if p.kind == 'set':
            for m in sorted(p.members):
                q = self.by_name.get(m)
                if q and q.queue:
                    return q
            return None
        return p if p.queue else None

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
        dest.queue.append((bytes(raw), ool))

    def deliver(self, port, buf_p, buf_size, option):
        """Write the first message on port into the receive buffer. Returns a kern_return."""
        raw, ool = port.queue[0]
        trailer = TRAILER_SIZES.get((option >> 24) & 0xf, 8)
        if len(raw) + trailer > buf_size:
            if option & MACH_RCV_LARGE:
                struct.pack_into('<I', bytearray(4), 0, 0)
                self.emu.w32(buf_p + 4, len(raw) + trailer)
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
        self.emu.write(buf_p + len(raw), struct.pack('<II', 0, trailer) + bytes(trailer - 8))
        return 0
