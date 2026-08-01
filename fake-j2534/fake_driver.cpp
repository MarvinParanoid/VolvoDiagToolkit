/*
 * A J2534 device that is not there.
 *
 * It implements enough of the API to exercise the proxy, the log pipeline and
 * the Python client without a car or an adapter: ISO15765 channels, flow
 * control filters, and a small simulated ECM.
 *
 * IMPORTANT: every identifier this ECM answers to below 0xF000 is either a
 * standard OBD-II / UDS one or FABRICATED. The fabricated ones live in the
 * 0xFE00 block and are described in definitions/fake/simulator.yaml. They are
 * not Volvo data and must never be copied into definitions/volvo/.
 */
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstring>
#include <deque>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "j2534.h"

namespace {

constexpr unsigned long kDeviceId = 0x1000;
constexpr const char *kVin = "YV1MW7546A2000001";

struct Channel {
    unsigned long protocol = 0;
    unsigned long flags = 0;
    unsigned long baud = 0;
    unsigned long next_filter = 1;
    std::deque<PASSTHRU_MSG> rx;
};

std::mutex g_mu;
std::map<unsigned long, Channel> g_channels;
unsigned long g_next_channel = 0x2000;
bool g_open = false;
std::string g_last_error;
std::chrono::steady_clock::time_point g_start = std::chrono::steady_clock::now();

double seconds() {
    return std::chrono::duration<double>(std::chrono::steady_clock::now() - g_start).count();
}

unsigned long timestamp_us() {
    return static_cast<unsigned long>(seconds() * 1e6);
}

unsigned long can_id(const PASSTHRU_MSG &m) {
    if (m.DataSize < 4) return 0;
    return (static_cast<unsigned long>(m.Data[0]) << 24) | (m.Data[1] << 16) | (m.Data[2] << 8) |
           m.Data[3];
}

/* A slowly breathing engine so the client shows moving numbers. */
struct Simulated {
    double rpm;
    double boost_kpa;       /* absolute */
    double dpf_dp_kpa;
    double soot_percent;
    double egt_c;
    bool regenerating;
};

Simulated sample() {
    const double t = seconds();
    Simulated s{};
    s.rpm = 820 + 380 * (0.5 + 0.5 * std::sin(t / 7.0));
    s.boost_kpa = 101 + 60 * std::max(0.0, std::sin(t / 11.0));
    s.dpf_dp_kpa = 0.6 + 2.4 * (s.boost_kpa - 101) / 60.0;
    s.soot_percent = std::fmod(t / 3.0, 100.0);
    s.regenerating = s.soot_percent > 80.0;
    s.egt_c = 230 + 320 * (s.boost_kpa - 101) / 60.0 + (s.regenerating ? 250 : 0);
    return s;
}

void push_u16(std::vector<unsigned char> &out, double value, double scale) {
    const long raw = static_cast<long>(value / scale + 0.5);
    out.push_back(static_cast<unsigned char>((raw >> 8) & 0xFF));
    out.push_back(static_cast<unsigned char>(raw & 0xFF));
}

/* Returns the UDS/OBD payload the simulated ECM would answer with. */
std::vector<unsigned char> ecu_reply(const unsigned char *req, size_t len) {
    std::vector<unsigned char> r;
    if (len == 0) return r;
    const Simulated sim = sample();

    auto negative = [&](unsigned char nrc) {
        r = {0x7F, req[0], nrc};
        return r;
    };

    switch (req[0]) {
        case 0x01:  /* OBD-II mode 01 */
            if (len < 2) return negative(0x13);
            switch (req[1]) {
                case 0x00: r = {0x41, 0x00, 0x18, 0x3B, 0x80, 0x11}; return r;
                case 0x0B: r = {0x41, 0x0B, static_cast<unsigned char>(sim.boost_kpa)}; return r;
                case 0x0C: {
                    const unsigned long q = static_cast<unsigned long>(sim.rpm * 4);
                    r = {0x41, 0x0C, static_cast<unsigned char>(q >> 8),
                         static_cast<unsigned char>(q & 0xFF)};
                    return r;
                }
                case 0x0D: r = {0x41, 0x0D, 0x00}; return r;
                case 0x33: r = {0x41, 0x33, 101}; return r;
                default: return negative(0x12);
            }
        case 0x09:  /* OBD-II mode 09 */
            if (len >= 2 && req[1] == 0x02) {
                r = {0x49, 0x02, 0x01};
                r.insert(r.end(), kVin, kVin + std::strlen(kVin));
                return r;
            }
            return negative(0x12);
        case 0x3E:  /* TesterPresent */
            r = {0x7E, len > 1 ? req[1] : static_cast<unsigned char>(0x00)};
            return r;
        case 0x10:  /* DiagnosticSessionControl */
            if (len < 2) return negative(0x13);
            r = {0x50, req[1], 0x00, 0x32, 0x01, 0xF4};
            return r;
        case 0x19:  /* ReadDTCInformation, reportDTCByStatusMask */
            if (len >= 3 && req[1] == 0x02) {
                r = {0x59, 0x02, 0xFF, 0x11, 0x03, 0x00, 0x2F, 0x14, 0x8A, 0x00, 0x2F};
                return r;
            }
            return negative(0x12);
        case 0x22: {  /* ReadDataByIdentifier */
            if (len < 3) return negative(0x13);
            const unsigned did = (req[1] << 8) | req[2];
            r = {0x62, req[1], req[2]};
            switch (did) {
                case 0xF190:
                    r.insert(r.end(), kVin, kVin + std::strlen(kVin));
                    return r;
                case 0xF18C: {
                    const char *sn = "FAKE-ECM-0001";
                    r.insert(r.end(), sn, sn + std::strlen(sn));
                    return r;
                }
                case 0xF194: {
                    const char *sw = "30786112 AA";
                    r.insert(r.end(), sw, sw + std::strlen(sw));
                    return r;
                }
                /* --- fabricated simulator block, see the header comment --- */
                case 0xFE01: push_u16(r, sim.boost_kpa, 0.1); return r;      /* boost actual  */
                case 0xFE02: push_u16(r, sim.boost_kpa + 8, 0.1); return r;  /* boost request */
                case 0xFE03: push_u16(r, sim.dpf_dp_kpa, 0.01); return r;    /* DPF delta p   */
                case 0xFE04: push_u16(r, sim.soot_percent, 0.1); return r;   /* soot load     */
                case 0xFE05: push_u16(r, sim.egt_c + 40, 0.1); return r;     /* EGT           */
                case 0xFE06: r.push_back(sim.regenerating ? 1 : 0); return r;
                case 0xFE07: push_u16(r, std::fmod(seconds() * 3, 600.0), 1.0); return r; /* km */
                default: return negative(0x31);
            }
        }
        default:
            return negative(0x11);
    }
}

void queue_reply(Channel &ch, unsigned long request_id, const unsigned char *payload, size_t len) {
    const std::vector<unsigned char> reply = ecu_reply(payload, len);
    if (reply.empty()) return;

    PASSTHRU_MSG m{};
    m.ProtocolID = ch.protocol;
    m.RxStatus = 0;
    m.Timestamp = timestamp_us();
    /* Physical response address: request id + 8, the usual OBD pairing. */
    const unsigned long rx_id = request_id + 8;
    m.Data[0] = static_cast<unsigned char>(rx_id >> 24);
    m.Data[1] = static_cast<unsigned char>(rx_id >> 16);
    m.Data[2] = static_cast<unsigned char>(rx_id >> 8);
    m.Data[3] = static_cast<unsigned char>(rx_id);
    std::memcpy(m.Data + 4, reply.data(), reply.size());
    m.DataSize = static_cast<unsigned long>(4 + reply.size());
    ch.rx.push_back(m);
}

}  // namespace

