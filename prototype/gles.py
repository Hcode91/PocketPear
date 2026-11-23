"""OpenGL ES for the guest, run on the host's OpenGL ES (Mesa through EGL here; the device's own
GLES on Android later).

The guest's OpenGLES.framework talks to the PowerVR driver through IOKit, which is not here, so
its exported gl* functions and the EAGLContext methods are replaced (hle.Stubs) with Python ones
that call the host. Object names (textures, buffers...) are the host's. iOS passes float
arguments in core registers (softfp), so every argument starts as a 32-bit word.
"""
import ctypes
import os
import struct
import time

from unicorn.arm_const import *

# ---------------------------------------------------------------------------- host
ANDROID = hasattr(__import__('sys'), 'getandroidapilevel')


def _lib(*names):
    for n in names:
        try:
            return ctypes.CDLL(n)
        except OSError:
            pass
    raise OSError(f'none of {names} found')


_egl = _lib('libEGL.so.1', 'libEGL.so')
_gl = _lib('libGLESv2.so.2', 'libGLESv2.so')
if not ANDROID:
    _egl.eglGetPlatformDisplay.restype = ctypes.c_void_p
_egl.eglGetDisplay.restype = ctypes.c_void_p
_egl.eglCreatePbufferSurface.restype = ctypes.c_void_p
_egl.eglCreateContext.restype = ctypes.c_void_p
_egl.eglGetProcAddress.restype = ctypes.c_void_p

C = {'i': ctypes.c_int, 'u': ctypes.c_uint, 'f': ctypes.c_float, 'b': ctypes.c_ubyte, 'p': ctypes.c_void_p,
     'z': ctypes.c_ssize_t}


def host(name, args, ret='v'):
    fn = getattr(_gl, name, None)
    if fn is None:
        a = _egl.eglGetProcAddress(name.encode())
        if not a:
            return None
        fn = ctypes.CFUNCTYPE(None)(a)
        fn = ctypes.cast(a, ctypes.CFUNCTYPE(*([C.get(ret)] if ret != 'v' else [None]) + [C[c] for c in args]))
    else:
        fn.argtypes = [C[c] for c in args]
        fn.restype = None if ret == 'v' else C[ret]
    return fn


# Every core OpenGL ES 2.0 function whose arguments are all scalars: name -> (args, return).
SCALAR = {
    'glActiveTexture': ('u', 'v'), 'glAttachShader': ('uu', 'v'), 'glBindBuffer': ('uu', 'v'),
    'glBindFramebuffer': ('uu', 'v'), 'glBindRenderbuffer': ('uu', 'v'), 'glBindTexture': ('uu', 'v'),
    'glBlendColor': ('ffff', 'v'), 'glBlendEquation': ('u', 'v'), 'glBlendEquationSeparate': ('uu', 'v'),
    'glBlendFunc': ('uu', 'v'), 'glBlendFuncSeparate': ('uuuu', 'v'), 'glCheckFramebufferStatus': ('u', 'u'),
    'glClear': ('u', 'v'), 'glClearColor': ('ffff', 'v'), 'glClearDepthf': ('f', 'v'), 'glClearStencil': ('i', 'v'),
    'glColorMask': ('bbbb', 'v'), 'glCompileShader': ('u', 'v'), 'glCopyTexImage2D': ('uiuiiiii', 'v'),
    'glCopyTexSubImage2D': ('uiiiiiii', 'v'), 'glCreateProgram': ('', 'u'), 'glCreateShader': ('u', 'u'),
    'glCullFace': ('u', 'v'), 'glDeleteProgram': ('u', 'v'), 'glDeleteShader': ('u', 'v'), 'glDepthFunc': ('u', 'v'),
    'glDepthMask': ('b', 'v'), 'glDepthRangef': ('ff', 'v'), 'glDetachShader': ('uu', 'v'), 'glDisable': ('u', 'v'),
    'glDisableVertexAttribArray': ('u', 'v'), 'glEnable': ('u', 'v'), 'glEnableVertexAttribArray': ('u', 'v'),
    'glFinish': ('', 'v'), 'glFlush': ('', 'v'), 'glFramebufferRenderbuffer': ('uuuu', 'v'),
    'glFramebufferTexture2D': ('uuuui', 'v'), 'glFrontFace': ('u', 'v'), 'glGenerateMipmap': ('u', 'v'),
    'glGetError': ('', 'u'), 'glHint': ('uu', 'v'), 'glIsBuffer': ('u', 'b'), 'glIsEnabled': ('u', 'b'),
    'glIsFramebuffer': ('u', 'b'), 'glIsProgram': ('u', 'b'), 'glIsRenderbuffer': ('u', 'b'), 'glIsShader': ('u', 'b'),
    'glIsTexture': ('u', 'b'), 'glLineWidth': ('f', 'v'), 'glLinkProgram': ('u', 'v'), 'glPixelStorei': ('ui', 'v'),
    'glPolygonOffset': ('ff', 'v'), 'glReleaseShaderCompiler': ('', 'v'), 'glRenderbufferStorage': ('uuii', 'v'),
    'glSampleCoverage': ('fb', 'v'), 'glScissor': ('iiii', 'v'), 'glStencilFunc': ('uiu', 'v'),
    'glStencilFuncSeparate': ('uuiu', 'v'), 'glStencilMask': ('u', 'v'), 'glStencilMaskSeparate': ('uu', 'v'),
    'glStencilOp': ('uuu', 'v'), 'glStencilOpSeparate': ('uuuu', 'v'), 'glTexParameterf': ('uuf', 'v'),
    'glTexParameteri': ('uui', 'v'), 'glUniform1f': ('if', 'v'), 'glUniform2f': ('iff', 'v'),
    'glUniform3f': ('ifff', 'v'), 'glUniform4f': ('iffff', 'v'), 'glUniform1i': ('ii', 'v'), 'glUniform2i': ('iii', 'v'),
    'glUniform3i': ('iiii', 'v'), 'glUniform4i': ('iiiii', 'v'), 'glUseProgram': ('u', 'v'),
    'glValidateProgram': ('u', 'v'), 'glVertexAttrib1f': ('uf', 'v'), 'glVertexAttrib2f': ('uff', 'v'),
    'glVertexAttrib3f': ('ufff', 'v'), 'glVertexAttrib4f': ('uffff', 'v'), 'glViewport': ('iiii', 'v'),
    # extensions iOS has, mapped onto OpenGL ES 3 core
    'glBindVertexArrayOES': ('u', 'v', 'glBindVertexArray'), 'glIsVertexArrayOES': ('u', 'b', 'glIsVertexArray'),
    'glRenderbufferStorageMultisampleAPPLE': ('uiuii', 'v', 'glRenderbufferStorageMultisample'),
    'glTexStorage2DEXT': ('uiuii', 'v', 'glTexStorage2D'),
}

# How many values glGet* writes, by parameter name (anything else: one)
GET_COUNTS = {0x0BA2: 4, 0x0C10: 4, 0x0C22: 4, 0x0C23: 4, 0x8005: 4, 0x0D3A: 2, 0x846D: 2, 0x846E: 2, 0x0B70: 2}

