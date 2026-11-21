"""AudioToolbox's audio queues (AudioQueueNewOutput and friends).

On a device they are a client of mediaserverd's AudioQueueServer, which is not here, so
AudioQueueNewOutput fails and games that do not check (SvR 2010's mixer goes straight on to
AudioQueueAllocateBuffer and reads the buffer it did not get) crash. These stand-ins keep the
queue's bookkeeping in guest memory exactly as the API describes, so the game's audio code runs.
Nothing is played yet, and enqueued buffers are not handed back through the output callback."""
import struct

from unicorn.arm_const import *

BUFFER_HEADER = 28     # AudioQueueBuffer: capacity, data, byte size, user data, pd capacity, pds, pd count

FUNCS = ['AudioQueueNewOutput', 'AudioQueueAllocateBuffer', 'AudioQueueAllocateBufferWithPacketDescriptions',
         'AudioQueueFreeBuffer', 'AudioQueueEnqueueBuffer', 'AudioQueueEnqueueBufferWithParameters',
         'AudioQueueStart', 'AudioQueuePrime', 'AudioQueueStop', 'AudioQueuePause', 'AudioQueueFlush',
         'AudioQueueReset', 'AudioQueueDispose', 'AudioQueueSetParameter', 'AudioQueueGetParameter',
         'AudioQueueSetProperty', 'AudioQueueGetProperty', 'AudioQueueGetPropertySize',
         'AudioQueueAddPropertyListener', 'AudioQueueRemovePropertyListener', 'AudioQueueGetCurrentTime',
         'AudioQueueCreateTimeline', 'AudioQueueDisposeTimeline', 'AudioQueueDeviceGetCurrentTime']


class Queue:
    def __init__(self, fmt, callback, user):
        self.fmt = fmt              # (rate, format id, flags, bytes/packet, frames/packet, bytes/frame, channels, bits, reserved)
        self.callback, self.user = callback, user
        self.running = False
        self.enqueued = []
        self.volume = 1.0


