#!/bin/sh
# Builds Unicorn (ARM only) for Android arm64 and puts it where the app build looks:
#   app/src/main/jniLibs/arm64-v8a/libunicorn.so  and  app/build/unicorn-python/unicorn/
# Usage: ./build-unicorn.sh <android-ndk-dir> [unicorn-source-dir]
set -e
NDK=$1
SRC=${2:-build-unicorn/unicorn}
HERE=$(cd "$(dirname "$0")" && pwd)
cd "$HERE"
if [ ! -d "$SRC" ]; then
    git clone -q --depth 1 --branch 2.1.4 https://github.com/unicorn-engine/unicorn.git "$SRC"
fi
cmake -S "$SRC" -B build-unicorn/out -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE="$NDK/build/cmake/android.toolchain.cmake" \
    -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=26 -DCMAKE_BUILD_TYPE=Release \
    -DUNICORN_ARCH=arm -DUNICORN_BUILD_TESTS=OFF -DBUILD_SHARED_LIBS=ON
ninja -C build-unicorn/out unicorn
mkdir -p app/src/main/jniLibs/arm64-v8a app/build/unicorn-python
cp build-unicorn/out/libunicorn.so app/src/main/jniLibs/arm64-v8a/
"$NDK"/toolchains/llvm/prebuilt/*/bin/llvm-strip app/src/main/jniLibs/arm64-v8a/libunicorn.so
rm -rf app/build/unicorn-python/unicorn
cp -r "$SRC/bindings/python/unicorn" app/build/unicorn-python/
# The binding asks importlib.resources where its own lib/ folder is. Inside an APK the answer is
# an AssetPath, which is not a filesystem path, and Path(AssetPath) throws a TypeError that ends
# the import. The library comes from LIBUNICORN_PATH (the app's native library folder), so the
# lookup is dropped.
python3 - app/build/unicorn-python/unicorn/unicorn_py3/unicorn.py <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
old = "    canonicals = resources.files('unicorn') / 'lib'\n"
new = "    canonicals = None  # PocketPear: an APK has no lib/ folder; LIBUNICORN_PATH is used\n"
assert old in s, 'unicorn binding changed: update build-unicorn.sh'
open(p, 'w').write(s.replace(old, new))
PY
echo "Unicorn ready"