GL_VENDOR, GL_RENDERER, GL_VERSION, GL_EXTENSIONS, GL_SHADING_LANGUAGE_VERSION = 0x1F00, 0x1F01, 0x1F02, 0x1F03, 0x8B8C
# What an iPhone 5 (PowerVR SGX 543MP3, iOS 6) reports, so games take the paths they take there.
DEVICE_STRINGS = {
    GL_VENDOR: 'Imagination Technologies', GL_RENDERER: 'PowerVR SGX 543',
    GL_VERSION: 'OpenGL ES 2.0 IMGSGX543-73.16', GL_SHADING_LANGUAGE_VERSION: 'OpenGL ES GLSL ES 1.00',
    GL_EXTENSIONS: ' '.join([
        'GL_OES_depth_texture', 'GL_OES_depth24', 'GL_OES_element_index_uint', 'GL_OES_fbo_render_mipmap',
        'GL_OES_mapbuffer', 'GL_OES_packed_depth_stencil', 'GL_OES_rgb8_rgba8', 'GL_OES_standard_derivatives',
        'GL_OES_texture_float', 'GL_OES_texture_half_float', 'GL_OES_vertex_array_object',
        'GL_EXT_blend_minmax', 'GL_EXT_debug_label', 'GL_EXT_debug_marker', 'GL_EXT_discard_framebuffer',
        'GL_EXT_read_format_bgra', 'GL_EXT_separate_shader_objects', 'GL_EXT_shader_texture_lod',
        'GL_EXT_texture_filter_anisotropic', 'GL_APPLE_framebuffer_multisample', 'GL_APPLE_rgb_422',
        'GL_APPLE_texture_format_BGRA8888', 'GL_APPLE_texture_max_level', 'GL_IMG_read_format',
        'GL_IMG_texture_compression_pvrtc']),
}

# The iPhone 5's (PowerVR SGX 543MP3, iOS 6) limits, never more than the host's.
DEVICE_LIMITS = {
    0x8872: 8,       # MAX_TEXTURE_IMAGE_UNITS
    0x8B4D: 8,       # MAX_COMBINED_TEXTURE_IMAGE_UNITS
    0x8B4C: 8,       # MAX_VERTEX_TEXTURE_IMAGE_UNITS
    0x8869: 16,      # MAX_VERTEX_ATTRIBS
    0x8DFB: 128,     # MAX_VERTEX_UNIFORM_VECTORS
    0x8DFD: 64,      # MAX_FRAGMENT_UNIFORM_VECTORS
    0x8DFC: 8,       # MAX_VARYING_VECTORS
    0x0D33: 4096,    # MAX_TEXTURE_SIZE
    0x851C: 4096,    # MAX_CUBE_MAP_TEXTURE_SIZE
    0x84E8: 4096,    # MAX_RENDERBUFFER_SIZE
}

GL_ARRAY_BUFFER, GL_ELEMENT_ARRAY_BUFFER = 0x8892, 0x8893
GL_ARRAY_BUFFER_BINDING, GL_ELEMENT_ARRAY_BUFFER_BINDING = 0x8894, 0x8895
GL_FRAMEBUFFER, GL_RENDERBUFFER, GL_COLOR_ATTACHMENT0 = 0x8D40, 0x8D41, 0x8CE0
GL_RENDERBUFFER_BINDING, GL_FRAMEBUFFER_BINDING = 0x8CA7, 0x8CA6
TYPE_SIZES = {0x1400: 1, 0x1401: 1, 0x1402: 2, 0x1403: 2, 0x1404: 4, 0x1405: 4, 0x1406: 4, 0x140C: 4, 0x8D61: 2}


def bits_to_float(w):
    return struct.unpack('<f', struct.pack('<I', w & 0xffffffff))[0]


