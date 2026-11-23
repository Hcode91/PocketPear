"""MPMoviePlayerController without mediaserverd.

The real controller plays through AVFoundation, whose decoding runs in mediaserverd; with no
such daemon every movie ends at once with MPMovieFinishReasonPlaybackError. Its public methods
are replaced here: the movie is played by the host (Android's MediaPlayer, under the game's
frame), and the controller's view is a plain UIView in a key colour, so wherever it shows on
screen (under any views the game puts over it) the frame is left transparent and the host's
video shows through. A movie whose view is not in a window is shown full screen, as iOS shows a
fullscreen player in a window of its own.

Notifications go out on the main thread, as on a device: load state and playback state changes,
and MPMoviePlayerPlaybackDidFinishNotification when the movie ends or is stopped.
"""
import os
import struct

from unicorn.arm_const import *

from guestcall import InlineCalls
from sched import CLOCK

MOVIE_KEY = (0, 255, 255, 255)

# MPMoviePlaybackState
STOPPED, PLAYING, PAUSED = 0, 1, 2
# MPMovieFinishReason
ENDED, ERROR, USER_EXITED = 0, 1, 2


class Player:
    def __init__(self, obj):
        self.obj = obj
        self.url = 0
        self.path = None
        self.view = 0
        self.state = STOPPED
        self.prepared = False
        self.repeat = False
        self.fullscreen = False
        self.control_style = 1
        self.scaling = 1
        self.source_type = 0
        self.autoplay = True
        self.position = 0.0
        self.started = None
        self.duration = 0.0
        self.size = (0.0, 0.0)
        self.id = 0


METHODS = {
    'init': 'm_init', 'initWithContentURL:': 'm_init_url', 'view': 'm_view', 'backgroundView': 'm_view',
    'contentURL': 'm_content_url', 'setContentURL:': 'm_set_content_url',
    'prepareToPlay': 'm_prepare', 'play': 'm_play', 'pause': 'm_pause', 'stop': 'm_stop',
    'playbackState': 'm_state', 'loadState': 'm_load_state', 'isPreparedToPlay': 'm_prepared',
    'readyForDisplay': 'm_prepared', 'currentPlaybackTime': 'm_time', 'setCurrentPlaybackTime:': 'm_set_time',
    'duration': 'm_duration', 'playableDuration': 'm_duration', 'naturalSize': 'm_natural_size',
    'currentPlaybackRate': 'm_rate', 'setCurrentPlaybackRate:': 'm_ignore',
    'repeatMode': 'm_repeat', 'setRepeatMode:': 'm_set_repeat',
    'isFullscreen': 'm_fullscreen', 'setFullscreen:': 'm_set_fullscreen', 'setFullscreen:animated:': 'm_set_fullscreen',
    'controlStyle': 'm_control_style', 'setControlStyle:': 'm_set_control_style',
    'scalingMode': 'm_scaling', 'setScalingMode:': 'm_set_scaling',
    'movieSourceType': 'm_source_type', 'setMovieSourceType:': 'm_set_source_type',
    'shouldAutoplay': 'm_autoplay', 'setShouldAutoplay:': 'm_set_autoplay',
    'setInitialPlaybackTime:': 'm_ignore', 'setEndPlaybackTime:': 'm_ignore',
    'initialPlaybackTime': 'm_zero_double', 'endPlaybackTime': 'm_minus_one_double',
    'setAllowsAirPlay:': 'm_ignore', 'allowsAirPlay': 'm_zero', 'isAirPlayVideoActive': 'm_zero',
    # anything else (setUseApplicationAudioSession:, setMovieControlMode:, ...) the real
    # controller forwards to a private object ours does not have: it is answered with nil
    'methodSignatureForSelector:': 'm_signature', 'forwardInvocation:': 'm_forward',
    'movieMediaTypes': 'm_zero', 'skipToBeginning': 'm_skip_to_beginning',
    'beginSeekingForward': 'm_ignore', 'beginSeekingBackward': 'm_ignore', 'endSeeking': 'm_ignore',
    'dealloc': 'm_dealloc',
}

