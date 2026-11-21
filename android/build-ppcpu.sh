#!/bin/sh
# Builds PocketPear's CPU core (core/ppcpu.cpp over dynarmic) for Android arm64 and puts it where
# the app build looks: app/src/main/jniLibs/arm64-v8a/libppcpu.so
# Usage: ./build-ppcpu.sh <android-ndk-dir> [dynarmic-source-dir]
# Needs the Boost headers (libboost-dev), found in /usr/include unless BOOST_INCLUDE_DIR says otherwise.
set -e
NDK=$1
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=${2:-$HERE/build-ppcpu/dynarmic}
DYNARMIC_COMMIT=a46601580d5512d324104f985b5f0209dc980ddc   # azahar-emu/dynarmic (0BSD)
cd "$HERE"
if [ ! -d "$SRC/.git" ]; then
    git clone -q https://github.com/azahar-emu/dynarmic.git "$SRC"
    git -C "$SRC" checkout -q "$DYNARMIC_COMMIT"
    git -C "$SRC" submodule update -q --init --recursive --depth 1
fi
# Boost is header-only here. Its headers get a folder of their own: /usr/include itself must not
# reach the compiler, or the host's C library headers would shadow the NDK's.
mkdir -p build-ppcpu/boost
ln -sfn "${BOOST_INCLUDE_DIR:-/usr/include}/boost" build-ppcpu/boost/boost
# Newer NDKs' libc++ no longer brings in <cstdlib> by itself, and dynarmic's bundled fmt calls
# malloc and free without including it (NDK 30: "use of undeclared identifier 'malloc'").
cmake -S ../core -B build-ppcpu/out -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE="$NDK/build/cmake/android.toolchain.cmake" \
    -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=26 -DCMAKE_BUILD_TYPE=Release \
    -DDYNARMIC_DIR="$SRC" -DBoost_INCLUDE_DIR="$HERE/build-ppcpu/boost" \
    -DCMAKE_CXX_FLAGS="-include cstdlib"
ninja -C build-ppcpu/out ppcpu
mkdir -p app/src/main/jniLibs/arm64-v8a
cp build-ppcpu/out/libppcpu.so app/src/main/jniLibs/arm64-v8a/
"$NDK"/toolchains/llvm/prebuilt/*/bin/llvm-strip app/src/main/jniLibs/arm64-v8a/libppcpu.so
echo "ppcpu ready"
