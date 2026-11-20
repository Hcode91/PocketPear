"""CADisplayLink without a display server.

On a device a display link is ticked by vsync messages that backboardd's framebuffer sends to
QuartzCore. Nothing here sends those, and +[CADisplay mainDisplay] needs the render server's
display list, so the class is replaced: a link is a small object whose isa is the real
CADisplayLink class (so retain/release and respondsToSelector: work), and its ticks are
delivered the way vsync would arrive, on the main thread while its run loop waits.

Delivering a tick borrows the main thread while it is blocked in mach_msg: its blocked state is
saved, it calls [target selector:link], and the call returns into a stub that puts the wait
back exactly as it was. That is the same stack a real tick runs on (the run loop's source
handler, called from inside the wait), and it needs no help from the run loop itself.
"""
import struct

from unicorn.arm_const import *

from sched import CLOCK

REFRESH = 60.0


class Link:
    def __init__(self, target, sel):
        self.target, self.sel = target, sel
        self.interval = 1
        self.paused = False
        self.added = False
        self.due = None
        self.timestamp = 0.0


METHODS = {
    '+[CADisplayLink displayLinkWithTarget:selector:]': 'link_with_target',
    '+[CADisplayLink displayLinkWithDisplay:target:selector:]': 'link_with_display',
    '-[CADisplayLink addToRunLoop:forMode:]': 'add_to_runloop',
    '-[CADisplayLink removeFromRunLoop:forMode:]': 'remove_from_runloop',
    '-[CADisplayLink invalidate]': 'invalidate',
    '-[CADisplayLink setFrameInterval:]': 'set_frame_interval',
    '-[CADisplayLink frameInterval]': 'frame_interval',
    '-[CADisplayLink setPaused:]': 'set_paused',
    '-[CADisplayLink isPaused]': 'is_paused',
    '-[CADisplayLink timestamp]': 'get_timestamp',
    '-[CADisplayLink duration]': 'get_duration',
    '-[CADisplayLink dealloc]': 'dealloc',
}