# MPMoviePlayerViewController: its own methods reach into MPMoviePlayerController's private
# objects (_videoViewController), which the replaced controller does not have, so it is
# replaced too: it owns one of ours and shows its view, and plays when it appears.
VC_METHODS = {
    'init': 'vc_init', 'initWithContentURL:': 'vc_init_url', 'moviePlayer': 'vc_player',
    'loadView': 'vc_load_view', 'viewWillAppear:': 'vc_appear', 'viewDidAppear:': 'vc_appear',
    'viewWillDisappear:': 'vc_nothing', 'viewDidDisappear:': 'vc_nothing',
    'shouldAutorotateToInterfaceOrientation:': 'vc_yes', 'canBecomeFirstResponder': 'vc_yes',
    'willAnimateRotationToInterfaceOrientation:duration:': 'vc_nothing',
    'didRotateFromInterfaceOrientation:': 'vc_nothing', 'remoteControlReceivedWithEvent:': 'vc_nothing',
    '_restoreStatusBarAnimated:': 'vc_nothing', '_canReloadView': 'vc_no',
    '_shouldUseFullScreenLayoutInWindow:parentViewController:': 'vc_yes',
    '_moviePlayerViewController_playbackDidFinishNotification:': 'vc_nothing',
    '_moviePlayerViewController_applicationDidEnterBackgroundNotification:': 'vc_nothing',
    'dealloc': 'vc_dealloc',
}


def dbits(x):
    return struct.unpack('<II', struct.pack('<d', x))


