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


def hand_info(kind, x, y):
    """GSHandInfo with one GSPathInfo, as GSEventGetHandInfo/GetPathInfoAtIndex read them on
    iOS 6: hand info 0x24 bytes (type, two touch counts, six floats, four bytes), then 0x1c-byte path infos (index, identity, proximity, pressure, major
    radius, location, window, one more word). The path count is the byte at +0x22."""
    down = kind != TOUCH_UP
    hand = struct.pack('<IHH6fBBBB', kind, 1, 1, *([0.0] * 6), 0, 0, 1, 0)
    path = struct.pack('<BBBxff2fII', 1, 2, 3 if down else 0, 1.0 if down else 0.0, 1.0, x, y, 0, 0)
    return hand + path


class Touches:
    """Scripted taps and drags (PP_TOUCH), sent to the app as GraphicsServices hand events the
    way backboardd sends a finger. Points are in the screen's portrait coordinates (320x568
    points on an iPhone 5), which UIKit rotates into the app's orientation itself.

    PP_TOUCH="t:x,y;..." taps at guest time t (seconds); "t:x,y>x2,y2" drags. With
    PP_TOUCH_SPACE=frame the points are pixels of the saved frames instead (1136x640 for a
    landscape game), turned into screen points for the landscape-right orientation, or
    landscape-left with PP_TOUCH_SPACE=frame-left."""

    def __init__(self, kernel, script):
        self.k = kernel
        self.space = os.environ.get('PP_TOUCH_SPACE', '')
        self.events = []                      # (time, kind, x, y)
        for item in filter(None, script.split(';')):
            t, _, where = item.partition(':')
            t = float(t)
            a, _, b = where.partition('>')
            x, y = self.to_screen(*map(float, a.split(',')))
            self.events.append((t, TOUCH_DOWN, x, y))
            if b:
                x2, y2 = self.to_screen(*map(float, b.split(',')))
                for i in range(1, 11):
                    f = i / 10
                    self.events.append((t + 0.02 * i, TOUCH_DRAGGED, x + (x2 - x) * f, y + (y2 - y) * f))
                self.events.append((t + 0.24, TOUCH_UP, x2, y2))
            else:
                self.events.append((t + 0.08, TOUCH_UP, x, y))
        self.events.sort()

    def inject(self, kind, x, y):
        """A live touch (from the Android app): kind 'down', 'move' or 'up', in frame pixels."""
        from sched import CLOCK
        k = {'down': TOUCH_DOWN, 'move': TOUCH_DRAGGED, 'up': TOUCH_UP}[kind]
        x, y = self.to_screen(x, y, space='frame')
        self.events.append((CLOCK.now(), k, x, y))

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
        port = self.k.launcher.port if self.k.launcher.sent else None
        return self.events[0][0] if self.events and port else None

    def deliver(self, now):
        port = self.k.launcher.port
        while self.events and self.k.launcher.sent and self.events[0][0] <= now:
            _, kind, x, y = self.events.pop(0)
            from ports import TOKEN_SYSTEM
            stamp = int(now * 24e6)
            # backboardd names the window's render context; UIKit finds the window by it
            contexts = getattr(self.k, 'render_contexts', [])
            context = int(os.environ.get('PP_TOUCH_CONTEXT', '0'), 0) or (contexts[-1] if contexts else 0)
            msg = gs_event(port, HAND, info=hand_info(kind, x, y), location=(x, y), timestamp=stamp,
                           context=context)
            self.k.pr.get(port).queue.append((msg, [], TOKEN_SYSTEM))
            print(f'[backboard] touch {("down", "drag", "", "", "up")[kind - 1]} at ({x:.0f},{y:.0f}) t={now:.2f}s')


from services.base import MigServer


class HIDServices(MigServer):
    """backboardd's HID services (accelerometer, buttons). Requests are acknowledged; sensor
    events are not produced yet."""
    name = 'hid'

    def default(self, req):
        return self.reply(req, ret=0)
