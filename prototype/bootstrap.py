"""A stand-in for launchd's bootstrap server: the guest's Mach service names.

The app checks in the ports it serves (GraphicsServices' event port, for one) and looks up the
system's. Names it checks in become receive rights it holds; names the system would serve are
answered by the stand-ins in services/ when one exists, and as unknown otherwise.
"""
import struct

BOOTSTRAP_UNKNOWN_SERVICE = 1102
BOOTSTRAP_SERVICE_ACTIVE = 1103
NDR = struct.pack('<8B', 0, 0, 0, 0, 1, 0, 0, 0)
MOVE_RECEIVE, MOVE_SEND = 16, 17


def port_reply(port, disposition):
    """A complex MIG reply body carrying one port."""
    return bytearray(struct.pack('<I', 1) + struct.pack('<IIHBB', port, 0, 0, disposition, 0))


class Bootstrap:
    def __init__(self, kernel):
        self.k = kernel
        self.services = {}        # name -> port name in the guest's space
        self.providers = {}       # name -> factory() -> port, for system services we stand in for
        from services.springboard import Workspace, SystemApp
        self.providers['PurpleWorkspacePort'] = lambda: Workspace(kernel).port
        self.providers['PurpleSystemAppPort'] = lambda: SystemApp(kernel).port
        from services.backboard import HIDServices
        self.providers['com.apple.backboard.hid.services'] = lambda: HIDServices(kernel).port
        from services.syslog import SysLogger
        self.providers['com.apple.system.logger'] = lambda: SysLogger(kernel).port
        from services.carender import RenderServer
        self.providers['com.apple.CARenderServer'] = lambda: RenderServer(kernel).port

    def name(self, body, off):
        return body[off:off + 128].split(b'\0')[0].decode('utf-8', 'replace')

    def handle(self, mid, body):
        """Returns a reply body (bytes: simple RetCode reply; bytearray: complex) or None if unknown."""
        if mid == 402:                          # bootstrap_check_in
            name = self.name(body, 8)
            p = self.services.get(name)
            if p is None or self.k.pr.kind(p) != 'receive':
                p = self.k.new_port('receive')
                self.k.pr.get(p).label = name
                self.services[name] = p
            print(f'[launchd] check-in {name!r} -> {p:#x}')
            self.k.launcher.note_check_in(name, p)
            return port_reply(p, MOVE_RECEIVE)
        if mid == 403:                          # bootstrap_register
            port, = struct.unpack_from('<I', body, 4)
            name = self.name(body, 24)
            self.services[name] = port
            print(f'[launchd] register {name!r} -> {port:#x}')
            self.k.launcher.note_check_in(name, port, replace=True)
            return struct.pack('<i', 0)
        if mid == 404:                          # bootstrap_look_up
            name = self.name(body, 8)
            p = self.services.get(name)
            if p is None and name in self.providers:
                p = self.services[name] = self.providers[name]()
            print(f'[launchd] look-up {name!r} -> {"unknown" if p is None else hex(p)}')
            if p is None:
                return struct.pack('<i', BOOTSTRAP_UNKNOWN_SERVICE)
            return port_reply(p, MOVE_SEND)
        return None