class Movies:
    def __init__(self, emu):
        self.emu = emu
        self.uc = emu.uc
        self.players = {}
        self.next_id = 1
        self.sink = None              # host player: sink(event, player_id, path, repeat); see android
        self.posts = []               # (player, notification symbol, reason or None)
        self.inbox = []               # events from the host player's thread
        self.dealloc_real = None
        self.calls = None

    def install(self, stubs):
        names = {}
        for n, a in zip(self.emu.syms.names, self.emu.syms.addrs):
            names.setdefault(n, a)
        self.names = names
        made = 0
        for sel, attr in METHODS.items():
            a = names.get(f'-[MPMoviePlayerController {sel}] (MediaPlayer)')
            if a is None:
                continue
            stubs.replace(a, getattr(self, attr), a in self.emu.syms.thumb)
            made += 1
        if not made:
            return
        self.view_controllers = {}   # MPMoviePlayerViewController -> its MPMoviePlayerController
        vc = 0
        import bisect
        starts = sorted(set(self.emu.syms.addrs))
        for sel, attr in VC_METHODS.items():
            a = names.get(f'-[MPMoviePlayerViewController {sel}] (MediaPlayer)')
            i = bisect.bisect_right(starts, a) if a is not None else 0
            if a is not None and i < len(starts) and starts[i] - a < 8:
                continue        # too short to hold a stub (one that only returns YES, say): kept
            if a is not None:
                stubs.replace(a, getattr(self, attr), a in self.emu.syms.thumb)
                vc += 1
        if vc:
            print(f'[movie] MPMoviePlayerViewController: {vc} methods replaced')
        self.calls = InlineCalls(self.emu, stubs)
        self.fn = {n.split(' (')[0]: a for n, a in names.items() if n.startswith('_') and ' (' in n}
        print(f'[movie] MPMoviePlayerController: {made} methods replaced; movies play on the host')

    # ------------------------------------------------------------ helpers
    def arg(self, n):
        if n < 4:
            return self.uc.reg_read(UC_ARM_REG_R0 + n)
        return self.emu.r32(self.uc.reg_read(UC_ARM_REG_SP) + 4 * (n - 4))

    def ret(self, v=0):
        self.uc.reg_write(UC_ARM_REG_R0, v & 0xffffffff)

    def ret_double(self, x):
        lo, hi = dbits(x)
        self.uc.reg_write(UC_ARM_REG_R0, lo)
        self.uc.reg_write(UC_ARM_REG_R1, hi)

    def player(self):
        return self.players.get(self.arg(0))

    def sel(self, name):
        if not hasattr(self, '_sels'):
            self._sels = {}
            self._strings = 0
        if name not in self._sels:
            a = self.cstring(name)
            r, _ = yield (self.fn['_sel_registerName'], [a])
            self._sels[name] = r
        return self._sels[name]

    def cstring(self, s):
        if not getattr(self, '_strpage', 0):
            self._strpage = self.emu.find_free(0x4000)
            self.emu.map(self._strpage, 0x4000)
            self._strpos = self._strpage
        b = s.encode() + b'\0'
        a = self._strpos
        self.emu.write(a, b)
        self._strpos += (len(b) + 3) & ~3
        return a

    def send(self, obj, selname, *args):
        s = yield from self.sel(selname)
        r = yield (self.emu.objc_msgsend, [obj, s, *args])
        return r[0]

    def cls(self, name):
        if not hasattr(self, '_classes'):
            self._classes = {}
        if name not in self._classes:
            r, _ = yield (self.fn['_objc_getClass'], [self.cstring(name)])
            self._classes[name] = r
        return self._classes[name]

    # ------------------------------------------------------------ the controller
    def setup(self, obj, url):
        p = Player(obj)
        p.id = self.next_id
        self.next_id += 1
        self.players[obj] = p
        view_cls = yield from self.cls('UIView')
        v = yield from self.send(view_cls, 'alloc')
        # initWithFrame: CGRectZero (four float zeros: r2, r3 and two on the stack)
        s = yield from self.sel('initWithFrame:')
        v, _ = yield (self.emu.objc_msgsend, [v, s, 0, 0, 0, 0])
        color_cls = yield from self.cls('UIColor')
        f = [struct.unpack('<I', struct.pack('<f', c / 255))[0] for c in MOVIE_KEY]
        s = yield from self.sel('colorWithRed:green:blue:alpha:')
        color, _ = yield (self.emu.objc_msgsend, [color_cls, s, *f])
        yield from self.send(v, 'setBackgroundColor:', color)
        yield from self.send(v, 'setAutoresizingMask:', 0x12)     # flexible width and height
        p.view = v
        yield from self.set_url(p, url)
        return obj

    def set_url(self, p, url):
        if p.url:
            yield from self.send(p.url, 'release')
        p.url = url
        p.path = None
        if url:
            yield from self.send(url, 'retain')
            path = yield from self.send(url, 'path')
            c = (yield from self.send(path, 'UTF8String')) if path else 0
            if c:
                guest = self.emu.cstr(c)
                try:
                    p.path = self.emu.host_path(guest)
                except Exception:
                    p.path = None
                print(f'[movie] player {p.id}: {guest}' + ('' if p.path and os.path.exists(p.path) else ' (not found on the host)'))
        p.prepared = False

    def m_init(self):
        self.calls.run(self.setup(self.arg(0), 0))

    def m_init_url(self):
        self.calls.run(self.setup(self.arg(0), self.arg(2)))

    def m_view(self):
        p = self.player()
        self.ret(p.view if p else 0)

    def m_content_url(self):
        p = self.player()
        self.ret(p.url if p else 0)

    def m_set_content_url(self):
        p = self.player()
        if p is None:
            self.ret(0)
            return
        was = p.state
        self.host_stop(p)

        def g():
            yield from self.set_url(p, self.arg_saved)
            if was == PLAYING:
                self.start(p)
        self.arg_saved = self.arg(2)
        self.calls.run(g())

    def m_prepare(self):
        p = self.player()
        if p and not p.prepared:
            p.prepared = True
            self.post(p, 'MPMoviePlayerLoadStateDidChangeNotification')
        self.ret(0)

    def start(self, p):
        if not p.prepared:
            p.prepared = True
            self.post(p, 'MPMoviePlayerLoadStateDidChangeNotification')
        if not p.path or not os.path.exists(p.path):
            print(f'[movie] player {p.id}: nothing to play; it ends with an error')
            p.state = STOPPED
            self.post(p, 'MPMoviePlayerPlaybackDidFinishNotification', ERROR)
            return
        resume = p.state == PAUSED
        p.state = PLAYING
        p.started = CLOCK.now() - p.position
        self.post(p, 'MPMoviePlayerPlaybackStateDidChangeNotification')
        if self.sink is not None:
            self.sink('resume' if resume else 'play', p.id, p.path, p.repeat)
        print(f'[movie] player {p.id} {"resumed" if resume else "plays"} {os.path.basename(p.path)}'
              + (' (repeating)' if p.repeat else '') + ('' if self.sink else '; no host player, so it ends after a few seconds'))

    def m_play(self):
        p = self.player()
        if p and p.state != PLAYING:
            self.start(p)
        self.ret(0)

    def m_pause(self):
        p = self.player()
        if p and p.state == PLAYING:
            p.position = CLOCK.now() - p.started
            p.state = PAUSED
            if self.sink is not None:
                self.sink('pause', p.id, p.path, p.repeat)
            self.post(p, 'MPMoviePlayerPlaybackStateDidChangeNotification')
        self.ret(0)

    def host_stop(self, p):
        if p.state != STOPPED and self.sink is not None:
            self.sink('stop', p.id, p.path, p.repeat)

    def m_stop(self):
        p = self.player()
        if p and p.state != STOPPED:
            self.host_stop(p)
            p.state = STOPPED
            p.position = 0.0
            self.post(p, 'MPMoviePlayerPlaybackStateDidChangeNotification')
            self.post(p, 'MPMoviePlayerPlaybackDidFinishNotification', ENDED)
        self.ret(0)

    def m_skip_to_beginning(self):
        p = self.player()
        if p:
            p.position = 0.0
            p.started = CLOCK.now()
            if p.state == PLAYING and self.sink is not None:
                self.sink('restart', p.id, p.path, p.repeat)
        self.ret(0)

    def m_state(self):
        p = self.player()
        self.ret(p.state if p else 0)

    def m_load_state(self):
        p = self.player()
        self.ret(3 if p and p.prepared else 0)     # playable | playthrough OK

    def m_prepared(self):
        p = self.player()
        self.ret(1 if p and p.prepared else 0)

    def position(self, p):
        if p.state == PLAYING and p.started is not None:
            return CLOCK.now() - p.started
        return p.position

    def m_time(self):
        p = self.player()
        self.ret_double(self.position(p) if p else 0.0)

    def m_set_time(self):
        p = self.player()
        if p:
            p.position = struct.unpack('<d', struct.pack('<II', self.arg(2), self.arg(3)))[0]
            p.started = CLOCK.now() - p.position
        self.ret(0)

    def m_duration(self):
        p = self.player()
        self.ret_double(p.duration if p else 0.0)

    def m_natural_size(self):
        # CGSize is returned through a pointer in r0; self and _cmd move to r1 and r2
        out = self.arg(0)
        p = self.players.get(self.arg(1))
        w, h = p.size if p and p.size[0] else (480.0, 320.0)
        self.emu.write(out, struct.pack('<ff', w, h))

    def m_rate(self):
        p = self.player()
        self.uc.reg_write(UC_ARM_REG_R0, struct.unpack('<I', struct.pack('<f', 1.0 if p and p.state == PLAYING else 0.0))[0])

    def m_repeat(self):
        p = self.player()
        self.ret(1 if p and p.repeat else 0)

    def m_set_repeat(self):
        # Bioshock starts its intro movie and only then sets "no repeat"; a host player already
        # looping has to be told, or the movie plays for ever and the game waits on its end.
        p = self.player()
        if p:
            repeat = self.arg(2) == 1
            if repeat != p.repeat and p.state != STOPPED and self.sink is not None:
                self.sink('repeat', p.id, p.path, repeat)
            p.repeat = repeat
        self.ret(0)

    def m_fullscreen(self):
        p = self.player()
        self.ret(1 if p and p.fullscreen else 0)

    def m_set_fullscreen(self):
        p = self.player()
        if p:
            p.fullscreen = bool(self.arg(2) & 0xff)
        self.ret(0)

    def m_control_style(self):
        p = self.player()
        self.ret(p.control_style if p else 0)

    def m_set_control_style(self):
        p = self.player()
        if p:
            p.control_style = self.arg(2)
        self.ret(0)

    def m_scaling(self):
        p = self.player()
        self.ret(p.scaling if p else 1)

    def m_set_scaling(self):
        p = self.player()
        if p:
            p.scaling = self.arg(2)
        self.ret(0)

    def m_source_type(self):
        p = self.player()
        self.ret(p.source_type if p else 0)

    def m_set_source_type(self):
        p = self.player()
        if p:
            p.source_type = self.arg(2)
        self.ret(0)

    def m_autoplay(self):
        p = self.player()
        self.ret(1 if p is None or p.autoplay else 0)

    def m_set_autoplay(self):
        p = self.player()
        if p:
            p.autoplay = bool(self.arg(2) & 0xff)
        self.ret(0)

    def m_ignore(self):
        self.ret(0)

    def m_signature(self):
        sel = self.arg(2)
        if not hasattr(self, '_unknown'):
            self._unknown = set()
        if sel not in self._unknown:
            self._unknown.add(sel)
            try:
                name = self.emu.cstr(sel)
            except Exception:
                name = hex(sel)
            print(f'[movie] MPMoviePlayerController {name} is not implemented; it answers nil')

        def g():
            if not getattr(self, '_any_sig', 0):
                cls = yield from self.cls('NSMethodSignature')
                sig = yield from self.send(cls, 'signatureWithObjCTypes:', self.cstring('@@:'))
                yield from self.send(sig, 'retain')
                self._any_sig = sig
            return self._any_sig
        self.calls.run(g())

    def m_forward(self):
        # the invocation's return value is left nil
        self.ret(0)

    def m_one(self):
        self.ret(1)

    def m_zero(self):
        self.ret(0)

    def m_zero_double(self):
        self.ret_double(0.0)

    def m_minus_one_double(self):
        self.ret_double(-1.0)

    def m_dealloc(self):
        obj = self.arg(0)
        p = self.players.pop(obj, None)
        if p is None:
            self.ret(0)
            return
        print(f'[movie] player {p.id} released')
        self.host_stop(p)
        self.posts = [x for x in self.posts if x[0] is not p]

        def g():
            if p.view:
                yield from self.send(p.view, 'removeFromSuperview')
                yield from self.send(p.view, 'release')
            if p.url:
                yield from self.send(p.url, 'release')
            # [super dealloc]: NSObject's
            nsobject = yield from self.cls('NSObject')
            s = yield from self.sel('dealloc')
            sup = self.cstring('')
            self._strpos += 8
            self.emu.write(sup, struct.pack('<II', obj, nsobject))
            yield (self.fn['_objc_msgSendSuper'], [sup, s])
        self.calls.run(g())

    # ------------------------------------------------------------ MPMoviePlayerViewController
    def super_send(self, obj, cls_name, selname, *args):
        """[super selname] for obj, whose superclass is cls_name's."""
        if not getattr(self, '_super_struct', 0):
            self._super_struct = self.cstring('12345678')
        cls = yield from self.cls(cls_name)
        s = yield from self.sel(selname)
        self.emu.write(self._super_struct, struct.pack('<II', obj, cls))
        r = yield (self.fn['_objc_msgSendSuper2'], [self._super_struct, s, *args])
        return r[0]

    def vc_setup(self, me, url):
        me = yield from self.super_send(me, 'MPMoviePlayerViewController', 'init')
        if not me:
            return 0
        cls = yield from self.cls('MPMoviePlayerController')
        player = yield from self.send(cls, 'alloc')
        # the replaced -init / -initWithContentURL:, run here rather than sent: an inline call
        # cannot make inline calls of its own
        player = yield from self.setup(player, url)
        self.view_controllers[me] = player
        return me

    def vc_init(self):
        self.calls.run(self.vc_setup(self.arg(0), 0))

    def vc_init_url(self):
        self.calls.run(self.vc_setup(self.arg(0), self.arg(2)))

    def vc_player(self):
        self.ret(self.view_controllers.get(self.arg(0), 0))

    def vc_load_view(self):
        me = self.arg(0)
        player = self.view_controllers.get(me)
        p = self.players.get(player)

        def g():
            if p is not None and p.view:
                yield from self.send(me, 'setView:', p.view)
        self.calls.run(g())

    def vc_appear(self):
        p = self.players.get(self.view_controllers.get(self.arg(0)))
        if p is not None and p.autoplay and p.state != PLAYING:
            self.start(p)
        self.ret(0)

    def vc_nothing(self):
        self.ret(0)

    def vc_yes(self):
        self.ret(1)

    def vc_no(self):
        self.ret(0)

    def vc_dealloc(self):
        me = self.arg(0)
        player = self.view_controllers.pop(me, 0)
        print(f'[movie] view controller {me:#x} released')

        def g():
            if player:
                p = self.players.get(player)
                if p is not None and p.state != STOPPED:
                    self.host_stop(p)
                    p.state = STOPPED
                yield from self.send(player, 'release')
            yield from self.super_send(me, 'MPMoviePlayerViewController', 'dealloc')
        self.calls.run(g())

    # ------------------------------------------------------------ host events and notifications
    def post(self, p, name, reason=None):
        self.posts.append((p, name, reason))

    def finished(self, pid, reason=ENDED, duration=None, size=None):
        """The host player ended a movie (or reports its length and size)."""
        for p in self.players.values():
            if p.id != pid:
                continue
            if duration is not None:
                p.duration = duration
            if size is not None:
                p.size = size
            if reason is None:
                return
            if p.state == PLAYING:
                p.state = STOPPED
                p.position = 0.0
                print(f'[movie] player {p.id} finished' + (' with an error' if reason == ERROR else ''))
                self.post(p, 'MPMoviePlayerPlaybackStateDidChangeNotification')
                self.post(p, 'MPMoviePlayerPlaybackDidFinishNotification', reason)

    def tick(self):
        """Takes the host player's events; without a host player a movie 'plays' for a few
        seconds (enough to see the flow)."""
        while self.inbox:
            ev = self.inbox.pop(0)
            if ev[0] == 'done':
                self.finished(ev[1], ev[2])
            elif ev[0] == 'info':
                self.finished(ev[1], None, ev[2], ev[3])
        if self.sink is not None:
            return
        limit = float(os.environ.get('PP_MOVIE_SECONDS', 5))
        for p in list(self.players.values()):
            if p.state == PLAYING and not p.repeat and CLOCK.now() - p.started >= limit - 1e-6:
                self.finished(p.id)

    def pending_job(self):
        """A notification to post on the main thread, as a guest-call generator, or None."""
        self.tick()
        if not self.posts:
            return None
        # everything queued goes out in one go, as one player event's notifications did on the
        # phone: a game that frees its observer when the first arrives (Splinter Cell's game
        # thread, a frame later) must not find the second still waiting for the next delivery
        posts, self.posts = self.posts, []

        def all_of():
            for p, name, reason in posts:
                yield from self.post_job(p, name, reason)
        return all_of()

    def post_job(self, p, name, reason):
        var = self.names.get(f'_{name} (MediaPlayer)')
        if var is None:
            return
        print(f'[movie] player {p.id}: {name}' + ('' if reason is None else f' (reason {reason})'))
        nsname = self.emu.r32(var)
        pool, _ = yield (self.fn['_objc_autoreleasePoolPush'], [])
        center_cls = yield from self.cls('NSNotificationCenter')
        center = yield from self.send(center_cls, 'defaultCenter')
        info = 0
        if reason is not None:
            key = self.emu.r32(self.names[f'_MPMoviePlayerPlaybackDidFinishReasonUserInfoKey (MediaPlayer)'])
            num_cls = yield from self.cls('NSNumber')
            num = yield from self.send(num_cls, 'numberWithInt:', reason)
            dict_cls = yield from self.cls('NSDictionary')
            info = yield from self.send(dict_cls, 'dictionaryWithObject:forKey:', num, key)
        # another thread may have released the player while this was being built (Splinter Cell
        # stops and frees its movie from its game thread on the state change); a released
        # player posts nothing, and its observers may be gone with it
        if self.players.get(p.obj) is p:
            yield from self.send(center, 'postNotificationName:object:userInfo:', nsname, p.obj, info)
        else:
            print(f'[movie] player {p.id} was released; {name} not posted')
        yield (self.fn['_objc_autoreleasePoolPop'], [pool])

    def next_due(self):
        if self.posts or self.inbox:
            return CLOCK.now()
        if self.sink is not None and self.playing():
            return CLOCK.now() + 0.1          # look for the host player's events now and then
        if self.sink is None:
            limit = float(os.environ.get('PP_MOVIE_SECONDS', 5))
            ds = [p.started + limit for p in self.players.values() if p.state == PLAYING and not p.repeat]
            return min(ds) if ds else None
        return None

    def playing(self):
        return any(p.state == PLAYING for p in self.players.values())