class AudioQueues:
    def __init__(self, emu):
        self.emu = emu
        self.uc = emu.uc
        self.queues = {}
        self.next_id = 0x51000001
        self.arena = self.arena_end = 0

    def install(self, stubs):
        names = dict(zip(self.emu.syms.names, self.emu.syms.addrs))
        made = 0
        for f in FUNCS:
            a = names.get('_' + f + ' (AudioToolbox)')
            if a is not None:
                stubs.replace(a, getattr(self, f), a in self.emu.syms.thumb)
                made += 1
        if made:
            print(f'[audio] {made} AudioQueue functions replaced (no sound output yet)')

    # ------------------------------------------------------------------ plumbing
    def arg(self, n):
        if n < 4:
            return self.uc.reg_read(UC_ARM_REG_R0 + n)
        return self.emu.r32(self.uc.reg_read(UC_ARM_REG_SP) + 4 * (n - 4))

    def ret(self, v=0):
        self.uc.reg_write(UC_ARM_REG_R0, v & 0xffffffff)

    def alloc(self, size):
        size = (size + 15) & ~15
        if self.arena + size > self.arena_end:
            n = max(0x40000, (size + 0xfff) & ~0xfff)
            self.arena = self.emu.find_free(n)
            self.emu.map(self.arena, n)
            self.arena_end = self.arena + n
        a = self.arena
        self.arena += size
        return a

    # ------------------------------------------------------------------ queues
    def AudioQueueNewOutput(self):
        fmt_p, callback, user, out = self.arg(0), self.arg(1), self.arg(2), self.arg(6)
        fmt = struct.unpack('<d8I', self.emu.read(fmt_p, 40)) if fmt_p else None
        q = self.next_id
        self.next_id += 1
        self.queues[q] = Queue(fmt, callback, user)
        if out:
            self.emu.w32(out, q)
        if fmt:
            print(f'[audio] output queue {q:#x}: {fmt[0]:.0f} Hz, {fmt[6]} channel(s), {fmt[7]} bits, format {fmt[1]:#x}')
        self.ret(0)

    def new_buffer(self, size, packets, out):
        a = self.alloc(BUFFER_HEADER + size + 16 * packets)
        data = a + BUFFER_HEADER
        pds = data + size if packets else 0
        self.emu.write(a, struct.pack('<7I', size, data, 0, 0, packets, pds, 0))
        if out:
            self.emu.w32(out, a)
        self.ret(0)

    def AudioQueueAllocateBuffer(self):
        self.new_buffer(self.arg(1), 0, self.arg(2))

    def AudioQueueAllocateBufferWithPacketDescriptions(self):
        self.new_buffer(self.arg(1), self.arg(2), self.arg(3))

    def AudioQueueFreeBuffer(self): self.ret(0)

    def AudioQueueEnqueueBuffer(self):
        q = self.queues.get(self.arg(0))
        if q is not None:
            q.enqueued.append(self.arg(1))
            del q.enqueued[:-64]
        self.ret(0)

    AudioQueueEnqueueBufferWithParameters = AudioQueueEnqueueBuffer

    def AudioQueueStart(self):
        q = self.queues.get(self.arg(0))
        if q is not None:
            q.running = True
        self.ret(0)

    def AudioQueuePrime(self):
        if self.arg(2):
            self.emu.w32(self.arg(2), 0)
        self.ret(0)

    def AudioQueueStop(self):
        q = self.queues.get(self.arg(0))
        if q is not None:
            q.running = False
            q.enqueued.clear()
        self.ret(0)

    def AudioQueuePause(self):
        q = self.queues.get(self.arg(0))
        if q is not None:
            q.running = False
        self.ret(0)

    def AudioQueueFlush(self): self.ret(0)

    def AudioQueueReset(self):
        q = self.queues.get(self.arg(0))
        if q is not None:
            q.enqueued.clear()
        self.ret(0)

    def AudioQueueDispose(self):
        self.queues.pop(self.arg(0), None)
        self.ret(0)

    def AudioQueueSetParameter(self):
        q = self.queues.get(self.arg(0))
        if q is not None and self.arg(1) == 1:        # kAudioQueueParam_Volume
            q.volume = struct.unpack('<f', struct.pack('<I', self.arg(2)))[0]
        self.ret(0)

    def AudioQueueGetParameter(self):
        q = self.queues.get(self.arg(0))
        if self.arg(2):
            v = q.volume if q is not None and self.arg(1) == 1 else 1.0
            self.emu.write(self.arg(2), struct.pack('<f', v))
        self.ret(0)

    def AudioQueueSetProperty(self): self.ret(0)

    def AudioQueueGetProperty(self):
        q = self.queues.get(self.arg(0))
        prop, data, size_p = self.arg(1), self.arg(2), self.arg(3)
        if prop == 0x6171726e and data:              # 'aqrn': kAudioQueueProperty_IsRunning
            self.emu.w32(data, int(bool(q and q.running)))
            if size_p:
                self.emu.w32(size_p, 4)
            self.ret(0)
        elif prop == 0x61716674 and data and q and q.fmt:   # 'aqft': the stream format
            self.emu.write(data, struct.pack('<d8I', *q.fmt))
            if size_p:
                self.emu.w32(size_p, 40)
            self.ret(0)
        else:
            self.ret(0x70726f70)                     # 'prop': kAudioQueueErr_InvalidProperty-ish

    def AudioQueueGetPropertySize(self):
        if self.arg(2):
            self.emu.w32(self.arg(2), 40 if self.arg(1) == 0x61716674 else 4)
        self.ret(0)

    def AudioQueueAddPropertyListener(self): self.ret(0)
    def AudioQueueRemovePropertyListener(self): self.ret(0)

    def AudioQueueGetCurrentTime(self):
        stamp = self.arg(2)
        if stamp:
            # AudioTimeStamp: sample time (double), host time (u64), rate scalar, word clock, SMPTE, flags
            self.emu.write(stamp, bytes(64))
        if self.arg(3):
            self.emu.w32(self.arg(3), 0)
        self.ret(0)

    def AudioQueueCreateTimeline(self):
        if self.arg(1):
            self.emu.w32(self.arg(1), 0x52000001)
        self.ret(0)

    def AudioQueueDisposeTimeline(self): self.ret(0)

    def AudioQueueDeviceGetCurrentTime(self):
        if self.arg(1):
            self.emu.write(self.arg(1), bytes(64))
        self.ret(0)
