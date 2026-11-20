# iOS userland emulator prototype (PocketOuya iPhone section, experiment)

Runs an iPhone app's ARM code with Apple's own dyld and frameworks, taken from a firmware
root filesystem the user supplies, under a CPU emulator (Unicorn). Only the kernel is
imitated: BSD system calls, Mach traps and MIG messages, the commpage.

No Apple code is in this folder. `migids.json` lists MIG routine numbers and names read
from the firmware's stubs.

    pip install unicorn capstone
    python3 emu.py <rootfs> <path/to/App.app>

Status (2026-11-20, iOS 6.1.4 iPhone 5 rootfs, Wipeout 2.0.5):
- dyld maps the shared cache, loads and binds all 29 of the app's frameworks
- libSystem, libxpc, CoreFoundation, Foundation and libobjc initialisers run; the app's
  own +load methods start
- stops in libdispatch: needs kqueue/kevent and worker threads

Next: guest threads (bsdthread_create, cooperative scheduler), Mach port message queues
between guest threads, kqueue/kevent, launchd bootstrap stand-ins; then UIKit's
GraphicsServices/backboardd conversation, OpenGL ES to the host GPU.
