#include "real_driver.h"

#include <mutex>

#include "config.h"
#include "logger.h"

#ifdef _WIN32
#  include <windows.h>
#else
#  include <dlfcn.h>
#endif

namespace proxy {
namespace {

RealDriver g_real;
Config g_cfg;
std::string g_dir;
std::string g_proxy_error;
std::once_flag g_once;
void *g_module = nullptr;

#ifdef _WIN32
std::string last_win_error() {
    const DWORD code = GetLastError();
    char *text = nullptr;
    FormatMessageA(FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM |
                       FORMAT_MESSAGE_IGNORE_INSERTS,
                   nullptr, code, 0, reinterpret_cast<char *>(&text), 0, nullptr);
    std::string msg = "win32 error " + std::to_string(code);
    if (text) {
        msg += ": ";
        msg += text;
        LocalFree(text);
        while (!msg.empty() && (msg.back() == '\n' || msg.back() == '\r')) msg.pop_back();
    }
    return msg;
}

void compute_module_dir() {
    char buf[MAX_PATH * 2] = {0};
    const DWORD n = GetModuleFileNameA(static_cast<HMODULE>(g_module), buf, sizeof(buf) - 1);
    if (n == 0) return;
    std::string path(buf, n);
    const size_t slash = path.find_last_of("\\/");
    g_dir = (slash == std::string::npos) ? std::string() : path.substr(0, slash + 1);
}

template <typename T>
void resolve(HMODULE lib, const char *name, T &slot) {
    slot = reinterpret_cast<T>(reinterpret_cast<void *>(GetProcAddress(lib, name)));
}

/* File name only: the 79-character error buffer has no room for a path. */
std::string base_name(const std::string &path) {
    const size_t slash = path.find_last_of("\\/");
    return slash == std::string::npos ? path : path.substr(slash + 1);
}

void load_vendor_dll() {
    if (g_cfg.real_dll.empty()) {
        g_real.load_error = "proxy: real_dll is not configured";
        g_real.load_detail =
            "Put real_dll=<path to the vendor DLL> in " +
            (g_cfg.config_source.empty() || g_cfg.config_source.rfind("missing:", 0) == 0
                 ? g_dir + "j2534proxy.ini"
                 : g_cfg.config_source) +
            " or set VOLVO_J2534_REAL_DLL. scripts\\install-proxy.ps1 does this for you.";
        return;
    }

    /* LOAD_WITH_ALTERED_SEARCH_PATH makes the vendor DLL resolve its own
       dependencies from its own directory, which is how it behaves when VIDA
       loads it directly. */
    HMODULE lib = LoadLibraryExA(g_cfg.real_dll.c_str(), nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (!lib) {
        g_real.load_error = "proxy: cannot load " + base_name(g_cfg.real_dll);
        g_real.load_detail = "LoadLibraryEx(" + g_cfg.real_dll + ") failed: " + last_win_error() +
                             ". Check the path and that its bitness matches this proxy.";
        return;
    }

    resolve(lib, "PassThruOpen", g_real.Open);
    resolve(lib, "PassThruClose", g_real.Close);
    resolve(lib, "PassThruConnect", g_real.Connect);
    resolve(lib, "PassThruDisconnect", g_real.Disconnect);
    resolve(lib, "PassThruReadMsgs", g_real.ReadMsgs);
    resolve(lib, "PassThruWriteMsgs", g_real.WriteMsgs);
    resolve(lib, "PassThruStartPeriodicMsg", g_real.StartPeriodicMsg);
    resolve(lib, "PassThruStopPeriodicMsg", g_real.StopPeriodicMsg);
    resolve(lib, "PassThruStartMsgFilter", g_real.StartMsgFilter);
    resolve(lib, "PassThruStopMsgFilter", g_real.StopMsgFilter);
    resolve(lib, "PassThruSetProgrammingVoltage", g_real.SetProgrammingVoltage);
    resolve(lib, "PassThruReadVersion", g_real.ReadVersion);
    resolve(lib, "PassThruGetLastError", g_real.GetLastError);
    resolve(lib, "PassThruIoctl", g_real.Ioctl);

    g_real.loaded = g_real.Open != nullptr;
    if (!g_real.loaded) {
        g_real.load_error = "proxy: " + base_name(g_cfg.real_dll) + " has no PassThruOpen";
        g_real.load_detail = g_cfg.real_dll +
                             " loaded but exports no PassThruOpen: wrong file, or a J2534 DLL of "
                             "the other bitness. Run scripts\\list-j2534.ps1.";
        return;
    }

    logger::Rec("driver_loaded")
        .s("dll", g_cfg.real_dll)
        .b("has_periodic", g_real.StartPeriodicMsg != nullptr)
        .b("has_prog_voltage", g_real.SetProgrammingVoltage != nullptr)
        .b("has_ioctl", g_real.Ioctl != nullptr)
        .b("has_read_version", g_real.ReadVersion != nullptr);
}
#else
/* The POSIX build exists so the proxy, the fake driver and the log pipeline
   can be exercised on Linux. It is not used with VIDA. */
void compute_module_dir() {
    Dl_info info{};
    if (dladdr(reinterpret_cast<void *>(&compute_module_dir), &info) && info.dli_fname) {
        std::string path(info.dli_fname);
        const size_t slash = path.find_last_of('/');
        g_dir = (slash == std::string::npos) ? std::string() : path.substr(0, slash + 1);
    }
}

template <typename T>
void resolve(void *lib, const char *name, T &slot) {
    slot = reinterpret_cast<T>(dlsym(lib, name));
}

void load_vendor_dll() {
    if (g_cfg.real_dll.empty()) {
        g_real.load_error = "proxy: real_dll is not configured";
        return;
    }
    void *lib = dlopen(g_cfg.real_dll.c_str(), RTLD_NOW | RTLD_LOCAL);
    if (!lib) {
        const char *err = dlerror();
        g_real.load_error = "proxy: cannot load " + g_cfg.real_dll;
        g_real.load_detail = err ? err : "unknown dlopen failure";
        return;
    }

    resolve(lib, "PassThruOpen", g_real.Open);
    resolve(lib, "PassThruClose", g_real.Close);
    resolve(lib, "PassThruConnect", g_real.Connect);
    resolve(lib, "PassThruDisconnect", g_real.Disconnect);
    resolve(lib, "PassThruReadMsgs", g_real.ReadMsgs);
    resolve(lib, "PassThruWriteMsgs", g_real.WriteMsgs);
    resolve(lib, "PassThruStartPeriodicMsg", g_real.StartPeriodicMsg);
    resolve(lib, "PassThruStopPeriodicMsg", g_real.StopPeriodicMsg);
    resolve(lib, "PassThruStartMsgFilter", g_real.StartMsgFilter);
    resolve(lib, "PassThruStopMsgFilter", g_real.StopMsgFilter);
    resolve(lib, "PassThruSetProgrammingVoltage", g_real.SetProgrammingVoltage);
    resolve(lib, "PassThruReadVersion", g_real.ReadVersion);
    resolve(lib, "PassThruGetLastError", g_real.GetLastError);
    resolve(lib, "PassThruIoctl", g_real.Ioctl);

    g_real.loaded = g_real.Open != nullptr;
    if (!g_real.loaded) {
        g_real.load_error = "proxy: " + g_cfg.real_dll + " has no PassThruOpen";
        return;
    }
    logger::Rec("driver_loaded").s("dll", g_cfg.real_dll);
}
#endif

void init_once() {
    compute_module_dir();
    g_cfg = load_config(g_dir);
    logger::start(g_cfg);
    load_vendor_dll();
    if (!g_real.loaded) {
        logger::Rec("driver_load_failed")
            .s("error", g_real.load_error)
            .s("detail", g_real.load_detail)
            .s("real_dll", g_cfg.real_dll);
        g_proxy_error = g_real.load_error;
    }
}

}  // namespace

void set_module_handle(void *handle) { g_module = handle; }

void ensure_init() { std::call_once(g_once, init_once); }

const RealDriver &real() {
    ensure_init();
    return g_real;
}

const std::string &module_dir() { return g_dir; }

void set_proxy_error(const std::string &msg) { g_proxy_error = msg; }
const std::string &proxy_error() { return g_proxy_error; }
bool have_proxy_error() { return !g_proxy_error.empty(); }
void clear_proxy_error() { g_proxy_error.clear(); }

/* The configuration is only needed by the exports for the data cap. */
const Config &config() {
    ensure_init();
    return g_cfg;
}

#ifndef _WIN32
/* DllMain's counterpart for the POSIX test build. */
__attribute__((destructor)) static void on_unload() { logger::stop(); }
#endif

}  // namespace proxy
