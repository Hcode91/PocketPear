// ppcpu: PocketPear's CPU core. A 32-bit ARM (ARMv7, ARM and Thumb) guest run by dynarmic, behind
// a small C interface that mirrors the parts of Unicorn the emulator uses, so the Python side can
// swap one for the other (prototype/cpu.py).
//
// Memory is a page table of host pointers (4 KB pages over the 4 GB guest space); each map() is
// one contiguous anonymous host block, so a mapping can also be handed to Unicorn with
// mem_map_ptr for instructions dynarmic cannot run (InterpreterFallback).
//
// The CPU stops and returns to the caller for each svc, each code hook, each fault and each
// unsupported instruction; the caller handles it and runs again. Code hooks are traps compiled
// in front of the hooked instruction; step() runs that instruction once with the trap bypassed.

#include <sys/mman.h>

#include <array>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <map>
#include <memory>
#include <optional>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#include <algorithm>
#include <cmath>
#include <cstdlib>

#include "dynarmic/frontend/A32/a32_ir_emitter.h"
#include "dynarmic/interface/A32/a32.h"
#include "dynarmic/interface/A32/config.h"
#include "dynarmic/interface/A32/coprocessor.h"
#include "dynarmic/interface/exclusive_monitor.h"
#include "dynarmic/ir/terminal.h"

using u8 = std::uint8_t;
using u16 = std::uint16_t;
using u32 = std::uint32_t;
using u64 = std::uint64_t;

namespace {

constexpr u32 PAGE_BITS = 12;
constexpr u32 PAGE = 1u << PAGE_BITS;
constexpr u32 HOOK_SWI = 0x1000000;  // no real svc can carry this (ARM imm is 24 bits)

enum Reason : int {
    STOPPED = 0,    // stop() was called, or the instruction count / time limit ran out
    SVC = 1,        // info0 = swi number; PC is past the svc
    HOOK = 2,       // info0 = hooked address; PC is that address
    FAULT = 3,      // info0 = address, info1 = 1 for a write; PC is not reliable
    EXCEPTION = 4,  // info0 = pc, info1 = dynarmic Exception
    FALLBACK = 5,   // info0 = pc, info1 = instruction count the caller must run itself
};

// Context as saved per guest thread.
struct Context {
    u32 regs[16];
    u32 cpsr;
    u32 fpscr;
    u32 ext[64];
    u32 tpidruro;
    u32 tpidrurw;
};

struct Region {
    u32 size;
    u8* host;
};

struct CPU;
using Dynarmic::A32::CoprocReg;

struct CP15 final : Dynarmic::A32::Coprocessor {
    CPU* cpu;
    explicit CP15(CPU* c) : cpu(c) {}
    std::optional<Callback> CompileInternalOperation(bool, unsigned, CoprocReg, CoprocReg, CoprocReg, unsigned) override {
        return std::nullopt;
    }
    CallbackOrAccessOneWord CompileSendOneWord(bool two, unsigned opc1, CoprocReg CRn, CoprocReg CRm, unsigned opc2) override;
    CallbackOrAccessTwoWords CompileSendTwoWords(bool, unsigned, CoprocReg) override { return std::monostate{}; }
    CallbackOrAccessOneWord CompileGetOneWord(bool two, unsigned opc1, CoprocReg CRn, CoprocReg CRm, unsigned opc2) override;
    CallbackOrAccessTwoWords CompileGetTwoWords(bool, unsigned, CoprocReg) override { return std::monostate{}; }
    std::optional<Callback> CompileLoadWords(bool, bool, CoprocReg, std::optional<u8>) override { return std::nullopt; }
    std::optional<Callback> CompileStoreWords(bool, bool, CoprocReg, std::optional<u8>) override { return std::nullopt; }
};

static u64 NoOp(void*, u32, u32) { return 0; }

struct CPU final : Dynarmic::A32::UserCallbacks {
    std::unique_ptr<std::array<u8*, Dynarmic::A32::UserConfig::NUM_PAGE_TABLE_ENTRIES>> pages;
    std::map<u32, Region> regions;
    std::unique_ptr<Dynarmic::ExclusiveMonitor> monitor;
    std::unique_ptr<Dynarmic::A32::Jit> jit;
    std::unordered_set<u32> hooks;
    bool bypass_hooks = false;

