"""PP_SAMPLE=seconds: a sampling profiler for the emulator itself. A background thread looks at
the emulation thread's Python stack every few milliseconds and, every `seconds`, prints where the
time went: by the innermost frame (where it was spent) and by every frame on the stack (what it
was spent under). Time inside the CPU core shows up as the Python call that entered it."""
import collections
import sys
import threading
import time


def start(period):
    target = threading.get_ident()
    own, incl = collections.Counter(), collections.Counter()
    state = {'n': 0, 'last': time.time()}

    def name(f):
        c = f.f_code
        return f'{c.co_filename.rsplit("/", 1)[-1]}:{c.co_name}'

    def loop():
        while True:
            time.sleep(0.004)
            f = sys._current_frames().get(target)
            if f is None:
                return
            own[name(f)] += 1
            seen = set()
            while f is not None:
                n = name(f)
                if n not in seen:
                    incl[n] += 1
                    seen.add(n)
                f = f.f_back
            state['n'] += 1
            if time.time() - state['last'] >= period and state['n']:
                n = state['n']
                lines = [f'[sample] {n} samples over {time.time() - state["last"]:.0f}s; where the time went:']
                lines += [f'    {c * 100 / n:5.1f}%  {k}' for k, c in own.most_common(14)]
                lines += ['  under:']
                lines += [f'    {c * 100 / n:5.1f}%  {k}' for k, c in incl.most_common(22)]
                print('\n'.join(lines), flush=True)
                own.clear(); incl.clear()
                state['n'] = 0
                state['last'] = time.time()

    threading.Thread(target=loop, daemon=True).start()
