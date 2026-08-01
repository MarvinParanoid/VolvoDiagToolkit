#pragma once
#include <string>

#include "config.h"
#include "j2534.h"

namespace proxy {

/* The effective configuration (loaded on first use). */
const Config &config();

/* Function pointers into the genuine vendor DLL. Any of them may be null if
   the vendor driver does not export that entry point. */
struct RealDriver {
    long (J2534_API *Open)(const void *, unsigned long *) = nullptr;
    long (J2534_API *Close)(unsigned long) = nullptr;
    long (J2534_API *Connect)(unsigned long, unsigned long, unsigned long, unsigned long,
                              unsigned long *) = nullptr;
    long (J2534_API *Disconnect)(unsigned long) = nullptr;
    long (J2534_API *ReadMsgs)(unsigned long, PASSTHRU_MSG *, unsigned long *,
                               unsigned long) = nullptr;
    long (J2534_API *WriteMsgs)(unsigned long, PASSTHRU_MSG *, unsigned long *,
                                unsigned long) = nullptr;
    long (J2534_API *StartPeriodicMsg)(unsigned long, const PASSTHRU_MSG *, unsigned long *,
                                       unsigned long) = nullptr;
    long (J2534_API *StopPeriodicMsg)(unsigned long, unsigned long) = nullptr;
    long (J2534_API *StartMsgFilter)(unsigned long, unsigned long, const PASSTHRU_MSG *,
                                     const PASSTHRU_MSG *, const PASSTHRU_MSG *,
                                     unsigned long *) = nullptr;
    long (J2534_API *StopMsgFilter)(unsigned long, unsigned long) = nullptr;
    long (J2534_API *SetProgrammingVoltage)(unsigned long, unsigned long, unsigned long) = nullptr;
    long (J2534_API *ReadVersion)(unsigned long, char *, char *, char *) = nullptr;
    long (J2534_API *GetLastError)(char *) = nullptr;
    long (J2534_API *Ioctl)(unsigned long, unsigned long, const void *, void *) = nullptr;

    bool loaded = false;
    /* PassThruGetLastError hands the caller an 80-byte buffer, so the short
       form has to fit in 79 characters; the full explanation goes to the log,
       which has no such limit. */
    std::string load_error;
    std::string load_detail;
};

/* Reads the configuration, opens the log file and loads the vendor DLL.
   Called at the top of every export; runs exactly once. Must never be called
   from DllMain — LoadLibrary under the loader lock deadlocks. */
void ensure_init();

/* The resolved entry point table. Always a valid reference; check .loaded. */
const RealDriver &real();

/* Remembered from DllMain so the config and the vendor DLL can be found
   relative to this file rather than to VIDA's working directory. */
void set_module_handle(void *handle);

/* Directory of this DLL, with a trailing separator. */
const std::string &module_dir();

/* Message stored for PassThruGetLastError when the proxy itself fails. */
void set_proxy_error(const std::string &msg);
const std::string &proxy_error();
bool have_proxy_error();
void clear_proxy_error();

}  // namespace proxy
