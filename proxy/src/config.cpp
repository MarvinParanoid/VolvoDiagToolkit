#include "config.h"

#include <cstdlib>
#include <fstream>
#include <sstream>

namespace proxy {
namespace {

std::string trim(const std::string &s) {
    const char *ws = " \t\r\n\"";
    const size_t b = s.find_first_not_of(ws);
    if (b == std::string::npos) return {};
    const size_t e = s.find_last_not_of(ws);
    return s.substr(b, e - b + 1);
}

bool to_bool(const std::string &v, bool dflt) {
    if (v.empty()) return dflt;
    switch (v[0]) {
        case '1': case 'y': case 'Y': case 't': case 'T': return true;
        case '0': case 'n': case 'N': case 'f': case 'F': return false;
        default: return dflt;
    }
}

std::string env(const char *name) {
    const char *v = std::getenv(name);
    return v ? std::string(v) : std::string();
}

bool is_absolute(const std::string &path) {
    if (path.empty()) return false;
    if (path[0] == '/' || path[0] == '\\') return true;
    return path.size() > 1 && path[1] == ':';  // C:\...
}

/* Relative paths are resolved against the directory the DLL lives in, not the
   current one: the current directory belongs to VIDA and is not ours to
   predict. */
std::string anchor(const std::string &path, const std::string &dir) {
    if (path.empty() || is_absolute(path) || dir.empty()) return path;
    return dir + path;
}

}  // namespace

int parse_ini(const std::string &text, Config &cfg) {
    std::istringstream in(text);
    std::string line;
    int applied = 0;

    while (std::getline(in, line)) {
        const size_t comment = line.find_first_of("#;");
        if (comment != std::string::npos) line = line.substr(0, comment);
        line = trim(line);
        if (line.empty() || line[0] == '[') continue;

        const size_t eq = line.find('=');
        if (eq == std::string::npos) continue;
        const std::string key = trim(line.substr(0, eq));
        const std::string val = trim(line.substr(eq + 1));

        if (key == "real_dll")                cfg.real_dll = val;
        else if (key == "log_dir")            cfg.log_dir = val;
        else if (key == "session_tag")        cfg.session_tag = val;
        else if (key == "logging")            cfg.logging = to_bool(val, cfg.logging);
        else if (key == "max_data_bytes")     cfg.max_data_bytes = std::strtoul(val.c_str(), nullptr, 10);
        else if (key == "flush_each")         cfg.flush_each = to_bool(val, cfg.flush_each);
        else if (key == "log_empty_reads")    cfg.log_empty_reads = to_bool(val, cfg.log_empty_reads);
        else if (key == "log_get_last_error") cfg.log_get_last_error = to_bool(val, cfg.log_get_last_error);
        else continue;
        ++applied;
    }
    return applied;
}

Config load_config(const std::string &dll_dir) {
    Config cfg;

    std::string path = env("VOLVO_J2534_PROXY_CONFIG");
    if (path.empty() && !dll_dir.empty()) path = dll_dir + "j2534proxy.ini";

    if (!path.empty()) {
        std::ifstream f(path.c_str(), std::ios::binary);
        if (f) {
            std::ostringstream ss;
            ss << f.rdbuf();
            parse_ini(ss.str(), cfg);
            cfg.config_source = path;
        } else {
            cfg.config_source = "missing:" + path;
        }
    }

    /* Environment wins over the file: handy for one-off experiments without
       touching the installed configuration. */
    const std::string real = env("VOLVO_J2534_REAL_DLL");
    if (!real.empty()) cfg.real_dll = real;
    const std::string dir = env("VOLVO_J2534_LOG_DIR");
    if (!dir.empty()) cfg.log_dir = dir;
    const std::string tag = env("VOLVO_J2534_SESSION_TAG");
    if (!tag.empty()) cfg.session_tag = tag;
    const std::string on = env("VOLVO_J2534_LOGGING");
    if (!on.empty()) cfg.logging = to_bool(on, cfg.logging);

    if (cfg.log_dir.empty()) cfg.log_dir = "logs";
    cfg.log_dir = anchor(cfg.log_dir, dll_dir);
    cfg.real_dll = anchor(cfg.real_dll, dll_dir);
    return cfg;
}

}  // namespace proxy
