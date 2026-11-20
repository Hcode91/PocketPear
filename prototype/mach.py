"""mach_msg: the kernel's MIG servers (task, host, vm, port) and stand-ins for system services.

A request's msgh_id names the routine; the reply goes back into the caller's buffer as
header + NDR record + return code (+ out values), with id = request id + 100.
"""
import struct

KERN_SUCCESS, KERN_FAILURE, KERN_INVALID_ARGUMENT = 0, 5, 4
MACH_RCV_TIMED_OUT = 0x10004003
MIG_BAD_ID = -303
MACH_SEND_MSG, MACH_RCV_MSG = 1, 2
NDR = struct.pack('<8B', 0, 0, 0, 0, 1, 0, 0, 0)

NAMES = {200: 'host_info', 201: 'host_kernel_version', 202: 'host_page_size', 205: 'host_get_io_master',
         206: 'host_get_clock_service', 3405: 'task_info', 3409: 'task_get_special_port', 3410: 'task_set_special_port',
         3402: 'task_threads', 3418: 'semaphore_create', 3419: 'semaphore_destroy', 3404: 'mach_ports_lookup', 3206: 'mach_port_deallocate', 3204: 'mach_port_allocate',
         3213: 'mach_port_request_notification', 3214: 'mach_port_insert_right', 3218: 'mach_port_get_attributes',
         3217: 'mach_port_set_attributes', 3216: 'mach_port_get_attributes',
         4800: 'mach_vm_allocate', 4801: 'mach_vm_deallocate', 4802: 'mach_vm_protect', 4811: 'mach_vm_map',
         4816: 'mach_vm_region', 3800: 'vm_region', 3801: 'vm_allocate', 3802: 'vm_deallocate', 3803: 'vm_protect',
         3812: 'vm_map', 3612: 'thread_info', 3616: 'thread_policy', 3617: 'thread_policy_set', 3618: 'thread_policy_get',
         1000: 'clock_get_time', 1001: 'clock_get_attributes', 404: 'bootstrap_look_up'}


import json, os
_MIG = {int(k): v for k, v in json.load(open(os.path.join(os.path.dirname(__file__), 'migids.json'))).items()}
for _k, _v in _MIG.items():
    NAMES.setdefault(_k, _v)


