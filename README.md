# PocketPear

Private, experimental: run iPhone games on Android by running the game's own ARM code together
with Apple's own system frameworks, taken from a firmware image the user supplies. Only the
kernel is imitated (BSD system calls, Mach traps and messages, the commpage), plus the system
services an app talks to, and OpenGL ES is translated to the host GPU.

No Apple code, firmware or apps belong in this repository. See `.gitignore`.

- `prototype/` - Python + Unicorn prototype used to find out what the frameworks need.
- `tools/` - helpers that read the user's shared cache: a disassembler by symbol name, and the
  generator for `prototype/bsdnums.json` (BSD system call numbers, taken from libsystem_kernel's
  own stubs).

## Running the prototype

    python3 prototype/emu.py <decrypted rootfs> <App.app>

The app's data container (Documents, Library, tmp) is kept per app under `~/.pocketpear/`
(`PP_DATA` moves it). Guest time is virtual, so a run is the same every time; set
`PP_REALTIME=1` to follow the host clock instead.

Debugging switches: `TRACE=1` (file system calls), `TRACE_SYS=1` (every call with its result),
`BREAK=addr,...` (`BREAK_CFSTR`, `BREAK_OBJ`, `BREAK_DUMP=addr[:words],...` say what to print
there, `BREAK_BT=n` prints a backtrace every nth hit, `BREAK_TRACE=1` turns on `TRACE_SYS` from
the first hit), `WATCH=addr` (writes to 8 bytes, by guest code or by the kernel), `TRACK_CF=1`,
`PP_TRACE_FROM=n` (trace from the nth call), `PP_HEARTBEAT=s` with `PP_BT_THREADS=frames`
(every thread's state and backtrace every s seconds), `BT_REQTHREADS=1` (who asks libdispatch
for worker threads). Frames are saved as PNGs to `PP_FRAMES` (default `/tmp/pocketpear-frames`)
every `PP_FRAME_EVERY` presents.

## Where it has got to

Wipeout (iOS 6.1.4 rootfs, iPhone5,2) now launches like on a device: dyld, libSystem,
libdispatch, Foundation and the app's initialisers run, `UIApplicationMain` checks in its ports,
the backboard stand-in sends the GraphicsServices application-launch event (type 2000), UIKit
registers a render context with the CARenderServer stand-in, the app delegate's
`applicationDidFinishLaunching` runs, preferences are saved in the container, and the game's
Mono runtime loads `mscorlib` and the game's assemblies, OpenGL ES runs on the host GPU, and the
game draws its studio logos, title screen and full main menu (1136x640, multisampled) at 60
frames a second of guest time, and taps reach Unity's view. Core Animation commits arrive at the
render server stand-in but are not drawn (Unity draws everything itself). No sound yet.

Tapping "Play Wipeout!" and then "Yes" on the first-time dialog starts the audition: the 3D
tutorial course renders and the game asks for thumb input.

Touches are scripted for now: `PP_TOUCH="t:x,y;t:x,y>x2,y2"` taps (or drags) at guest time t
seconds, in the screen's portrait points (320x568), or with `PP_TOUCH_SPACE=frame` in the
saved frames' pixels (landscape right; `frame-left` for landscape left). For example
`PP_TOUCH_SPACE=frame PP_TOUCH="9:430,280;11:840,535"` reaches Wipeout's tutorial. They are sent as GraphicsServices hand events
(type 3001) carrying the app window's render context, as backboardd sends a finger. The game's
own log (NSLog, ASL, Unity's Debug.Log) is printed as `[log]` lines by a
`com.apple.system.logger` stand-in, and SpringBoard's `GSCapabilities` shared memory object
(`prototype/capabilities.py`) answers capability questions as an iPhone 5 would.

`CADisplayLink` is replaced (`prototype/displaylink.py`): without a display server there is no
vsync, so each tick borrows the main thread while its run loop waits in `mach_msg`, calls the
target, and puts the wait back. UIScreen and GraphicsServices are given an iPhone 5's screen
(640x1136 at 2x).

pthread mutexes, condition variables and rwlocks are implemented at the libsystem_c function
level (`prototype/hle.py`) rather than through the kernel's psynch calls. Threads switch only at
system calls (and every 512th call), because stopping Unicorn mid-instruction-stream loses the
Thumb IT-block state; `PP_SLICE=n` forces instruction-count slices anyway, and `PP_WATCHDOG`
(seconds) reports a thread that never makes a call that waits.


## Android app (`android/`)

A Kotlin app (package `com.hcode91.pocketpear`) that runs the prototype on the phone through
Chaquopy (Python 3.13), with Unicorn cross-built for arm64 and the phone's own OpenGL ES.
It is the same emulator as the desktop runs, so it is just as slow: a demo, not yet playable.
The fast native core (step 4) replaces it later.

- **Firmware**: *Import firmware (.zip)* takes a zip of an iPhone 5 iOS 6 root filesystem
  (any folder level; it must contain
  `System/Library/Caches/com.apple.dyld/dyld_shared_cache_armv7s`). Symlinks are kept.
- **Games**: *Import game (.ipa)* takes a decrypted `.ipa`. Long-press a game to remove it.
- **Playing**: frames are shown as they are presented; touches go to the game live. Each run
  is its own process (`:game`), so Back ends it cleanly. The full log of a run is
  `files/run.log` in the app's storage.

Building: GitHub Actions (`.github/workflows/android.yml`) builds `app-debug.apk` on every push
to `main` (artifact *PocketPear-apk*). Locally: `android/build-unicorn.sh <ndk>` once (builds
`libunicorn.so` and copies Unicorn's Python binding), then `./gradlew assembleDebug` in
`android/`. The APK is signed with a fixed key in the repo, so new builds install over old ones.
