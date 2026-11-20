"""What backboardd and SpringBoard send a launching app.

On a device, once an app's main thread is in its run loop, the system sends it a GraphicsServices
event of type 2000 (application launch) on the purple port it checked in under its bundle ID.
UIKit's -[UIApplication handleEvent:withNewEvent:] ignores every other event until that one has
arrived, then calls -_runWithURL:payload:launchOrientation:statusBarStyle:statusBarHidden:,
which runs the app delegate's launch methods.
"""
import struct

GSEVENT_MSG_ID = 0x7b
APPLICATION_LAUNCH = 2000


def gs_event(port, etype, subtype=0, info=b'', location=(0.0, 0.0), timestamp=0, sender_pid=50):
    """A GSEvent message as GSSendEvent builds it, in the form the receiver gets."""
    record = struct.pack('<ii2f2fIQIIII', etype, subtype, *location, *location, 0, timestamp, 0,
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
