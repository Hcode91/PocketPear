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
`BREAK=addr,...` (`BREAK_CFSTR`, `BREAK_OBJ`, `BREAK_DUMP=addr,...` say what to print there),
`WATCH=addr` (writes to 8 bytes, by guest code or by the kernel), `TRACK_CF=1`.

## Where it has got to

Wipeout (iOS 6.1.4 rootfs, iPhone5,2) now launches like on a device: dyld, libSystem,
libdispatch, Foundation and the app's initialisers run, `UIApplicationMain` checks in its ports,
the backboard stand-in sends the GraphicsServices application-launch event (type 2000), UIKit
registers a render context with the CARenderServer stand-in, the app delegate's
`applicationDidFinishLaunching` runs, preferences are saved in the container, and the game's
Mono runtime loads `mscorlib` and the game's assemblies; it stops at "gfx device initialization failed", i.e. OpenGL ES. Core Animation commits arrive at the render server
stand-in but are not drawn yet.

pthread mutexes, condition variables and rwlocks are implemented at the libsystem_c function
level (`prototype/hle.py`) rather than through the kernel's psynch calls. Threads switch only at
system calls (and every 512th call), because stopping Unicorn mid-instruction-stream loses the
Thumb IT-block state; `PP_SLICE=n` forces instruction-count slices anyway, and `PP_WATCHDOG`
(seconds) reports a thread that never makes a call that waits.