class GLES:
    def __init__(self, emu):
        self.emu = emu
        self.uc = emu.uc
        self.display = None
        self.contexts = {}           # guest EAGLContext -> (host context, api)
        self.api = 2                 # of the current context
        from gles1 import ES1
        self.es1 = ES1(self)
        self.current = {}            # thread id -> guest EAGLContext
        self.strings = {}
        # Client-side vertex arrays and enabled attributes are vertex array object state, so they
        # are kept per VAO (Unity uses OES_vertex_array_object): vao -> {index: (size, type,
        # normalized, stride, guest pointer)} and vao -> {enabled indices}.
        self.vao = 0
        self.vao_attribs = {0: {}}
        self.vao_enabled = {0: set()}
        self.empty_textures = set()   # PP_GL_EMPTYTEX: textures given storage but no pixels
        self.shader_sources = {}     # shader name -> the source given to the host
        self.texture_targets = {}    # texture name -> the target it was bound to
        self.retired = set()         # texture names the app deleted (kept, emptied: see retire_textures)
        self.warned_retired = False
        self.ctx_state = {}          # guest EAGLContext -> its (vao, vao_attribs, vao_enabled)
        self.host_guest = None       # the guest context current on the host
        self.host_tid = None         # the guest thread the host context was last chosen for
        self.context_switches = 0
        self.mapped = {}             # buffer target -> (guest address, size, host buffer)
        self.index_shadow = {}       # element array buffer name -> its contents
        self.frame = 0
        self.draws = self.client_draws = 0     # since the last present
        self.unimplemented = {}
        # Frames go to PNGs (PP_FRAMES; PP_FRAMES='' turns that off) and to frame_sink, a
        # callable(rgba_bytes_bottom_up, width, height) the Android app sets to show them.
        self.out_dir = os.environ.get('PP_FRAMES', '' if ANDROID else '/tmp/pocketpear-frames')
        self.frame_sink = None
        if self.out_dir:
            os.makedirs(self.out_dir, exist_ok=True)
        landscape = any('Landscape' in str(o) for o in (emu.info.get('UISupportedInterfaceOrientations') or [])) \
            if not str(emu.info.get('UIInterfaceOrientation', '')).endswith('Portrait') else False
        self.landscape = landscape
        self.layer_scale = {}        # CAEAGLLayer -> contentsScale, when the app set one
        self.drawable_layers = set() # CAEAGLLayers given renderbuffer storage (uicompose keys them out)
        self.last_present = 0.0      # wall time of the last presented GL frame
        w, h = getattr(emu, 'screen_points', (320, 568))
        self.scale = 2.0
        self.screen = (h * 2, w * 2) if landscape else (w * 2, h * 2)

    # ------------------------------------------------------------------------ plumbing
    def arg(self, n):
        if n < 4:
            return self.uc.reg_read(UC_ARM_REG_R0 + n)
        return self.emu.r32(self.uc.reg_read(UC_ARM_REG_SP) + 4 * (n - 4))

    def args(self, n):
        return [self.arg(i) for i in range(n)]

    def ret(self, v):
        self.uc.reg_write(UC_ARM_REG_R0, int(v) & 0xffffffff)

    def alloc(self, size):
        a = self.emu.find_free(size)
        self.emu.map(a, size)
        return a

    def ensure_display(self):
        if self.display is not None:
            return
        if ANDROID:
            d = _egl.eglGetDisplay(None)                             # EGL_DEFAULT_DISPLAY
        else:
            d = _egl.eglGetPlatformDisplay(0x31DD, None, None)      # EGL_PLATFORM_SURFACELESS_MESA
        maj, mi = ctypes.c_int(), ctypes.c_int()
        _egl.eglInitialize(ctypes.c_void_p(d), ctypes.byref(maj), ctypes.byref(mi))
        _egl.eglBindAPI(0x30A0)
        cfg, n = ctypes.c_void_p(), ctypes.c_int()
        # ES 3, RGBA8, depth 24, stencil 8, pbuffer-capable (Android has no surfaceless guarantee)
        attrs = (ctypes.c_int * 17)(0x3040, 0x40, 0x3024, 8, 0x3023, 8, 0x3022, 8, 0x3021, 8, 0x3025, 24,
                                    0x3026, 8, 0x3033, 0x1, 0x3038)
        _egl.eglChooseConfig(ctypes.c_void_p(d), attrs, ctypes.byref(cfg), 1, ctypes.byref(n))
        self.display, self.config = d, cfg
        self.share = None
        self.surface = None
        if ANDROID:
            # Every guest context draws into framebuffer objects; the pbuffer only makes the
            # context current where surfaceless contexts are not supported.
            pb = (ctypes.c_int * 5)(0x3057, 16, 0x3056, 16, 0x3038)
            self.surface = _egl.eglCreatePbufferSurface(ctypes.c_void_p(d), cfg, pb)
        print(f'[gles] host EGL {maj.value}.{mi.value}, screen {self.screen[0]}x{self.screen[1]}')

    def on_own_context(self, impl):
        """Runs a GL call on the context current on the calling guest thread. EAGL keeps a
        current context per thread, but the host runs every guest thread on one thread of its
        own: when the scheduler switches from a thread drawing on one context to one loading
        textures on another, the host's current context has to follow, or the second thread's
        binds land in the first's state (Bioshock's water drawn with a painting's texture)."""
        def call():
            tid = self.emu.sched.cur.tid
            if tid != self.host_tid:
                want = self.current.get(tid)
                if want and want != self.host_guest:
                    self.make_current(want)
                    self.context_switches += 1
                    if self.context_switches == 1:
                        print(f'[gles] guest threads draw on different contexts; the host context follows the thread')
                self.host_tid = tid
                self.sync_native()
            impl()
        return call

    def sync_native(self):
        """Tell the CPU core which guest thread may run GL calls itself (natively_run): the one
        whose context is current on the host, while that context is OpenGL ES 2."""
        uc = self.emu.uc
        if not getattr(uc, 'native_gl', False):
            return
        v = self.host_tid if self.host_tid is not None and self.api == 2 else 0xffffffff
        if v != getattr(self, '_native_tid', None):
            self._native_tid = v
            uc.gl_tid(v)

    def natively_run(self, n, base):
        """Let the CPU core run stub n (GL function base) without coming back to Python: GL calls
        that only pass their arguments to the host's function, which the renderer makes tens of
        thousands of times a second. PP_SLOW_GL=1 keeps them all in Python."""
        uc = self.emu.uc
        if not getattr(uc, 'native_gl', False) or os.environ.get('PP_SLOW_GL'):
            return False
        if base == 'glBindTexture' and hasattr(uc, 'tex_target'):
            # the texture's target is kept by the core for retire_textures; the warning about a
            # deleted name bound again is only given when this runs in Python
            uc.gl_fn(n, ctypes.cast(_gl.glBindTexture, ctypes.c_void_p).value, 4, 'uu', 'v', 0)
            return True
        if base in SCALAR and base not in TRACKED:
            spec = SCALAR[base]
            fn = host(spec[2] if len(spec) > 2 else base, spec[0], spec[1])
            if fn is None or len(spec[0]) > 11 or spec[0].count('f') > 8 or len(spec[0]) - spec[0].count('f') > 6:
                return False
            uc.gl_fn(n, ctypes.cast(fn, ctypes.c_void_p).value, 1, spec[0], spec[1], 0)
            return True
        import re
        m = re.fullmatch(r'glUniform([1-4])([fi])v', base) or re.fullmatch(r'glUniformMatrix([2-4])(f)v', base)
        if m:
            fn = getattr(_gl, base, None)
            if fn is None:
                return False
            k = int(m.group(1))
            matrix = 'Matrix' in base
            uc.gl_fn(n, ctypes.cast(fn, ctypes.c_void_p).value, 3 if matrix else 2, '', 'v', k * k if matrix else k)
            return True
        return False

    def make_current(self, guest_ctx):
        if guest_ctx != self.host_guest:
            # Vertex array state is per context: keep each context's own
            self.ctx_state[self.host_guest] = (self.vao, self.vao_attribs, self.vao_enabled)
            self.vao, self.vao_attribs, self.vao_enabled = self.ctx_state.get(guest_ctx, (0, {0: {}}, {0: set()}))
            self.host_guest = guest_ctx
        self.host_tid = self.emu.sched.cur.tid if getattr(self.emu, 'sched', None) else None
        ctx = self.contexts.get(guest_ctx, (None,))[0] if guest_ctx else None
        surf = ctypes.c_void_p(self.surface) if self.surface and ctx else None
        _egl.eglMakeCurrent(ctypes.c_void_p(self.display), surf, surf, ctypes.c_void_p(ctx) if ctx else None)
        self.api = self.contexts.get(guest_ctx, (None, 2))[1] if guest_ctx else self.api
        self.sync_native()

    # ------------------------------------------------------------------------ install
    def install(self, stubs, syms):
        """syms: name -> address for OpenGLES.framework; thumb: set of Thumb addresses."""
        ES1_NAMES.update(_es1_names())
        made = native = 0
        plain = not os.environ.get('GL_ERRORS') and not os.environ.get('PP_GL_FIRST')
        for name, addr in syms.items():
            impl = None
            base = name[1:] if name.startswith('_') else name
            if base.endswith('OES') and base not in SCALAR and base not in ('glGenVertexArraysOES', 'glDeleteVertexArraysOES',
                                                                             'glMapBufferOES', 'glUnmapBufferOES'):
                core = base[:-3]                         # framebuffer_object's OES names are core in ES 2.0
                if core in SCALAR or core in ('glGenFramebuffers', 'glGenRenderbuffers', 'glDeleteFramebuffers',
                                              'glDeleteRenderbuffers') or core in GLES.__dict__:
                    base = core
            if base in TRACKED:
                impl = getattr(self, base)
            elif base in SCALAR:
                impl = self.scalar(base)
            elif hasattr(self, base):
                impl = getattr(self, base)
            elif base.startswith('gl'):
                impl = self.missing(base)
            elif name in OBJC:
                impl = getattr(self, OBJC[name])
            if base in ES1_NAMES:
                impl = self.by_api(base, impl or self.missing(base))
            if impl is not None and os.environ.get('GL_ERRORS') and base.startswith('gl') and base != 'glGetError':
                impl = self.checked(base, impl)
            if impl is not None and os.environ.get('PP_GL_FIRST'):
                impl = self.first_call(base, impl)
            if impl is not None:
                impl = self.on_own_context(impl)
                impl.__name__ = base
                n = stubs.replace(addr, impl, addr in self.emu.syms.thumb)
                made += 1
                if plain and self.natively_run(n, base):
                    native += 1
        print(f'[gles] {made} OpenGL ES entry points replaced' + (f', {native} run by the CPU core' if native else ''))
        if native:
            from hle import INDEXED_THUMB, INDEXED_ARM
            self.emu.uc.gl_indexed(INDEXED_THUMB, INDEXED_ARM)

    def first_call(self, name, impl):
        """PP_GL_FIRST=1: say when each GL entry point is first called, and how often since."""
        seen = self.__dict__.get('_gl_counts')
        if seen is None:
            seen = self._gl_counts = {}
            import atexit
            atexit.register(lambda: print('[gles] calls: ' + ', '.join(f'{k} {v}' for k, v in
                                                                       sorted(seen.items(), key=lambda kv: -kv[1]))))
        def call():
            n = seen.get(name, 0)
            seen[name] = n + 1
            if n == 0:
                print(f'[gles] first call: {name} (api {self.api})')
            impl()
        return call

    def by_api(self, name, es2_impl):
        """A call ES 1 answers differently (or only ES 1 has), routed by the current context."""
        es1_impl = getattr(self.es1, name)
        def call():
            if self.api == 1:
                es1_impl()
            else:
                es2_impl()
        return call

    def scalar(self, name):
        spec = SCALAR[name]
        args, rtype = spec[0], spec[1]
        fn = host(spec[2] if len(spec) > 2 else name, args, rtype)
        if fn is None:
            return self.missing(name)
        def call():
            vals = []
            for i, c in enumerate(args):
                w = self.arg(i)
                vals.append(bits_to_float(w) if c == 'f' else (w & 0xff if c == 'b' else (w - (1 << 32) if c == 'i' and w & 0x80000000 else w)))
            r = fn(*vals)
            if rtype != 'v':
                self.ret(r or 0)
        return call

    def checked(self, name, impl):
        """GL_ERRORS=1: report each distinct GL error a call raises (once per call and error)."""
        seen = self.__dict__.setdefault('_gl_errors', set())
        get_error = host('glGetError', '', 'u')
        def call():
            impl()
            e = get_error()
            if e and (name, e) not in seen:
                seen.add((name, e))
                print(f'[gles] {name} raised GL error {e:#x} (args {[hex(self.arg(i)) for i in range(4)]})')
        return call

    def missing(self, name):
        def call():
            n = self.unimplemented.get(name, 0) + 1
            self.unimplemented[name] = n
            if n <= 2:
                print(f'[gles] {name} not implemented')
            self.ret(0)
        return call

    # ------------------------------------------------------------------------ EAGL
    def eagl_init_with_api(self):
        guest, _cmd, api = self.args(3)
        self.ensure_display()
        if api not in (1, 2):
            self.ret(0); return
        # an ES 1 context is an ES 3 one too: gles1 draws its fixed-function state with shaders
        version = (ctypes.c_int * 3)(0x3098, 2, 0x3038)
        ctx = _egl.eglCreateContext(ctypes.c_void_p(self.display), self.config, ctypes.c_void_p(self.share), version)
        if not ctx:
            print('[gles] host context creation failed')
            self.ret(0); return
        if self.share is None:
            self.share = ctx
        self.contexts[guest] = (ctx, api)
        print(f'[gles] EAGLContext {guest:#x} (OpenGL ES {api}.0) -> host context {ctx:#x}')
        if guest == self.host_guest:
            # the object is current already (an app that tried ES 2, released the context and
            # got the same address back for ES 1): its API changes now, and with it which calls
            # the CPU core may run itself
            self.make_current(guest)
        self.ret(guest)

    def eagl_init_with_api_sharegroup(self):
        self.eagl_init_with_api()

    def eagl_set_current(self):
        cls, _cmd, guest = self.args(3)
        tid = self.emu.sched.cur.tid
        self.current[tid] = guest
        self.make_current(guest)
        self.ret(1)

    def eagl_current(self):
        self.ret(self.current.get(self.emu.sched.cur.tid, 0))

    def eagl_api(self):
        guest = self.arg(0)
        self.ret(self.contexts.get(guest, (0, 0))[1])

    def eagl_sharegroup(self):
        self.ret(0)

    def eagl_dealloc(self):
        self.ret(0)

    def display_turn(self):
        """How the host has to turn a frame to show it upright: 'cw', 'ccw' or None. Apps made
        before UIKit rotated GL views draw landscape games into a portrait renderbuffer, turned
        for the way the phone is held: Info.plist's UIInterfaceOrientation or, when that says
        nothing, the status bar orientation the app sets as it starts (Splinter Cell sets
        UIInterfaceOrientationLandscapeRight in code)."""
        w, h = self.screen
        if w >= h:
            return None
        o = str(self.emu.info.get('UIInterfaceOrientation', ''))
        if 'Landscape' in o:
            return 'cw' if o.endswith('LandscapeLeft') else 'ccw'
        ui = getattr(self.emu, 'ui', None)
        return {4: 'cw', 3: 'ccw'}.get(getattr(ui, 'orientation', 1))

    def eagl_storage_from_drawable(self):
        guest, _cmd, target, layer = self.args(4)
        pw, ph = getattr(self.emu, 'screen_points', (320, 568))
        self.scale = self.layer_scale.get(layer, 1.0) or 1.0
        w, h = int(round((ph if self.landscape else pw) * self.scale)), int(round((pw if self.landscape else ph) * self.scale))
        self.screen = (w, h)
        if layer:
            self.drawable_layers.add(layer)
        _gl.glRenderbufferStorage(target, 0x8058, w, h)      # GL_RGBA8_OES
        print(f'[gles] renderbuffer storage from drawable {layer:#x}: {w}x{h}')
        self.ret(1)

    def eagl_present(self):
        guest, _cmd, target = self.args(3)
        self.frame += 1
        self.last_present = time.time()
        every = int(os.environ.get('PP_FRAME_EVERY', 30))
        save = self.out_dir and (self.frame == 1 or self.frame % every == 0)
        ui = getattr(self.emu, 'ui', None)
        if self.frame_sink is not None and not save and self.same_as_last(ui):
            self.draws = self.client_draws = 0
            self.ret(1)
            return
        if self.frame_sink is not None or save:
            rgba = self.read_frame()
            if ui is not None and ui.overlay is not None:
                rgba = ui.over(rgba, *self.screen)
            if self.frame_sink is not None:
                self.frame_sink(rgba, *self.screen)
            if save:
                self.save_frame(rgba)
        self.draws = self.client_draws = 0
        self.ret(1)

    def same_as_last(self, ui):
        """Whether this frame would show exactly what the last one did, so it need not be read
        back, composited and sent again. A frame with no draw calls is one colour (whatever it
        was cleared to), so one pixel tells; with the UIKit views unchanged it is the same
        picture. Bioshock presents sixty such frames a second behind its menu and movies, and
        each cost a full-screen readback and composite on the phone."""
        if self.draws or self.client_draws:
            self.last_empty = None
            return False
        px = (ctypes.c_ubyte * 4)()
        _gl.glReadPixels(0, 0, 1, 1, 0x1908, 0x1401, px)
        key = (bytes(px), getattr(ui, 'version', 0), self.screen)
        if key == getattr(self, 'last_empty', None):
            self.skipped = getattr(self, 'skipped', 0) + 1
            return True
        self.last_empty = key
        return False

    def present_ui_only(self):
        """Show the UIKit views alone, for an app that presents no GL frames (or none lately)."""
        ui = self.emu.ui
        w, h = self.screen
        rgba = ui.over(bytes(w * h * 4), w, h)
        self.frame += 1
        if self.frame_sink is not None:
            self.frame_sink(rgba, w, h)
        every = int(os.environ.get('PP_FRAME_EVERY', 30))
        if self.out_dir and (self.frame == 1 or self.frame % every == 0):
            self.save_frame(rgba)

    def read_frame(self):
        """The presented renderbuffer as RGBA bytes, bottom row first (GL order)."""
        rb = ctypes.c_int()
        _gl.glGetIntegerv(GL_RENDERBUFFER_BINDING, ctypes.byref(rb))
        prev = ctypes.c_int()
        _gl.glGetIntegerv(GL_FRAMEBUFFER_BINDING, ctypes.byref(prev))
        fbo = ctypes.c_uint()
        _gl.glGenFramebuffers(1, ctypes.byref(fbo))
        _gl.glBindFramebuffer(GL_FRAMEBUFFER, fbo)
        _gl.glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RENDERBUFFER, rb.value)
        w, h = self.screen
        buf = (ctypes.c_ubyte * (w * h * 4))()
        _gl.glReadPixels(0, 0, w, h, 0x1908, 0x1401, buf)
        _gl.glBindFramebuffer(GL_FRAMEBUFFER, prev.value)
        _gl.glDeleteFramebuffers(1, ctypes.byref(fbo))
        return bytes(buf)

    def save_frame(self, buf=None):
        w, h = self.screen
        if buf is None:
            buf = self.read_frame()
        try:
            from PIL import Image
            img = Image.frombuffer('RGBA', (w, h), bytes(buf), 'raw', 'RGBA', 0, -1)
            path = os.path.join(self.out_dir, f'frame{self.frame:05d}.png')
            img.convert('RGB').save(path)          # the screen is opaque; keep viewers from showing alpha
            print(f'[gles] frame {self.frame} -> {path} ({self.draws} draws, {self.client_draws} from client arrays)')
        except Exception as e:
            print('[gles] could not save frame:', e)

    # ------------------------------------------------------------------------ pointers
    def read(self, a, n):
        return self.emu.read(a, n) if a and n > 0 else b''

    def glGetString(self):
        name, = self.args(1)
        if name not in self.strings:
            text = DEVICE_STRINGS.get(name)
            if text is None:
                self.ret(0); return
            b = text.encode() + b'\0'
            a = self.alloc(len(b))
            self.emu.write(a, b)
            self.strings[name] = a
        self.ret(self.strings[name])

    def gen(self, hostname):
        fn = host(hostname, 'ip')
        def call():
            n, p = self.args(2)
            arr = (ctypes.c_uint * max(n, 1))()
            fn(n, arr)
            self.emu.write(p, bytes(arr)[:4 * n])
        return call

    def delete(self, hostname):
        fn = host(hostname, 'ip')
        def call():
            n, p = self.args(2)
            data = self.read(p, 4 * n)
            arr = (ctypes.c_uint * max(n, 1)).from_buffer_copy(data.ljust(4 * max(n, 1), b'\0'))
            fn(n, arr)
        return call

    def glBindTexture(self):
        target, name = self.args(2)
        if name:
            self.texture_targets[name] = target
            if name in self.retired and not self.warned_retired:
                self.warned_retired = True
                print(f'[gles] the app binds texture {name} after deleting it (it stays an empty texture)')
        _gl.glBindTexture(target, name)

    def retire_textures(self):
        """glDeleteTextures, the way iOS behaves for an app that keeps using a deleted name:
        iOS hands out texture names in order and does not give a deleted one out again soon,
        while Android drivers reuse them at once, so a stale name the app still binds would show
        whatever texture got the name next (Bioshock's water showed a painting). The texture's
        storage is freed (made 1x1, empty) but the name is kept, so the host never reuses it."""
        n, p = self.args(2)
        names = struct.unpack(f'<{n}I', self.read(p, 4 * n)) if n > 0 else ()
        empty = bytes(4)
        for name in names:
            if not name or name in self.retired:
                continue
            target = self.texture_targets.pop(name, 0)
            native = getattr(self.emu.uc, 'tex_target', None)
            if native is not None:
                target = native(name) or target
            target = target or 0x0DE1
            binding = 0x8514 if target == 0x8513 else 0x8069       # TEXTURE_BINDING_CUBE_MAP / _2D
            prev = self.bound(binding)
            _gl.glBindTexture(target, name)
            faces = range(0x8515, 0x851B) if target == 0x8513 else (0x0DE1,)
            for face in faces:
                _gl.glTexImage2D(face, 0, 0x1908, 1, 1, 0, 0x1908, 0x1401, empty)
            _gl.glBindTexture(target, prev if prev != name else 0)
            self.retired.add(name)

    def __getattr__(self, name):
        table = {'glGenBuffers': ('gen', 'glGenBuffers'), 'glGenTextures': ('gen', 'glGenTextures'),
                 'glGenFramebuffers': ('gen', 'glGenFramebuffers'), 'glGenRenderbuffers': ('gen', 'glGenRenderbuffers'),
                 'glGenVertexArraysOES': ('gen', 'glGenVertexArrays'),
                 'glDeleteBuffers': ('delete', 'glDeleteBuffers'), 'glDeleteTextures': ('delete', 'glDeleteTextures'),
                 'glDeleteFramebuffers': ('delete', 'glDeleteFramebuffers'),
                 'glDeleteRenderbuffers': ('delete', 'glDeleteRenderbuffers'),
                 'glDeleteVertexArraysOES': ('delete', 'glDeleteVertexArrays')}
        if name == 'glDeleteTextures':
            return self.retire_textures
        if name in table:
            kind, h = table[name]
            return getattr(self, kind)(h)
        raise AttributeError(name)

    # Index buffers are shadowed: a draw with a bound index buffer but client-side vertex arrays
    # (Unity's GUI does this) needs the highest index to know how much vertex data to upload.
    def shadow_write(self, target, off, blob, size=None):
        if target != 0x8893:                       # GL_ELEMENT_ARRAY_BUFFER
            return
        name = self.bound(GL_ELEMENT_ARRAY_BUFFER_BINDING)
        if size is not None:
            self.index_shadow[name] = bytearray(size)
        buf = self.index_shadow.setdefault(name, bytearray())
        if len(buf) < off + len(blob):
            buf.extend(bytes(off + len(blob) - len(buf)))
        buf[off:off + len(blob)] = blob

    def glBufferData(self):
        target, size, data, usage = self.args(4)
        blob = self.read(data, size)
        _gl.glBufferData(ctypes.c_uint(target), ctypes.c_ssize_t(size), blob if data else None, ctypes.c_uint(usage))
        self.shadow_write(target, 0, blob if data else b'', size)

    def glBufferSubData(self):
        target, off, size, data = self.args(4)
        blob = self.read(data, size)
        _gl.glBufferSubData(ctypes.c_uint(target), ctypes.c_ssize_t(off), ctypes.c_ssize_t(size), blob)
        self.shadow_write(target, off, blob)

    def image_size(self, w, h, fmt, typ):
        comps = {0x1906: 1, 0x1909: 1, 0x190A: 2, 0x1907: 3, 0x1908: 4, 0x80E1: 4, 0x1902: 1}.get(fmt, 4)
        if typ in (0x8363, 0x8033, 0x8034):            # packed 16-bit
            bpp = 2
        else:
            bpp = comps * TYPE_SIZES.get(typ, 1)
        row = (w * bpp + 3) & ~3                       # default unpack alignment 4
        return row * h

    def glTexImage2D(self):
        target, level, ifmt, w, h, border, fmt, typ, pixels = self.args(9)
        data = self.read(pixels, self.image_size(w, h, fmt, typ))
        if fmt == 0x80E1:                              # BGRA: swap to RGBA for the host
            data, fmt, ifmt = bgra_to_rgba(data), 0x1908, 0x1908
        given = pixels
        if not pixels and 0 < w * h <= 4096 * 4096:
            # Storage with no pixels starts out zero on iOS; Android drivers hand back whatever
            # the memory last held (Bioshock's empty second lightmap showed an old painting
            # across the water), so the host is given zeros.
            data = bytes(self.image_size(w, h, fmt, typ))
            pixels = 1
        _gl.glTexImage2D(target, level, ifmt, w, h, border, fmt, typ, data if pixels else None)
        if level == 0 and os.environ.get('PP_GL_EMPTYTEX'):
            t = self.bound(0x8514 if 0x8515 <= target <= 0x851A else 0x8069)
            if given:
                self.empty_textures.discard(t)
            else:
                self.empty_textures.add(t)
                print(f'[gles] texture {t} allocated {w}x{h} fmt {fmt:#x} type {typ:#x} with no data')

    def glTexSubImage2D(self):
        target, level, x, y, w, h, fmt, typ, pixels = self.args(9)
        data = self.read(pixels, self.image_size(w, h, fmt, typ))
        if fmt == 0x80E1:
            data, fmt = bgra_to_rgba(data), 0x1908
        _gl.glTexSubImage2D(target, level, x, y, w, h, fmt, typ, data)

    def glCompressedTexImage2D(self):
        target, level, ifmt, w, h, border, size, data = self.args(8)
        blob = self.read(data, size)
        if 0x8C00 <= ifmt <= 0x8C03:                   # PVRTC: decoded on the CPU
            rgba = decode_pvrtc(self.emu, blob, w, h, 2 if ifmt in (0x8C01, 0x8C03) else 4)
            _gl.glTexImage2D(target, level, 0x1908, w, h, 0, 0x1908, 0x1401, rgba)
            return
        _gl.glCompressedTexImage2D(target, level, ifmt, w, h, border, size, blob)

    def glReadPixels(self):
        x, y, w, h, fmt, typ, pixels = self.args(7)
        size = self.image_size(w, h, fmt, typ)
        buf = (ctypes.c_ubyte * size)()
        _gl.glReadPixels(x, y, w, h, fmt if fmt != 0x80E1 else 0x1908, typ, buf)
        data = bytes(buf)
        if fmt == 0x80E1:
            data = bgra_to_rgba(data)
        self.emu.write(pixels, data)

    def glShaderSource(self):
        shader, count, strings, lengths = self.args(4)
        parts = []
        for i in range(count):
            sp = self.emu.r32(strings + 4 * i)
            ln = self.emu.r32(lengths + 4 * i) if lengths else 0xffffffff
            if ln & 0x80000000:
                parts.append(self.emu.cstr(sp, limit=1 << 20).encode())    # shaders run past cstr's 4 KB default
            else:
                parts.append(self.read(sp, ln))
        src = fix_shader(b''.join(parts))
        self.shader_sources[shader] = src
        dump = os.environ.get('PP_SHADER_DUMP')
        if dump:
            os.makedirs(dump, exist_ok=True)
            with open(os.path.join(dump, f'shader{len(os.listdir(dump)):04d}_{shader}.glsl'), 'wb') as f:
                f.write(src)
        arr = (ctypes.c_char_p * 1)(src)
        _gl.glShaderSource(shader, 1, arr, None)

    def glCompileShader(self):
        """Compiles, and says so when the host's compiler refuses a shader the app's (Apple's)
        took: the program built from it then fails to link, and its draws use whatever program
        was in use before, with that program's textures (no error the app would see)."""
        shader, = self.args(1)
        _gl.glCompileShader(shader)
        ok = ctypes.c_int()
        _gl.glGetShaderiv(shader, 0x8B81, ctypes.byref(ok))          # COMPILE_STATUS
        if not ok.value:
            self.report_shader_failure('shader', shader, self.shader_sources.get(shader, b''),
                                       host('glGetShaderInfoLog', 'uipp', 'v'))

    def glLinkProgram(self):
        prog, = self.args(1)
        _gl.glLinkProgram(prog)
        ok = ctypes.c_int()
        _gl.glGetProgramiv(prog, 0x8B82, ctypes.byref(ok))           # LINK_STATUS
        if not ok.value:
            self.report_shader_failure('program', prog, b'', host('glGetProgramInfoLog', 'uipp', 'v'))

    def report_shader_failure(self, what, name, src, get_log):
        self.shader_failures = getattr(self, 'shader_failures', 0) + 1
        n = self.shader_failures
        log = b''
        if get_log:
            buf = ctypes.create_string_buffer(2048)
            ln = ctypes.c_int()
            get_log(name, 2048, ctypes.byref(ln), buf)
            log = buf.value
        if n <= 4:
            print(f'[gles] the host refused {what} {name}: {log.decode("utf-8", "replace").strip()[:800]}')
            if src:
                lines = src.split(b'\n')
                import re
                for m in re.finditer(rb'(\d+):(\d+)', log):
                    i = int(m.group(2))
                    if 0 < i <= len(lines):
                        print(f'[gles]   line {i}: {lines[i - 1].decode("utf-8", "replace").strip()[:200]}')
                        break
        elif n in (10, 100, 1000):
            print(f'[gles] {n} shaders or programs refused by the host so far')

    def get_iv(self, hostname, nargs, count=1):
        fn = getattr(_gl, hostname)
        def call():
            a = self.args(nargs)
            out = a[-1]
            buf = (ctypes.c_int * 16)()
            fn(*a[:-1], buf)
            n = GET_COUNTS.get(a[-2], count)
            self.emu.write(out, bytes(buf)[:4 * n])
        return call

    def glGetIntegerv(self):
        pname, out = self.args(2)
        buf = (ctypes.c_int * 16)()
        _gl.glGetIntegerv(pname, buf)
        if pname in DEVICE_LIMITS:
            # The iPhone's limits, not the host's: a game sizes tables and picks texture units
            # from them, and should do what it did on the hardware it was made for.
            buf[0] = min(buf[0], DEVICE_LIMITS[pname]) if buf[0] else DEVICE_LIMITS[pname]
        self.emu.write(out, bytes(buf)[:4 * GET_COUNTS.get(pname, 1)])

    def glGetFloatv(self):
        pname, out = self.args(2)
        buf = (ctypes.c_float * 16)()
        _gl.glGetFloatv(pname, buf)
        self.emu.write(out, bytes(buf)[:4 * GET_COUNTS.get(pname, 1)])

    def glGetBooleanv(self):
        pname, out = self.args(2)
        buf = (ctypes.c_ubyte * 16)()
        _gl.glGetBooleanv(pname, buf)
        self.emu.write(out, bytes(buf)[:GET_COUNTS.get(pname, 1)])

    def glGetShaderiv(self): self.get_iv('glGetShaderiv', 3)()
    def glGetProgramiv(self): self.get_iv('glGetProgramiv', 3)()
    def glGetBufferParameteriv(self): self.get_iv('glGetBufferParameteriv', 3)()
    def glGetRenderbufferParameteriv(self): self.get_iv('glGetRenderbufferParameteriv', 3)()
    def glGetTexParameteriv(self): self.get_iv('glGetTexParameteriv', 3)()
    def glGetFramebufferAttachmentParameteriv(self): self.get_iv('glGetFramebufferAttachmentParameteriv', 4)()
    def glGetVertexAttribiv(self): self.get_iv('glGetVertexAttribiv', 3, 1)()
    def glGetUniformiv(self): self.get_iv('glGetUniformiv', 3, 16)()

    def glGetUniformfv(self):
        prog, loc, out = self.args(3)
        buf = (ctypes.c_float * 16)()
        _gl.glGetUniformfv(prog, loc, buf)
        self.emu.write(out, bytes(buf)[:4])

    def info_log(self, hostname):
        fn = getattr(_gl, hostname)
        def call():
            obj, size, length_p, log_p = self.args(4)
            buf = ctypes.create_string_buffer(max(size, 1))
            n = ctypes.c_int()
            fn(obj, size, ctypes.byref(n), buf)
            if length_p:
                self.emu.w32(length_p, n.value)
            if log_p and size:
                self.emu.write(log_p, buf.raw[:min(size, n.value + 1)])
            if n.value:
                print(f'[gles] {hostname}: {buf.value.decode(errors="replace").strip()}')
        return call

    def glGetShaderInfoLog(self): self.info_log('glGetShaderInfoLog')()
    def glGetProgramInfoLog(self): self.info_log('glGetProgramInfoLog')()

    def glGetShaderPrecisionFormat(self):
        stype, ptype, range_p, prec_p = self.args(4)
        r, p = (ctypes.c_int * 2)(), ctypes.c_int()
        _gl.glGetShaderPrecisionFormat(stype, ptype, r, ctypes.byref(p))
        self.emu.write(range_p, bytes(r))
        self.emu.w32(prec_p, p.value)

    def located(self, hostname):
        fn = getattr(_gl, hostname)
        fn.restype = ctypes.c_int
        def call():
            prog, name_p = self.args(2)
            self.ret(fn(prog, self.emu.cstr(name_p).encode()))
        return call

    def glGetAttribLocation(self): self.located('glGetAttribLocation')()
    def glGetUniformLocation(self): self.located('glGetUniformLocation')()

    def glBindAttribLocation(self):
        prog, idx, name_p = self.args(3)
        _gl.glBindAttribLocation(prog, idx, self.emu.cstr(name_p).encode())

    def active(self, hostname):
        fn = getattr(_gl, hostname)
        def call():
            prog, idx, size, len_p, sz_p, type_p, name_p = self.args(7)
            n, sz, t = ctypes.c_int(), ctypes.c_int(), ctypes.c_uint()
            buf = ctypes.create_string_buffer(max(size, 1))
            fn(prog, idx, size, ctypes.byref(n), ctypes.byref(sz), ctypes.byref(t), buf)
            if len_p:
                self.emu.w32(len_p, n.value)
            self.emu.w32(sz_p, sz.value)
            self.emu.w32(type_p, t.value)
            if size:
                self.emu.write(name_p, buf.raw[:n.value + 1])
        return call

    def glGetActiveAttrib(self): self.active('glGetActiveAttrib')()
    def glGetActiveUniform(self): self.active('glGetActiveUniform')()

    def uniform_v(self, hostname, comps, ctype, matrix=False):
        fn = getattr(_gl, hostname)
        def call():
            if matrix:
                loc, count, transpose, p = self.args(4)
                data = self.read(p, 4 * comps * count)
                fn(loc, count, transpose & 0xff, (ctype * (comps * count)).from_buffer_copy(data))
            else:
                loc, count, p = self.args(3)
                data = self.read(p, 4 * comps * count)
                fn(loc, count, (ctype * (comps * count)).from_buffer_copy(data))
        return call

    def glTexParameteriv(self):
        target, pname, p = self.args(3)
        _gl.glTexParameteri(target, pname, ctypes.c_int(self.emu.r32(p)))

    def glTexParameterfv(self):
        target, pname, p = self.args(3)
        _gl.glTexParameterf(target, pname, ctypes.c_float(bits_to_float(self.emu.r32(p))))

    def attrib_v(self, comps):
        fn = getattr(_gl, f'glVertexAttrib{comps}fv')
        def call():
            idx, p = self.args(2)
            fn(idx, (ctypes.c_float * comps).from_buffer_copy(self.read(p, 4 * comps)))
        return call

    # ------------------------------------------------------------------------ drawing
    def bound(self, binding):
        v = ctypes.c_int()
        _gl.glGetIntegerv(binding, ctypes.byref(v))
        return v.value

    def glVertexAttribPointer(self):
        idx, size, typ, norm, stride, ptr = self.args(6)
        if self.bound(GL_ARRAY_BUFFER_BINDING):
            self.attribs.pop(idx, None)
            _gl.glVertexAttribPointer(idx, size, typ, norm & 0xff, stride, ctypes.c_void_p(ptr))
        else:
            self.attribs[idx] = (size, typ, norm & 0xff, stride, ptr)

    @property
    def attribs(self):
        return self.vao_attribs.setdefault(self.vao, {})

    def glBindVertexArrayOES(self):
        vao, = self.args(1)
        host('glBindVertexArray', 'u', 'v')(vao)
        self.vao = vao

    def glEnableVertexAttribArray(self):
        idx, = self.args(1)
        _gl.glEnableVertexAttribArray(idx)
        self.vao_enabled.setdefault(self.vao, set()).add(idx)

    def glDisableVertexAttribArray(self):
        idx, = self.args(1)
        _gl.glDisableVertexAttribArray(idx)
        self.vao_enabled.setdefault(self.vao, set()).discard(idx)

    def upload_client_arrays(self, first, count):
        """Client-side vertex arrays live in guest memory: copy the range a draw reads into a
        temporary host buffer and point the attribute at it."""
        if not any(i in self.vao_enabled.get(self.vao, ()) for i in self.attribs):
            return None
        vbo = ctypes.c_uint()
        _gl.glGenBuffers(1, ctypes.byref(vbo))
        _gl.glBindBuffer(GL_ARRAY_BUFFER, vbo)
        blob = b''
        layout = []
        enabled = self.vao_enabled.get(self.vao, set())
        for idx, (size, typ, norm, stride, ptr) in self.attribs.items():
            if idx not in enabled:
                continue
            elem = size * TYPE_SIZES.get(typ, 4)
            st = stride or elem
            start = ptr + first * st
            data = self.read(start, st * (count - 1) + elem) if count else b''
            off = len(blob)
            blob += data + bytes((-len(data)) % 4)
            layout.append((idx, size, typ, norm, st, off - first * st if False else off))
        _gl.glBufferData(GL_ARRAY_BUFFER, len(blob), blob, 0x88E0)       # GL_STREAM_DRAW
        for idx, size, typ, norm, st, off in layout:
            # the copied data starts at element `first`
            _gl.glVertexAttribPointer(idx, size, typ, norm, st, ctypes.c_void_p(off - first * st))
        return vbo

    def finish_client_arrays(self, vbo):
        if vbo is None:
            return
        _gl.glBindBuffer(GL_ARRAY_BUFFER, 0)
        _gl.glDeleteBuffers(1, ctypes.byref(vbo))

    def glDrawArrays(self):
        mode, first, count = self.args(3)
        self.draws += 1
        self.client_draws += bool(self.attribs)
        vbo = self.upload_client_arrays(first, count)
        _gl.glDrawArrays(mode, first, count)
        self.finish_client_arrays(vbo)

    def check_empty_textures(self):
        if not self.empty_textures:
            return
        active = self.bound(0x84E0)
        for unit in range(8):
            _gl.glActiveTexture(0x84C0 + unit)
            t = self.bound(0x8069)
            if t in self.empty_textures:
                prog = self.bound(0x8B8D)
                key = (t, unit, prog)
                if key not in self.empty_seen:
                    self.empty_seen.add(key)
                    print(f'[gles] frame {self.frame}: program {prog} draws with texture {t} on unit {unit}, which holds no pixels')
        _gl.glActiveTexture(active)

    def glDrawElements(self):
        if self.empty_textures:
            self.empty_seen = getattr(self, 'empty_seen', set())
            self.check_empty_textures()
        mode, count, typ, indices = self.args(4)
        self.draws += 1
        self.client_draws += bool(self.attribs)
        isz = {0x1401: 1, 0x1403: 2, 0x1405: 4}.get(typ, 2)
        ibo = None
        if not self.bound(GL_ELEMENT_ARRAY_BUFFER_BINDING):
            data = self.read(indices, count * isz)
            fmt = {1: 'B', 2: 'H', 4: 'I'}[isz]
            idxs = struct.unpack(f'<{count}{fmt}', data) if count else ()
            hi = max(idxs) if idxs else 0
            vbo = self.upload_client_arrays(0, hi + 1)
            ibo = ctypes.c_uint()
            _gl.glGenBuffers(1, ctypes.byref(ibo))
            _gl.glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, ibo)
            _gl.glBufferData(GL_ELEMENT_ARRAY_BUFFER, len(data), data, 0x88E0)
            _gl.glDrawElements(mode, count, typ, None)
            _gl.glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, 0)
            _gl.glDeleteBuffers(1, ctypes.byref(ibo))
        else:
            vbo = None
            if self.attribs:
                buf = self.index_shadow.get(self.bound(GL_ELEMENT_ARRAY_BUFFER_BINDING), b'')
                data = bytes(buf[indices:indices + count * isz])
                if len(data) == count * isz and count:
                    fmt = {1: 'B', 2: 'H', 4: 'I'}[isz]
                    vbo = self.upload_client_arrays(0, max(struct.unpack(f'<{count}{fmt}', data)) + 1)
                if os.environ.get('GL_DEBUG'):
                    print(f'[gles] drawElements ib={self.bound(GL_ELEMENT_ARRAY_BUFFER_BINDING)} off={indices} n={count} '
                          f'shadow={len(buf)} attribs={self.attribs} max={max(struct.unpack(f"<{count}{fmt}", data)) if vbo else None}', flush=True)
            _gl.glDrawElements(mode, count, typ, ctypes.c_void_p(indices))
        self.finish_client_arrays(vbo)

    # ------------------------------------------------------------------------ extensions
    def glDiscardFramebufferEXT(self):
        target, count, p = self.args(3)
        att = [self.emu.r32(p + 4 * i) for i in range(count)]
        att = [0x1800 + (a - 0x8CE0) if False else a for a in att]
        arr = (ctypes.c_uint * max(count, 1))(*att)
        f = host('glInvalidateFramebuffer', 'uip')
        if f:
            f(target, count, arr)

    def glResolveMultisampleFramebufferAPPLE(self):
        """Resolve the multisampled read framebuffer into the draw framebuffer. ES 3 only
        resolves between identical formats, and iOS games often multisample in RGB565 or RGB8
        while the screen is RGBA8, so the samples are first resolved into a single-sampled
        renderbuffer of their own format, which can then be blitted with conversion."""
        w, h = self.screen
        get_iv = host('glGetFramebufferAttachmentParameteriv', 'uuup', 'v')
        v = ctypes.c_int()
        get_iv(0x8CA8, GL_COLOR_ATTACHMENT0, 0x8CD1, ctypes.byref(v))      # attachment object name
        ms_rb = v.value
        prev_rb = ctypes.c_int()
        _gl.glGetIntegerv(GL_RENDERBUFFER_BINDING, ctypes.byref(prev_rb))
        _gl.glBindRenderbuffer(GL_RENDERBUFFER, ms_rb)
        fmt, rw, rh = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        _gl.glGetRenderbufferParameteriv(GL_RENDERBUFFER, 0x8D44, ctypes.byref(fmt))   # internal format
        _gl.glGetRenderbufferParameteriv(GL_RENDERBUFFER, 0x8D42, ctypes.byref(rw))
        _gl.glGetRenderbufferParameteriv(GL_RENDERBUFFER, 0x8D43, ctypes.byref(rh))
        w, h = rw.value or w, rh.value or h
        key = (fmt.value, w, h)
        if getattr(self, '_resolve_key', None) != key:
            if getattr(self, '_resolve_rb', None):
                _gl.glDeleteRenderbuffers(1, ctypes.byref(self._resolve_rb))
                _gl.glDeleteFramebuffers(1, ctypes.byref(self._resolve_fb))
            self._resolve_rb, self._resolve_fb = ctypes.c_uint(), ctypes.c_uint()
            _gl.glGenRenderbuffers(1, ctypes.byref(self._resolve_rb))
            _gl.glBindRenderbuffer(GL_RENDERBUFFER, self._resolve_rb)
            _gl.glRenderbufferStorage(GL_RENDERBUFFER, fmt.value, w, h)
            _gl.glGenFramebuffers(1, ctypes.byref(self._resolve_fb))
            self._resolve_key = key
            print(f'[gles] multisample resolve through a {w}x{h} renderbuffer, format {fmt.value:#x}')
        _gl.glBindRenderbuffer(GL_RENDERBUFFER, prev_rb.value)
        read_fb, draw_fb = ctypes.c_int(), ctypes.c_int()
        _gl.glGetIntegerv(0x8CAA, ctypes.byref(read_fb))      # GL_READ_FRAMEBUFFER_BINDING
        _gl.glGetIntegerv(0x8CA6, ctypes.byref(draw_fb))      # GL_DRAW_FRAMEBUFFER_BINDING
        _gl.glBindFramebuffer(0x8CA9, self._resolve_fb)
        _gl.glFramebufferRenderbuffer(0x8CA9, GL_COLOR_ATTACHMENT0, GL_RENDERBUFFER, self._resolve_rb.value)
        _gl.glBlitFramebuffer(0, 0, w, h, 0, 0, w, h, 0x4000, 0x2600)
        _gl.glBindFramebuffer(0x8CA8, self._resolve_fb)
        _gl.glBindFramebuffer(0x8CA9, draw_fb.value)
        _gl.glBlitFramebuffer(0, 0, w, h, 0, 0, w, h, 0x4000, 0x2600)
        _gl.glBindFramebuffer(0x8CA8, read_fb.value)

    @staticmethod
    def bound_buffer(target):
        b = ctypes.c_int()
        _gl.glGetIntegerv(0x8895 if target == 0x8893 else 0x8894, ctypes.byref(b))   # ELEMENT/ARRAY_BUFFER_BINDING
        return b.value

    def glMapBufferOES(self):
        # The game gets guest memory holding the buffer's current contents (it may change only
        # part of it), and unmapping uploads it all. Mappings are kept per buffer object, not per
        # target: Unity maps several vertex buffers in a row (binding each in turn) and only then
        # unmaps them. The guest pages are kept for the next map of the same size.
        target, access = self.args(2)
        size = ctypes.c_int()
        _gl.glGetBufferParameteriv(ctypes.c_uint(target), 0x8764, ctypes.byref(size))   # GL_BUFFER_SIZE
        pages = (max(size.value, 1) + 0xfff) & ~0xfff
        pool = self.__dict__.setdefault('map_pool', {})
        free = pool.get(pages)
        a = free.pop() if free else self.alloc(pages)
        if size.value:
            _gl.glMapBufferRange.restype = ctypes.c_void_p
            p = _gl.glMapBufferRange(ctypes.c_uint(target), ctypes.c_ssize_t(0),
                                     ctypes.c_ssize_t(size.value), ctypes.c_uint(1))   # GL_MAP_READ_BIT
            if p:
                self.emu.write(a, ctypes.string_at(p, size.value))
                _gl.glUnmapBuffer(ctypes.c_uint(target))
        self.mapped[(target, self.bound_buffer(target))] = (a, size.value, pages)
        self.ret(a)

    def glUnmapBufferOES(self):
        target, = self.args(1)
        a, size, pages = self.mapped.pop((target, self.bound_buffer(target)), (0, 0, 0))
        if a:
            if size:
                blob = self.read(a, size)
                # GLintptr/GLsizeiptr are 64-bit on the host: pass them typed, or the top half of
                # the offset is whatever was in the register
                _gl.glBufferSubData(ctypes.c_uint(target), ctypes.c_ssize_t(0), ctypes.c_ssize_t(size), blob)
                self.shadow_write(target, 0, blob)
            self.__dict__.setdefault('map_pool', {}).setdefault(pages, []).append(a)
        self.ret(1)

    def glLabelObjectEXT(self): self.ret(0)
    def glInsertEventMarkerEXT(self): self.ret(0)
    def glPushGroupMarkerEXT(self): self.ret(0)
    def glPopGroupMarkerEXT(self): self.ret(0)


