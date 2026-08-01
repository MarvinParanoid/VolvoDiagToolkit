#include "logger.h"

#include <chrono>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <functional>
#include <mutex>
#include <string>

#ifndef VOLVO_PROXY_VERSION
#  define VOLVO_PROXY_VERSION "dev"
#endif

#ifdef _WIN32
#  include <windows.h>
#  include <direct.h>
#  include <process.h>
#else
#  include <sys/stat.h>
#  include <sys/types.h>
#  include <unistd.h>
#  include <thread>
#endif

namespace proxy {
namespace logger {
namespace {

std::mutex g_mu;
FILE *g_file = nullptr;
std::string g_path;
bool g_flush = true;
unsigned long g_seq = 0;
long long g_t0 = 0;

unsigned long current_tid() {
#ifdef _WIN32
    return static_cast<unsigned long>(GetCurrentThreadId());
#else
    return static_cast<unsigned long>(
        std::hash<std::thread::id>{}(std::this_thread::get_id()) & 0xFFFFFFFFu);
#endif
}

unsigned long current_pid() {
#ifdef _WIN32
    return static_cast<unsigned long>(GetCurrentProcessId());
#else
    return static_cast<unsigned long>(getpid());
#endif
}

void mkdir_one(const std::string &p) {
#ifdef _WIN32
    _mkdir(p.c_str());
#else
    ::mkdir(p.c_str(), 0777);
#endif
}

void mkdir_p(const std::string &path) {
    std::string acc;
    for (size_t i = 0; i < path.size(); ++i) {
        const char c = path[i];
        acc.push_back(c);
        if ((c == '/' || c == '\\') && acc.size() > 1) mkdir_one(acc.substr(0, acc.size() - 1));
    }
    if (!acc.empty()) mkdir_one(acc);
}

std::string timestamp_for_name() {
    const std::time_t now = std::time(nullptr);
    std::tm tmv{};
#ifdef _WIN32
    localtime_s(&tmv, &now);
#else
    localtime_r(&now, &tmv);
#endif
    char buf[32];
    std::strftime(buf, sizeof(buf), "%Y%m%d-%H%M%S", &tmv);
    return buf;
}

void append_u(std::string &out, unsigned long long v) {
    char b[24];
    std::snprintf(b, sizeof(b), "%llu", v);
    out += b;
}

}  // namespace

long long now_micros() {
    return std::chrono::duration_cast<std::chrono::microseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

bool active() { return g_file != nullptr; }
const std::string &path() { return g_path; }

void start(const Config &cfg) {
    std::lock_guard<std::mutex> lock(g_mu);
    if (g_file || !cfg.logging) return;

    mkdir_p(cfg.log_dir);

    std::string name = cfg.log_dir;
    if (!name.empty() && name.back() != '/' && name.back() != '\\') name += "/";
    name += "j2534-" + timestamp_for_name() + "-";
    append_u(name, current_pid());
    if (!cfg.session_tag.empty()) name += "-" + cfg.session_tag;
    name += ".jsonl";

    g_file = std::fopen(name.c_str(), "ab");
    if (!g_file) return;
    g_path = name;
    g_flush = cfg.flush_each;
    g_t0 = now_micros();

    std::string hdr = "{\"ev\":\"session\",\"t\":";
    append_u(hdr, static_cast<unsigned long long>(g_t0 / 1000));
    hdr += ",\"pid\":";
    append_u(hdr, current_pid());
    hdr += ",\"proxy_version\":\"" + std::string(VOLVO_PROXY_VERSION) + "\"";
    hdr += ",\"bits\":";
    append_u(hdr, sizeof(void *) * 8);
    hdr += ",\"real_dll\":\"" + json_escape(cfg.real_dll) + "\"";
    hdr += ",\"config\":\"" + json_escape(cfg.config_source) + "\"";
    hdr += ",\"tag\":\"" + json_escape(cfg.session_tag) + "\"}\n";
    std::fwrite(hdr.data(), 1, hdr.size(), g_file);
    std::fflush(g_file);
}

void stop() {
    std::lock_guard<std::mutex> lock(g_mu);
    if (!g_file) return;
    const char *tail = "{\"ev\":\"session_end\"}\n";
    std::fwrite(tail, 1, std::strlen(tail), g_file);
    std::fclose(g_file);
    g_file = nullptr;
}

/* ---- Rec --------------------------------------------------------------- */

Rec::Rec(const char *ev) : on_(g_file != nullptr) {
    if (!on_) return;
    buf_.reserve(256);
    buf_ = "{\"ev\":\"";
    buf_ += ev;
    buf_ += "\",\"t\":";
    const long long t = now_micros();
    append_u(buf_, static_cast<unsigned long long>(t / 1000));
    buf_ += ",\"mono\":";
    append_u(buf_, static_cast<unsigned long long>(t - g_t0));
    buf_ += ",\"tid\":";
    append_u(buf_, current_tid());
}

Rec::~Rec() {
    if (!on_) return;
    buf_ += "}\n";
    std::lock_guard<std::mutex> lock(g_mu);
    if (!g_file) return;
    buf_.insert(buf_.size() - 2, ",\"n\":" + std::to_string(++g_seq));
    std::fwrite(buf_.data(), 1, buf_.size(), g_file);
    if (g_flush) std::fflush(g_file);
}

Rec &Rec::u(const char *key, unsigned long v) {
    if (!on_) return *this;
    buf_ += ",\"";
    buf_ += key;
    buf_ += "\":";
    append_u(buf_, v);
    return *this;
}

Rec &Rec::x(const char *key, unsigned long v) {
    if (!on_) return *this;
    char b[24];
    std::snprintf(b, sizeof(b), "\"0x%lX\"", v);
    buf_ += ",\"";
    buf_ += key;
    buf_ += "\":";
    buf_ += b;
    return *this;
}

Rec &Rec::i(const char *key, long v) {
    if (!on_) return *this;
    buf_ += ",\"";
    buf_ += key;
    buf_ += "\":";
    buf_ += std::to_string(v);
    return *this;
}

Rec &Rec::b(const char *key, bool v) {
    if (!on_) return *this;
    buf_ += ",\"";
    buf_ += key;
    buf_ += "\":";
    buf_ += v ? "true" : "false";
    return *this;
}

Rec &Rec::s(const char *key, const std::string &v) {
    if (!on_) return *this;
    buf_ += ",\"";
    buf_ += key;
    buf_ += "\":\"";
    buf_ += json_escape(v);
    buf_ += "\"";
    return *this;
}

Rec &Rec::json(const char *key, const std::string &raw) {
    if (!on_) return *this;
    buf_ += ",\"";
    buf_ += key;
    buf_ += "\":";
    buf_ += raw.empty() ? "null" : raw;
    return *this;
}

Rec &Rec::ret(long code) {
    if (!on_) return *this;
    i("result", code);
    return s("result_name", error_name(code));
}

Rec &Rec::took(long long micros) {
    if (!on_) return *this;
    buf_ += ",\"us\":";
    buf_ += std::to_string(micros);
    return *this;
}

/* ---- formatting -------------------------------------------------------- */

std::string json_escape(const std::string &s) {
    std::string out;
    out.reserve(s.size() + 8);
    for (unsigned char c : s) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    char b[8];
                    std::snprintf(b, sizeof(b), "\\u%04X", c);
                    out += b;
                } else {
                    out.push_back(static_cast<char>(c));
                }
        }
    }
    return out;
}