    // pthread mutexes, which the Python side replaces with svcs. Games lock and unlock tens of
    // thousands of times a second and almost never wait, so the uncontended cases are answered
    // here without leaving the JIT; anything else (a wait, a relock, an error) halts to Python,
    // which reads and writes the same table. owner is the thread id + 1, 0 when free.
    struct Mutex { u32 owner = 0, count = 0; };
    std::unordered_map<u32, Mutex> mutexes;
    u32 cur_tid = 0;
    u32 svc_lock[2] = {~0u, ~0u}, svc_trylock[2] = {~0u, ~0u}, svc_unlock[2] = {~0u, ~0u};
    u64 fast_calls = 0;

    // OpenGL ES calls that need nothing from the Python side but a straight call to the host's
    // GL (the "scalar" ones, and uniforms from a guest array), registered by the svc stub index
    // Python gave them. They are run here only on the thread whose context is current on the
    // host (gl_tid); anything else goes to Python, which switches contexts first.
    struct GlFn {
        void* fn = nullptr;
        int kind = 0;           // 1 scalar, 4 glBindTexture (a scalar that also notes non-2D targets), 2 uniform vector (loc, count, ptr), 3 uniform matrix (loc, count, transpose, ptr)
        char spec[12] = {};     // scalar argument kinds: u i f b
        char ret = 'v';         // v u b
        int comps = 0;          // floats or ints per element (kinds 2 and 3)
    };
    std::vector<GlFn> glfns;
    std::unordered_map<u32, u32> tex_targets;     // texture name -> target, for targets other than TEXTURE_2D
    u32 gl_tid = ~0u;
    u32 svc_indexed_thumb = ~0u, svc_indexed_arm = ~0u;

    u32 arg(int i) {
        auto& r = jit->Regs();
        return i < 4 ? r[i] : load<u32>(r[13] + 4 * (i - 4));
    }

    bool fast_gl(u32 swi) {
        auto& r = jit->Regs();
        bool thumb = jit->Cpsr() & 0x20;
        if (swi != (thumb ? svc_indexed_thumb : svc_indexed_arm) || cur_tid != gl_tid) return false;
        u8* np = host(r[15] + (thumb ? 2 : 4));
        if (!np) return false;
        u32 n;
        std::memcpy(&n, np, 4);
        if (n >= glfns.size() || !glfns[n].fn) return false;
        const GlFn& g = glfns[n];
        if (g.kind == 4) {
            u32 target = r[0], name = r[1];
            if (name) {
                if (target != 0x0DE1) tex_targets[name] = target;
                else if (!tex_targets.empty()) tex_targets.erase(name);
            }
            reinterpret_cast<void (*)(u32, u32)>(g.fn)(target, name);
        } else if (g.kind == 1) {
            u64 iv[8] = {};
            float fv[8] = {};
            int ni = 0, nf = 0;
            for (int i = 0; g.spec[i]; i++) {
                u32 w = arg(i);
                switch (g.spec[i]) {
                case 'f': { float f; std::memcpy(&f, &w, 4); if (nf < 8) fv[nf++] = f; break; }
                case 'b': if (ni < 8) iv[ni++] = w & 0xff; break;
                case 'i': if (ni < 8) iv[ni++] = u64(s64(s32(w))); break;
                default: if (ni < 8) iv[ni++] = w; break;
                }
            }
            // integer and floating-point arguments are given registers separately (System V
            // x86-64 and AAPCS64 alike), so one signature fits every mix of up to 6 + 8
            using F = u64 (*)(u64, u64, u64, u64, u64, u64, u64, u64, float, float, float, float, float, float, float, float);
            u64 v = reinterpret_cast<F>(g.fn)(iv[0], iv[1], iv[2], iv[3], iv[4], iv[5], iv[6], iv[7],
                                               fv[0], fv[1], fv[2], fv[3], fv[4], fv[5], fv[6], fv[7]);
            if (g.ret == 'u') r[0] = u32(v);
            else if (g.ret == 'b') r[0] = u32(v & 0xff);
        } else {
            bool matrix = g.kind == 3;
            s32 loc = s32(r[0]);
            s32 count = s32(r[1]);
            u32 p = matrix ? r[3] : r[2];
            u32 len = u32(count) * u32(g.comps) * 4;
            if (count <= 0 || count > 4096) return false;
            u8* a = host(p);
            u8* b = host(p + len - 1);
            if (!a || !b || b - a != std::ptrdiff_t(len - 1)) return false;
            if (matrix) reinterpret_cast<void (*)(s32, s32, u8, const void*)>(g.fn)(loc, count, u8(r[2]), a);
            else reinterpret_cast<void (*)(s32, s32, const void*)>(g.fn)(loc, count, a);
        }
        fast_calls++;
        return true;
    }