def _es1_names():
    import gles1
    return gles1.NAMES


ES1_NAMES = set()


def bgra_to_rgba(data):
    b = bytearray(data)
    b[0::4], b[2::4] = b[2::4], b[0::4]
    return bytes(b)


def fix_shader(src):
    """Desktop-free GLSL ES 1.00 runs on the host as it is, with two adjustments: a #version line
    is added when missing (some drivers default differently), and #extension directives are
    moved up to just after it. Apple's compiler accepted them anywhere; the spec, and Android's
    drivers, want them before any other code (Bioshock's shaders, generated by Unreal, have one
    two thousand lines in). "require" becomes "enable", so an extension the host lacks is a
    warning rather than a failed shader."""
    lines = src.split(b'\n')
    version = None
    if lines and lines[0].lstrip().startswith(b'#version'):
        version = lines.pop(0)
    extensions, rest = [], []
    for ln in lines:
        t = ln.strip()
        if t.startswith(b'#extension'):
            extensions.append(t.replace(b': require', b': enable').replace(b':require', b':enable'))
        else:
            rest.append(ln)
    return b'\n'.join([version or b'#version 100'] + extensions + rest)


# Objective-C methods replaced: symbol -> GLES method name
TRACKED = ('glBindVertexArrayOES', 'glBindTexture', 'glCompileShader', 'glLinkProgram', 'glEnableVertexAttribArray', 'glDisableVertexAttribArray')