std::string hex(const unsigned char *data, size_t len, unsigned long max_bytes) {
    static const char *digits = "0123456789ABCDEF";
    if (!data) return {};
    const size_t n = (max_bytes && len > max_bytes) ? max_bytes : len;
    std::string out;
    out.reserve(n * 2);
    for (size_t i = 0; i < n; ++i) {
        out.push_back(digits[data[i] >> 4]);
        out.push_back(digits[data[i] & 0x0F]);
    }
    return out;
}

std::string msg_json(const PASSTHRU_MSG *m, unsigned long max_data) {
    if (!m) return "null";
    const unsigned long size = m->DataSize > J2534_MSG_DATA_SIZE ? J2534_MSG_DATA_SIZE : m->DataSize;
    std::string out = "{\"proto\":";
    append_u(out, m->ProtocolID);
    out += ",\"rx_status\":";
    append_u(out, m->RxStatus);
    out += ",\"tx_flags\":";
    append_u(out, m->TxFlags);
    out += ",\"ts\":";
    append_u(out, m->Timestamp);
    out += ",\"len\":";
    append_u(out, m->DataSize);
    out += ",\"extra\":";
    append_u(out, m->ExtraDataIndex);
    out += ",\"data\":\"" + hex(m->Data, size, max_data) + "\"";
    if (max_data && size > max_data) out += ",\"truncated\":true";
    out += "}";
    return out;
}

std::string msgs_json(const PASSTHRU_MSG *m, unsigned long count, unsigned long max_data) {
    if (!m) return "null";
    std::string out = "[";
    for (unsigned long i = 0; i < count; ++i) {
        if (i) out += ",";
        out += msg_json(&m[i], max_data);
    }
    out += "]";
    return out;
}