    bool fast_svc(u32 swi) {
        if (fast_gl(swi)) return true;
        bool lock = swi == svc_lock[0] || swi == svc_lock[1];
        bool trylock = swi == svc_trylock[0] || swi == svc_trylock[1];
        bool unlock = swi == svc_unlock[0] || swi == svc_unlock[1];
        if (!lock && !trylock && !unlock) return false;
        auto& r = jit->Regs();
        u32 me = cur_tid + 1;
        if (unlock) {
            auto it = mutexes.find(r[0]);
            if (it == mutexes.end() || it->second.owner != me) return false;
            if (--it->second.count == 0 || int(it->second.count) < 0) it->second = {};
        } else {
            Mutex& m = mutexes[r[0]];
            if (m.owner != 0) return false;
            m.owner = me;
            m.count = 1;
        }
        r[0] = 0;
        fast_calls++;
        return true;
    }

    u32 tpidruro = 0, tpidrurw = 0;

    // per run
    int reason = STOPPED;
    u32 info0 = 0, info1 = 0;
    u64 ticks_left = 0;

    CPU() : pages(std::make_unique<std::array<u8*, Dynarmic::A32::UserConfig::NUM_PAGE_TABLE_ENTRIES>>()) {
        pages->fill(nullptr);
        Dynarmic::A32::UserConfig cfg;
        cfg.callbacks = this;
        cfg.arch_version = Dynarmic::A32::ArchVersion::v7;
        cfg.page_table = pages.get();
        // accesses that straddle two pages go through the callbacks (the pages may not be adjacent on the host)
        cfg.detect_misaligned_access_via_page_table = 16 | 32 | 64 | 128;
        cfg.only_detect_misalignment_via_page_table_on_page_boundary = true;
        monitor = std::make_unique<Dynarmic::ExclusiveMonitor>(1);
        cfg.global_monitor = monitor.get();
        cfg.processor_id = 0;
        cfg.coprocessors[15] = std::make_shared<CP15>(this);
        // Speed over bit-exactness where games do not notice: NEON float ops need not reproduce
        // ARM's "standard FPSCR" (flush-to-zero, default NaN) or exact NaN payloads. (The global
        // monitor stays: a guest thread can be switched out between ldrex and strex.)
        // PPCPU_SAFE=1 in the environment turns this off.
        if (!getenv("PPCPU_SAFE")) {
            cfg.unsafe_optimizations = true;
            cfg.optimizations |= Dynarmic::OptimizationFlag::Unsafe_InaccurateNaN
                               | Dynarmic::OptimizationFlag::Unsafe_IgnoreStandardFPCRValue;
        }
        jit = std::make_unique<Dynarmic::A32::Jit>(cfg);
    }

    ~CPU() override {
        for (auto& [a, r] : regions) munmap(r.host, r.size);
    }

    // ------------------------------------------------------------------ memory
    u8* host(u32 a) {
        u8* p = (*pages)[a >> PAGE_BITS];
        return p ? p + (a & (PAGE - 1)) : nullptr;
    }

    int map(u32 addr, u32 size) {
        if ((addr | size) & (PAGE - 1) || size == 0) return -1;
        for (u64 a = addr; a < u64(addr) + size; a += PAGE)
            if ((*pages)[a >> PAGE_BITS]) return -2;
        void* m = mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (m == MAP_FAILED) return -3;
        u8* h = static_cast<u8*>(m);
        regions[addr] = Region{size, h};
        for (u32 off = 0; off < size; off += PAGE) (*pages)[(addr + off) >> PAGE_BITS] = h + off;
        return 0;
    }

