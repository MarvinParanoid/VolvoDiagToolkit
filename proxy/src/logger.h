#pragma once
#include <string>

#include "config.h"
#include "j2534.h"

namespace proxy {
namespace logger {

/* Opens <log_dir>/j2534-<date>-<pid>[-<tag>].jsonl and writes the session
   header record. Safe to call twice; the second call is ignored. */
void start(const Config &cfg);
void stop();
bool active();
const std::string &path();

/* One JSON object per instance, written on destruction.

     Rec("write").u("channel", id).json("msgs", msgs_json(...)).ret(rc);

   Every record carries: t (unix ms), mono (us since start), tid, ev. */
class Rec {
  public:
    explicit Rec(const char *ev);
    ~Rec();
    Rec(const Rec &) = delete;
    Rec &operator=(const Rec &) = delete;

    Rec &u(const char *key, unsigned long v);
    Rec &x(const char *key, unsigned long v);          /* hex string, e.g. "0x7E0" */
    Rec &i(const char *key, long v);
    Rec &b(const char *key, bool v);
    Rec &s(const char *key, const std::string &v);
    Rec &json(const char *key, const std::string &raw);  /* pre-rendered value */
    Rec &ret(long code);                                 /* result + result_name */
    /* Wall-clock microseconds spent inside the real driver. */
    Rec &took(long long micros);

  private:
    bool on_;
    std::string buf_;
};

std::string hex(const unsigned char *data, size_t len, unsigned long max_bytes);
std::string json_escape(const std::string &s);

/* PASSTHRU_MSG -> JSON object / array of objects. */
std::string msg_json(const PASSTHRU_MSG *m, unsigned long max_data);
std::string msgs_json(const PASSTHRU_MSG *m, unsigned long count, unsigned long max_data);

/* Ioctl payloads: SCONFIG_LIST, SBYTE_ARRAY, PASSTHRU_MSG or scalar,
   selected by IoctlID. Returns "null" when there is nothing to show. */
std::string ioctl_input_json(unsigned long ioctl_id, const void *input, unsigned long max_data);
std::string ioctl_output_json(unsigned long ioctl_id, const void *output, unsigned long max_data);

const char *ioctl_name(unsigned long id);
const char *protocol_name(unsigned long id);
const char *filter_name(unsigned long id);
const char *config_param_name(unsigned long id);
const char *error_name(long code);

long long now_micros();

}  // namespace logger
}  // namespace proxy