OBJC = {
    '-[EAGLContext initWithAPI:]': 'eagl_init_with_api',
    '-[EAGLContext initWithAPI:sharegroup:]': 'eagl_init_with_api_sharegroup',
    '+[EAGLContext setCurrentContext:]': 'eagl_set_current',
    '+[EAGLContext currentContext]': 'eagl_current',
    '-[EAGLContext API]': 'eagl_api',
    '-[EAGLContext sharegroup]': 'eagl_sharegroup',
    '-[EAGLContext renderbufferStorage:fromDrawable:]': 'eagl_storage_from_drawable',
    '-[EAGLContext presentRenderbuffer:]': 'eagl_present',
}


def _vector_methods():
    for n in (1, 2, 3, 4):
        setattr(GLES, f'glUniform{n}fv', lambda self, n=n: self.uniform_v(f'glUniform{n}fv', n, ctypes.c_float)())
        setattr(GLES, f'glUniform{n}iv', lambda self, n=n: self.uniform_v(f'glUniform{n}iv', n, ctypes.c_int)())
        setattr(GLES, f'glVertexAttrib{n}fv', lambda self, n=n: self.attrib_v(n)())
    for n in (2, 3, 4):
        setattr(GLES, f'glUniformMatrix{n}fv', lambda self, n=n: self.uniform_v(f'glUniformMatrix{n}fv', n * n, ctypes.c_float, True)())


_vector_methods()


def decode_pvrtc(emu, blob, w, h, bpp):
    """PVRTC to RGBA: in the CPU library when it has the decoder, else in numpy."""
    L = getattr(getattr(emu.uc, 'L', None), 'ppcpu_pvrtc', None) if hasattr(emu, 'uc') else None
    if L is not None and w > 0 and h > 0:
        out = ctypes.create_string_buffer(w * h * 4)
        data = bytes(blob)
        L(data, len(data), w, h, bpp, out)
        return out.raw
    from pvrtc import decode
    return decode(blob, w, h, bpp)