    int unmap(u32 addr, u32 size) {
        if ((addr | size) & (PAGE - 1)) return -1;
        u64 end = u64(addr) + size;
        // split every region that overlaps [addr, end)
        auto it = regions.upper_bound(addr);
        if (it != regions.begin()) --it;
        while (it != regions.end() && it->first < end) {
            u32 s = it->first;
            Region r = it->second;
            u64 e = u64(s) + r.size;
            if (e <= addr) { ++it; continue; }
            it = regions.erase(it);
            u64 lo = std::max<u64>(s, addr), hi = std::min<u64>(e, end);
            munmap(r.host + (lo - s), hi - lo);
            for (u64 a = lo; a < hi; a += PAGE) (*pages)[a >> PAGE_BITS] = nullptr;
            if (s < lo) regions[s] = Region{u32(lo - s), r.host};
            if (hi < e) regions[u32(hi)] = Region{u32(e - hi), r.host + (hi - s)};
            it = regions.upper_bound(u32(hi - 1));
        }
        jit->InvalidateCacheRange(addr, size);
        return 0;
    }

    int read(u32 addr, u8* out, u32 n) {
        while (n) {
            u8* p = host(addr);
            if (!p) return -1;
            u32 k = std::min<u32>(n, PAGE - (addr & (PAGE - 1)));
            std::memcpy(out, p, k);
            out += k; addr += k; n -= k;
        }
        return 0;
    }

    int write(u32 addr, const u8* in, u32 n) {
        u32 a0 = addr, n0 = n;
        while (n) {
            u8* p = host(addr);
            if (!p) return -1;
            u32 k = std::min<u32>(n, PAGE - (addr & (PAGE - 1)));
            std::memcpy(p, in, k);
            in += k; addr += k; n -= k;
        }
        // writes from the host (stubs, loaders) may change code that is already compiled
        if (!jit->IsExecuting()) jit->InvalidateCacheRange(a0, n0);
        return 0;
    }

    void fault(u32 addr, bool is_write) {
        if (reason == STOPPED) {
            reason = FAULT;
            info0 = addr;
            info1 = is_write;
        }
        jit->HaltExecution();
    }

    template <typename T>
    T load(u32 a) {
        T v = 0;
        for (u32 i = 0; i < sizeof(T); i++) {
            u8* p = host(a + i);
            if (!p) { fault(a, false); return 0; }
            v |= T(*p) << (8 * i);
        }
        return v;
    }

    template <typename T>
    void store(u32 a, T v) {
        for (u32 i = 0; i < sizeof(T); i++) {
            u8* p = host(a + i);
            if (!p) { fault(a, true); return; }
            *p = u8(v >> (8 * i));
        }
    }

    // ------------------------------------------------------------------ dynarmic callbacks
    std::optional<u32> MemoryReadCode(u32 vaddr) override {
        u8* p = host(vaddr);
        if (!p) return std::nullopt;
        u32 v;
        std::memcpy(&v, p, 4);
        return v;
    }
    u8 MemoryRead8(u32 a) override { return load<u8>(a); }
    u16 MemoryRead16(u32 a) override { return load<u16>(a); }
    u32 MemoryRead32(u32 a) override { return load<u32>(a); }
    u64 MemoryRead64(u32 a) override { return load<u64>(a); }
    void MemoryWrite8(u32 a, u8 v) override { store<u8>(a, v); }
    void MemoryWrite16(u32 a, u16 v) override { store<u16>(a, v); }
    void MemoryWrite32(u32 a, u32 v) override { store<u32>(a, v); }
    void MemoryWrite64(u32 a, u64 v) override { store<u64>(a, v); }
    // one guest CPU (guest threads take turns), so an exclusive store only has to check the value
    template <typename T>
    bool store_exclusive(u32 a, T v, T expected) {
        if (load<T>(a) != expected) return false;
        store<T>(a, v);
        return true;
    }
    bool MemoryWriteExclusive8(u32 a, u8 v, u8 e) override { return store_exclusive(a, v, e); }
    bool MemoryWriteExclusive16(u32 a, u16 v, u16 e) override { return store_exclusive(a, v, e); }
    bool MemoryWriteExclusive32(u32 a, u32 v, u32 e) override { return store_exclusive(a, v, e); }
    bool MemoryWriteExclusive64(u32 a, u64 v, u64 e) override { return store_exclusive(a, v, e); }

