#include <windows.h>

#include "logger.h"
#include "real_driver.h"

/*
 * Nothing but bookkeeping happens here. Loading the vendor DLL or opening the
 * log file from DllMain would run under the loader lock; both are deferred to
 * the first PassThru* call (see proxy::ensure_init).
 */
BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID) {
    switch (reason) {
        case DLL_PROCESS_ATTACH:
            DisableThreadLibraryCalls(hModule);
            proxy::set_module_handle(hModule);
            break;
        case DLL_PROCESS_DETACH:
            proxy::logger::stop();
            break;
        default:
            break;
    }
    return TRUE;
}
