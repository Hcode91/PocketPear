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
        # Anything else the app sends (commits, mostly) may have changed what it shows.
        ui = getattr(self.k.emu, 'ui', None)
        if ui is not None:
            ui.mark_dirty()
        return self.reply(req, ret=0)

    def msg_40215(self, req):
        self.info_calls = getattr(self, 'info_calls', 0) + 1
        if self.info_calls in (2, 1000):
            print(f'[carender] 40215 call #{self.info_calls} from:')
            self.k.emu.backtrace(30)
        # Read by QuartzCore before registering: out-of-line data plus a count.
        r = self.reply(req, data=struct.pack('<I', 0), ool=[b''])
        return [(r, self.take_ool())]

    def msg_40203(self, req):
        # Register a client: the reply names the port its context talks to, and two numbers
        # (the context ID, and a second one QuartzCore stores beside it).
        ctx = self.next_context
        self.next_context += 1
        self.k.render_contexts = getattr(self.k, 'render_contexts', []) + [ctx]
        print(f'[carender] client {req.args[:4].hex()} registered, context {ctx:#x}')
        return self.reply(req, data=struct.pack('<II', ctx, 0), ports=[(self.port, 0x11)])

    def msg_40201(self, req):
        # A simple routine (no reply port in the header) carrying a send-once right the client
        # then waits on with no timeout: QuartzCore sends it while displaying a layer whose
        # backing store buffers the server may still hold, and waits until the server says it
        # is done with them. Nothing here holds them, so answer at once. Unanswered, the
        # main thread waits for ever in the middle of a commit: Resident Evil Degeneration
        # (Marmalade) draws its loading screen with -drawRect: twice and then stops.
        if not req.ports:
            return None
        import copy
        r = copy.copy(req)
        r.local = req.ports[0][0]
        return self.reply(r, ret=0)