class MachMsg:
    def __init__(self, kernel):
        self.k = kernel
        self.emu = kernel.emu
        self.seen = {}

    def msg(self, msg_p, option, send_size, rcv_size, rcv_name, timeout, notify):
        if not (option & MACH_SEND_MSG):
            if option & MACH_RCV_MSG:
                # Nothing will ever arrive on a receive-only wait yet.
                return MACH_RCV_TIMED_OUT
            return KERN_SUCCESS
        bits, size, remote, local, reserved, mid = struct.unpack('<6I', self.emu.read(msg_p, 24))
        size = min(size, send_size) if send_size else size
        body = self.emu.read(msg_p + 24, max(0, size - 24))
        name = NAMES.get(mid, '?')
        dest = self.k.ports.get(remote, f'port {remote:#x}')
        n = self.seen.get(mid, 0) + 1
        self.seen[mid] = n
        if n <= 3 or self.emu.trace:
            print(f'[mach] msg id {mid} ({name}) to {dest}, size {size} bits {bits:#x} send_size {send_size} opt {option:#x}')
        h = getattr(self, 'mig_' + name.replace('?', ''), None)
        out = h(body, remote) if h else None
        if isinstance(out, bytearray):     # pre-built complex reply body (after the header)
            if option & MACH_RCV_MSG:
                header = struct.pack('<6I', 0x80000012, 24 + len(out), 0, local, 0, mid + 100)
                self.emu.write(msg_p, header + bytes(out))
            return KERN_SUCCESS
        if out is None:
            # Unknown routine: most kernel MIG calls are setters whose reply is only a return
            # code, so answer success. A getter will see a short reply and fail its own size check.
            out = struct.pack('<i', KERN_SUCCESS if (h is None and dest != 'bootstrap') else (MIG_BAD_ID if h is None else KERN_FAILURE))
            simple = True
            ports = None
        elif isinstance(out, tuple):      # (ports to transfer, trailing data) for complex replies
            ports, trailing = out
            simple = False
        else:
            simple = True
        if not (option & MACH_RCV_MSG):
            return KERN_SUCCESS
        if simple:
            reply = NDR + out
            header = struct.pack('<6I', 0x12, 24 + len(reply), 0, local, 0, mid + 100)
            self.emu.write(msg_p, header + reply)
        else:
            # complex: header, body count, port descriptors, NDR, trailing data
            desc = b''.join(struct.pack('<I', p) + struct.pack('<I', 0) + struct.pack('<HBB', 0, 0x11, 0) for p in ports)
            reply = struct.pack('<I', len(ports)) + desc + (NDR + trailing if trailing else b'')
            header = struct.pack('<6I', 0x80000012, 24 + len(reply), 0, local, 0, mid + 100)
            self.emu.write(msg_p, header + reply)
        return KERN_SUCCESS

    # ---------------------------------------------------------------- host
    def mig_host_info(self, body, port):
        flavor, count = struct.unpack_from('<iI', body, 8)
        if flavor == 1:   # HOST_BASIC_INFO
            # max_cpus, avail_cpus, memory_size, cpu_type (ARM), cpu_subtype (armv7s), threadtype,
            # physical_cpu, logical_cpu, logical_cpu_max, max_mem
            info = struct.pack('<iiIiiiiiiQ', 2, 2, 1 << 30, 12, 11, 0, 2, 2, 2, 1 << 30)[:min(count, 12) * 4]
        elif flavor == 5:  # HOST_PRIORITY_INFO
            info = struct.pack('<8i', 31, 31, 0, 31, 47, 0, 47, 79)
        else:
            return None
        n = len(info) // 4
        return struct.pack('<iI', KERN_SUCCESS, n) + info

    def mig_host_page_size(self, body, port):
        return struct.pack('<iI', KERN_SUCCESS, 4096)

    # ---------------------------------------------------------------- task
    def mig_task_get_special_port(self, body, port):
        which, = struct.unpack_from('<i', body, 8)
        if which == 4:        # TASK_BOOTSTRAP_PORT
            p = getattr(self.k, 'bootstrap_port', None) or self.k.new_port('bootstrap')
            self.k.bootstrap_port = p
            return ([p], None)
        return None

    def mig_task_info(self, body, port):
        flavor, count = struct.unpack_from('<iI', body, 8)
        if flavor == 17:   # TASK_DYLD_INFO: all_image_info_addr(64), size(64), format
            addr = getattr(self.k, 'all_image_info_addr', 0)
            info = struct.pack('<QQi', addr, 0, 1)
        elif flavor in (3, 18, 20):    # TASK_BASIC_INFO variants
            info = bytes(count * 4)
        else:
            return None
        return struct.pack('<iI', KERN_SUCCESS, len(info) // 4) + info

    def mig_thread_policy_set(self, body, port): return struct.pack('<i', KERN_SUCCESS)
    def mig_thread_policy(self, body, port): return struct.pack('<i', KERN_SUCCESS)

    def mig_task_get_exception_ports(self, body, port):
        # No exception handlers: 32 null port descriptors and an empty mask list.
        desc = struct.pack('<IIHBB', 0, 0, 0, 0x11, 0) * 32
        return bytearray(struct.pack('<I', 32) + desc + NDR + struct.pack('<I', 0))

    def mig_vm_map(self, body, port):
        # complex request: body count, memory object port descriptor, NDR, then the arguments
        address, size, mask, flags = struct.unpack_from('<IIII', body, 4 + 12 + 8)
        if flags & 1 or not address:            # VM_FLAGS_ANYWHERE
            align = max(4096, (mask + 1) if mask else 4096)
            a = self.emu.find_free(size + align)
            a = (a + align - 1) & ~(align - 1)
        else:
            a = address
        print('[mach] vm_map body', [hex(x) for x in struct.unpack_from('<%dI' % (len(body)//4), body)])
        print(f'[mach] vm_map addr={address:#x} size={size:#x} mask={mask:#x} flags={flags:#x} -> {a:#x}')
        self.emu.map(a, size)
        self.emu.write(a, bytes((size + 0xfff) & ~0xfff))
        return struct.pack('<iI', KERN_SUCCESS, a)

    def mig_mach_ports_lookup(self, body, port):
        # The ports launchd registered for the task: an out-of-line array of three, all null.
        n = 3
        a = self.emu.find_free(4096)
        self.emu.map(a, 4096)
        self.emu.write(a, bytes(4 * n))
        desc = struct.pack('<IIBBBB', a, n, 1, 0, 0x11, 2)      # 32-bit layout: address, count, deallocate, copy, disposition, type=OOL_PORTS
        return bytearray(struct.pack('<I', 1) + desc + NDR + struct.pack('<I', n))

    def mig_semaphore_create(self, body, port):
        policy, value = struct.unpack_from('<ii', body, 8)
        p = self.k.new_port('semaphore')
        self.k.semaphores[p] = value
        return ([p], None)

    def mig_semaphore_destroy(self, body, port): return struct.pack('<i', KERN_SUCCESS)

    def mig_host_get_clock_service(self, body, port):
        which, = struct.unpack_from('<i', body, 8)
        return ([self.k.new_port(f'clock{which}')], None)
    def mig_mach_port_deallocate(self, body, port): return struct.pack('<i', KERN_SUCCESS)
    def mig_mach_port_request_notification(self, body, port): return ([0], b'')
    def mig_mach_port_set_attributes(self, body, port): return struct.pack('<i', KERN_SUCCESS)
