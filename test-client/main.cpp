/*
 * Minimal J2534 application. Point it at any PassThru DLL — the proxy, the
 * genuine VXDIAG driver, or the fake driver — and it performs the same
 * sequence VIDA performs at the start of a session.
 *
 *   j2534-test <dll> [--tx 7E0] [--rx 7E8] [--request 22F190 ...] [--repeat N]
 */
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#ifdef _WIN32
#  include <windows.h>
#else
#  include <dlfcn.h>
#endif

#include "j2534.h"

namespace {

struct Api {
    long(J2534_API *Open)(const void *, unsigned long *) = nullptr;
    long(J2534_API *Close)(unsigned long) = nullptr;
    long(J2534_API *Connect)(unsigned long, unsigned long, unsigned long, unsigned long,
                             unsigned long *) = nullptr;
    long(J2534_API *Disconnect)(unsigned long) = nullptr;
    long(J2534_API *ReadMsgs)(unsigned long, PASSTHRU_MSG *, unsigned long *, unsigned long) = nullptr;
    long(J2534_API *WriteMsgs)(unsigned long, PASSTHRU_MSG *, unsigned long *, unsigned long) = nullptr;
    long(J2534_API *StartMsgFilter)(unsigned long, unsigned long, const PASSTHRU_MSG *,
                                    const PASSTHRU_MSG *, const PASSTHRU_MSG *,
                                    unsigned long *) = nullptr;
    long(J2534_API *ReadVersion)(unsigned long, char *, char *, char *) = nullptr;
    long(J2534_API *GetLastError)(char *) = nullptr;
    long(J2534_API *Ioctl)(unsigned long, unsigned long, const void *, void *) = nullptr;
};

void *g_lib = nullptr;

void *sym(const char *name) {
#ifdef _WIN32
    return reinterpret_cast<void *>(GetProcAddress(static_cast<HMODULE>(g_lib), name));
#else
    return dlsym(g_lib, name);
#endif
}

bool load(const char *path, Api &api) {
#ifdef _WIN32
    g_lib = LoadLibraryExA(path, nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (!g_lib) {
        std::printf("LoadLibrary failed, win32 error %lu\n", GetLastError());
        return false;
    }
#else
    g_lib = dlopen(path, RTLD_NOW);
    if (!g_lib) {
        std::printf("dlopen failed: %s\n", dlerror());
        return false;
    }
#endif
    api.Open = reinterpret_cast<decltype(api.Open)>(sym("PassThruOpen"));
    api.Close = reinterpret_cast<decltype(api.Close)>(sym("PassThruClose"));
    api.Connect = reinterpret_cast<decltype(api.Connect)>(sym("PassThruConnect"));
    api.Disconnect = reinterpret_cast<decltype(api.Disconnect)>(sym("PassThruDisconnect"));
    api.ReadMsgs = reinterpret_cast<decltype(api.ReadMsgs)>(sym("PassThruReadMsgs"));
    api.WriteMsgs = reinterpret_cast<decltype(api.WriteMsgs)>(sym("PassThruWriteMsgs"));
    api.StartMsgFilter = reinterpret_cast<decltype(api.StartMsgFilter)>(sym("PassThruStartMsgFilter"));
    api.ReadVersion = reinterpret_cast<decltype(api.ReadVersion)>(sym("PassThruReadVersion"));
    api.GetLastError = reinterpret_cast<decltype(api.GetLastError)>(sym("PassThruGetLastError"));
    api.Ioctl = reinterpret_cast<decltype(api.Ioctl)>(sym("PassThruIoctl"));

    if (!api.Open) {
        std::printf("PassThruOpen not exported — wrong DLL or wrong bitness\n");
        return false;
    }
    return true;
}

void report(const Api &api, const char *what, long rc) {
    std::printf("%-22s -> %ld", what, rc);
    if (rc != STATUS_NOERROR && api.GetLastError) {
        char err[256] = {0};
        api.GetLastError(err);
        std::printf("  (%s)", err);
    }
    std::printf("\n");
}

std::vector<unsigned char> parse_hex(const std::string &s) {
    std::vector<unsigned char> out;
    unsigned value = 0;
    int nibbles = 0;
    for (char c : s) {
        int digit;
        if (c >= '0' && c <= '9') digit = c - '0';
        else if (c >= 'a' && c <= 'f') digit = c - 'a' + 10;
        else if (c >= 'A' && c <= 'F') digit = c - 'A' + 10;
        else continue;
        value = (value << 4) | static_cast<unsigned>(digit);
        if (++nibbles == 2) {
            out.push_back(static_cast<unsigned char>(value));
            value = 0;
            nibbles = 0;
        }
    }
    return out;
}

void print_msg(const char *prefix, const PASSTHRU_MSG &m) {
    std::printf("%s proto=%lu rx=0x%lX ts=%lu len=%lu  ", prefix, m.ProtocolID, m.RxStatus,
                m.Timestamp, m.DataSize);
    for (unsigned long i = 0; i < m.DataSize && i < 64; ++i) {
        if (i == 4) std::printf("| ");
        std::printf("%02X ", m.Data[i]);
    }
    if (m.DataSize > 64) std::printf("...");
    std::printf("\n");
}

void set_id(PASSTHRU_MSG &m, unsigned long id) {
    m.Data[0] = static_cast<unsigned char>(id >> 24);
    m.Data[1] = static_cast<unsigned char>(id >> 16);
    m.Data[2] = static_cast<unsigned char>(id >> 8);
    m.Data[3] = static_cast<unsigned char>(id);
    m.DataSize = 4;
}

}  // namespace

int main(int argc, char **argv) {
    if (argc < 2) {
        std::printf("usage: %s <path-to-j2534-dll> [--tx 7E0] [--rx 7E8] "
                    "[--request 22F190] [--repeat N]\n", argv[0]);
        return 2;
    }

    const char *dll = argv[1];
    unsigned long tx_id = 0x7E0, rx_id = 0x7E8;
    int repeat = 1;
    std::vector<std::string> requests;

    for (int i = 2; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--tx" && i + 1 < argc) tx_id = std::strtoul(argv[++i], nullptr, 16);
        else if (arg == "--rx" && i + 1 < argc) rx_id = std::strtoul(argv[++i], nullptr, 16);
        else if (arg == "--request" && i + 1 < argc) requests.push_back(argv[++i]);
        else if (arg == "--repeat" && i + 1 < argc) repeat = std::atoi(argv[++i]);
        else std::printf("ignoring unknown argument %s\n", arg.c_str());
    }
    if (requests.empty()) requests = {"22F190", "010C", "3E00"};

    Api api;
    if (!load(dll, api)) return 1;
    std::printf("loaded %s (%zu-bit process)\n\n", dll, sizeof(void *) * 8);

    unsigned long device = 0;
    report(api, "PassThruOpen", api.Open(nullptr, &device));

    if (api.ReadVersion) {
        char fw[128] = {0}, dllv[128] = {0}, apiv[128] = {0};
        const long rc = api.ReadVersion(device, fw, dllv, apiv);
        report(api, "PassThruReadVersion", rc);
        if (rc == STATUS_NOERROR) std::printf("    firmware=%s dll=%s api=%s\n", fw, dllv, apiv);
    }

    if (api.Ioctl) {
        unsigned long millivolts = 0;
        const long rc = api.Ioctl(0, READ_VBATT, nullptr, &millivolts);
        report(api, "Ioctl READ_VBATT", rc);
        if (rc == STATUS_NOERROR) std::printf("    battery = %lu mV\n", millivolts);
    }

    unsigned long channel = 0;
    long rc = api.Connect(device, ISO15765, 0, 500000, &channel);
    report(api, "PassThruConnect", rc);
    if (rc != STATUS_NOERROR) {
        if (api.Close) api.Close(device);
        return 1;
    }

    /* The flow control filter is what makes an ISO15765 channel usable:
       pattern = the ECU's response ID, flow control = our request ID. */
    PASSTHRU_MSG mask{}, pattern{}, flow{};
    mask.ProtocolID = pattern.ProtocolID = flow.ProtocolID = ISO15765;
    mask.TxFlags = pattern.TxFlags = flow.TxFlags = ISO15765_FRAME_PAD;
    set_id(mask, 0xFFFFFFFF);
    set_id(pattern, rx_id);
    set_id(flow, tx_id);
    unsigned long filter = 0;
    report(api, "StartMsgFilter FC", api.StartMsgFilter(channel, FLOW_CONTROL_FILTER, &mask,
                                                        &pattern, &flow, &filter));

    for (int round = 0; round < repeat; ++round) {
        for (const std::string &hex : requests) {
            const std::vector<unsigned char> payload = parse_hex(hex);
            if (payload.empty()) continue;

            PASSTHRU_MSG tx{};
            tx.ProtocolID = ISO15765;
            tx.TxFlags = ISO15765_FRAME_PAD;
            set_id(tx, tx_id);
            std::memcpy(tx.Data + 4, payload.data(), payload.size());
            tx.DataSize = static_cast<unsigned long>(4 + payload.size());

            unsigned long count = 1;
            std::printf("\n");
            print_msg("TX", tx);
            report(api, "PassThruWriteMsgs", api.WriteMsgs(channel, &tx, &count, 1000));

            /* Read until the timeout: an ISO15765 exchange can produce a
               TxDone indication and one or more response frames. */
            for (int attempt = 0; attempt < 4; ++attempt) {
                PASSTHRU_MSG rx[8]{};
                unsigned long n = 8;
                const long read_rc = api.ReadMsgs(channel, rx, &n, 500);
                if (read_rc == ERR_BUFFER_EMPTY) break;
                if (read_rc != STATUS_NOERROR) {
                    report(api, "PassThruReadMsgs", read_rc);
                    break;
                }
                for (unsigned long i = 0; i < n; ++i) print_msg("RX", rx[i]);
                if (n > 0) break;
            }
        }
    }

    std::printf("\n");
    report(api, "PassThruDisconnect", api.Disconnect(channel));
    report(api, "PassThruClose", api.Close(device));
    return 0;
}
