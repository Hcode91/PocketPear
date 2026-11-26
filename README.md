# PocketPear

Play old iPhone games on Android. PocketPear runs a game's own ARM code together with Apple's own
system frameworks, which it takes from an iPhone firmware image you supply. Only the iPhone's
kernel is imitated (BSD system calls, Mach traps and messages, the commpage), plus the system
services a game talks to, and OpenGL ES is passed to the phone's own GPU.

> PocketPear is an independent fan project. It is not affiliated with, endorsed by or connected
> to Apple or any game publisher. "iPhone" and "iOS" are trademarks of Apple Inc., used here only
> to say which games the app works with. **No firmware, Apple code or games are included**, and
> none belong in this repository (see `.gitignore`). Use it only with firmware and game files you
> are entitled to use.

## Games confirmed working

Tested on a Galaxy S23. "Working" means the game starts and plays; see the limits below.

- BioShock
- N.O.V.A.
- Resident Evil: Degeneration
- WWE SmackDown vs. Raw 2010
- Wipeout

Games not listed may work too; they just haven't been tested yet. Unity, Marmalade, OpenGL ES 1
and OpenGL ES 2 games all run.

## Limits

- **No game sound yet.** Games' audio code runs, but nothing is played. Movies (cutscenes) play
  through Android's own video player.
- **Firmware:** an iPhone 5 root filesystem from iOS 6 is needed. Games made for iOS 7 can start
  too.
- **Games** must be decrypted `.ipa` files.
- Still experimental: expect some games to be slow, freeze or not start.

## Using the app

- **Firmware:** *Import firmware* takes the iPhone 5 (iOS 6) root filesystem (details below).
- **Games:** *Import game (.ipa)* adds a game; the list shows each game's own icon. Tap a game
  to play it. Each game runs in its own process, so Back ends it cleanly.
- **Long-press a game** to clear its data, remove it, add it to the home screen, turn its picture
  if a landscape game shows sideways, or choose its speed: *slow motion when behind* (the
  default) or *real time*.
- **Controls:** touch, with every finger passed to the game (multi-touch).
- **Saves** are kept where you can see and back them up.
- **Copy log** on the game screen copies the run's log, for bug reports.
- **Starting a game from another app** (such as BacklogQuest): send an intent with the action
  `com.hcode91.pocket.LAUNCH_GAME` to `com.hcode91.pocketpear` with a `game_id` extra holding
  the game's name or bundle ID.

## Importing firmware

The firmware button takes the iPhone 5 (iOS 6) root filesystem as you have it: a decrypted
`.dmg` (UDIF, or a bare HFS+ image), a `.zip` holding the `.dmg` or the root folder, or the split
parts of either (`.001`, `.002`, ...) picked together. The app reads the HFS+ volume itself
(compressed files and absolute symlinks included), so nothing needs unpacking first. A zipped
`.dmg` that is compressed inside the zip is unpacked to the app's cache first, which needs about
1 GB free for a moment.

## How it works

The phone app (`android/`, Kotlin) runs the emulator through Chaquopy (Python 3.13). The guest
CPU runs on dynarmic's JIT (`core/`, `libppcpu`), with Unicorn kept for the parts that still use
it, and the phone's own OpenGL ES draws.

- `prototype/` - the emulator itself, also runnable on a computer (below).
- `core/` - the native CPU core.
- `tools/` - helpers that read the user's shared cache: a disassembler by symbol name, and the
  generator for `prototype/bsdnums.json` (BSD system call numbers, taken from libsystem_kernel's
  own stubs).

`CADisplayLink` is replaced (`prototype/displaylink.py`): without a display server there is no
vsync, so each tick borrows the main thread while its run loop waits in `mach_msg`, calls the
target, and puts the wait back. UIScreen and GraphicsServices are given an iPhone 5's screen
(640x1136 at 2x).

pthread mutexes, condition variables and rwlocks are implemented at the libsystem_c function
level (`prototype/hle.py`) rather than through the kernel's psynch calls. Threads switch only at
system calls (and every 512th call), because stopping Unicorn mid-instruction-stream loses the
Thumb IT-block state; `PP_SLICE=n` forces instruction-count slices anyway, and `PP_WATCHDOG`
(seconds) reports a thread that never makes a call that waits.

## Running on a computer

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

## Building

GitHub Actions (`.github/workflows/android.yml`) builds the APK on every push to `main`
(artifact *PocketPear-apk*). Locally: `android/build-unicorn.sh <ndk>` and
`android/build-ppcpu.sh <ndk>` once, then `./gradlew assembleDebug` in `android/`.

Signing: if the repository has the secrets `POCKETPEAR_KEYSTORE_B64` (the keystore, base64),
`POCKETPEAR_KEYSTORE_PASSWORD` and, if its alias isn't `pocketpear`, `POCKETPEAR_KEY_ALIAS`, the
APK is signed with that key. Without them it falls back to the shared test key in
`android/app/pocketpear-debug.keystore`, which is public and only for test builds.

## Licence

GPL-2.0 (see `LICENSE`). PocketPear includes Unicorn (GPL-2.0) and dynarmic (0BSD).