class DisplayLinks:
    def __init__(self, emu):
        self.emu = emu
        self.uc = emu.uc
        self.links = {}
        self.cls = None
        self.saved = None          # the main thread's wait while it runs a tick
        self.ticks = 0
        self.arena = self.arena_end = 0
        self.ret_stub = None

    def install(self, stubs):
        names = dict(zip(self.emu.syms.names, self.emu.syms.addrs))
        made = 0
        for name, attr in METHODS.items():
            addr = names.get(name + ' (QuartzCore)')
            if addr is not None:
                stubs.replace(addr, getattr(self, attr), addr in self.emu.syms.thumb)
                made += 1
        self.cls = names.get('_OBJC_CLASS_$_CADisplayLink (QuartzCore)')
        # a page for link objects and the tick return stub
        page = self.emu.find_free(0x10000)
        self.emu.map(page, 0x10000)
        self.ret_stub = page
        stubs.replace(page, self.tick_return, False)
        self.arena, self.arena_end = page + 0x100, page + 0x10000
        print(f'[displaylink] {made} CADisplayLink methods replaced, class {self.cls and hex(self.cls)}')

    # -------------------------------------------------------------------- guest methods
    def args(self, n):
        return [self.uc.reg_read(UC_ARM_REG_R0 + i) for i in range(n)]

    def ret(self, v=0, hi=None):
        self.uc.reg_write(UC_ARM_REG_R0, v & 0xffffffff)
        if hi is not None:
            self.uc.reg_write(UC_ARM_REG_R1, hi & 0xffffffff)

    def ret_double(self, x):
        lo, hi = struct.unpack('<II', struct.pack('<d', x))
        self.ret(lo, hi)

    def new_link(self, target, sel):
        if self.arena + 32 > self.arena_end:
            self.ret(0)
            return
        obj = self.arena
        self.arena += 32
        self.emu.write(obj, struct.pack('<I', self.cls or 0) + bytes(28))
        self.links[obj] = Link(target, sel)
        print(f'[displaylink] link {obj:#x} -> target {target:#x} selector {self.emu.cstr(sel)!r}')
        self.ret(obj)

    def link_with_target(self):
        _cls, _cmd, target, sel = self.args(4)
        self.new_link(target, sel)

    def link_with_display(self):
        _cls, _cmd, _display, target = self.args(4)
        sp = self.uc.reg_read(UC_ARM_REG_SP)
        sel = struct.unpack('<I', self.emu.read(sp, 4))[0]
        self.new_link(target, sel)

    def link(self):
        return self.links.get(self.uc.reg_read(UC_ARM_REG_R0))

    def add_to_runloop(self):
        l = self.link()
        if l:
            l.added = True
            l.due = CLOCK.now() + l.interval / REFRESH
        self.ret(0)

    def remove_from_runloop(self):
        l = self.link()
        if l:
            l.added = False
        self.ret(0)

    def invalidate(self):
        l = self.link()
        if l:
            l.added = False
            l.target = 0
        self.ret(0)

    def dealloc(self):
        self.links.pop(self.uc.reg_read(UC_ARM_REG_R0), None)
        self.ret(0)

    def set_frame_interval(self):
        l = self.link()
        n = self.args(3)[2]
        if l and n >= 1:
            l.interval = n
        self.ret(0)

    def frame_interval(self):
        l = self.link()
        self.ret(l.interval if l else 1)

    def set_paused(self):
        l = self.link()
        if l:
            l.paused = bool(self.args(3)[2] & 0xff)
            if not l.paused:
                l.due = CLOCK.now() + l.interval / REFRESH
        self.ret(0)

    def is_paused(self):
        l = self.link()
        self.ret(1 if l and l.paused else 0)

    def get_timestamp(self):
        l = self.link()
        self.ret_double(l.timestamp if l else 0.0)

    def get_duration(self):
        self.ret_double(1 / REFRESH)

    # -------------------------------------------------------------------- ticking
    def active(self):
        return [(a, l) for a, l in self.links.items() if l.added and not l.paused and l.target]

    def next_due(self):
        """When the next tick is due, if any link is running and none is being delivered."""
        if self.saved is not None or not self.main_waiting():
            return None
        ds = [l.due for _, l in self.active()]
        return min(ds) if ds else None

    def main_waiting(self):
        main = self.emu.sched.threads[0]
        return not main.dead and main.ready is not None and main.why.startswith('mach_msg receive')

    def deliver(self):
        """If a tick is due and the main thread waits in mach_msg, borrow it to run the tick."""
        if self.saved is not None:
            return
        now = CLOCK.now()
        due = [(l.due, a, l) for a, l in self.active() if l.due <= now]
        if not due:
            return
        if not self.main_waiting():
            return
        main = self.emu.sched.threads[0]
        _, obj, l = min(due)
        l.timestamp = now
        period = l.interval / REFRESH
        l.due += period
        if l.due <= now:                       # fell behind: skip missed frames, as vsync would
            l.due = now + period
        self.saved = (main.ctx, main.ready, main.complete, main.deadline, main.why)
        self.uc.context_restore(main.ctx)
        sp = (self.uc.reg_read(UC_ARM_REG_SP) - 64) & ~7
        self.uc.reg_write(UC_ARM_REG_SP, sp)
        self.uc.reg_write(UC_ARM_REG_R0, l.target)
        self.uc.reg_write(UC_ARM_REG_R1, l.sel)
        self.uc.reg_write(UC_ARM_REG_R2, obj)
        self.uc.reg_write(UC_ARM_REG_LR, self.ret_stub)
        msg_send = self.emu.objc_msgsend
        thumb = msg_send in self.emu.syms.thumb
        cpsr = self.uc.reg_read(UC_ARM_REG_CPSR) & ~(1 << 5)
        self.uc.reg_write(UC_ARM_REG_CPSR, cpsr | (thumb << 5))
        self.uc.reg_write(UC_ARM_REG_PC, msg_send | thumb)
        main.ctx = self.uc.context_save()
        main.ready = main.complete = main.deadline = None
        main.why = 'display link tick'
        self.ticks += 1
        if self.ticks in (1, 2, 60) or self.ticks % 600 == 0:
            print(f'[displaylink] tick {self.ticks} at {now:.3f}s')

    def tick_return(self):
        """The tick's call came back: put the main thread back in the wait it was taken from."""
        main = self.emu.sched.threads[0]
        ctx, ready, complete, deadline, why = self.saved
        self.saved = None
        self.uc.context_restore(ctx)
        main.ready, main.complete, main.deadline, main.why = ready, complete, deadline, why
        self.uc.emu_stop()
