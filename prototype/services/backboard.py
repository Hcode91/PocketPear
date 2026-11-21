"""What backboardd and SpringBoard send a launching app.

On a device, once an app's main thread is in its run loop, the system sends it a GraphicsServices
event of type 2000 (application launch) on the purple port it checked in under its bundle ID.
UIKit's -[UIApplication handleEvent:withNewEvent:] ignores every other event until that one has
arrived, then calls -_runWithURL:payload:launchOrientation:statusBarStyle:statusBarHidden:,
which runs the app delegate's launch methods.
"""
import os
import struct

GSEVENT_MSG_ID = 0x7b
APPLICATION_LAUNCH = 2000


def gs_event(port, etype, subtype=0, info=b'', location=(0.0, 0.0), timestamp=0, sender_pid=50, context=0):
    """A GSEvent message as GSSendEvent builds it, in the form the receiver gets."""
    record = struct.pack('<ii2f2fIQIIII', etype, subtype, *location, *location, context, timestamp, 0,
                         sender_pid, 0, len(info)) + info
    # header as received: remote 0 (no reply wanted), local = the destination port
    msg_size = 24 + len(record)
    pad = (-msg_size) % 4
    return struct.pack('<6I', 0x13, msg_size + pad, 0, port, 0, GSEVENT_MSG_ID) + record + bytes(pad)


def launch_info(orientation=1, status_bar_style=0, status_bar_hidden=False):
    """The info block of an application-launch event, as handleEvent:withNewEvent: reads it:
    +0 context, +4 flags, +0xc launch orientation, +0x10 status bar style, +0x14 status bar
    hidden, +0x16/+0x17 two bytes, +0x18/+0x20 two doubles used when flag 0x10 is set."""
    return struct.pack('<iiiiibbbbdd', 0, 0, 0, orientation, status_bar_style,
                       1 if status_bar_hidden else 0, 0, 0, 0, 0.0, 0.0)


class Launcher:
    """Sends the launch event the first time the app's main thread waits on its purple port."""

    def __init__(self, kernel):
        self.k = kernel
        self.port = None
        self.sent = False

    def note_check_in(self, name, port, replace=False):
        """GraphicsServices checks in its bundle ID and then registers a port of its own under
        it; the registered one is the one its run loop listens on."""
        bundle = getattr(self.k.emu, 'bundle_id', None)
        if bundle and name == bundle and (self.port is None or replace):
            self.port = port

    def before_receive(self, rcv_name):
        if self.sent or self.port is None:
            return
        p = self.k.pr.get(rcv_name)
        if p is None:
            return
        if rcv_name != self.port and self.port not in p.members:
            return
        self.sent = True
        from ports import TOKEN_SYSTEM
        msg = gs_event(self.port, APPLICATION_LAUNCH, info=launch_info())
        self.k.pr.get(self.port).queue.append((msg, [], TOKEN_SYSTEM))
        print(f'[backboard] application-launch event sent to {self.port:#x}')


HAND = 3001                                   # kGSEventHand
TOUCH_DOWN, TOUCH_DRAGGED, TOUCH_UP = 1, 2, 5


def hand_info(kind, paths):
    """GSHandInfo with one GSPathInfo per finger, as GSEventGetHandInfo/GetPathInfoAtIndex read
    them on iOS 6: hand info 0x24 bytes (type, two touch counts, six floats, four bytes), then
    0x1c-byte path infos (index, identity, proximity, pressure, major radius, location, window,
    one more word). The path count is the byte at +0x22. paths: (slot, x, y, down) per finger;
    a finger lifting in this event is sent once more with proximity 0, which is how UIKit sees
    that touch end while the others carry on."""
    count = sum(1 for p in paths if p[3])
    hand = struct.pack('<IHH6fBBBB', kind, count, count, *([0.0] * 6), 0, 0, len(paths), 0)
    body = b''.join(struct.pack('<BBBxff2fII', slot + 1, slot + 2, 3 if down else 0, 1.0 if down else 0.0,
                                1.0, x, y, 0, 0) for slot, x, y, down in paths)
    return hand + body


