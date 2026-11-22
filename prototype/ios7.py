"""iOS 7 behaviour an iOS 6 system does not have, for apps built for iOS 7.

The emulated system is iOS 6, and a few UIKit defaults changed in iOS 7 that apps built for it
rely on without saying so. Each is applied only when the app's MinimumOSVersion is 7 or later.

UILabel: iOS 6 gives a new label an opaque white background; iOS 7 a clear one. A game that
puts a subtitle label over its GL view and never sets the background (Bioshock) shows a white
bar under the picture on iOS 6. -[UILabel initWithFrame:] is replaced with the same two steps
(super's initWithFrame:, then _commonInit) followed by a clear background and opaque = NO.
Labels from a nib (initWithCoder:) keep the colour that was archived with them.
"""
import struct

from unicorn.arm_const import *

from guestcall import InlineCalls


def wants_ios7(info):
    v = str(info.get('MinimumOSVersion', '0'))
    try:
        return int(v.split('.')[0]) >= 7
    except ValueError:
        return False


class Ios7:
    def __init__(self, emu):
        self.emu = emu
        self.uc = emu.uc

    def install(self, stubs):
        if not wants_ios7(getattr(self.emu, 'info', {}) or {}):
            return
        names = {}
        for n, a in zip(self.emu.syms.names, self.emu.syms.addrs):
            names.setdefault(n, a)
        self.fn = {n.split(' (')[0]: a for n, a in names.items() if ' (' in n}
        init = names.get('-[UILabel initWithFrame:] (UIKit)')
        need = ['_objc_getClass', '_sel_registerName', '_objc_msgSendSuper2', '_objc_msgSend']
        if init is None or any(n not in self.fn for n in need):
            print('[ios7] UILabel defaults left as iOS 6 has them (symbols not found)')
            return
        self.calls = InlineCalls(self.emu, stubs)
        page = self.emu.find_free(0x1000)
        self.emu.map(page, 0x1000)
        self.scratch = page           # strings, then a super struct per thread
        self.emu.write(page, b'UILabel\0initWithFrame:\0_commonInit\0clearColor\0'
                             b'setBackgroundColor:\0setOpaque:\0UIColor\0')
        self.str = {}
        off = 0
        for s in ('UILabel', 'initWithFrame:', '_commonInit', 'clearColor', 'setBackgroundColor:',
                  'setOpaque:', 'UIColor'):
            self.str[s] = page + off
            off += len(s) + 1
        self.cache = {}
        stubs.replace(init, self.label_init, init in self.emu.syms.thumb)
        print('[ios7] UILabel starts with a clear background, as on iOS 7')

    def label_init(self):
        uc = self.uc
        me, cmd = uc.reg_read(UC_ARM_REG_R0), uc.reg_read(UC_ARM_REG_R1)
        x, y = uc.reg_read(UC_ARM_REG_R2), uc.reg_read(UC_ARM_REG_R3)
        sp = uc.reg_read(UC_ARM_REG_SP)
        w, h = struct.unpack('<II', self.emu.read(sp, 8))
        tid = self.emu.sched.cur.tid
        self.calls.run(self.init_steps(me, cmd, x, y, w, h, tid))

    def lookup(self):
        if not self.cache:
            for c in ('UILabel', 'UIColor'):
                self.cache[c], _ = yield (self.fn['_objc_getClass'], [self.str[c]])
            for s in ('_commonInit', 'clearColor', 'setBackgroundColor:', 'setOpaque:'):
                self.cache[s], _ = yield (self.fn['_sel_registerName'], [self.str[s]])

    def init_steps(self, me, cmd, x, y, w, h, tid):
        yield from self.lookup()
        sup = self.scratch + 0x200 + (tid % 128) * 8
        self.emu.write(sup, struct.pack('<II', me, self.cache['UILabel']))
        me, _ = yield (self.fn['_objc_msgSendSuper2'], [sup, cmd, x, y, w, h])
        if not me:
            return 0
        send = self.fn['_objc_msgSend']
        yield (send, [me, self.cache['_commonInit']])
        clear, _ = yield (send, [self.cache['UIColor'], self.cache['clearColor']])
        yield (send, [me, self.cache['setBackgroundColor:'], clear])
        yield (send, [me, self.cache['setOpaque:'], 0])
        return me