    bool PreCodeReadHook(bool, u32 pc, Dynarmic::A32::IREmitter& ir) override {
        if (bypass_hooks || hooks.empty() || !hooks.count(pc)) return true;
        // stop in front of the hooked instruction, with PC on it
        ir.BranchWritePC(ir.Imm32(pc));
        ir.CallSupervisor(ir.Imm32(HOOK_SWI));
        ir.SetTerm(Dynarmic::IR::Term::CheckHalt{Dynarmic::IR::Term::ReturnToDispatch{}});
        return false;
    }

    void InterpreterFallback(u32 pc, size_t n) override {
        if (reason == STOPPED) {
            reason = FALLBACK;
            info0 = pc;
            info1 = u32(n);
        }
        jit->HaltExecution();
    }

    void CallSVC(u32 swi) override {
        if (reason == STOPPED && !bypass_hooks && fast_svc(swi)) return;
        if (reason == STOPPED) {
            if (swi == HOOK_SWI) {
                reason = HOOK;
                info0 = jit->Regs()[15];
            } else {
                reason = SVC;
                info0 = swi;
            }
        }
        jit->HaltExecution();
    }

    void ExceptionRaised(u32 pc, Dynarmic::A32::Exception e) override {
        if (reason == STOPPED) {
            reason = EXCEPTION;
            info0 = pc;
            info1 = u32(e);
        }
        jit->Regs()[15] = pc;
        jit->HaltExecution();
    }

    void AddTicks(u64 t) override { ticks_left = t >= ticks_left ? 0 : ticks_left - t; }
    u64 GetTicksRemaining() override { return ticks_left; }

    // ------------------------------------------------------------------ running
    // dynarmic only returns when its tick budget is spent or something halts it, so a run is a
    // series of short budgets with the count and the clock checked between them.
    static constexpr u64 CHUNK = 1u << 20;
    static constexpr auto HALTS = Dynarmic::HaltReason::UserDefined1 | Dynarmic::HaltReason::UserDefined2;

    int run(u64 count, u64 timeout_us, bool step) {
        reason = STOPPED;
        info0 = info1 = 0;
        if (step) {
            bypass_hooks = true;
            jit->Step();
            bypass_hooks = false;
            jit->ClearHalt(HALTS);
            return reason;
        }
        auto deadline = std::chrono::steady_clock::now() + std::chrono::microseconds(timeout_us);
        u64 left = count;
        for (;;) {
            u64 budget = count ? std::min(left, CHUNK) : CHUNK;
            ticks_left = budget;
            auto hr = jit->Run();
            jit->ClearHalt(HALTS);
            if (reason != STOPPED || Dynarmic::Has(hr, Dynarmic::HaltReason::UserDefined2)) break;
            if (count) {
                u64 done = budget - std::min(budget, ticks_left);
                left = done >= left ? 0 : left - done;
                if (!left) break;
            }
            if (timeout_us && std::chrono::steady_clock::now() >= deadline) break;
        }
        return reason;
    }

    void save(Context* c) {
        std::memcpy(c->regs, jit->Regs().data(), sizeof c->regs);
        c->cpsr = jit->Cpsr();
        c->fpscr = jit->Fpscr();
        std::memcpy(c->ext, jit->ExtRegs().data(), sizeof c->ext);
        c->tpidruro = tpidruro;
        c->tpidrurw = tpidrurw;
    }

    void restore(const Context* c) {
        std::memcpy(jit->Regs().data(), c->regs, sizeof c->regs);
        jit->SetCpsr(c->cpsr);
        jit->SetFpscr(c->fpscr);
        std::memcpy(jit->ExtRegs().data(), c->ext, sizeof c->ext);
        tpidruro = c->tpidruro;
        tpidrurw = c->tpidrurw;
        jit->ClearExclusiveState();
    }
};

Dynarmic::A32::Coprocessor::CallbackOrAccessOneWord CP15::CompileGetOneWord(bool two, unsigned opc1, CoprocReg CRn, CoprocReg CRm, unsigned opc2) {
    if (!two && opc1 == 0 && CRn == CoprocReg::C13 && CRm == CoprocReg::C0) {
        if (opc2 == 3) return &cpu->tpidruro;   // TPIDRURO: the thread's TLS base
        if (opc2 == 2) return &cpu->tpidrurw;
    }
    return std::monostate{};
}

Dynarmic::A32::Coprocessor::CallbackOrAccessOneWord CP15::CompileSendOneWord(bool two, unsigned opc1, CoprocReg CRn, CoprocReg CRm, unsigned opc2) {
    if (!two && opc1 == 0 && CRn == CoprocReg::C13 && CRm == CoprocReg::C0 && opc2 == 2) return &cpu->tpidrurw;
    if (!two && opc1 == 0 && CRn == CoprocReg::C7)  // cache maintenance and the old barrier forms
        return Callback{&NoOp, std::nullopt};
    return std::monostate{};
}

}  // namespace

