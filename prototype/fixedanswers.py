"""System answers that never change while an app runs, computed once and then given straight back.

-[UIDevice systemVersion] asks MobileGestalt, which on this system reads and parses
SystemVersion.plist from disk on every call (open, stat, read, a property list parse). On a
device that is cheap enough; Bioshock asks from its renderer for every draw
(IsIOSVersionGreaterOrEqualTo8), several hundred times a second, which cost more than the draws.
The first call asks MobileGestalt as UIKit would; the string is retained and returned from then
on. PP_NO_FIXED_ANSWERS=1 turns it off.
"""
from unicorn.arm_const import *

from guestcall import InlineCalls


class FixedAnswers:
    def __init__(self, emu):
        self.emu = emu
        self.uc = emu.uc
        self.version = None

    def install(self, stubs):
        names = {}
        for n, a in zip(self.emu.syms.names, self.emu.syms.addrs):
            names.setdefault(n, a)
        self.fn = {n.split(' (')[0]: a for n, a in names.items() if ' (' in n}
        method = names.get('-[UIDevice systemVersion] (UIKit)')
        need = ['_sel_registerName', '_objc_msgSend', '_CFStringCreateWithCString']
        if method is None or any(n not in self.fn for n in need):
            return
        self.calls = InlineCalls(self.emu, stubs)
        page = self.emu.find_free(0x1000)
        self.emu.map(page, 0x1000)
        self.emu.write(page, b'deviceInfoForKey:\0ProductVersion\0retain\0')
        self.str = {'deviceInfoForKey:': page, 'ProductVersion': page + 18, 'retain': page + 33}
        stubs.replace(method, self.system_version, method in self.emu.syms.thumb)

    def system_version(self):
        if self.version is not None:
            self.uc.reg_write(UC_ARM_REG_R0, self.version)
            return
        me = self.uc.reg_read(UC_ARM_REG_R0)
        self.calls.run(self.ask(me))

    def ask(self, me):
        send = self.fn['_objc_msgSend']
        sel, _ = yield (self.fn['_sel_registerName'], [self.str['deviceInfoForKey:']])
        key, _ = yield (self.fn['_CFStringCreateWithCString'], [0, self.str['ProductVersion'], 0x08000100])
        v, _ = yield (send, [me, sel, key])
        if v:
            retain, _ = yield (self.fn['_sel_registerName'], [self.str['retain']])
            yield (send, [v, retain])
            self.version = v
        return v
