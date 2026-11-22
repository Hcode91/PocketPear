"""Showing what UIKit draws.

On a device backboardd composites the app's Core Animation layer tree: UIKit views (splash
screens, menus, buttons) and the CAEAGLLayer a game draws into with OpenGL ES. Here nothing
renders the layer tree, so only the GL frames reached the screen, and a game whose menu is made
of UIKit views (Bioshock's) showed black.

The tree is drawn by the app's own QuartzCore: the main thread is borrowed while its run loop
waits (as for a display link tick), and asked to -renderInContext: each window's layer into a
screen-sized bitmap. CAEAGLLayers have no contents that renderInContext: can draw, so for the
duration their background is set to a key colour, and the pixels left in that colour become
transparent: the GL frame shows through there, under any views above it, as on a device.

The result is composited over each presented GL frame, or shown alone when the app presents no
GL frames.
"""
import os
import struct
import time

import numpy as np
from unicorn.arm_const import *

from sched import CLOCK
from guestcall import GuestCalls, fbits

MAX_RATE = float(os.environ.get('PP_UI_RATE', 10))      # compositions a second, at most
REFRESH = 1.0                                             # recompose this often without commits
KEY = (255, 0, 255, 255)


class Compositor:
    def __init__(self, emu):
        self.emu = emu
        self.calls = None
        self.dirty = True
        self.last = -1.0
        self.hole = None           # where a movie view shows: left transparent for the host's video
        self.overlay = None        # RGBA, top row first, screen (portrait) orientation, premultiplied
        self.orientation = 1
        self.count = 0
        self.ctx = 0
        self.failed = False
        names = {}
        for a, n in zip(emu.syms.names, emu.syms.addrs):
            if ' (' in a:
                names.setdefault(a.split(' (')[0], n)
        self.fn = names

    def install(self, stubs):
        need = ['_objc_getClass', '_sel_registerName', '_CGColorSpaceCreateDeviceRGB', '_CGBitmapContextCreate',
                '_CGContextTranslateCTM', '_CGContextScaleCTM', '_CGColorCreate', '_CGColorRetain', '_CGColorRelease',
                '_objc_autoreleasePoolPush', '_objc_autoreleasePoolPop']
        missing = [n for n in need if n not in self.fn]
        if missing:
            print(f'[ui] cannot draw UIKit views: {", ".join(missing)} not found')
            self.failed = True
            return
        self.calls = GuestCalls(self.emu, stubs)
        pw, ph = getattr(self.emu, 'screen_points', (320, 480))
        self.scale = 2
        self.w, self.h = pw * self.scale, ph * self.scale
        size = (self.w * self.h * 4 + 0xfff) & ~0xfff
        self.buf = self.emu.find_free(size + 0x1000)
        self.emu.map(self.buf, size + 0x1000)
        self.strings = self.buf + size
        self.strings_end = self.strings + 0x1000
        self.sels = {}
        self.classes = {}

    # ---------------------------------------------------------------- scheduling
    def mark_dirty(self):
        self.dirty = True

    def main_waiting(self):
        main = self.emu.sched.threads[0]
        return not main.dead and main.ready is not None and main.why.startswith('mach_msg receive')

    def next_due(self):
        if self.failed or self.calls is None or self.calls.busy():
            return None
        if not self.main_waiting():
            return None
        movies = getattr(self.emu, 'movies', None)
        if movies is not None and movies.next_due() is not None:
            return movies.next_due()
        if not getattr(self.emu.kernel, 'render_contexts', None):
            return None
        # Only a pending change wakes a waiting app; the slow refresh happens while it runs.
        return self.last + 1 / MAX_RATE if self.dirty else None

    def deliver(self):
        if self.failed or self.calls is None or self.calls.busy():
            return
        if self.emu.displaylinks.saved is not None or not self.main_waiting():
            return
        movies = getattr(self.emu, 'movies', None)
        job = movies.pending_job() if movies is not None and movies.calls is not None else None
        if job is not None:
            self.calls.start(job, 'posting a movie notification')
            return
        if not getattr(self.emu.kernel, 'render_contexts', None):
            return                        # no UIKit window yet
        now = CLOCK.now()
        if now < self.last + (1 / MAX_RATE if self.dirty else REFRESH):
            return
        self.last = now
        self.dirty = False
        self.calls.start(self.compose(), 'drawing UIKit views')

    # ---------------------------------------------------------------- the guest calls
    def cstring(self, s):
        b = s.encode() + b'\0'
        a = self.strings
        if a + len(b) > self.strings_end:
            raise RuntimeError('string space used up')
        self.emu.write(a, b)
        self.strings += (len(b) + 3) & ~3
        return a

    def sel(self, name):
        if name not in self.sels:
            r0, _ = yield (self.fn['_sel_registerName'], [self.cstring(name)])
            self.sels[name] = r0
        return self.sels[name]

    def cls(self, name):
        if name not in self.classes:
            r0, _ = yield (self.fn['_objc_getClass'], [self.cstring(name)])
            self.classes[name] = r0
        return self.classes[name]

    def send(self, obj, selname, *args):
        s = yield from self.sel(selname)
        r = yield (self.emu.objc_msgsend, [obj, s, *args])
        return r[0]

    def compose(self):
        if not self.ctx:
            cs, _ = yield (self.fn['_CGColorSpaceCreateDeviceRGB'], [])
            ctx, _ = yield (self.fn['_CGBitmapContextCreate'],
                            [self.buf, self.w, self.h, 8, self.w * 4, cs, 1 | (4 << 12)])
            if not ctx:
                print('[ui] CGBitmapContextCreate failed; UIKit views will not be shown')
                self.failed = True
                return
            yield (self.fn['_CGContextTranslateCTM'], [ctx, fbits(0.0), fbits(float(self.h))])
            yield (self.fn['_CGContextScaleCTM'], [ctx, fbits(float(self.scale)), fbits(-float(self.scale))])
            comps = self.cstring('')            # 16 bytes of space for four floats
            self.strings += 16
            self.emu.write(comps, struct.pack('<4f', *(c / 255 for c in KEY)))
            self.key_color, _ = yield (self.fn['_CGColorCreate'], [cs, comps])
            self.ctx = ctx
            print(f'[ui] drawing UIKit views into a {self.w}x{self.h} bitmap')
        self.emu.write(self.buf, bytes(self.w * self.h * 4))
        pool, _ = yield (self.fn['_objc_autoreleasePoolPush'], [])
        transaction = yield from self.cls('CATransaction')
        yield from self.send(transaction, 'begin')
        yield from self.send(transaction, 'setDisableActions:', 1)
        app_cls = yield from self.cls('UIApplication')
        app = yield from self.send(app_cls, 'sharedApplication')
        drawn = 0
        if app:
            self.orientation = yield from self.send(app, 'statusBarOrientation')
            gl_layers = list(getattr(self.emu.gles, 'drawable_layers', ()))
            saved = []
            for layer in gl_layers:
                old = yield from self.send(layer, 'backgroundColor')
                yield (self.fn['_CGColorRetain'], [old])
                saved.append((layer, old))
                yield from self.send(layer, 'setBackgroundColor:', self.key_color)
            windows = yield from self.send(app, 'windows')
            n = (yield from self.send(windows, 'count')) if windows else 0
            for i in range(min(n, 16)):
                w = yield from self.send(windows, 'objectAtIndex:', i)
                hidden = (yield from self.send(w, 'isHidden')) & 0xff
                if hidden:
                    continue
                layer = yield from self.send(w, 'layer')
                yield from self.send(layer, 'renderInContext:', self.ctx)
                drawn += 1
                tree = os.environ.get('PP_UI_TREE', '')
                if tree and str(self.count + 1) in tree.split(','):
                    desc = yield from self.send(w, 'recursiveDescription')
                    text = (yield from self.send(desc, 'UTF8String')) if desc else 0
                    if text:
                        tree_text = self.emu.cstr(text, 65536)
                        print('[ui] view tree:\n' + tree_text)
                        import re
                        for m in re.finditer(r'<(UILabel|UIView|UIImageView): (0x[0-9a-f]+);([^\n]*)', tree_text):
                            if 'hidden = YES' in m.group(3):
                                continue
                            v = int(m.group(2), 16)
                            parts = []
                            for selname in ('backgroundColor', 'textColor', 'text'):
                                if selname != 'backgroundColor' and m.group(1) != 'UILabel':
                                    continue
                                o = yield from self.send(v, selname)
                                if o:
                                    d = yield from self.send(o, 'description')
                                    c = (yield from self.send(d, 'UTF8String')) if d else 0
                                    parts.append(f'{selname}={self.emu.cstr(c, 300) if c else "?"}')
                                else:
                                    parts.append(f'{selname}=nil')
                            opq = (yield from self.send(v, 'isOpaque')) & 0xff
                            print(f'[ui]   {m.group(1)} {m.group(2)}: opaque={opq} ' + ' '.join(parts))
            for layer, old in saved:
                yield from self.send(layer, 'setBackgroundColor:', old)
                yield (self.fn['_CGColorRelease'], [old])
        yield from self.send(transaction, 'commit')
        yield (self.fn['_objc_autoreleasePoolPop'], [pool])
        self.take(drawn)

    def take(self, drawn):
        img = np.frombuffer(self.emu.read(self.buf, self.w * self.h * 4), np.uint8).reshape(self.h, self.w, 4).copy()
        key = (img[:, :, 0] == KEY[0]) & (img[:, :, 1] == KEY[1]) & (img[:, :, 2] == KEY[2]) & (img[:, :, 3] == KEY[3])
        from movie import MOVIE_KEY
        hole = (img[:, :, 0] == MOVIE_KEY[0]) & (img[:, :, 1] == MOVIE_KEY[1]) & (img[:, :, 2] == MOVIE_KEY[2]) & (img[:, :, 3] == MOVIE_KEY[3])
        unblend(img, key, 1, 0)           # magenta: green is 0 in the key
        unblend(img, hole, 0, 1)          # cyan: red is 0 in the key
        img[key] = 0
        if hole.any():
            img[hole] = 0
            self.hole = hole
        else:
            self.hole = None
        self.overlay = img if img[:, :, 3].any() or self.hole is not None else None
        self.version = getattr(self, 'version', 0) + 1
        self.cache = None
        self.count += 1
        if self.count in (1, 2, 10) or self.count % 300 == 0 or (self.hole is not None) != getattr(self, 'had_hole', False):
            self.had_hole = self.hole is not None
            cover = 0 if self.overlay is None else float((img[:, :, 3] > 0).mean())
            movie = ''
            if self.hole is not None:
                ys, xs = np.nonzero(self.hole)
                movie = f', movie view at {xs.min()},{ys.min()} {xs.max() - xs.min() + 1}x{ys.max() - ys.min() + 1}'
            print(f'[ui] composition {self.count}: {drawn} window(s), {cover * 100:.0f}% of the screen covered by views,'
                  f' orientation {self.orientation}{movie}')
        dump = os.environ.get('PP_UI_DUMP')
        if dump and self.overlay is not None and (self.count <= 20 or os.environ.get('PP_UI_DUMP_ALL')):
            from PIL import Image
            Image.fromarray(self.overlay, 'RGBA').save(os.path.join(dump, f'ui{self.count:03d}.png'))
        g = self.emu.gles
        if self.overlay is not None and time.time() - getattr(g, 'last_present', 0) > 0.3:
            g.present_ui_only()

    # ---------------------------------------------------------------- compositing
    def oriented(self, fw, fh, img=None):
        """The overlay (or img) turned and sized to a frame of fw x fh, top row first, or None."""
        if img is None:
            img = self.overlay
        if img is None:
            return None
        if fw > fh:                       # the GL frame is in the interface's landscape orientation
            k = {3: 1, 4: -1}.get(self.orientation, 1)
            img = np.rot90(img, k)
        elif self.orientation == 2:
            img = np.rot90(img, 2)
        h, w = img.shape[:2]
        if (w, h) != (fw, fh):
            ys = (np.arange(fh) * h // fh)
            xs = (np.arange(fw) * w // fw)
            img = img[ys][:, xs]
        return img

    def over(self, rgba, fw, fh):
        """Composite the overlay over a frame given bottom row first (GL order)."""
        if self.overlay is None:
            return rgba
        c = getattr(self, 'cache', None)
        if c is None or c[0] != (fw, fh):
            # turned, flipped to GL row order and split, once per composition rather than per frame
            top = self.oriented(fw, fh)[::-1]
            inv = (255 - top[:, :, 3:4]).astype(np.uint16)
            hole = self.oriented(fw, fh, self.hole)[::-1] if self.hole is not None else None
            covered = bool(top[:, :, 3].any())
            c = self.cache = ((fw, fh), top.astype(np.uint16), inv, hole, covered)
        _, top, inv, hole, covered = c
        frame = np.frombuffer(rgba, np.uint8).reshape(fh, fw, 4)
        if covered:
            out = top + ((frame.astype(np.uint16) * inv + 255) >> 8)
            out[:, :, 3] = 255
            out = np.minimum(out, 255).astype(np.uint8)
        else:
            out = frame.copy()
            out[:, :, 3] = 255
        if hole is not None:
            out[hole] = 0
        return out.tobytes()


def unblend(img, mask, zero, full):
    """Antialiased edges of text and art drawn over a key colour are a mix of the two, and are
    left as a coloured fringe once the key itself is cleared. Next to the key, a pixel on the line
    between the key and a grey is taken as a grey of some coverage: in the key, channel `zero` is
    0 and channel `full` 255, so for grey v at coverage a, zero = a*v and full = a*v + 255*(1-a).
    The result is premultiplied, like the rest of the overlay. Coloured edges keep a little of the
    key; text, the common case, comes out clean."""
    if not mask.any():
        return
    near = mask.copy()
    for _ in range(2):                    # pixels within two of the key
        grown = near.copy()
        grown[1:] |= near[:-1]; grown[:-1] |= near[1:]
        grown[:, 1:] |= near[:, :-1]; grown[:, :-1] |= near[:, 1:]
        near = grown
    other = 3 - zero - full               # the key's third colour channel, 255 like `full`
    z = img[:, :, zero].astype(np.int16)
    f = img[:, :, full].astype(np.int16)
    o = img[:, :, other].astype(np.int16)
    edge = near & ~mask & (img[:, :, 3] == 255) & (np.abs(f - o) <= 2) & (f > z + 8)
    if not edge.any():
        return
    a = 255 - (f - z)                     # coverage 0..255
    v = z                                 # premultiplied grey
    for c in range(3):
        img[:, :, c][edge] = np.clip(v, 0, 255)[edge].astype(np.uint8)
    img[:, :, 3][edge] = np.clip(a, 0, 255)[edge].astype(np.uint8)
