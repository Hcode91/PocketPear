# PocketPear

Private, experimental: run iPhone games on Android by running the game's own ARM code together
with Apple's own system frameworks, taken from a firmware image the user supplies. Only the
kernel is imitated (BSD system calls, Mach traps and messages, the commpage), plus the system
services an app talks to, and OpenGL ES is translated to the host GPU.

No Apple code, firmware or apps belong in this repository. See `.gitignore`.

- `prototype/` - Python + Unicorn prototype used to find out what the frameworks need.
