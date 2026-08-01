# Cross-compile the Windows x86 artifacts from Linux.
#
#   cmake -S . -B build-win32 -G Ninja \
#         -DCMAKE_TOOLCHAIN_FILE=cmake/mingw-i686.cmake \
#         -DCMAKE_BUILD_TYPE=RelWithDebInfo
#
# x86 is the bitness most VXDIAG J2534 drivers and VIDA installations use —
# but measure it with scripts\list-j2534.ps1 rather than trusting that.

set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_SYSTEM_PROCESSOR x86)

set(TOOLCHAIN_PREFIX i686-w64-mingw32)
set(CMAKE_C_COMPILER   ${TOOLCHAIN_PREFIX}-gcc)
set(CMAKE_CXX_COMPILER ${TOOLCHAIN_PREFIX}-g++)
set(CMAKE_RC_COMPILER  ${TOOLCHAIN_PREFIX}-windres)

set(CMAKE_FIND_ROOT_PATH /usr/${TOOLCHAIN_PREFIX})
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
