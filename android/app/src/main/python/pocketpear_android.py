"""The Android app's side of the emulator: imports, the game list, and running a game.

Kotlin calls these through Chaquopy. The emulator itself is the prototype (emu.py and friends),
unchanged except that frames go to a callback instead of PNG files and touches come from the
screen instead of a script.
"""
import json
import os
import plistlib
import shutil
import stat
import sys
import threading
import zipfile

CACHE_IN_ROOT = 'System/Library/Caches/com.apple.dyld/dyld_shared_cache_armv7s'
_emu = None


def setup(files_dir, native_dir, cache_dir):
    os.environ['HOME'] = files_dir
    os.environ['TMPDIR'] = cache_dir
    os.environ['PP_DATA'] = os.path.join(files_dir, 'data')
    os.environ['PP_FRAMES'] = ''
    os.environ['LIBUNICORN_PATH'] = native_dir
    os.environ.setdefault('PP_TOUCH_SPACE', 'frame')
    for d in ('firmware', 'apps', 'data'):
        os.makedirs(os.path.join(files_dir, d), exist_ok=True)


# ---------------------------------------------------------------------------- imports
def _extract(zf, members, prefix, dest, progress, label):
    """Extract zip members under prefix into dest, keeping symlinks (zipfile does not)."""
    total = sum(m.file_size for m in members) or 1
    done = 0
    last = -1
    for m in members:
        rel = m.filename[len(prefix):]
        if not rel or rel.startswith('/') or '..' in rel.split('/'):
            continue
        out = os.path.join(dest, rel)
        mode = (m.external_attr >> 16) & 0xffff
        if m.is_dir():
            os.makedirs(out, exist_ok=True)
        elif stat.S_ISLNK(mode):
            os.makedirs(os.path.dirname(out), exist_ok=True)
            target = zf.read(m).decode('utf-8')
            if os.path.lexists(out):
                os.remove(out)
            os.symlink(target, out)
        else:
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with zf.open(m) as src, open(out, 'wb') as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
        done += m.file_size
        pct = done * 100 // total
        if pct != last and progress is not None:
            last = pct
            progress.onProgress(label, pct)


def import_firmware(fd, files_dir, progress=None):
    """A .zip of an iOS 6 root filesystem (any folder level). Returns a message for the user."""
    with os.fdopen(fd, 'rb') as f, zipfile.ZipFile(f) as zf:
        names = zf.namelist()
        hit = next((n for n in names if n.endswith(CACHE_IN_ROOT)), None)
        if hit is None:
            return 'That zip has no ' + CACHE_IN_ROOT + ' - it needs the whole root filesystem of an iPhone 5 firmware (iOS 6).'
        prefix = hit[:-len(CACHE_IN_ROOT)]
        dest = os.path.join(files_dir, 'firmware', 'root')
        if os.path.exists(dest):
            shutil.rmtree(dest)
        members = [m for m in zf.infolist() if m.filename.startswith(prefix)]
        _extract(zf, members, prefix, dest, progress, 'Firmware')
    return 'Firmware imported.'


def import_ipa(fd, files_dir, progress=None):
    """A decrypted .ipa (zip with Payload/<name>.app). Returns a message for the user."""
    with os.fdopen(fd, 'rb') as f, zipfile.ZipFile(f) as zf:
        apps = sorted({n.split('/')[1] for n in zf.namelist()
                       if n.startswith('Payload/') and n.count('/') >= 2 and n.split('/')[1].endswith('.app')})
        if not apps:
            return 'That file has no Payload/*.app - is it an .ipa?'
        app = apps[0]
        dest = os.path.join(files_dir, 'apps', app)
        if os.path.exists(dest):
            shutil.rmtree(dest)
        prefix = 'Payload/' + app + '/'
        members = [m for m in zf.infolist() if m.filename.startswith(prefix)]
        _extract(zf, members, prefix, dest, progress, app)
    return f'{_display_name(dest)} imported.'


def _display_name(app_dir):
    try:
        info = plistlib.load(open(os.path.join(app_dir, 'Info.plist'), 'rb'))
        return info.get('CFBundleDisplayName') or info.get('CFBundleName') or os.path.basename(app_dir)
    except Exception:
        return os.path.basename(app_dir)


def firmware_ready(files_dir):
    return os.path.exists(os.path.join(files_dir, 'firmware', 'root', CACHE_IN_ROOT))


def list_apps(files_dir):
    d = os.path.join(files_dir, 'apps')
    out = []
    for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if name.endswith('.app'):
            out.append({'name': _display_name(os.path.join(d, name)), 'path': os.path.join(d, name)})
    return json.dumps(out)


def remove_app(path):
    shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------- running
class _Log:
    """stdout during a run: everything to run.log, interesting lines to the screen."""
    SHOW = ('[log]', '[emu]', '[gles] host', '[gles] renderbuffer', '[displaylink] tick 1 ', 'Traceback',
            'Error', 'error', '[backboard] touch', 'exited', 'blocked for ever')

    def __init__(self, path, listener):
        self.f = open(path, 'w', buffering=1 << 16)
        self.listener = listener
        self.part = ''

    def write(self, s):
        self.f.write(s)
        self.part += s
        while '\n' in self.part:
            line, self.part = self.part.split('\n', 1)
            if any(k in line for k in self.SHOW):
                try:
                    self.listener.onLog(line[:300])
                except Exception:
                    pass
        return len(s)

    def flush(self):
        self.f.flush()


def run(files_dir, app_path, listener):
    """Run a game until it exits or stop() is called. Blocks; call it on its own thread."""
    global _emu
    log = _Log(os.path.join(files_dir, 'run.log'), listener)
    old = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = log
    try:
        import numpy as np
        import emu as emu_mod
        rootfs = os.path.join(files_dir, 'firmware', 'root')
        e = emu_mod.start(rootfs, app_path)
        _emu = e
        frames = [0]

        def sink(rgba, w, h):
            frames[0] += 1
            img = np.frombuffer(rgba, np.uint8).reshape(h, w, 4)[::-1]   # GL rows are bottom-up
            listener.onFrame(img.tobytes(), w, h)

        e.gles.frame_sink = sink
        e.run(0)
        listener.onLog(f'[pocketpear] stopped after {frames[0]} frames')
    except BaseException as ex:
        import traceback
        traceback.print_exc()
        listener.onLog(f'[pocketpear] {type(ex).__name__}: {ex}')
    finally:
        sys.stdout, sys.stderr = old
        log.flush()
        listener.onExit()


def touch(kind, x, y):
    """kind: 'down', 'move' or 'up'; x, y in the game's frame pixels."""
    e = _emu
    if e is not None:
        e.kernel.touches.inject(kind, float(x), float(y))


def stop():
    e = _emu
    if e is not None:
        e.quit = True
        try:
            e.uc.emu_stop()
        except Exception:
            pass
