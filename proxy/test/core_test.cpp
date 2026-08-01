/* Checks for the platform independent half of the proxy.
   CHECK is used instead of the standard assert() so the test also runs in a
   NDEBUG build. */
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "config.h"
#include "j2534.h"
#include "logger.h"

using namespace proxy;

static int g_failures = 0;

#define CHECK(expr)                                                                   \
    do {                                                                              \
        if (!(expr)) {                                                                \
            std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #expr);               \
            ++g_failures;                                                             \
        }                                                                             \
    } while (0)

static void test_ini() {
    Config cfg;
    const char *text =
        "[proxy]\n"
        "# a comment\n"
        "real_dll = C:\\Program Files\\VXDIAG\\vxdiag.dll  ; trailing comment\n"
        "log_dir=\"D:/logs\"\n"
        "log_empty_reads = yes\n"
        "max_data_bytes = 64\n"
        "flush_each=0\n"
        "nonsense line without equals\n"
        "unknown_key = 5\n";
    const int applied = parse_ini(text, cfg);
    CHECK(applied == 5);
    CHECK(cfg.real_dll == "C:\\Program Files\\VXDIAG\\vxdiag.dll");
    CHECK(cfg.log_dir == "D:/logs");
    CHECK(cfg.log_empty_reads == true);
    CHECK(cfg.max_data_bytes == 64);
    CHECK(cfg.flush_each == false);
    CHECK(cfg.logging == true);  /* untouched default */
}

static void test_relative_paths_are_anchored_to_the_dll() {
    /* load_config honours the environment, so skip when it is set. */
    if (std::getenv("VOLVO_J2534_LOG_DIR") || std::getenv("VOLVO_J2534_PROXY_CONFIG")) {
        std::puts("  (skipping path test: VOLVO_J2534_* is set in the environment)");
        return;
    }

    /* A relative log_dir must not depend on VIDA's working directory. */
    Config cfg = load_config("C:\\proxy\\");
    CHECK(cfg.log_dir == "C:\\proxy\\logs");

    Config unknown = load_config("");
    CHECK(unknown.log_dir == "logs");
}

static void test_hex_and_escape() {
    const unsigned char data[] = {0x00, 0x00, 0x07, 0xE0, 0x22, 0xF1, 0x90};
    CHECK(logger::hex(data, sizeof(data), 0) == "000007E022F190");
    CHECK(logger::hex(data, sizeof(data), 4) == "000007E0");
    CHECK(logger::json_escape("a\"b\\c\nd") == "a\\\"b\\\\c\\nd");
    CHECK(logger::json_escape("\x01") == "\\u0001");
}

static void test_msg_json() {
    PASSTHRU_MSG m{};
    m.ProtocolID = ISO15765;
    m.TxFlags = ISO15765_FRAME_PAD;
    m.Timestamp = 123456;
    m.DataSize = 7;
    const unsigned char data[] = {0x00, 0x00, 0x07, 0xE0, 0x22, 0xF1, 0x90};
    std::memcpy(m.Data, data, sizeof(data));

    const std::string j = logger::msg_json(&m, 0);
    CHECK(j.find("\"proto\":6") != std::string::npos);
    CHECK(j.find("\"tx_flags\":64") != std::string::npos);
    CHECK(j.find("\"data\":\"000007E022F190\"") != std::string::npos);
    CHECK(logger::msg_json(nullptr, 0) == "null");

    const std::string arr = logger::msgs_json(&m, 1, 0);
    CHECK(arr.front() == '[' && arr.back() == ']');
}

static void test_ioctl_json() {
    SCONFIG params[2] = {{DATA_RATE, 500000}, {ISO15765_STMIN, 20}};
    SCONFIG_LIST list{2, params};
    const std::string j = logger::ioctl_input_json(SET_CONFIG, &list, 0);
    CHECK(j.find("\"name\":\"DATA_RATE\"") != std::string::npos);
    CHECK(j.find("\"value\":500000") != std::string::npos);
    CHECK(j.find("\"name\":\"ISO15765_STMIN\"") != std::string::npos);

    unsigned long millivolts = 14210;
    CHECK(logger::ioctl_output_json(READ_VBATT, &millivolts, 0) == "{\"millivolts\":14210}");
    CHECK(logger::ioctl_input_json(CLEAR_RX_BUFFER, nullptr, 0) == "null");
}

static void test_names() {
    CHECK(std::strcmp(logger::protocol_name(ISO15765), "ISO15765") == 0);
    CHECK(std::strcmp(logger::error_name(ERR_BUFFER_EMPTY), "ERR_BUFFER_EMPTY") == 0);
    CHECK(std::strcmp(logger::error_name(0x1234), "VENDOR_SPECIFIC") == 0);
    CHECK(std::strcmp(logger::filter_name(FLOW_CONTROL_FILTER), "FLOW_CONTROL") == 0);
}

int main() {
    test_ini();
    test_relative_paths_are_anchored_to_the_dll();
    test_hex_and_escape();
    test_msg_json();
    test_ioctl_json();
    test_names();
    if (g_failures) {
        std::printf("proxy_core: %d check(s) failed\n", g_failures);
        return 1;
    }
    std::puts("proxy_core: all checks passed");
    return 0;
}
