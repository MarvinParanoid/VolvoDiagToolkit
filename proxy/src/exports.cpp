/*
 * The J2534 entry points. Every one of them does the same three things:
 * log the call, forward it verbatim to the vendor driver, log the result.
 * Nothing is interpreted, rewritten or filtered — VIDA must not be able to
 * tell the difference.
 */
#include <cstring>
#include <string>

#include "j2534.h"
#include "logger.h"
#include "real_driver.h"

using proxy::config;
using proxy::real;
using proxy::RealDriver;
using proxy::logger::Rec;

namespace {

unsigned long cap() { return config().max_data_bytes; }

/* Used when the vendor DLL could not be loaded or lacks the entry point.
   The message has to survive being copied into a 79-character buffer, so the
   load failure speaks for itself and the function name only appears when
   there is nothing better to say. */
long not_available(const char *fn) {
    const std::string &why = real().load_error;
    proxy::set_proxy_error(why.empty() ? std::string("proxy: vendor DLL has no ") + fn : why);
    Rec("proxy_error").s("fn", fn).s("error", proxy::proxy_error())
        .s("detail", real().load_detail);
    return ERR_DEVICE_NOT_CONNECTED;
}

std::string safe_str(const char *s, size_t limit = 256) {
    if (!s) return {};
    size_t n = 0;
    while (n < limit && s[n]) ++n;
    return std::string(s, n);
}

struct Timer {
    long long t0 = proxy::logger::now_micros();
    long long elapsed() const { return proxy::logger::now_micros() - t0; }
};

}  // namespace

