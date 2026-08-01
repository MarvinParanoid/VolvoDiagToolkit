#pragma once
#include <string>

namespace proxy {

struct Config {
    /* Absolute path to the genuine vendor J2534 DLL to forward to. */
    std::string real_dll;
    /* Directory that receives the JSONL logs. */
    std::string log_dir;
    /* Optional tag placed in every log record and in the log file name. */
    std::string session_tag;

    bool logging = true;
    /* Truncate PASSTHRU_MSG.Data in the log after this many bytes (0 = no cap). */
    unsigned long max_data_bytes = 0;
    /* fflush after every record. Slower, but survives a VIDA crash. */
    bool flush_each = true;
    /* Log ReadMsgs calls that returned no messages. VIDA polls hard; this is
       ~95% of all calls and hides the interesting traffic. */
    bool log_empty_reads = false;
    /* Log GetLastError calls. Also very noisy. */
    bool log_get_last_error = false;

    std::string config_source;  /* where the settings came from, for the log header */
};

/* Parses "key = value" lines. '#' and ';' start a comment, [sections] are
   ignored, unknown keys are ignored. Returns the number of keys applied. */
int parse_ini(const std::string &text, Config &cfg);

/* Reads <dir>/j2534proxy.ini (or $VOLVO_J2534_PROXY_CONFIG), then applies the
   VOLVO_J2534_* environment overrides. Never throws. */
Config load_config(const std::string &dll_dir);

}  // namespace proxy
