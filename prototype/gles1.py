"""OpenGL ES 1.1 on top of the host's OpenGL ES 3 context.

Games written for the iPhone's first GPUs (2008-2011) use OpenGL ES 1: matrices, lights, fog,
alpha test, texture environments and client arrays, with no shaders. Rather than ask the host
for an ES 1 context (which would need its own config, entry points and framebuffer names), the
guest's ES 1 context is an ordinary host ES 3 context, and this module keeps the fixed-function
state and draws with shaders generated from it. Everything else (framebuffers, textures, buffer
objects, readback, client-side arrays) goes through the same code as an ES 2 game's.

Calls are routed here by GLES.install while the current guest context is an ES 1 one."""
import ctypes
import math
import struct

from gles import _gl, host, bits_to_float, GET_COUNTS, DEVICE_STRINGS, GL_ARRAY_BUFFER_BINDING

# matrices
MODELVIEW, PROJECTION, TEXTURE = 0x1700, 0x1701, 0x1702
# client arrays -> attribute index
A_POS, A_COLOR, A_NORMAL, A_TC0, A_TC1, A_PSIZE = range(6)
ARRAYS = {0x8074: A_POS, 0x8076: A_COLOR, 0x8075: A_NORMAL, 0x8B9C: A_PSIZE}   # texcoords: by client unit
TEXCOORD_ARRAY = 0x8078
# capabilities kept here (the host's ES 3 has none of them)
TEXTURE_2D, LIGHTING, ALPHA_TEST, FOG, COLOR_MATERIAL = 0x0DE1, 0x0B50, 0x0BC0, 0x0B60, 0x0B57
NORMALIZE, RESCALE_NORMAL, POINT_SPRITE = 0x0BA1, 0x803A, 0x8861
LIGHT0 = 0x4000
IGNORED_CAPS = {0x0B10, 0x0B20, 0x809D, 0x809F, 0x0BF2, 0x0B52} | {0x3000 + i for i in range(6)}
LOCAL_CAPS = {TEXTURE_2D, LIGHTING, ALPHA_TEST, FOG, COLOR_MATERIAL, NORMALIZE, RESCALE_NORMAL, POINT_SPRITE} \
    | {LIGHT0 + i for i in range(8)} | IGNORED_CAPS
# texture environment
TEXTURE_ENV, ENV_MODE, ENV_COLOR = 0x2300, 0x2200, 0x2201
MODULATE, DECAL, BLEND, ADD, REPLACE, COMBINE = 0x2100, 0x2101, 0x0BE2, 0x0104, 0x1E01, 0x8570
COMBINE_RGB, COMBINE_ALPHA, RGB_SCALE, ALPHA_SCALE = 0x8571, 0x8572, 0x8573, 0x0D1C
ADD_SIGNED, INTERPOLATE, SUBTRACT, DOT3_RGB, DOT3_RGBA = 0x8574, 0x8575, 0x84E7, 0x86AE, 0x86AF
SRC_TEXTURE, SRC_CONSTANT, SRC_PRIMARY, SRC_PREVIOUS = 0x1702, 0x8576, 0x8577, 0x8578
OP_COLOR, OP_ONE_MINUS_COLOR, OP_ALPHA, OP_ONE_MINUS_ALPHA = 0x0300, 0x0301, 0x0302, 0x0303
COORD_REPLACE = 0x8862
GENERATE_MIPMAP = 0x8191
POINTS = 0
# texture base formats, by internal format
FORMATS = {0x1906: 'a', 0x1909: 'l', 0x190A: 'la', 0x1907: 'rgb', 0x1908: 'rgba', 0x80E1: 'rgba',
           0x8D62: 'rgb', 0x8C00: 'rgb', 0x8C01: 'rgb', 0x8C02: 'rgba', 0x8C03: 'rgba', 0x8D64: 'rgb',
           0x8051: 'rgb', 0x8058: 'rgba', 0x8056: 'rgba', 0x8057: 'rgba'}

ES1_VERSION = 'OpenGL ES-CM 1.1 IMGSGX543-73.16'
ES1_EXTENSIONS = ' '.join([
    'GL_APPLE_framebuffer_multisample', 'GL_APPLE_texture_2D_limited_npot', 'GL_APPLE_texture_format_BGRA8888',
    'GL_APPLE_texture_max_level', 'GL_EXT_blend_minmax', 'GL_EXT_discard_framebuffer', 'GL_EXT_read_format_bgra',
    'GL_EXT_texture_filter_anisotropic', 'GL_EXT_texture_lod_bias', 'GL_IMG_read_format',
    'GL_IMG_texture_compression_pvrtc', 'GL_OES_blend_equation_separate', 'GL_OES_blend_func_separate',
    'GL_OES_blend_subtract', 'GL_OES_depth24', 'GL_OES_fbo_render_mipmap', 'GL_OES_framebuffer_object',
    'GL_OES_mapbuffer', 'GL_OES_packed_depth_stencil', 'GL_OES_point_size_array', 'GL_OES_point_sprite',
    'GL_OES_read_format', 'GL_OES_rgb8_rgba8', 'GL_OES_stencil_wrap', 'GL_OES_stencil8',
    'GL_OES_texture_mirrored_repeat'])

# glGet values answered here: pname -> how to compute it
ALPHA_FUNCS = {0x200: 'false', 0x201: '<', 0x202: '==', 0x203: '<=', 0x204: '>', 0x205: '!=', 0x206: '>=', 0x207: 'true'}


# ---------------------------------------------------------------------- column-major 4x4 matrices
IDENTITY = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)


def mul(a, b):
    return tuple(sum(a[k * 4 + i] * b[c * 4 + k] for k in range(4)) for c in range(4) for i in range(4))


def transform(m, v):
    return tuple(sum(m[k * 4 + i] * v[k] for k in range(4)) for i in range(4))


def rotation(angle, x, y, z):
    n = math.sqrt(x * x + y * y + z * z)
    if n == 0:
        return IDENTITY
    x, y, z = x / n, y / n, z / n
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    t = 1 - c
    return (t * x * x + c, t * x * y + s * z, t * x * z - s * y, 0.0,
            t * x * y - s * z, t * y * y + c, t * y * z + s * x, 0.0,
            t * x * z + s * y, t * y * z - s * x, t * z * z + c, 0.0,
            0.0, 0.0, 0.0, 1.0)


