"""A stand-in for backboardd's Core Animation render server (com.apple.CARenderServer).

QuartzCore holds both ends of this MIG protocol (subsystem 40200-40227, CASCARenderServices):
apps are clients, and backboardd runs the server. The app needs a remote context before UIKit
will create a window; what it later commits into that context is the layer tree.
"""
import struct

from services.base import MigServer


class RenderServer(MigServer):
    name = 'carender'

    def __init__(self, kernel):
        super().__init__(kernel)
        self.next_context = 0x1001

    def default(self, req):
        # Most routines here only return a code; answer success and see what the app does.
        return self.reply(req, ret=0)

    def msg_40215(self, req):
        # Read by QuartzCore before registering: out-of-line data plus a count.
        r = self.reply(req, data=struct.pack('<I', 0), ool=[b''])
        return [(r, self.take_ool())]

    def msg_40203(self, req):
        # Register a client: the reply names the port its context talks to, and two numbers
        # (the context ID, and a second one QuartzCore stores beside it).
        ctx = self.next_context
        self.next_context += 1
        print(f'[carender] client {req.args[:4].hex()} registered, context {ctx:#x}')
        return self.reply(req, data=struct.pack('<II', ctx, 0), ports=[(self.port, 0x11)])