class Touches:
    """Taps and drags, sent to the app as GraphicsServices hand events the way backboardd sends
    fingers. Live touches come from the Android app, several fingers at once; scripted ones from
    PP_TOUCH. Points are in the screen's portrait coordinates (320x568 points on an iPhone 5),
    which UIKit rotates into the app's orientation itself.

    PP_TOUCH="t:x,y;..." taps at guest time t (seconds); "t:x,y>x2,y2" drags. With
    PP_TOUCH_SPACE=frame the points are pixels of the saved frames instead (1136x640 for a
    landscape game), turned into screen points for the landscape-right orientation, or
    landscape-left with PP_TOUCH_SPACE=frame-left."""

    def __init__(self, kernel, script):
        self.k = kernel
        self.space = os.environ.get('PP_TOUCH_SPACE', '')
        self.events = []                      # (time, seq, kind, [(slot, x, y, down)])
        self.seq = 0
        self.live = {}                        # pointer id -> (slot, x, y)
        self.scripted = []                    # (time, pointer, 'down'|'move'|'up', x, y)
        for item in filter(None, script.split(';')):
            t, _, where = item.partition(':')
            t = float(t)
            a, _, b = where.partition('>')
            x, y = self.to_screen(*map(float, a.split(',')))
            self.scripted.append((t, 'down', x, y))
            if b:
                x2, y2 = self.to_screen(*map(float, b.split(',')))
                for i in range(1, 11):
                    f = i / 10
                    self.scripted.append((t + 0.02 * i, 'move', x + (x2 - x) * f, y + (y2 - y) * f))
                self.scripted.append((t + 0.24, 'up', x2, y2))
            else:
                self.scripted.append((t + 0.08, 'up', x, y))
        self.scripted.sort()

    def inject(self, kind, x, y, pointer=0):
        """One finger (from the Android app): kind 'down', 'move' or 'up', in frame pixels."""
        x, y = self.to_screen(x, y, space='frame')
        self.update({pointer: (kind, x, y)})

    def inject_all(self, ids, xs, ys, lifted=-1):
        """Every finger on the screen at once (from the Android app), in frame pixels; lifted
        is the pointer that has just gone up (still listed), or -1."""
        changes = {}
        for i, x, y in zip(ids, xs, ys):
            x, y = self.to_screen(float(x), float(y), space='frame')
            kind = 'up' if i == lifted else ('move' if i in self.live else 'down')
            changes[int(i)] = (kind, x, y)
        for i in list(self.live):             # fingers the app no longer lists have gone
            if i not in changes:
                _, x, y = self.live[i]
                changes[i] = ('up', x, y)
        self.update(changes)

    def update(self, changes, at=None):
        """Applies finger changes (pointer -> (kind, x, y)) and queues the hand event."""
        from sched import CLOCK
        was_down = bool(self.live)
        paths, self.new_slots = [], getattr(self, 'new_slots', set())
        for i, (kind, x, y) in changes.items():
            if kind == 'down' and i not in self.live:
                used = {s for s, _, _ in self.live.values()}
                slot = next(s for s in range(32) if s not in used)
                self.live[i] = (slot, x, y)
                self.new_slots.add(slot)
            elif i in self.live:
                self.live[i] = (self.live[i][0], x, y)
            else:
                continue
            if kind == 'up':
                slot = self.live.pop(i)[0]
                paths.append((slot, x, y, False))
        for i, (slot, x, y) in self.live.items():
            paths.append((slot, x, y, True))
        if not paths:
            return
        paths.sort()
        kind = TOUCH_UP if not self.live else (TOUCH_DRAGGED if was_down else TOUCH_DOWN)
        self.seq += 1
        self.events.append((CLOCK.now() if at is None else at, self.seq, kind, paths))

    def to_screen(self, x, y, space=None):
        """Frame pixels of a landscape game -> portrait screen points (320x568, 2x)."""
        space = space or self.space
        if not space.startswith('frame'):
            return x, y
        try:
            w, h = self.k.emu.gles.screen
        except AttributeError:
            w, h = 1136, 640
        if w < h:                                     # a portrait game: frame pixels / 2
            return x / 2, y / 2
        lx, ly = x / 2, y / 2                         # landscape points
        if space == 'frame-left' or self.space == 'frame-left':
            return ly, 568 - lx
        return 320 - ly, lx

    def next_due(self):
        if not self.k.launcher.sent or self.k.launcher.port is None:
            return None
        ts = [e[0] for e in self.events[:1]] + [e[0] for e in self.scripted[:1]]
        return min(ts) if ts else None

    def deliver(self, now):
        if not self.k.launcher.sent:
            return
        while self.scripted and self.scripted[0][0] <= now:
            t, kind, x, y = self.scripted.pop(0)
            self.update({-1: (kind, x, y)}, at=t)
        port = self.k.launcher.port
        while self.events and self.events[0][0] <= now:
            _, _, kind, paths = self.events.pop(0)
            from ports import TOKEN_SYSTEM
            stamp = int(now * 24e6)
            # backboardd names the window's render context; UIKit finds the window by it
            contexts = getattr(self.k, 'render_contexts', [])
            context = int(os.environ.get('PP_TOUCH_CONTEXT', '0'), 0) or (contexts[-1] if contexts else 0)
            first = next((p for p in paths if p[3]), paths[0])
            msg = gs_event(port, HAND, info=hand_info(kind, paths), location=(first[1], first[2]),
                           timestamp=stamp, context=context)
            self.k.pr.get(port).queue.append((msg, [], TOKEN_SYSTEM))
            new = {p[0] for p in paths if p[3]} & self.new_slots
            self.new_slots -= new
            if kind != TOUCH_DRAGGED or new or any(not p[3] for p in paths) or os.environ.get('PP_TOUCH_TRACE'):
                fingers = ' '.join(f'{s}:{"down" if d else "up"}({x:.0f},{y:.0f})' for s, x, y, d in paths)
                print(f'[backboard] touch {("down", "drag", "", "", "up")[kind - 1]} t={now:.2f}s {fingers}')


from services.base import MigServer


class HIDServices(MigServer):
    """backboardd's HID services (accelerometer, buttons). Requests are acknowledged; sensor
    events are not produced yet."""
    name = 'hid'

    def default(self, req):
        return self.reply(req, ret=0)