std::string ioctl_input_json(unsigned long ioctl_id, const void *input, unsigned long max_data) {
    if (!input) return "null";
    switch (ioctl_id) {
        case GET_CONFIG:
        case SET_CONFIG: {
            const SCONFIG_LIST *list = static_cast<const SCONFIG_LIST *>(input);
            if (!list->ConfigPtr) return "null";
            std::string out = "[";
            for (unsigned long i = 0; i < list->NumOfParams && i < 128; ++i) {
                if (i) out += ",";
                out += "{\"param\":";
                append_u(out, list->ConfigPtr[i].Parameter);
                out += ",\"name\":\"";
                out += config_param_name(list->ConfigPtr[i].Parameter);
                out += "\",\"value\":";
                append_u(out, list->ConfigPtr[i].Value);
                out += "}";
            }
            out += "]";
            return out;
        }
        case FIVE_BAUD_INIT:
        case ADD_TO_FUNCT_MSG_LOOKUP_TABLE:
        case DELETE_FROM_FUNCT_MSG_LOOKUP_TABLE: {
            const SBYTE_ARRAY *arr = static_cast<const SBYTE_ARRAY *>(input);
            if (!arr->BytePtr) return "null";
            return "{\"bytes\":\"" + hex(arr->BytePtr, arr->NumOfBytes, max_data) + "\"}";
        }
        case FAST_INIT:
            return msg_json(static_cast<const PASSTHRU_MSG *>(input), max_data);
        default:
            return "null";
    }
}

std::string ioctl_output_json(unsigned long ioctl_id, const void *output, unsigned long max_data) {
    if (!output) return "null";
    switch (ioctl_id) {
        case READ_VBATT:
        case READ_PROG_VOLTAGE: {
            std::string out = "{\"millivolts\":";
            append_u(out, *static_cast<const unsigned long *>(output));
            out += "}";
            return out;
        }
        case GET_CONFIG:
            return ioctl_input_json(GET_CONFIG, output, max_data);
        case FIVE_BAUD_INIT: {
            const SBYTE_ARRAY *arr = static_cast<const SBYTE_ARRAY *>(output);
            if (!arr->BytePtr) return "null";
            return "{\"bytes\":\"" + hex(arr->BytePtr, arr->NumOfBytes, max_data) + "\"}";
        }
        case FAST_INIT:
            return msg_json(static_cast<const PASSTHRU_MSG *>(output), max_data);
        default:
            return "null";
    }
}

const char *ioctl_name(unsigned long id) {
    switch (id) {
        case GET_CONFIG: return "GET_CONFIG";
        case SET_CONFIG: return "SET_CONFIG";
        case READ_VBATT: return "READ_VBATT";
        case FIVE_BAUD_INIT: return "FIVE_BAUD_INIT";
        case FAST_INIT: return "FAST_INIT";
        case CLEAR_TX_BUFFER: return "CLEAR_TX_BUFFER";
        case CLEAR_RX_BUFFER: return "CLEAR_RX_BUFFER";
        case CLEAR_PERIODIC_MSGS: return "CLEAR_PERIODIC_MSGS";
        case CLEAR_MSG_FILTERS: return "CLEAR_MSG_FILTERS";
        case CLEAR_FUNCT_MSG_LOOKUP_TABLE: return "CLEAR_FUNCT_MSG_LOOKUP_TABLE";
        case ADD_TO_FUNCT_MSG_LOOKUP_TABLE: return "ADD_TO_FUNCT_MSG_LOOKUP_TABLE";
        case DELETE_FROM_FUNCT_MSG_LOOKUP_TABLE: return "DELETE_FROM_FUNCT_MSG_LOOKUP_TABLE";
        case READ_PROG_VOLTAGE: return "READ_PROG_VOLTAGE";
        default: return "UNKNOWN";
    }
}

const char *protocol_name(unsigned long id) {
    switch (id & 0xFF) {
        case J1850VPW: return "J1850VPW";
        case J1850PWM: return "J1850PWM";
        case ISO9141: return "ISO9141";
        case ISO14230: return "ISO14230";
        case CAN: return "CAN";
        case ISO15765: return "ISO15765";
        case SCI_A_ENGINE: return "SCI_A_ENGINE";
        case SCI_A_TRANS: return "SCI_A_TRANS";
        case SCI_B_ENGINE: return "SCI_B_ENGINE";
        case SCI_B_TRANS: return "SCI_B_TRANS";
        default: return "UNKNOWN";
    }
}

const char *filter_name(unsigned long id) {
    switch (id) {
        case PASS_FILTER: return "PASS";
        case BLOCK_FILTER: return "BLOCK";
        case FLOW_CONTROL_FILTER: return "FLOW_CONTROL";
        default: return "UNKNOWN";
    }
}

