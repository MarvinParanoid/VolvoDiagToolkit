cmake_minimum_required(VERSION 3.16)
project(volvo-diagnostics VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(MSVC)
    add_compile_options(/W4 /permissive-)
    # /MT, not /MD: the proxy is loaded into VIDA's process on a machine that
    # may have neither the matching Visual C++ redistributable nor the
    # Universal CRT (Windows 7 without KB2999226). Needs CMake 3.15+, which
    # the 3.16 minimum above guarantees along with policy CMP0091.
    set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
else()
    add_compile_options(-Wall -Wextra -Wno-unused-parameter)
endif()

if(MINGW)
    # The proxy is loaded into VIDA's process on a machine that has no GCC
    # runtime, so nothing may be left to resolve at load time. Applied to
    # every target: an executable that needs libwinpthread-1.dll next to it
    # is just as useless for testing on that machine.
    add_link_options(-static)
endif()

if(WIN32 AND CMAKE_SIZEOF_VOID_P EQUAL 8)
    message(WARNING
        "Building a 64-bit J2534 proxy. VIDA and most VXDIAG J2534 DLLs are "
        "32-bit, and a proxy of the wrong bitness cannot be loaded at all. "
        "Check with scripts/list-j2534.ps1 first; for x86 configure with "
        "-A Win32 (MSVC) or -DCMAKE_TOOLCHAIN_FILE=cmake/mingw-i686.cmake.")
endif()

add_subdirectory(proxy)
add_subdirectory(fake-j2534)
add_subdirectory(test-client)

enable_testing()
add_test(NAME proxy_core COMMAND proxy_core_test)