def normal_matrix(m):
    """Inverse transpose of the upper 3x3, row by row as a column-major mat3."""
    a, b, c = m[0], m[4], m[8]
    d, e, f = m[1], m[5], m[9]
    g, h, i = m[2], m[6], m[10]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        return (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    inv = 1.0 / det
    # inverse (row-major), then transposed into column-major = the inverse read row-major
    r = ((e * i - f * h), -(b * i - c * h), (b * f - c * e),
         -(d * i - f * g), (a * i - c * g), -(a * f - c * d),
         (d * h - e * g), -(a * h - b * g), (a * e - b * d))
    return tuple(v * inv for v in r)


class TexUnit:
    def __init__(self):
        self.enabled = False
        self.mode = MODULATE
        self.color = (0.0, 0.0, 0.0, 0.0)
        self.combine = [MODULATE, MODULATE]                       # rgb, alpha
        self.src = [[SRC_TEXTURE, SRC_PREVIOUS, SRC_CONSTANT], [SRC_TEXTURE, SRC_PREVIOUS, SRC_CONSTANT]]
        self.op = [[OP_COLOR, OP_COLOR, OP_ALPHA], [OP_ALPHA, OP_ALPHA, OP_ALPHA]]
        self.scale = [1.0, 1.0]
        self.coord_replace = False
        self.texture = 0
        self.stack = [IDENTITY]
        self.coord = (0.0, 0.0, 0.0, 1.0)

    def env_key(self):
        if self.mode != COMBINE:
            return (self.mode,)
        return (COMBINE, tuple(self.combine), tuple(map(tuple, self.src)), tuple(map(tuple, self.op)),
                tuple(self.scale))


class Light:
    def __init__(self, i):
        on = 1.0 if i == 0 else 0.0
        self.ambient = (0.0, 0.0, 0.0, 1.0)
        self.diffuse = (on, on, on, 1.0)
        self.specular = (on, on, on, 1.0)
        self.position = (0.0, 0.0, 1.0, 0.0)            # eye space
        self.spot_dir = (0.0, 0.0, -1.0)
        self.spot_exp = 0.0
        self.spot_cutoff = 180.0
        self.att = [1.0, 0.0, 0.0]


class ES1:
    def __init__(self, g):
        self.g = g
        self.mode = MODELVIEW
        self.stacks = {MODELVIEW: [IDENTITY], PROJECTION: [IDENTITY]}
        self.caps = set()
        self.units = [TexUnit(), TexUnit()]
        self.unit = 0                    # glActiveTexture
        self.client_unit = 0             # glClientActiveTexture
        self.client = set()              # enabled client arrays (attribute indices)
        self.color = (1.0, 1.0, 1.0, 1.0)
        self.normal = (0.0, 0.0, 1.0)
        self.point_size = 1.0
        self.alpha_func, self.alpha_ref = 0x207, 0.0
        self.fog = {'mode': 0x0800, 'density': 1.0, 'start': 0.0, 'end': 1.0, 'color': (0.0, 0.0, 0.0, 0.0)}
        self.lights = [Light(i) for i in range(8)]
        self.model_ambient = (0.2, 0.2, 0.2, 1.0)
        self.material = {0x1200: (0.2, 0.2, 0.2, 1.0), 0x1201: (0.8, 0.8, 0.8, 1.0),
                         0x1202: (0.0, 0.0, 0.0, 1.0), 0x1600: (0.0, 0.0, 0.0, 1.0)}
        self.shininess = 0.0
        self.shade_model = 0x1D01
        self.tex_formats = {}            # texture name -> base format
        self.tex_automip = set()         # textures with GL_GENERATE_MIPMAP
        self.programs = {}               # state key -> (program, uniform locations)
        self.passthrough = {}
        self.strings = {}

    # ------------------------------------------------------------------ plumbing
    def a(self, i): return self.g.arg(i)
    def f(self, i): return bits_to_float(self.g.arg(i))
    def x(self, i):
        v = self.g.arg(i)
        return (v - (1 << 32) if v & 0x80000000 else v) / 65536.0
    def i(self, i):
        v = self.g.arg(i)
        return v - (1 << 32) if v & 0x80000000 else v

    def floats(self, p, n):
        return struct.unpack(f'<{n}f', self.g.read(p, 4 * n))

    def fixeds(self, p, n):
        return tuple(v / 65536.0 for v in struct.unpack(f'<{n}i', self.g.read(p, 4 * n)))

    def through(self, name):
        """The ES 2 implementation of a call, for calls that mean the same in both."""
        fn = self.passthrough.get(name)
        if fn is None:
            from gles import SCALAR
            fn = self.g.scalar(name) if name in SCALAR else getattr(self.g, name)
            self.passthrough[name] = fn
        return fn

    @property
    def stack(self):
        if self.mode == TEXTURE:
            return self.units[self.unit].stack
        return self.stacks[self.mode]

    def top(self, mode):
        return self.stacks[mode][-1]

    def set_top(self, m):
        self.stack[-1] = tuple(m)

    # ------------------------------------------------------------------ matrices
    def glMatrixMode(self): self.mode = self.a(0)
    def glLoadIdentity(self): self.set_top(IDENTITY)

    def glPushMatrix(self):
        s = self.stack
        if len(s) < 32:
            s.append(s[-1])

    def glPopMatrix(self):
        s = self.stack
        if len(s) > 1:
            s.pop()

    def glLoadMatrixf(self): self.set_top(self.floats(self.a(0), 16))
    def glLoadMatrixx(self): self.set_top(self.fixeds(self.a(0), 16))
    def glMultMatrixf(self): self.set_top(mul(self.stack[-1], self.floats(self.a(0), 16)))
    def glMultMatrixx(self): self.set_top(mul(self.stack[-1], self.fixeds(self.a(0), 16)))

    def translate(self, x, y, z):
        m = list(IDENTITY)
        m[12:15] = [x, y, z]
        self.set_top(mul(self.stack[-1], m))

    def glTranslatef(self): self.translate(self.f(0), self.f(1), self.f(2))
    def glTranslatex(self): self.translate(self.x(0), self.x(1), self.x(2))

    def scale(self, x, y, z):
        self.set_top(mul(self.stack[-1], (x, 0, 0, 0, 0, y, 0, 0, 0, 0, z, 0, 0, 0, 0, 1)))

    def glScalef(self): self.scale(self.f(0), self.f(1), self.f(2))
    def glScalex(self): self.scale(self.x(0), self.x(1), self.x(2))

    def glRotatef(self): self.set_top(mul(self.stack[-1], rotation(self.f(0), self.f(1), self.f(2), self.f(3))))
    def glRotatex(self): self.set_top(mul(self.stack[-1], rotation(self.x(0), self.x(1), self.x(2), self.x(3))))

    def ortho(self, l, r, b, t, n, f):
        if r == l or t == b or f == n:
            return
        m = (2 / (r - l), 0, 0, 0, 0, 2 / (t - b), 0, 0, 0, 0, -2 / (f - n), 0,
             -(r + l) / (r - l), -(t + b) / (t - b), -(f + n) / (f - n), 1)
        self.set_top(mul(self.stack[-1], m))

    def frustum(self, l, r, b, t, n, f):
        if r == l or t == b or f == n:
            return
        m = (2 * n / (r - l), 0, 0, 0, 0, 2 * n / (t - b), 0, 0,
             (r + l) / (r - l), (t + b) / (t - b), -(f + n) / (f - n), -1, 0, 0, -2 * f * n / (f - n), 0)
        self.set_top(mul(self.stack[-1], m))

    def six(self, conv):
        # six float arguments: four in registers, two on the stack
        return [conv(k) for k in range(6)]

    def glOrthof(self): self.ortho(*self.six(self.f))
    def glOrthox(self): self.ortho(*self.six(self.x))
    def glFrustumf(self): self.frustum(*self.six(self.f))
    def glFrustumx(self): self.frustum(*self.six(self.x))

    # ------------------------------------------------------------------ capabilities
    def set_cap(self, cap, on):
        if cap == TEXTURE_2D:
            self.units[self.unit].enabled = on
        elif cap in LOCAL_CAPS:
            (self.caps.add if on else self.caps.discard)(cap)
        else:
            self.through('glEnable' if on else 'glDisable')()

    def glEnable(self): self.set_cap(self.a(0), True)
    def glDisable(self): self.set_cap(self.a(0), False)

    def glIsEnabled(self):
        cap = self.a(0)
        if cap == TEXTURE_2D:
            self.g.ret(int(self.units[self.unit].enabled))
        elif cap in LOCAL_CAPS:
            self.g.ret(int(cap in self.caps))
        elif cap in ARRAYS or cap == TEXCOORD_ARRAY:
            idx = ARRAYS.get(cap, A_TC0 + self.client_unit)
            self.g.ret(int(idx in self.client))
        else:
            self.through('glIsEnabled')()

    def client_index(self, array):
        if array == TEXCOORD_ARRAY:
            return A_TC0 + self.client_unit
        return ARRAYS.get(array)

    def glEnableClientState(self):
        idx = self.client_index(self.a(0))
        if idx is not None:
            self.client.add(idx)

    def glDisableClientState(self):
        idx = self.client_index(self.a(0))
        if idx is not None:
            self.client.discard(idx)

    def glClientActiveTexture(self): self.client_unit = min(max(self.a(0) - 0x84C0, 0), 1)

    def glActiveTexture(self):
        self.unit = min(max(self.a(0) - 0x84C0, 0), 1)
        self.through('glActiveTexture')()

    def glBindTexture(self):
        target, name = self.a(0), self.a(1)
        if target == TEXTURE_2D:
            self.units[self.unit].texture = name
        self.through('glBindTexture')()

    # ------------------------------------------------------------------ arrays and current values
    def pointer(self, idx, size, typ, stride, ptr, normalized):
        g = self.g
        if g.bound(GL_ARRAY_BUFFER_BINDING):
            g.attribs.pop(idx, None)
            _gl.glVertexAttribPointer(idx, size, typ, int(normalized), stride, ctypes.c_void_p(ptr))
        else:
            g.attribs[idx] = (size, typ, int(normalized), stride, ptr)

    def glVertexPointer(self): self.pointer(A_POS, self.a(0), self.a(1), self.a(2), self.a(3), False)
    def glColorPointer(self):
        typ = self.a(1)
        self.pointer(A_COLOR, self.a(0), typ, self.a(2), self.a(3), typ in (0x1401, 0x1400))
    def glNormalPointer(self):
        typ = self.a(0)
        self.pointer(A_NORMAL, 3, typ, self.a(1), self.a(2), typ in (0x1400, 0x1402))
    def glTexCoordPointer(self):
        self.pointer(A_TC0 + self.client_unit, self.a(0), self.a(1), self.a(2), self.a(3), False)
    def glPointSizePointerOES(self): self.pointer(A_PSIZE, 1, self.a(0), self.a(1), self.a(2), False)

    def glColor4f(self): self.color = (self.f(0), self.f(1), self.f(2), self.f(3))
    def glColor4x(self): self.color = (self.x(0), self.x(1), self.x(2), self.x(3))
    def glColor4ub(self): self.color = tuple((self.a(k) & 0xff) / 255.0 for k in range(4))
    def glNormal3f(self): self.normal = (self.f(0), self.f(1), self.f(2))
    def glNormal3x(self): self.normal = (self.x(0), self.x(1), self.x(2))

    def glMultiTexCoord4f(self):
        u = min(max(self.a(0) - 0x84C0, 0), 1)
        self.units[u].coord = (self.f(1), self.f(2), self.f(3), self.f(4))

    def glMultiTexCoord4x(self):
        u = min(max(self.a(0) - 0x84C0, 0), 1)
        self.units[u].coord = (self.x(1), self.x(2), self.x(3), self.x(4))

    def glPointSize(self): self.point_size = self.f(0)
    def glPointSizex(self): self.point_size = self.x(0)
    def glPointParameterf(self): pass
    def glPointParameterfv(self): pass
    def glPointParameterx(self): pass
    def glPointParameterxv(self): pass
    def glShadeModel(self): self.shade_model = self.a(0)
    def glLogicOp(self): pass
    def glClipPlanef(self): pass
    def glClipPlanex(self): pass

    def glAlphaFunc(self): self.alpha_func, self.alpha_ref = self.a(0), self.f(1)
    def glAlphaFuncx(self): self.alpha_func, self.alpha_ref = self.a(0), self.x(1)

    # fixed-point forms of calls ES 2 also has
    def glClearColorx(self): host('glClearColor', 'ffff')(*(self.x(k) for k in range(4)))
    def glClearDepthx(self): host('glClearDepthf', 'f')(self.x(0))
    def glDepthRangex(self): host('glDepthRangef', 'ff')(self.x(0), self.x(1))
    def glLineWidthx(self): host('glLineWidth', 'f')(self.x(0))
    def glPolygonOffsetx(self): host('glPolygonOffset', 'ff')(self.x(0), self.x(1))
    def glSampleCoveragex(self): host('glSampleCoverage', 'fb')(self.x(0), self.a(1) & 0xff)

    # ------------------------------------------------------------------ texture environment
    # What GL accepts for each enum-valued parameter; anything else is GL_INVALID_ENUM and the
    # state stays as it was. Splinter Cell resets its combiner by setting every source and
    # operand to 0, which a phone ignores: taking the 0 drew its menu text as white boxes.
    ENV_MODES = {0x2100, 0x2101, 0x0BE2, 0x0104, 0x1E01, 0x8570}          # MODULATE DECAL BLEND ADD REPLACE COMBINE
    COMBINE_RGB_MODES = {0x1E01, 0x2100, 0x0104, 0x8574, 0x8575, 0x84E7, 0x86AE, 0x86AF}
    COMBINE_ALPHA_MODES = {0x1E01, 0x2100, 0x0104, 0x8574, 0x8575, 0x84E7}
    SOURCES = {SRC_TEXTURE, SRC_CONSTANT, SRC_PRIMARY, SRC_PREVIOUS}
    RGB_OPS = {OP_COLOR, OP_ONE_MINUS_COLOR, OP_ALPHA, OP_ONE_MINUS_ALPHA}
    ALPHA_OPS = {OP_ALPHA, OP_ONE_MINUS_ALPHA}

    def tex_env(self, target, pname, values):
        u = self.units[self.unit]
        v0 = values[0]
        valid = {ENV_MODE: self.ENV_MODES, COMBINE_RGB: self.COMBINE_RGB_MODES,
                 COMBINE_ALPHA: self.COMBINE_ALPHA_MODES}.get(pname)
        if valid is None:
            if 0x8580 <= pname <= 0x8582 or 0x8588 <= pname <= 0x858A:
                valid = self.SOURCES
            elif 0x8590 <= pname <= 0x8592:
                valid = self.RGB_OPS
            elif 0x8598 <= pname <= 0x859A:
                valid = self.ALPHA_OPS
        if valid is not None and target != POINT_SPRITE and int(v0) not in valid:
            return
        if target == POINT_SPRITE:
            if pname == COORD_REPLACE:
                u.coord_replace = bool(v0)
            return
        if pname == ENV_MODE:
            u.mode = int(v0)
        elif pname == ENV_COLOR:
            u.color = tuple(values[:4])
        elif pname == COMBINE_RGB:
            u.combine[0] = int(v0)
        elif pname == COMBINE_ALPHA:
            u.combine[1] = int(v0)
        elif pname == RGB_SCALE:
            u.scale[0] = float(v0)
        elif pname == ALPHA_SCALE:
            u.scale[1] = float(v0)
        elif 0x8580 <= pname <= 0x8582:
            u.src[0][pname - 0x8580] = int(v0)
        elif 0x8588 <= pname <= 0x858A:
            u.src[1][pname - 0x8588] = int(v0)
        elif 0x8590 <= pname <= 0x8592:
            u.op[0][pname - 0x8590] = int(v0)
        elif 0x8598 <= pname <= 0x859A:
            u.op[1][pname - 0x8598] = int(v0)

    def env_count(self, pname):
        return 4 if pname == ENV_COLOR else 1

    def glTexEnvi(self): self.tex_env(self.a(0), self.a(1), [self.i(2)])
    def glTexEnvf(self):
        pname = self.a(1)
        v = self.f(2)
        # enum-valued parameters are often passed as floats
        self.tex_env(self.a(0), pname, [v if pname in (RGB_SCALE, ALPHA_SCALE) else int(v)])
    def glTexEnvx(self):
        pname = self.a(1)
        v = self.a(2)
        self.tex_env(self.a(0), pname, [v / 65536.0 if pname in (RGB_SCALE, ALPHA_SCALE) else v])

    def glTexEnviv(self):
        t, p, ptr = self.a(0), self.a(1), self.a(2)
        vals = struct.unpack(f'<{self.env_count(p)}i', self.g.read(ptr, 4 * self.env_count(p)))
        if p == ENV_COLOR:                      # integer colours map [0, INT_MAX] onto [0, 1]
            vals = [v / 2147483647.0 for v in vals]
        self.tex_env(t, p, list(vals))

    def glTexEnvfv(self):
        t, p, ptr = self.a(0), self.a(1), self.a(2)
        vals = list(self.floats(ptr, self.env_count(p)))
        if p not in (ENV_COLOR, RGB_SCALE, ALPHA_SCALE):
            vals = [int(v) for v in vals]
        self.tex_env(t, p, vals)

    def glTexEnvxv(self):
        t, p, ptr = self.a(0), self.a(1), self.a(2)
        if p in (ENV_COLOR, RGB_SCALE, ALPHA_SCALE):
            vals = list(self.fixeds(ptr, self.env_count(p)))
        else:
            vals = list(struct.unpack('<i', self.g.read(ptr, 4)))
        self.tex_env(t, p, vals)

    # ------------------------------------------------------------------ textures
    def bound_texture(self):
        return self.units[self.unit].texture

    def tex_param(self, pname, value, call):
        if pname == GENERATE_MIPMAP:
            (self.tex_automip.add if value else self.tex_automip.discard)(self.bound_texture())
        else:
            call()

    def glTexParameteri(self): self.tex_param(self.a(1), self.a(2), self.through('glTexParameteri'))
    def glTexParameterf(self): self.tex_param(self.a(1), self.f(2), self.through('glTexParameterf'))
    def glTexParameterx(self):
        target, pname, v = self.a(0), self.a(1), self.a(2)
        self.tex_param(pname, v, lambda: host('glTexParameteri', 'uui')(target, pname, v))
    def glTexParameteriv(self): self.tex_param(self.a(1), self.g.emu.r32(self.a(2)), self.through('glTexParameteriv'))
    def glTexParameterfv(self):
        self.tex_param(self.a(1), self.floats(self.a(2), 1)[0], self.through('glTexParameterfv'))
    def glTexParameterxv(self):
        target, pname, v = self.a(0), self.a(1), self.g.emu.r32(self.a(2))
        self.tex_param(pname, v, lambda: host('glTexParameteri', 'uui')(target, pname, v))

    def note_image(self, level, internal):
        name = self.bound_texture()
        if level == 0:
            self.tex_formats[name] = FORMATS.get(internal, 'rgba')
            if name in self.tex_automip:
                _gl.glGenerateMipmap(TEXTURE_2D)

    def glTexImage2D(self):
        level, internal = self.a(1), self.a(2)
        self.through('glTexImage2D')()
        self.note_image(level, internal)

    def glTexSubImage2D(self):
        self.through('glTexSubImage2D')()
        if self.a(1) == 0 and self.bound_texture() in self.tex_automip:
            _gl.glGenerateMipmap(TEXTURE_2D)

    def glCompressedTexImage2D(self):
        level, internal = self.a(1), self.a(2)
        self.through('glCompressedTexImage2D')()
        if level == 0:
            self.tex_formats[self.bound_texture()] = FORMATS.get(internal, 'rgba')

    def glCopyTexImage2D(self):
        self.through('glCopyTexImage2D')()
        self.note_image(self.a(1), self.a(2))

    # ------------------------------------------------------------------ lights, material, fog
    def light(self, n, pname, v):
        if not 0 <= n < 8:
            return
        L = self.lights[n]
        if pname == 0x1200: L.ambient = tuple(v[:4])
        elif pname == 0x1201: L.diffuse = tuple(v[:4])
        elif pname == 0x1202: L.specular = tuple(v[:4])
        elif pname == 0x1203: L.position = transform(self.top(MODELVIEW), v[:4])
        elif pname == 0x1204:
            m = self.top(MODELVIEW)
            L.spot_dir = tuple(sum(m[k * 4 + i] * v[k] for k in range(3)) for i in range(3))
        elif pname == 0x1205: L.spot_exp = v[0]
        elif pname == 0x1206: L.spot_cutoff = v[0]
        elif 0x1207 <= pname <= 0x1209: L.att[pname - 0x1207] = v[0]

    LIGHT_COUNTS = {0x1200: 4, 0x1201: 4, 0x1202: 4, 0x1203: 4, 0x1204: 3}

    def glLightf(self): self.light(self.a(0) - LIGHT0, self.a(1), [self.f(2)])
    def glLightx(self): self.light(self.a(0) - LIGHT0, self.a(1), [self.x(2)])
    def glLightfv(self):
        p = self.a(1)
        self.light(self.a(0) - LIGHT0, p, list(self.floats(self.a(2), self.LIGHT_COUNTS.get(p, 1))))
    def glLightxv(self):
        p = self.a(1)
        self.light(self.a(0) - LIGHT0, p, list(self.fixeds(self.a(2), self.LIGHT_COUNTS.get(p, 1))))

    def light_model(self, pname, v):
        if pname == 0x0B53:
            self.model_ambient = tuple(v[:4])

    def glLightModelf(self): self.light_model(self.a(0), [self.f(1)])
    def glLightModelx(self): self.light_model(self.a(0), [self.x(1)])
    def glLightModelfv(self):
        p = self.a(0)
        self.light_model(p, list(self.floats(self.a(1), 4 if p == 0x0B53 else 1)))
    def glLightModelxv(self):
        p = self.a(0)
        self.light_model(p, list(self.fixeds(self.a(1), 4 if p == 0x0B53 else 1)))

    def material_set(self, pname, v):
        if pname == 0x1601:
            self.shininess = v[0]
        elif pname == 0x1602:
            self.material[0x1200] = self.material[0x1201] = tuple(v[:4])
        elif pname in self.material:
            self.material[pname] = tuple(v[:4])

    def glMaterialf(self): self.material_set(self.a(1), [self.f(2)])
    def glMaterialx(self): self.material_set(self.a(1), [self.x(2)])
    def glMaterialfv(self):
        p = self.a(1)
        self.material_set(p, list(self.floats(self.a(2), 1 if p == 0x1601 else 4)))
    def glMaterialxv(self):
        p = self.a(1)
        self.material_set(p, list(self.fixeds(self.a(2), 1 if p == 0x1601 else 4)))

    def fog_set(self, pname, v):
        if pname == 0x0B65: self.fog['mode'] = int(v[0])
        elif pname == 0x0B62: self.fog['density'] = v[0]
        elif pname == 0x0B63: self.fog['start'] = v[0]
        elif pname == 0x0B64: self.fog['end'] = v[0]
        elif pname == 0x0B66: self.fog['color'] = tuple(v[:4])

    def glFogf(self): self.fog_set(self.a(0), [self.f(1)])
    def glFogx(self):
        p = self.a(0)
        self.fog_set(p, [self.a(1) if p == 0x0B65 else self.x(1)])
    def glFogfv(self):
        p = self.a(0)
        self.fog_set(p, list(self.floats(self.a(1), 4 if p == 0x0B66 else 1)))
    def glFogxv(self):
        p = self.a(0)
        if p == 0x0B65:
            self.fog_set(p, [self.g.emu.r32(self.a(1))])
        else:
            self.fog_set(p, list(self.fixeds(self.a(1), 4 if p == 0x0B66 else 1)))

    # ------------------------------------------------------------------ queries
    def get_values(self, pname):
        """ES 1 state the host does not have, as a list of numbers, or None."""
        if pname == 0x0BA6: return list(self.top(MODELVIEW))
        if pname == 0x0BA7: return list(self.top(PROJECTION))
        if pname == 0x0BA8: return list(self.units[self.unit].stack[-1])
        if pname == 0x0BA0: return [self.mode]
        if pname == 0x0BA3: return [len(self.stacks[MODELVIEW])]
        if pname == 0x0BA4: return [len(self.stacks[PROJECTION])]
        if pname in (0x0D36, 0x0D38, 0x0D39): return [32]
        if pname == 0x0D31: return [8]                 # GL_MAX_LIGHTS
        if pname == 0x0D32: return [6]                 # GL_MAX_CLIP_PLANES
        if pname == 0x84E2: return [2]                 # GL_MAX_TEXTURE_UNITS
        if pname == 0x84E1: return [0x84C0 + self.client_unit]
        if pname == 0x0B00: return list(self.color)
        if pname == 0x0B02: return list(self.normal)
        if pname == 0x0B11: return [self.point_size]
        if pname == 0x0BC1: return [self.alpha_func]
        if pname == 0x0BC2: return [self.alpha_ref]
        if pname == 0x0B54: return [self.shade_model]
        if pname == 0x0B53: return list(self.model_ambient)
        return None

    def glGetFloatv(self):
        pname, out = self.a(0), self.a(1)
        v = self.get_values(pname)
        if v is None:
            return self.through('glGetFloatv')()
        self.g.emu.write(out, struct.pack(f'<{len(v)}f', *v))

    def glGetIntegerv(self):
        pname, out = self.a(0), self.a(1)
        v = self.get_values(pname)
        if v is None:
            return self.through('glGetIntegerv')()
        self.g.emu.write(out, struct.pack(f'<{len(v)}i', *(int(round(x)) for x in v)))

    def glGetFixedv(self):
        pname, out = self.a(0), self.a(1)
        v = self.get_values(pname)
        if v is None:
            buf = (ctypes.c_float * 16)()
            _gl.glGetFloatv(pname, buf)
            v = list(buf)[:GET_COUNTS.get(pname, 1)]
        self.g.emu.write(out, struct.pack(f'<{len(v)}i', *(int(x * 65536) for x in v)))

    def glGetBooleanv(self):
        pname, out = self.a(0), self.a(1)
        v = self.get_values(pname)
        if v is None:
            return self.through('glGetBooleanv')()
        self.g.emu.write(out, bytes(int(bool(x)) for x in v))

    def glGetString(self):
        name = self.a(0)
        text = {0x1F02: ES1_VERSION, 0x1F03: ES1_EXTENSIONS}.get(name)
        if text is None:
            return self.through('glGetString')()
        if name not in self.strings:
            b = text.encode() + b'\0'
            a = self.g.alloc(len(b))
            self.g.emu.write(a, b)
            self.strings[name] = a
        self.g.ret(self.strings[name])

    def glGetTexEnviv(self):
        u = self.units[self.unit]
        p, out = self.a(1), self.a(2)
        v = {ENV_MODE: u.mode, COMBINE_RGB: u.combine[0], COMBINE_ALPHA: u.combine[1]}.get(p, 0)
        self.g.emu.write(out, struct.pack('<i', v))

    def glGetTexEnvfv(self):
        u = self.units[self.unit]
        p, out = self.a(1), self.a(2)
        v = list(u.color) if p == ENV_COLOR else [float(u.mode if p == ENV_MODE else 0)]
        self.g.emu.write(out, struct.pack(f'<{len(v)}f', *v))

    def glGetLightfv(self):
        n, p, out = self.a(0) - LIGHT0, self.a(1), self.a(2)
        L = self.lights[n] if 0 <= n < 8 else self.lights[0]
        v = {0x1200: L.ambient, 0x1201: L.diffuse, 0x1202: L.specular, 0x1203: L.position,
             0x1204: L.spot_dir, 0x1205: (L.spot_exp,), 0x1206: (L.spot_cutoff,)}.get(p, (0.0,))
        self.g.emu.write(out, struct.pack(f'<{len(v)}f', *v))

    def glGetMaterialfv(self):
        p, out = self.a(1), self.a(2)
        v = (self.shininess,) if p == 0x1601 else self.material.get(p, (0.0,) * 4)
        self.g.emu.write(out, struct.pack(f'<{len(v)}f', *v))

    def glGetPointerv(self):
        self.g.emu.w32(self.a(1), 0)

    # ------------------------------------------------------------------ drawing
    def state_key(self, mode):
        lighting = LIGHTING in self.caps
        lights = tuple(i for i in range(8) if LIGHT0 + i in self.caps) if lighting else ()
        sprite = POINT_SPRITE in self.caps and mode == POINTS
        units = []
        for k, u in enumerate(self.units):
            if u.enabled and u.texture:
                units.append((k, self.tex_formats.get(u.texture, 'rgba'), u.env_key(),
                              sprite and u.coord_replace))
        return (lighting, lights, lighting and COLOR_MATERIAL in self.caps, tuple(units),
                ALPHA_FUNCS.get(self.alpha_func, 'true') if ALPHA_TEST in self.caps else 'true',
                self.fog['mode'] if FOG in self.caps else 0, mode == POINTS, A_PSIZE in self.client)

    def program(self, key):
        entry = self.programs.get(key)
        if entry is None:
            vs, fs = shaders(key)
            entry = (link(vs, fs), {})
            self.programs[key] = entry
        return entry

    def prepare(self, mode):
        """Before a draw: the program for the current state, its uniforms, and the attribute
        arrays (or constant values for arrays that are off)."""
        key = self.state_key(mode)
        prog, locs = self.program(key)
        if not prog:
            return False
        _gl.glUseProgram(prog)

        def loc(name):
            l = locs.get(name)
            if l is None:
                l = locs[name] = _gl.glGetUniformLocation(prog, name.encode())
            return l

        mv, proj = self.top(MODELVIEW), self.top(PROJECTION)
        mat4 = host('glUniformMatrix4fv', 'iibp')
        mat3 = host('glUniformMatrix3fv', 'iibp')
        mat4(loc('u_mvp'), 1, 0, (ctypes.c_float * 16)(*mul(proj, mv)))
        mat4(loc('u_mv'), 1, 0, (ctypes.c_float * 16)(*mv))
        lighting, lights, colmat, units, alpha, fog, points, psize_array = key
        if lighting:
            mat3(loc('u_nm'), 1, 0, (ctypes.c_float * 9)(*normal_matrix(mv)))
            v4 = host('glUniform4f', 'iffff')
            v4(loc('u_ambient'), *self.model_ambient)
            for name, p in (('u_ma', 0x1200), ('u_md', 0x1201), ('u_ms', 0x1202), ('u_me', 0x1600)):
                v4(loc(name), *self.material[p])
            host('glUniform1f', 'if')(loc('u_shin'), self.shininess)
            for i in lights:
                L = self.lights[i]
                v4(loc(f'u_la{i}'), *L.ambient)
                v4(loc(f'u_ld{i}'), *L.diffuse)
                v4(loc(f'u_ls{i}'), *L.specular)
                v4(loc(f'u_lp{i}'), *L.position)
                host('glUniform3f', 'ifff')(loc(f'u_lsd{i}'), *L.spot_dir)
                host('glUniform3f', 'ifff')(loc(f'u_latt{i}'), *L.att)
                host('glUniform2f', 'iff')(loc(f'u_lspot{i}'), L.spot_exp, L.spot_cutoff)
        for k, fmt, env, sprite in units:
            u = self.units[k]
            mat4(loc(f'u_tm{k}'), 1, 0, (ctypes.c_float * 16)(*u.stack[-1]))
            host('glUniform1i', 'ii')(loc(f'u_tex{k}'), k)
            host('glUniform4f', 'iffff')(loc(f'u_envc{k}'), *u.color)
        if alpha not in ('true', 'false'):
            host('glUniform1f', 'if')(loc('u_aref'), self.alpha_ref)
        if fog:
            f = self.fog
            host('glUniform4f', 'iffff')(loc('u_fogc'), *f['color'])
            host('glUniform3f', 'ifff')(loc('u_fogp'), f['density'], f['start'], f['end'])
        if points and not psize_array:
            host('glUniform1f', 'if')(loc('u_psize'), self.point_size)

        # attributes: arrays that are on come from the game, the others are constants
        g = self.g
        enabled = set(self.client)
        constants = {A_COLOR: self.color, A_NORMAL: self.normal + (0.0,),
                     A_TC0: self.units[0].coord, A_TC1: self.units[1].coord, A_PSIZE: (self.point_size, 0, 0, 1)}
        attrib4 = host('glVertexAttrib4f', 'uffff')
        for idx in range(6):
            if idx in enabled:
                _gl.glEnableVertexAttribArray(idx)
            else:
                _gl.glDisableVertexAttribArray(idx)
                if idx in constants:
                    attrib4(idx, *constants[idx])
        g.vao_enabled[g.vao] = enabled
        return True

    def glDrawArrays(self):
        if self.prepare(self.a(0)):
            self.through('glDrawArrays')()

    def glDrawElements(self):
        if self.prepare(self.a(0)):
            self.through('glDrawElements')()


# ---------------------------------------------------------------------- shader generation
def combine_arg(k, src, op, alpha):
    s = {SRC_TEXTURE: 't', SRC_CONSTANT: f'u_envc{k}', SRC_PRIMARY: 'v_color', SRC_PREVIOUS: 'c'}.get(src, 'c')
    if alpha:
        return {OP_ALPHA: f'{s}.a', OP_ONE_MINUS_ALPHA: f'(1.0 - {s}.a)'}.get(op, f'{s}.a')
    return {OP_COLOR: f'{s}.rgb', OP_ONE_MINUS_COLOR: f'(vec3(1.0) - {s}.rgb)', OP_ALPHA: f'vec3({s}.a)',
            OP_ONE_MINUS_ALPHA: f'vec3(1.0 - {s}.a)'}.get(op, f'{s}.rgb')


def combine_func(fn, a0, a1, a2, alpha):
    one = '1.0' if alpha else 'vec3(1.0)'
    half = '0.5' if alpha else 'vec3(0.5)'
    return {REPLACE: a0, MODULATE: f'{a0} * {a1}', ADD: f'{a0} + {a1}', ADD_SIGNED: f'{a0} + {a1} - {half}',
            INTERPOLATE: f'{a0} * {a2} + {a1} * ({one} - {a2})', SUBTRACT: f'{a0} - {a1}'}.get(fn, f'{a0} * {a1}')


def env_code(k, fmt, env):
    """GLSL for one texture unit: c (previous colour) and t (texture sample) -> c."""
    has_rgb = fmt != 'a'
    has_a = fmt in ('a', 'la', 'rgba')
    mode = env[0]
    if mode == REPLACE:
        rgb = 't.rgb' if has_rgb else 'c.rgb'
        a = 't.a' if has_a else 'c.a'
    elif mode == MODULATE:
        rgb = 'c.rgb * t.rgb' if has_rgb else 'c.rgb'
        a = 'c.a * t.a' if has_a else 'c.a'
    elif mode == DECAL:
        rgb = {'rgba': 'mix(c.rgb, t.rgb, t.a)', 'rgb': 't.rgb'}.get(fmt, 'c.rgb')
        a = 'c.a'
    elif mode == BLEND:
        rgb = f'mix(c.rgb, u_envc{k}.rgb, t.rgb)' if has_rgb else 'c.rgb'
        a = 'c.a * t.a' if has_a else 'c.a'
    elif mode == ADD:
        rgb = 'c.rgb + t.rgb' if has_rgb else 'c.rgb'
        a = 'c.a * t.a' if has_a else 'c.a'
    elif mode == COMBINE:
        _, (frgb, falpha), (srgb, salpha), (orgb, oalpha), (scrgb, sca) = env
        args = [combine_arg(k, srgb[n], orgb[n], False) for n in range(3)]
        if frgb in (DOT3_RGB, DOT3_RGBA):
            d = f'vec3(4.0 * dot({args[0]} - vec3(0.5), {args[1]} - vec3(0.5)))'
            rgb = d
            a = d + '.x' if frgb == DOT3_RGBA else None
        else:
            rgb = combine_func(frgb, *args, alpha=False)
            a = None
        if a is None:
            aargs = [combine_arg(k, salpha[n], oalpha[n], True) for n in range(3)]
            a = combine_func(falpha, *aargs, alpha=True)
        rgb = f'({rgb}) * {float(scrgb)}'
        a = f'({a}) * {float(sca)}'
    else:
        rgb, a = 'c.rgb', 'c.a'
    if fmt == 'a':
        pre = 't = vec4(0.0, 0.0, 0.0, t.a);'
    elif fmt in ('rgb', 'l'):
        pre = 't = vec4(t.rgb, 1.0);'
    else:
        pre = ''
    return f'{pre}\n    c = clamp(vec4({rgb}, {a}), 0.0, 1.0);'


def shaders(key):
    lighting, lights, colmat, units, alpha, fog, points, psize_array = key
    vs = ['#version 100',
          'attribute vec4 a_pos; attribute vec4 a_color; attribute vec3 a_normal;',
          'attribute vec4 a_tc0; attribute vec4 a_tc1; attribute float a_psize;',
          'uniform mat4 u_mvp; uniform mat4 u_mv;',
          'varying vec4 v_color; varying vec4 v_tc0; varying vec4 v_tc1; varying float v_fog;']
    if lighting:
        vs.append('uniform mat3 u_nm; uniform vec4 u_ambient, u_ma, u_md, u_ms, u_me; uniform float u_shin;')
        for i in lights:
            vs.append(f'uniform vec4 u_la{i}, u_ld{i}, u_ls{i}, u_lp{i}; uniform vec3 u_lsd{i}, u_latt{i}; '
                      f'uniform vec2 u_lspot{i};')
    for k, *_ in units:
        vs.append(f'uniform mat4 u_tm{k};')
    if fog:
        vs.append('uniform vec3 u_fogp;')
    if points and not psize_array:
        vs.append('uniform float u_psize;')
    body = ['void main() {', '    vec4 eye = u_mv * a_pos;', '    gl_Position = u_mvp * a_pos;']
    if lighting:
        ma = 'a_color' if colmat else 'u_ma'
        md = 'a_color' if colmat else 'u_md'
        body += ['    vec3 n = normalize(u_nm * a_normal);',
                 f'    vec4 ma = {ma}; vec4 md = {md};',
                 '    vec3 lit = u_me.rgb + u_ambient.rgb * ma.rgb;',
                 '    vec3 L; float att; float nl;']
        for i in lights:
            body += [f'    if (u_lp{i}.w != 0.0) {{ vec3 d = u_lp{i}.xyz - eye.xyz; float dist = length(d); L = d / max(dist, 1e-6);',
                     f'        att = 1.0 / max(u_latt{i}.x + u_latt{i}.y * dist + u_latt{i}.z * dist * dist, 1e-6); }}',
                     f'    else {{ L = normalize(u_lp{i}.xyz); att = 1.0; }}',
                     f'    if (u_lspot{i}.y < 180.0) {{ float sd = dot(-L, normalize(u_lsd{i}));',
                     f'        att *= sd >= cos(radians(u_lspot{i}.y)) ? pow(max(sd, 0.0), u_lspot{i}.x) : 0.0; }}',
                     '    nl = max(dot(n, L), 0.0);',
                     f'    lit += att * (u_la{i}.rgb * ma.rgb + nl * u_ld{i}.rgb * md.rgb);',
                     f'    if (nl > 0.0) lit += att * pow(max(dot(n, normalize(L + vec3(0.0, 0.0, 1.0))), 1e-4), u_shin) '
                     f'* u_ls{i}.rgb * u_ms.rgb;']
        body.append('    v_color = clamp(vec4(lit, md.a), 0.0, 1.0);')
    else:
        body.append('    v_color = a_color;')
    for k, *_ in units:
        body.append(f'    v_tc{k} = u_tm{k} * a_tc{k};')
    if fog:
        body.append('    float fd = abs(eye.z);')
        if fog == 0x2601:
            body.append('    v_fog = clamp((u_fogp.z - fd) / max(u_fogp.z - u_fogp.y, 1e-6), 0.0, 1.0);')
        elif fog == 0x0801:
            body.append('    v_fog = clamp(exp(-(u_fogp.x * fd) * (u_fogp.x * fd)), 0.0, 1.0);')
        else:
            body.append('    v_fog = clamp(exp(-u_fogp.x * fd), 0.0, 1.0);')
    else:
        body.append('    v_fog = 1.0;')
    if points:
        body.append('    gl_PointSize = ' + ('a_psize;' if psize_array else 'u_psize;'))
    body.append('}')

    fs = ['#version 100', 'precision mediump float;',
          'varying vec4 v_color; varying vec4 v_tc0; varying vec4 v_tc1; varying float v_fog;']
    for k, *_ in units:
        fs.append(f'uniform sampler2D u_tex{k}; uniform vec4 u_envc{k};')
    if alpha not in ('true', 'false'):
        fs.append('uniform float u_aref;')
    if fog:
        fs.append('uniform vec4 u_fogc;')
    fb = ['void main() {', '    vec4 c = v_color; vec4 t;']
    for k, fmt, env, sprite in units:
        coord = 'gl_PointCoord' if sprite else f'v_tc{k}.xy'
        fb.append(f'    t = texture2D(u_tex{k}, {coord});')
        fb.append('    ' + env_code(k, fmt, env))
    if alpha == 'false':
        fb.append('    discard;')
    elif alpha != 'true':
        fb.append(f'    if (!(c.a {alpha} u_aref)) discard;')
    if fog:
        fb.append('    c.rgb = mix(u_fogc.rgb, c.rgb, v_fog);')
    fb += ['    gl_FragColor = c;', '}']
    return '\n'.join(vs + body), '\n'.join(fs + fb)


def link(vs, fs):
    def compile_(kind, src):
        sh = _gl.glCreateShader(kind)
        b = ctypes.c_char_p(src.encode())
        _gl.glShaderSource(sh, 1, ctypes.byref(b), None)
        _gl.glCompileShader(sh)
        ok = ctypes.c_int()
        _gl.glGetShaderiv(sh, 0x8B81, ctypes.byref(ok))
        if not ok.value:
            log = ctypes.create_string_buffer(4096)
            _gl.glGetShaderInfoLog(sh, 4096, None, log)
            print(f'[gles1] shader did not compile: {log.value.decode(errors="replace")}\n{src}')
            return 0
        return sh
    v = compile_(0x8B31, vs)
    f = compile_(0x8B30, fs)
    if not v or not f:
        return 0
    p = _gl.glCreateProgram()
    _gl.glAttachShader(p, v)
    _gl.glAttachShader(p, f)
    for i, name in enumerate(('a_pos', 'a_color', 'a_normal', 'a_tc0', 'a_tc1', 'a_psize')):
        _gl.glBindAttribLocation(p, i, name.encode())
    _gl.glLinkProgram(p)
    ok = ctypes.c_int()
    _gl.glGetProgramiv(p, 0x8B82, ctypes.byref(ok))
    if not ok.value:
        log = ctypes.create_string_buffer(4096)
        _gl.glGetProgramInfoLog(p, 4096, None, log)
        print(f'[gles1] program did not link: {log.value.decode(errors="replace")}')
        return 0
    return p


# every ES 1 call this module answers (some of them also exist in ES 2 and are routed by API)
NAMES = {n for n in ES1.__dict__ if n.startswith('gl')}
