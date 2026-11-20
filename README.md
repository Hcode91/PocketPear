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

Wipeout (iOS 6.1.4 rootfs, iPhone5,2): dyld, libSystem, libdispatch (worker threads, kqueue),
Foundation, CoreFoundation, the app's own initialisers and Mono runtime all run, and it reaches
`UIApplicationMain`. GraphicsServices checks in its ports with the launchd stand-in. UIKit then
looks up SpringBoard's `PurpleWorkspacePort`, finds none, and exits cleanly with "Terminating
since there is no workspace". Next: SpringBoard, backboardd and CARenderServer stand-ins.
