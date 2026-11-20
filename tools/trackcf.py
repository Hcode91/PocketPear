"""Debug helper: record CF instances as they are created and watch their header word."""
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn.arm_const import *

def install(emu, create=0x310b09dc):
    made = {}
    pending = {}
    def on_create(uc, a, size, d):
        lr = uc.reg_read(UC_ARM_REG_LR) & ~1
        tid = emu.sched.cur.tid
        pending[(tid, lr)] = (uc.reg_read(UC_ARM_REG_R1), emu.syms.name(lr))
        h = uc.hook_add(UC_HOOK_CODE, on_ret, begin=lr, end=lr, user_data=(tid, lr))
    def on_ret(uc, a, size, key):
        if key not in pending or key[0] != emu.sched.cur.tid:
            return
        typ, caller = pending.pop(key)
        obj = uc.reg_read(UC_ARM_REG_R0)
        made[obj] = (typ, caller)
        w = uc.mem_read(obj, 8)
        if typ == 0x1d or 'URL' in caller:
            print(f'[cf] t{key[0]} created {obj:#x} type {typ:#x} by {caller} header {bytes(w).hex()}')
            uc.hook_add(UC_HOOK_MEM_WRITE, on_w, begin=obj + 4, end=obj + 7)
    def on_w(uc, access, address, size, value, d):
        print(f'[cf] t{emu.sched.cur.tid} write {address:#x} size {size} value {value:#x} pc={emu.syms.name(uc.reg_read(UC_ARM_REG_PC))} lr={emu.syms.name(uc.reg_read(UC_ARM_REG_LR))}')
    emu.uc.hook_add(UC_HOOK_CODE, on_create, begin=create, end=create)
    emu.cf_made = made
