#!/usr/bin/env sh
# Cross-compile the Windows artifacts from Linux and smoke-test them in wine.
#
#   ./scripts/build-mingw.sh            # x86, the usual VXDIAG bitness
#   ./scripts/build-mingw.sh x86_64
#   ./scripts/build-mingw.sh i686 --no-test
#
# Needs mingw-w64-gcc. The MSVC path (scripts\build-windows.ps1) remains the
# reference build; this one exists so the Windows binaries can be checked
# without leaving the development machine.
#
# WINDOWS 7 SP1: this toolchain links against the Universal CRT
# (api-ms-win-crt-*.dll), which Windows 7 does not have until KB2999226 is
# installed. Run scripts\list-j2534.ps1 on the target - it reports whether
# ucrtbase.dll is there. If it is not, either install KB2999226 or build with
# MSVC, which links the CRT statically and needs nothing.

set -eu

ARCH="${1:-i686}"
shift 2>/dev/null || true
RUN_TESTS=1
for arg in "$@"; do
    [ "$arg" = "--no-test" ] && RUN_TESTS=0
done

case "$ARCH" in
    i686)   BUILD=build-win32; PREFIX=i686-w64-mingw32 ;;
    x86_64) BUILD=build-win64; PREFIX=x86_64-w64-mingw32 ;;
    *) echo "usage: $0 [i686|x86_64] [--no-test]" >&2; exit 2 ;;
esac

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

command -v "${PREFIX}-g++" >/dev/null 2>&1 || {
    echo "${PREFIX}-g++ not found - install mingw-w64-gcc" >&2
    exit 1
}

GENERATOR=""
command -v ninja >/dev/null 2>&1 && GENERATOR="-G Ninja"

# shellcheck disable=SC2086
cmake -S . -B "$BUILD" $GENERATOR \
      -DCMAKE_TOOLCHAIN_FILE="cmake/mingw-${ARCH}.cmake" \
      -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build "$BUILD"

echo
echo "checking that nothing but system DLLs is needed at load time:"
for artifact in "$BUILD/proxy/j2534proxy.dll" \
                "$BUILD/fake-j2534/fake_j2534.dll" \
                "$BUILD/test-client/j2534-test.exe"; do
    deps=$("${PREFIX}-objdump" -p "$artifact" | sed -n 's/^\tDLL Name: //p' |
           grep -viE 'api-ms-win-|kernel32|ucrtbase|msvcrt|user32|advapi32' || true)
    if [ -n "$deps" ]; then
        echo "  $(basename "$artifact"): NEEDS $(echo "$deps" | tr '\n' ' ')" >&2
        exit 1
    fi
    echo "  $(basename "$artifact"): ok"
done

echo
echo "exported names (must be undecorated - no @4, @8 suffixes):"
"${PREFIX}-objdump" -p "$BUILD/proxy/j2534proxy.dll" |
    sed -n 's/.*\[ *[0-9]*\] +base\[ *[0-9]*\] *[0-9a-f]* \(PassThru.*\)/  \1/p'

if [ "$RUN_TESTS" -eq 1 ] && command -v wine >/dev/null 2>&1; then
    echo
    echo "core unit tests under wine:"
    WINEDEBUG=-all wine "$BUILD/proxy/proxy_core_test.exe"

    echo
    echo "proxy -> fake driver under wine:"
    WINEDEBUG=-all \
    VOLVO_J2534_REAL_DLL="$ROOT/$BUILD/fake-j2534/fake_j2534.dll" \
    VOLVO_J2534_LOG_DIR="$ROOT/$BUILD/logs" \
    VOLVO_J2534_SESSION_TAG=wine \
        wine "$BUILD/test-client/j2534-test.exe" "$BUILD/proxy/j2534proxy.dll" \
             --request 22F190 2>&1 | grep -v '^wine:'
fi

UCRT=$("${PREFIX}-objdump" -p "$BUILD/proxy/j2534proxy.dll" |
       grep -c 'api-ms-win-crt' || true)
echo
if [ "$UCRT" -gt 0 ]; then
    echo "note: this DLL imports the Universal CRT ($UCRT api-ms-win-crt-* imports)."
    echo "      Windows 10 has it; Windows 7 SP1 needs KB2999226."
    echo "      scripts\\list-j2534.ps1 reports whether the target machine has it,"
    echo "      and install-proxy.ps1 refuses to register the DLL if it does not."
fi

echo
echo "copy $BUILD/proxy/j2534proxy.dll to the Windows machine and run"
echo "scripts\\install-proxy.ps1 -ProxyDll <path> there."