extern "C" {

long J2534_API PassThruOpen(const void *pName, unsigned long *pDeviceID) {
    proxy::ensure_init();
    const RealDriver &d = real();
    if (!d.Open) return not_available("PassThruOpen");

    Timer timer;
    const long rc = d.Open(pName, pDeviceID);
    Rec("open")
        .s("name", safe_str(static_cast<const char *>(pName)))
        .u("device", pDeviceID ? *pDeviceID : 0)
        .took(timer.elapsed())
        .ret(rc);
    return rc;
}

long J2534_API PassThruClose(unsigned long DeviceID) {
    proxy::ensure_init();
    const RealDriver &d = real();
    if (!d.Close) return not_available("PassThruClose");

    Timer timer;
    const long rc = d.Close(DeviceID);
    Rec("close").u("device", DeviceID).took(timer.elapsed()).ret(rc);
    return rc;
}

long J2534_API PassThruConnect(unsigned long DeviceID, unsigned long ProtocolID,
                               unsigned long Flags, unsigned long BaudRate,
                               unsigned long *pChannelID) {
    proxy::ensure_init();
    const RealDriver &d = real();
    if (!d.Connect) return not_available("PassThruConnect");

    Timer timer;
    const long rc = d.Connect(DeviceID, ProtocolID, Flags, BaudRate, pChannelID);
    Rec("connect")
        .u("device", DeviceID)
        .u("protocol", ProtocolID)
        .s("protocol_name", proxy::logger::protocol_name(ProtocolID))
        .x("flags", Flags)
        .u("baud", BaudRate)
        .u("channel", pChannelID ? *pChannelID : 0)
        .took(timer.elapsed())
        .ret(rc);
    return rc;
}

long J2534_API PassThruDisconnect(unsigned long ChannelID) {
    proxy::ensure_init();
    const RealDriver &d = real();
    if (!d.Disconnect) return not_available("PassThruDisconnect");

    Timer timer;
    const long rc = d.Disconnect(ChannelID);
    Rec("disconnect").u("channel", ChannelID).took(timer.elapsed()).ret(rc);
    return rc;
}

long J2534_API PassThruReadMsgs(unsigned long ChannelID, PASSTHRU_MSG *pMsg,
                                unsigned long *pNumMsgs, unsigned long Timeout) {
    proxy::ensure_init();
    const RealDriver &d = real();
    if (!d.ReadMsgs) return not_available("PassThruReadMsgs");

    const unsigned long requested = pNumMsgs ? *pNumMsgs : 0;
    Timer timer;
    const long rc = d.ReadMsgs(ChannelID, pMsg, pNumMsgs, Timeout);
    const unsigned long returned = pNumMsgs ? *pNumMsgs : 0;

    /* VIDA polls continuously; empty reads are the bulk of the traffic. */
    if (returned == 0 && !config().log_empty_reads) return rc;

    Rec("read")
        .u("channel", ChannelID)
        .u("requested", requested)
        .u("returned", returned)
        .u("timeout", Timeout)
        .json("msgs", proxy::logger::msgs_json(pMsg, returned, cap()))
        .took(timer.elapsed())
        .ret(rc);
    return rc;
}

long J2534_API PassThruWriteMsgs(unsigned long ChannelID, PASSTHRU_MSG *pMsg,
                                 unsigned long *pNumMsgs, unsigned long Timeout) {
    proxy::ensure_init();
    const RealDriver &d = real();
    if (!d.WriteMsgs) return not_available("PassThruWriteMsgs");

    const unsigned long requested = pNumMsgs ? *pNumMsgs : 0;
    /* Rendered before the call: the driver is allowed to touch the buffer. */
    const std::string msgs = proxy::logger::msgs_json(pMsg, requested, cap());

    Timer timer;
    const long rc = d.WriteMsgs(ChannelID, pMsg, pNumMsgs, Timeout);
    Rec("write")
        .u("channel", ChannelID)
        .u("requested", requested)
        .u("written", pNumMsgs ? *pNumMsgs : 0)
        .u("timeout", Timeout)
        .json("msgs", msgs)
        .took(timer.elapsed())
        .ret(rc);
    return rc;
}

long J2534_API PassThruStartPeriodicMsg(unsigned long ChannelID, const PASSTHRU_MSG *pMsg,
                                        unsigned long *pMsgID, unsigned long TimeInterval) {
    proxy::ensure_init();
    const RealDriver &d = real();
    if (!d.StartPeriodicMsg) return not_available("PassThruStartPeriodicMsg");

    Timer timer;
    const long rc = d.StartPeriodicMsg(ChannelID, pMsg, pMsgID, TimeInterval);
    Rec("start_periodic")
        .u("channel", ChannelID)
        .u("msg_id", pMsgID ? *pMsgID : 0)
        .u("interval", TimeInterval)
        .json("msg", proxy::logger::msg_json(pMsg, cap()))
        .took(timer.elapsed())
        .ret(rc);
    return rc;
}

long J2534_API PassThruStopPeriodicMsg(unsigned long ChannelID, unsigned long MsgID) {
    proxy::ensure_init();
    const RealDriver &d = real();
    if (!d.StopPeriodicMsg) return not_available("PassThruStopPeriodicMsg");

    Timer timer;
    const long rc = d.StopPeriodicMsg(ChannelID, MsgID);
    Rec("stop_periodic").u("channel", ChannelID).u("msg_id", MsgID).took(timer.elapsed()).ret(rc);
    return rc;
}

long J2534_API PassThruStartMsgFilter(unsigned long ChannelID, unsigned long FilterType,
                                      const PASSTHRU_MSG *pMaskMsg, const PASSTHRU_MSG *pPatternMsg,
                                      const PASSTHRU_MSG *pFlowControlMsg,
                                      unsigned long *pFilterID) {
    proxy::ensure_init();
    const RealDriver &d = real();
    if (!d.StartMsgFilter) return not_available("PassThruStartMsgFilter");

    Timer timer;
    const long rc =
        d.StartMsgFilter(ChannelID, FilterType, pMaskMsg, pPatternMsg, pFlowControlMsg, pFilterID);
    Rec("start_filter")
        .u("channel", ChannelID)
        .u("filter_type", FilterType)
        .s("filter_name", proxy::logger::filter_name(FilterType))
        .u("filter_id", pFilterID ? *pFilterID : 0)
        .json("mask", proxy::logger::msg_json(pMaskMsg, cap()))
        .json("pattern", proxy::logger::msg_json(pPatternMsg, cap()))
        .json("flow_control", proxy::logger::msg_json(pFlowControlMsg, cap()))
        .took(timer.elapsed())
        .ret(rc);
    return rc;
}

long J2534_API PassThruStopMsgFilter(unsigned long ChannelID, unsigned long FilterID) {
    proxy::ensure_init();
    const RealDriver &d = real();
    if (!d.StopMsgFilter) return not_available("PassThruStopMsgFilter");

    Timer timer;
    const long rc = d.StopMsgFilter(ChannelID, FilterID);
    Rec("stop_filter").u("channel", ChannelID).u("filter_id", FilterID).took(timer.elapsed()).ret(rc);
    return rc;
}

long J2534_API PassThruSetProgrammingVoltage(unsigned long DeviceID, unsigned long PinNumber,
                                             unsigned long Voltage) {
    proxy::ensure_init();
    const RealDriver &d = real();
    if (!d.SetProgrammingVoltage) return not_available("PassThruSetProgrammingVoltage");

    Timer timer;
    const long rc = d.SetProgrammingVoltage(DeviceID, PinNumber, Voltage);
    Rec("set_programming_voltage")
        .u("device", DeviceID)
        .u("pin", PinNumber)
        .u("voltage", Voltage)
        .took(timer.elapsed())
        .ret(rc);
    return rc;
}

long J2534_API PassThruReadVersion(unsigned long DeviceID, char *pFirmwareVersion, char *pDllVersion,
                                   char *pApiVersion) {
    proxy::ensure_init();
    const RealDriver &d = real();
    if (!d.ReadVersion) return not_available("PassThruReadVersion");

    Timer timer;
    const long rc = d.ReadVersion(DeviceID, pFirmwareVersion, pDllVersion, pApiVersion);
    Rec("read_version")
        .u("device", DeviceID)
        .s("firmware", safe_str(pFirmwareVersion, 80))
        .s("dll", safe_str(pDllVersion, 80))
        .s("api", safe_str(pApiVersion, 80))
        .took(timer.elapsed())
        .ret(rc);
    return rc;
}

long J2534_API PassThruGetLastError(char *pErrorDescription) {
    proxy::ensure_init();
    const RealDriver &d = real();

    /* When the failure was ours, the vendor driver knows nothing about it. */
    if (proxy::have_proxy_error() || !d.GetLastError) {
        if (!pErrorDescription) return ERR_NULL_PARAMETER;
        const std::string &msg = proxy::proxy_error();
        /* The caller's buffer is 80 bytes per the specification. */
        std::strncpy(pErrorDescription, msg.c_str(), 79);
        pErrorDescription[79] = '\0';
        proxy::clear_proxy_error();
        return STATUS_NOERROR;
    }

    const long rc = d.GetLastError(pErrorDescription);
    if (config().log_get_last_error) {
        Rec("get_last_error").s("text", safe_str(pErrorDescription, 80)).ret(rc);
    }
    return rc;
}

long J2534_API PassThruIoctl(unsigned long ChannelID, unsigned long IoctlID, const void *pInput,
                             void *pOutput) {
    proxy::ensure_init();
    const RealDriver &d = real();
    if (!d.Ioctl) return not_available("PassThruIoctl");

    const std::string input = proxy::logger::ioctl_input_json(IoctlID, pInput, cap());

    Timer timer;
    const long rc = d.Ioctl(ChannelID, IoctlID, pInput, pOutput);
    Rec("ioctl")
        .u("channel", ChannelID)
        .u("ioctl_id", IoctlID)
        .s("ioctl_name", proxy::logger::ioctl_name(IoctlID))
        .json("input", input)
        .json("output", proxy::logger::ioctl_output_json(IoctlID, pOutput, cap()))
        .took(timer.elapsed())
        .ret(rc);
    return rc;
}

}  // extern "C"