const char *config_param_name(unsigned long id) {
    switch (id) {
        case DATA_RATE: return "DATA_RATE";
        case LOOPBACK: return "LOOPBACK";
        case NODE_ADDRESS: return "NODE_ADDRESS";
        case NETWORK_LINE: return "NETWORK_LINE";
        case P1_MIN: return "P1_MIN";
        case P1_MAX: return "P1_MAX";
        case P2_MIN: return "P2_MIN";
        case P2_MAX: return "P2_MAX";
        case P3_MIN: return "P3_MIN";
        case P3_MAX: return "P3_MAX";
        case P4_MIN: return "P4_MIN";
        case P4_MAX: return "P4_MAX";
        case W0: return "W0";
        case W1: return "W1";
        case W2: return "W2";
        case W3: return "W3";
        case W4: return "W4";
        case W5: return "W5";
        case TIDLE: return "TIDLE";
        case TINIL: return "TINIL";
        case TWUP: return "TWUP";
        case PARITY: return "PARITY";
        case BIT_SAMPLE_POINT: return "BIT_SAMPLE_POINT";
        case SYNC_JUMP_WIDTH: return "SYNC_JUMP_WIDTH";
        case T1_MAX: return "T1_MAX";
        case T2_MAX: return "T2_MAX";
        case T3_MAX: return "T3_MAX";
        case T4_MAX: return "T4_MAX";
        case T5_MAX: return "T5_MAX";
        case ISO15765_BS: return "ISO15765_BS";
        case ISO15765_STMIN: return "ISO15765_STMIN";
        case DATA_BITS: return "DATA_BITS";
        case FIVE_BAUD_MOD: return "FIVE_BAUD_MOD";
        case BS_TX: return "BS_TX";
        case STMIN_TX: return "STMIN_TX";
        case ISO15765_WFT_MAX: return "ISO15765_WFT_MAX";
        default: return "UNKNOWN";
    }
}

const char *error_name(long code) {
    switch (code) {
        case STATUS_NOERROR: return "STATUS_NOERROR";
        case ERR_NOT_SUPPORTED: return "ERR_NOT_SUPPORTED";
        case ERR_INVALID_CHANNEL_ID: return "ERR_INVALID_CHANNEL_ID";
        case ERR_INVALID_PROTOCOL_ID: return "ERR_INVALID_PROTOCOL_ID";
        case ERR_NULL_PARAMETER: return "ERR_NULL_PARAMETER";
        case ERR_INVALID_IOCTL_VALUE: return "ERR_INVALID_IOCTL_VALUE";
        case ERR_INVALID_FLAGS: return "ERR_INVALID_FLAGS";
        case ERR_FAILED: return "ERR_FAILED";
        case ERR_DEVICE_NOT_CONNECTED: return "ERR_DEVICE_NOT_CONNECTED";
        case ERR_TIMEOUT: return "ERR_TIMEOUT";
        case ERR_INVALID_MSG: return "ERR_INVALID_MSG";
        case ERR_INVALID_TIME_INTERVAL: return "ERR_INVALID_TIME_INTERVAL";
        case ERR_EXCEEDED_LIMIT: return "ERR_EXCEEDED_LIMIT";
        case ERR_INVALID_MSG_ID: return "ERR_INVALID_MSG_ID";
        case ERR_DEVICE_IN_USE: return "ERR_DEVICE_IN_USE";
        case ERR_INVALID_IOCTL_ID: return "ERR_INVALID_IOCTL_ID";
        case ERR_BUFFER_EMPTY: return "ERR_BUFFER_EMPTY";
        case ERR_BUFFER_FULL: return "ERR_BUFFER_FULL";
        case ERR_BUFFER_OVERFLOW: return "ERR_BUFFER_OVERFLOW";
        case ERR_PIN_INVALID: return "ERR_PIN_INVALID";
        case ERR_CHANNEL_IN_USE: return "ERR_CHANNEL_IN_USE";
        case ERR_MSG_PROTOCOL_ID: return "ERR_MSG_PROTOCOL_ID";
        case ERR_INVALID_FILTER_ID: return "ERR_INVALID_FILTER_ID";
        case ERR_NO_FLOW_CONTROL: return "ERR_NO_FLOW_CONTROL";
        case ERR_NOT_UNIQUE: return "ERR_NOT_UNIQUE";
        case ERR_INVALID_BAUDRATE: return "ERR_INVALID_BAUDRATE";
        case ERR_INVALID_DEVICE_ID: return "ERR_INVALID_DEVICE_ID";
        default: return "VENDOR_SPECIFIC";
    }
}

}  // namespace logger
}  // namespace proxy