extern "C" {

long J2534_API PassThruOpen(const void *, unsigned long *pDeviceID) {
    std::lock_guard<std::mutex> lock(g_mu);
    if (g_open) {
        g_last_error = "fake: device already open";
        return ERR_DEVICE_IN_USE;
    }
    if (!pDeviceID) return ERR_NULL_PARAMETER;
    g_open = true;
    *pDeviceID = kDeviceId;
    return STATUS_NOERROR;
}

long J2534_API PassThruClose(unsigned long DeviceID) {
    std::lock_guard<std::mutex> lock(g_mu);
    if (DeviceID != kDeviceId) return ERR_INVALID_DEVICE_ID;
    g_channels.clear();
    g_open = false;
    return STATUS_NOERROR;
}

long J2534_API PassThruConnect(unsigned long DeviceID, unsigned long ProtocolID,
                               unsigned long Flags, unsigned long BaudRate,
                               unsigned long *pChannelID) {
    std::lock_guard<std::mutex> lock(g_mu);
    if (DeviceID != kDeviceId) return ERR_INVALID_DEVICE_ID;
    if (!pChannelID) return ERR_NULL_PARAMETER;
    if (ProtocolID != ISO15765 && ProtocolID != CAN) return ERR_INVALID_PROTOCOL_ID;

    Channel ch;
    ch.protocol = ProtocolID;
    ch.flags = Flags;
    ch.baud = BaudRate;
    const unsigned long id = ++g_next_channel;
    g_channels[id] = std::move(ch);
    *pChannelID = id;
    return STATUS_NOERROR;
}

long J2534_API PassThruDisconnect(unsigned long ChannelID) {
    std::lock_guard<std::mutex> lock(g_mu);
    return g_channels.erase(ChannelID) ? STATUS_NOERROR : ERR_INVALID_CHANNEL_ID;
}

long J2534_API PassThruReadMsgs(unsigned long ChannelID, PASSTHRU_MSG *pMsg,
                                unsigned long *pNumMsgs, unsigned long Timeout) {
    if (!pMsg || !pNumMsgs) return ERR_NULL_PARAMETER;
    const unsigned long wanted = *pNumMsgs;
    *pNumMsgs = 0;

    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(Timeout);
    for (;;) {
        {
            std::lock_guard<std::mutex> lock(g_mu);
            auto it = g_channels.find(ChannelID);
            if (it == g_channels.end()) return ERR_INVALID_CHANNEL_ID;
            while (*pNumMsgs < wanted && !it->second.rx.empty()) {
                pMsg[(*pNumMsgs)++] = it->second.rx.front();
                it->second.rx.pop_front();
            }
        }
        if (*pNumMsgs > 0) return STATUS_NOERROR;
        if (std::chrono::steady_clock::now() >= deadline) return ERR_BUFFER_EMPTY;
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
}

long J2534_API PassThruWriteMsgs(unsigned long ChannelID, PASSTHRU_MSG *pMsg,
                                 unsigned long *pNumMsgs, unsigned long) {
    if (!pMsg || !pNumMsgs) return ERR_NULL_PARAMETER;
    std::lock_guard<std::mutex> lock(g_mu);
    auto it = g_channels.find(ChannelID);
    if (it == g_channels.end()) return ERR_INVALID_CHANNEL_ID;

    for (unsigned long i = 0; i < *pNumMsgs; ++i) {
        const PASSTHRU_MSG &m = pMsg[i];
        if (m.DataSize < 5) return ERR_INVALID_MSG;
        queue_reply(it->second, can_id(m), m.Data + 4, m.DataSize - 4);
    }
    return STATUS_NOERROR;
}

long J2534_API PassThruStartPeriodicMsg(unsigned long ChannelID, const PASSTHRU_MSG *,
                                        unsigned long *pMsgID, unsigned long) {
    std::lock_guard<std::mutex> lock(g_mu);
    if (!g_channels.count(ChannelID)) return ERR_INVALID_CHANNEL_ID;
    if (!pMsgID) return ERR_NULL_PARAMETER;
    *pMsgID = 1;
    return STATUS_NOERROR;
}

long J2534_API PassThruStopPeriodicMsg(unsigned long ChannelID, unsigned long) {
    std::lock_guard<std::mutex> lock(g_mu);
    return g_channels.count(ChannelID) ? STATUS_NOERROR : ERR_INVALID_CHANNEL_ID;
}

long J2534_API PassThruStartMsgFilter(unsigned long ChannelID, unsigned long FilterType,
                                      const PASSTHRU_MSG *, const PASSTHRU_MSG *,
                                      const PASSTHRU_MSG *pFlowControlMsg,
                                      unsigned long *pFilterID) {
    std::lock_guard<std::mutex> lock(g_mu);
    auto it = g_channels.find(ChannelID);
    if (it == g_channels.end()) return ERR_INVALID_CHANNEL_ID;
    if (!pFilterID) return ERR_NULL_PARAMETER;
    if (FilterType == FLOW_CONTROL_FILTER && !pFlowControlMsg) return ERR_NULL_PARAMETER;
    *pFilterID = it->second.next_filter++;
    return STATUS_NOERROR;
}

long J2534_API PassThruStopMsgFilter(unsigned long ChannelID, unsigned long) {
    std::lock_guard<std::mutex> lock(g_mu);
    return g_channels.count(ChannelID) ? STATUS_NOERROR : ERR_INVALID_CHANNEL_ID;
}

long J2534_API PassThruSetProgrammingVoltage(unsigned long, unsigned long, unsigned long) {
    return ERR_NOT_SUPPORTED;
}

long J2534_API PassThruReadVersion(unsigned long DeviceID, char *pFirmwareVersion,
                                   char *pDllVersion, char *pApiVersion) {
    if (DeviceID != kDeviceId) return ERR_INVALID_DEVICE_ID;
    if (!pFirmwareVersion || !pDllVersion || !pApiVersion) return ERR_NULL_PARAMETER;
    std::strcpy(pFirmwareVersion, "FAKE-FW 1.00");
    std::strcpy(pDllVersion, "fake-j2534 0.1.0");
    std::strcpy(pApiVersion, "04.04");
    return STATUS_NOERROR;
}

long J2534_API PassThruGetLastError(char *pErrorDescription) {
    if (!pErrorDescription) return ERR_NULL_PARAMETER;
    std::lock_guard<std::mutex> lock(g_mu);
    std::strncpy(pErrorDescription, g_last_error.c_str(), 79);
    pErrorDescription[79] = '\0';
    return STATUS_NOERROR;
}

long J2534_API PassThruIoctl(unsigned long ChannelID, unsigned long IoctlID, const void *pInput,
                             void *pOutput) {
    switch (IoctlID) {
        case READ_VBATT:
            if (!pOutput) return ERR_NULL_PARAMETER;
            *static_cast<unsigned long *>(pOutput) = 14210;
            return STATUS_NOERROR;
        case READ_PROG_VOLTAGE:
            if (!pOutput) return ERR_NULL_PARAMETER;
            *static_cast<unsigned long *>(pOutput) = 0;
            return STATUS_NOERROR;
        case SET_CONFIG:
            return pInput ? STATUS_NOERROR : ERR_NULL_PARAMETER;
        case GET_CONFIG: {
            if (!pOutput) return ERR_NULL_PARAMETER;
            SCONFIG_LIST *list = static_cast<SCONFIG_LIST *>(pOutput);
            for (unsigned long i = 0; i < list->NumOfParams && list->ConfigPtr; ++i) {
                if (list->ConfigPtr[i].Parameter == DATA_RATE) list->ConfigPtr[i].Value = 500000;
            }
            return STATUS_NOERROR;
        }
        case CLEAR_TX_BUFFER:
        case CLEAR_MSG_FILTERS:
        case CLEAR_PERIODIC_MSGS:
            return STATUS_NOERROR;
        case CLEAR_RX_BUFFER: {
            std::lock_guard<std::mutex> lock(g_mu);
            auto it = g_channels.find(ChannelID);
            if (it == g_channels.end()) return ERR_INVALID_CHANNEL_ID;
            it->second.rx.clear();
            return STATUS_NOERROR;
        }
        default:
            return ERR_INVALID_IOCTL_ID;
    }
}

}  // extern "C"
