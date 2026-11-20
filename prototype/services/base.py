"""A Mach service implemented in the emulator: requests arrive as bytes, replies are queued on
the request's reply port in the form the guest receives them."""
import struct

NDR = struct.pack('<8B', 0, 0, 0, 0, 1, 0, 0, 0)
MIG_BAD_ID = -303
COMPLEX = 0x80000000


class Request:
    def __init__(self, raw):
        self.raw = raw
        self.bits, self.size, self.remote, self.local, self.voucher, self.id = struct.unpack_from('<6I', raw, 0)
        self.complex = bool(self.bits & COMPLEX)
        self.ports = []                  # (name, disposition) of port descriptors
        self.ool = []                    # (address, size) of out-of-line memory
        o = 24
        if self.complex:
            count, = struct.unpack_from('<I', raw, 24)
            o = 28
            for _ in range(count):
                w0, w1, w2 = struct.unpack_from('<III', raw, o)
                t = w2 >> 24
                if t == 0:
                    self.ports.append((w0, (w2 >> 16) & 0xff))
                else:
                    self.ool.append((w0, w1))
                o += 12
        self.body = raw[o:]              # NDR + arguments for MIG requests
        self.args = raw[o + 8:] if len(raw) >= o + 8 else b''

    def __repr__(self):
        return f'<request {self.id} {len(self.raw)} bytes>'


class MigServer:
    name = 'service'
    from ports import TOKEN_SYSTEM as token     # whose audit token the replies carry

    def __init__(self, kernel):
        self.k = kernel
        self.port = kernel.new_port('service')
        self.k.pr.get(self.port).server = self
        self.seen = {}

    # ------------------------------------------------------------ replies
    def reply(self, req, data=b'', ports=(), ret=None, ool=()):
        """A MIG reply: header, [port and out-of-line descriptors], NDR, [return code], data.
        ool items are bytes; they are copied into the receiver's memory on delivery."""
        body = b''
        bits = 0x12
        self._pending_ool = []
        if ports or ool:
            bits |= COMPLEX
            body += struct.pack('<I', len(ports) + len(ool))
            for p, disp in ports:
                body += struct.pack('<IIHBB', p, 0, 0, disp, 0)
            for blob in ool:
                self._pending_ool.append((24 + len(body), blob))
                body += struct.pack('<IIBBBB', 0, len(blob), 1, 0, 0, 1)   # address, size, deallocate, copy, pad, type=OOL
        body += NDR
        if ret is not None:
            body += struct.pack('<i', ret)
        body += data
        msg = struct.pack('<6I', bits, 24 + len(body), 0, req.local, 0, req.id + 100) + body
        return msg

    def error(self, req, code):
        return self.reply(req, ret=code)

    def handle(self, req):
        """Return a list of reply messages (bytes). Unknown routines get MIG_BAD_ID."""
        h = getattr(self, f'msg_{req.id}', None)
        n = self.seen.get(req.id, 0) + 1
        self.seen[req.id] = n
        if h is None:
            if n <= 3:
                print(f'[{self.name}] unhandled message {req.id} ({len(req.raw)} bytes, {len(req.ports)} ports): {req.args[:48].hex()}')
            return [self.default(req)] if req.local else []
        out = h(req)
        if out is None:
            return []
        return out if isinstance(out, list) else [out]

    def take_ool(self):
        """The out-of-line blobs of the reply built last, as (offset, bytes) for delivery."""
        o, self._pending_ool = getattr(self, '_pending_ool', []), []
        return o

    def default(self, req):
        return self.error(req, MIG_BAD_ID)
