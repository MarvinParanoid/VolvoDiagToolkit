# The parts that are pure C++ (config parsing, JSONL rendering) live in a
# static library so they can be unit-tested on any platform.
add_library(proxy_core STATIC
    src/config.cpp
    src/logger.cpp
)
target_include_directories(proxy_core PUBLIC include src)
target_compile_definitions(proxy_core PUBLIC VOLVO_PROXY_VERSION="${PROJECT_VERSION}")
set_target_properties(proxy_core PROPERTIES POSITION_INDEPENDENT_CODE ON)

set(PROXY_SOURCES src/exports.cpp src/real_driver.cpp)
if(WIN32)
    list(APPEND PROXY_SOURCES src/dllmain.cpp src/j2534proxy.def)
endif()

add_library(j2534proxy SHARED ${PROXY_SOURCES})
target_link_libraries(j2534proxy PRIVATE proxy_core)
set_target_properties(j2534proxy PROPERTIES PREFIX "" OUTPUT_NAME "j2534proxy")

if(NOT WIN32)
    target_link_libraries(j2534proxy PRIVATE dl)
endif()

if(MINGW)
    # Together with src/j2534proxy.def this gives undecorated __stdcall
    # exports; -static comes from the top-level CMakeLists.
    target_link_options(j2534proxy PRIVATE -Wl,--kill-at)
endif()

add_executable(proxy_core_test test/core_test.cpp)
target_link_libraries(proxy_core_test PRIVATE proxy_core)
