"""syslogd's ASL port (com.apple.system.logger): NSLog, asl_log and Unity's Debug.Log end up here.

Messages arrive as out-of-line text in ASL's own encoding ("[Key Value] [Key Value] ...").
They are printed with their sender, which makes a game's own diagnostics part of the run log."""
import re

from services.base import MigServer


def parse(text):
    """ASL's "[Key Value] [Key Value]" text, with backslash escapes inside values."""
    fields, i, n = {}, 0, len(text)
    while i < n:
        if text[i] != '[':
            i += 1
            continue
        i += 1
        out, key = [], None
        while i < n and text[i] != ']':
            c = text[i]
            if c == '\\' and i + 1 < n:
                out.append(text[i + 1]); i += 2; continue
            if c == ' ' and key is None:
                key, out = ''.join(out), []
            else:
                out.append(c)
            i += 1
        i += 1
        if key is not None:
            fields[key] = ''.join(out)
    return fields


class SysLogger(MigServer):
    name = 'syslog'

    def default(self, req):
        for addr, size in req.ool:
            try:
                text = self.k.emu.read(addr, size).split(b'\0')[0].decode('utf-8', 'replace')
            except Exception:
                continue
            fields = parse(text)
            body = fields.get('Message', text)
            for line in body.splitlines() or ['']:
                print(f'[log] {line}')
        return self.reply(req, ret=0) if req.local else None

    def handle(self, req):
        out = self.default(req)
        return [out] if out else []