// --------------------------------------------------------------------- C interface
extern "C" {

#define EXPORT __attribute__((visibility("default")))

EXPORT void* ppcpu_new() { return new CPU(); }
EXPORT void ppcpu_free(void* c) { delete static_cast<CPU*>(c); }

EXPORT int ppcpu_map(void* c, u32 addr, u32 size) { return static_cast<CPU*>(c)->map(addr, size); }
EXPORT int ppcpu_unmap(void* c, u32 addr, u32 size) { return static_cast<CPU*>(c)->unmap(addr, size); }
EXPORT int ppcpu_read(void* c, u32 addr, void* out, u32 n) { return static_cast<CPU*>(c)->read(addr, static_cast<u8*>(out), n); }
EXPORT int ppcpu_write(void* c, u32 addr, const void* in, u32 n) { return static_cast<CPU*>(c)->write(addr, static_cast<const u8*>(in), n); }
EXPORT void* ppcpu_host(void* c, u32 addr) { return static_cast<CPU*>(c)->host(addr); }

// r0-r15 in place, for callers that read and write them often (one call instead of one per
// register). Valid for the life of the CPU. A PC written here does not switch ARM/Thumb.
EXPORT u32* ppcpu_regs(void* c) { return static_cast<CPU*>(c)->jit->Regs().data(); }

// PVRTC1 (2 or 4 bpp) to RGBA8, the same decoding as prototype/pvrtc.py (which stays as the
// fallback): block colours interpolated bilinearly, blended by each pixel's modulation value.
// out holds width * height * 4 bytes.
EXPORT void ppcpu_pvrtc(const u8* data, u32 len, int width, int height, int bpp, u8* out) {
    const int bw = bpp == 4 ? 4 : 8;
    const int nbx = std::max(width / bw, 2), nby = std::max(height / 4, 2);
    const int count = nbx * nby;
    std::vector<u64> words(count);
    std::vector<std::array<float, 4>> ca(count), cb(count);
    const int m = std::min(nbx, nby);
    for (int by = 0; by < nby; by++) {
        for (int bx = 0; bx < nbx; bx++) {
            long long idx = 0;
            int shift = 0;
            for (int bit = 1; bit < m; bit <<= 1) {
                idx |= (long long)((by & bit) != 0) << shift;
                idx |= (long long)((bx & bit) != 0) << (shift + 1);
                shift += 2;
            }
            if (nbx > nby) idx |= (long long)(bx >> (shift / 2)) << shift;
            else if (nby > nbx) idx |= (long long)(by >> (shift / 2)) << shift;
            if (idx > count - 1) idx = count - 1;
            if (idx < 0) idx = 0;
            u64 w = 0;
            for (int k = 0; k < 8; k++) {
                u32 o = (u32)idx * 8 + k;
                if (o < len) w |= (u64)data[o] << (8 * k);
            }
            const int i = by * nbx + bx;
            words[i] = w;
            for (int which = 0; which < 2; which++) {
                const u32 c = which == 0 ? (u32)(w >> 32) & 0xffff : (u32)(w >> 48) & 0xffff;
                const bool opaque = (c >> 15) & 1;
                u32 r, g, b, a;
                if (opaque) {
                    r = (c >> 10) & 0x1f; g = (c >> 5) & 0x1f;
                    b = which ? (c & 0x1f) : (((c >> 1) & 0xf) << 1 | ((c >> 4) & 1));
                    a = 255;
                } else {
                    r = ((c >> 8) & 0xf) << 1 | ((c >> 11) & 1);
                    g = ((c >> 4) & 0xf) << 1 | ((c >> 7) & 1);
                    b = which ? ((c & 0xf) << 1 | ((c >> 3) & 1)) : (((c >> 1) & 0x7) << 2 | ((c >> 2) & 3));
                    a = (((c >> 12) & 7) * 255) / 7;
                }
                auto to8 = [](u32 v) { return (float)((v << 3) | (v >> 2)); };
                auto& dst = which == 0 ? ca[i] : cb[i];
                dst = {to8(r), to8(g), to8(b), (float)a};
            }
        }
    }
    auto wrap = [](int v, int n) { v %= n; return v < 0 ? v + n : v; };
    for (int y = 0; y < height; y++) {
        const float v = (y - 2) / 4.0f;
        const int y0f = (int)std::floor(v);
        const float fy = v - y0f;
        const int y0 = wrap(y0f, nby), y1 = wrap(y0f + 1, nby);
        for (int x = 0; x < width; x++) {
            const float u = (x - bw / 2.0f) / bw;
            const int x0f = (int)std::floor(u);
            const float fx = u - x0f;
            const int x0 = wrap(x0f, nbx), x1 = wrap(x0f + 1, nbx);
            const int bi = (y / 4) * nbx + (x / bw);
            const u64 w = words[bi];
            const u32 lo = (u32)w;
            const u32 mode = (u32)(w >> 32) & 1;
            const int px = x % bw, py = y % 4;
            float weight;
            bool punch = false;
            if (bpp == 4) {
                const u32 mm = (lo >> (2 * (py * 4 + px))) & 3;
                static const float w0[4] = {0.0f, 3 / 8.0f, 5 / 8.0f, 1.0f}, w1[4] = {0.0f, 0.5f, 0.5f, 1.0f};
                weight = mode == 0 ? w0[mm] : w1[mm];
                punch = mode == 1 && mm == 2;
            } else {
                weight = (float)((lo >> (py * 8 + px)) & 1);
            }
            u8* o = out + ((size_t)y * width + x) * 4;
            for (int ch = 0; ch < 4; ch++) {
                auto up = [&](const std::vector<std::array<float, 4>>& col) {
                    return (col[y0 * nbx + x0][ch] * (1 - fx) + col[y0 * nbx + x1][ch] * fx) * (1 - fy)
                         + (col[y1 * nbx + x0][ch] * (1 - fx) + col[y1 * nbx + x1][ch] * fx) * fy;
                };
                float val = up(ca) * (1 - weight) + up(cb) * weight;
                if (ch == 3 && punch) val = 0;
                val = val < 0 ? 0 : (val > 255 ? 255 : val);
                o[ch] = (u8)val;
            }
        }
    }
}

// Regions: fills up to max (start, size, host) triples; returns how many there are.
EXPORT int ppcpu_regions(void* c, u32* starts, u32* sizes, void** hosts, int max) {
    auto* cpu = static_cast<CPU*>(c);
    int i = 0;
    for (auto& [a, r] : cpu->regions) {
        if (i < max) { starts[i] = a; sizes[i] = r.size; hosts[i] = r.host; }
        i++;
    }
    return i;
}

// Registers: 0-15 r0-r15, 16 cpsr, 17 fpscr, 18 tpidruro, 19 tpidrurw, 32-95 s0-s63.
EXPORT u32 ppcpu_reg(void* c, int i) {
    auto* cpu = static_cast<CPU*>(c);
    if (i < 16) return cpu->jit->Regs()[i];
    if (i == 16) return cpu->jit->Cpsr();
    if (i == 17) return cpu->jit->Fpscr();
    if (i == 18) return cpu->tpidruro;
    if (i == 19) return cpu->tpidrurw;
    if (i >= 32 && i < 96) return cpu->jit->ExtRegs()[i - 32];
    return 0;
}

EXPORT void ppcpu_set_reg(void* c, int i, u32 v) {
    auto* cpu = static_cast<CPU*>(c);
    if (i < 16) cpu->jit->Regs()[i] = v;
    else if (i == 16) cpu->jit->SetCpsr(v);
    else if (i == 17) cpu->jit->SetFpscr(v);
    else if (i == 18) cpu->tpidruro = v;
    else if (i == 19) cpu->tpidrurw = v;
    else if (i >= 32 && i < 96) cpu->jit->ExtRegs()[i - 32] = v;
}

EXPORT int ppcpu_context_size() { return sizeof(Context); }
EXPORT void ppcpu_save(void* c, void* out) { static_cast<CPU*>(c)->save(static_cast<Context*>(out)); }
EXPORT void ppcpu_restore(void* c, const void* in) { static_cast<CPU*>(c)->restore(static_cast<const Context*>(in)); }

EXPORT void ppcpu_add_hook(void* c, u32 addr) {
    auto* cpu = static_cast<CPU*>(c);
    cpu->hooks.insert(addr & ~1u);
    cpu->jit->InvalidateCacheRange(addr & ~1u, 4);
}

EXPORT void ppcpu_invalidate(void* c, u32 addr, u32 size) { static_cast<CPU*>(c)->jit->InvalidateCacheRange(addr, size); }

// Run until something needs the caller. count: instruction limit (0 = none); timeout_us: wall
// clock limit (0 = none). Returns a reason; *info0 and *info1 explain it.
EXPORT int ppcpu_run(void* c, u64 count, u64 timeout_us, u32* info0, u32* info1) {
    auto* cpu = static_cast<CPU*>(c);
    int r = cpu->run(count, timeout_us, false);
    *info0 = cpu->info0;
    *info1 = cpu->info1;
    return r;
}

// One instruction, with code hooks bypassed (to run a hooked instruction after its hook).
EXPORT int ppcpu_step(void* c, u32* info0, u32* info1) {
    auto* cpu = static_cast<CPU*>(c);
    int r = cpu->run(0, 0, true);
    *info0 = cpu->info0;
    *info1 = cpu->info1;
    return r;
}

EXPORT void ppcpu_mutex_svcs(void* c, const u32* six) {
    auto* p = static_cast<CPU*>(c);
    for (int i = 0; i < 2; i++) {
        p->svc_lock[i] = six[i];
        p->svc_trylock[i] = six[2 + i];
        p->svc_unlock[i] = six[4 + i];
    }
}
EXPORT void ppcpu_gl_indexed(void* c, u32 thumb, u32 arm) {
    static_cast<CPU*>(c)->svc_indexed_thumb = thumb;
    static_cast<CPU*>(c)->svc_indexed_arm = arm;
}
EXPORT void ppcpu_gl_fn(void* c, u32 n, void* fn, int kind, const char* spec, int ret, int comps) {
    auto& v = static_cast<CPU*>(c)->glfns;
    if (n >= v.size()) v.resize(n + 1);
    auto& g = v[n];
    g.fn = fn;
    g.kind = kind;
    std::strncpy(g.spec, spec ? spec : "", sizeof g.spec - 1);
    g.ret = char(ret);
    g.comps = comps;
}
EXPORT u32 ppcpu_tex_target(void* c, u32 name) {
    auto& m = static_cast<CPU*>(c)->tex_targets;
    auto it = m.find(name);
    if (it == m.end()) return 0;
    u32 t = it->second;
    m.erase(it);
    return t;
}
EXPORT void ppcpu_gl_tid(void* c, u32 tid) { static_cast<CPU*>(c)->gl_tid = tid; }
EXPORT void ppcpu_set_tid(void* c, u32 tid) { static_cast<CPU*>(c)->cur_tid = tid; }
// owner (thread id + 1, 0 when free) and count of the mutex at addr
EXPORT void ppcpu_mutex_get(void* c, u32 addr, u32* owner, u32* count) {
    auto& m = static_cast<CPU*>(c)->mutexes;
    auto it = m.find(addr);
    *owner = it == m.end() ? 0 : it->second.owner;
    *count = it == m.end() ? 0 : it->second.count;
}
EXPORT void ppcpu_mutex_set(void* c, u32 addr, u32 owner, u32 count) {
    auto& m = static_cast<CPU*>(c)->mutexes;
    if (owner) m[addr] = {owner, count}; else m.erase(addr);
}
EXPORT u64 ppcpu_fast_calls(void* c) { return static_cast<CPU*>(c)->fast_calls; }
EXPORT void ppcpu_stop(void* c) { static_cast<CPU*>(c)->jit->HaltExecution(Dynarmic::HaltReason::UserDefined2); }

}  // extern "C"
