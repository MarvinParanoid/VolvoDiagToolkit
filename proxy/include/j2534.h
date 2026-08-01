/*
 * SAE J2534-1 (PassThru) API, version 04.04.
 *
 * Kept free of <windows.h> so the non-Windows parts of the proxy (logger,
 * config, hex formatting) can be compiled and unit-tested on Linux.
 */
#ifndef VOLVO_J2534_H
#define VOLVO_J2534_H

#ifdef _WIN32
#  define J2534_API __stdcall
#else
#  define J2534_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* ---- Message ---------------------------------------------------------- */

#define J2534_MSG_DATA_SIZE 4128

typedef struct {
    unsigned long ProtocolID;
    unsigned long RxStatus;
    unsigned long TxFlags;
    unsigned long Timestamp;      /* microseconds */
    unsigned long DataSize;
    unsigned long ExtraDataIndex;
    unsigned char Data[J2534_MSG_DATA_SIZE];
} PASSTHRU_MSG;

typedef struct {
    unsigned long Parameter;
    unsigned long Value;
} SCONFIG;

typedef struct {
    unsigned long NumOfParams;
    SCONFIG *ConfigPtr;
} SCONFIG_LIST;

typedef struct {
    unsigned long NumOfBytes;
    unsigned char *BytePtr;
} SBYTE_ARRAY;

/* ---- Protocol IDs ------------------------------------------------------ */

#define J1850VPW        1
#define J1850PWM        2
#define ISO9141         3
#define ISO14230        4
#define CAN             5
#define ISO15765        6
#define SCI_A_ENGINE    7
#define SCI_A_TRANS     8
#define SCI_B_ENGINE    9
#define SCI_B_TRANS     10

/* ---- Return codes ------------------------------------------------------ */

#define STATUS_NOERROR              0x00
#define ERR_NOT_SUPPORTED           0x01
#define ERR_INVALID_CHANNEL_ID      0x02
#define ERR_INVALID_PROTOCOL_ID     0x03
#define ERR_NULL_PARAMETER          0x04
#define ERR_INVALID_IOCTL_VALUE     0x05
#define ERR_INVALID_FLAGS           0x06
#define ERR_FAILED                  0x07
#define ERR_DEVICE_NOT_CONNECTED    0x08
#define ERR_TIMEOUT                 0x09
#define ERR_INVALID_MSG             0x0A
#define ERR_INVALID_TIME_INTERVAL   0x0B
#define ERR_EXCEEDED_LIMIT          0x0C
#define ERR_INVALID_MSG_ID          0x0D
#define ERR_DEVICE_IN_USE           0x0E
#define ERR_INVALID_IOCTL_ID        0x0F
#define ERR_BUFFER_EMPTY            0x10
#define ERR_BUFFER_FULL             0x11
#define ERR_BUFFER_OVERFLOW         0x12
#define ERR_PIN_INVALID             0x13
#define ERR_CHANNEL_IN_USE          0x14
#define ERR_MSG_PROTOCOL_ID         0x15
#define ERR_INVALID_FILTER_ID       0x16
#define ERR_NO_FLOW_CONTROL         0x17
#define ERR_NOT_UNIQUE              0x18
#define ERR_INVALID_BAUDRATE        0x19
#define ERR_INVALID_DEVICE_ID       0x1A

/* ---- Filter types ------------------------------------------------------ */

#define PASS_FILTER                 0x00000001
#define BLOCK_FILTER                0x00000002
#define FLOW_CONTROL_FILTER         0x00000003

/* ---- Ioctl IDs --------------------------------------------------------- */

#define GET_CONFIG                          0x01
#define SET_CONFIG                          0x02
#define READ_VBATT                          0x03
#define FIVE_BAUD_INIT                      0x04
#define FAST_INIT                           0x05
#define CLEAR_TX_BUFFER                     0x07
#define CLEAR_RX_BUFFER                     0x08
#define CLEAR_PERIODIC_MSGS                 0x09
#define CLEAR_MSG_FILTERS                   0x0A
#define CLEAR_FUNCT_MSG_LOOKUP_TABLE        0x0B
#define ADD_TO_FUNCT_MSG_LOOKUP_TABLE       0x0C
#define DELETE_FROM_FUNCT_MSG_LOOKUP_TABLE  0x0D
#define READ_PROG_VOLTAGE                   0x0E

/* ---- Config parameters (SCONFIG.Parameter) ----------------------------- */

#define DATA_RATE           0x01
#define LOOPBACK            0x03
#define NODE_ADDRESS        0x04
#define NETWORK_LINE        0x05
#define P1_MIN              0x06
#define P1_MAX              0x07
#define P2_MIN              0x08
#define P2_MAX              0x09
#define P3_MIN              0x0A
#define P3_MAX              0x0B
#define P4_MIN              0x0C
#define P4_MAX              0x0D
#define W1                  0x0E
#define W2                  0x0F
#define W3                  0x10
#define W4                  0x11
#define W5                  0x12
#define TIDLE               0x13
#define TINIL               0x14
#define TWUP                0x15
#define PARITY              0x16
#define BIT_SAMPLE_POINT    0x17
#define SYNC_JUMP_WIDTH     0x18
#define W0                  0x19
#define T1_MAX              0x1A
#define T2_MAX              0x1B
#define T4_MAX              0x1C
#define T5_MAX              0x1D
#define ISO15765_BS         0x1E
#define ISO15765_STMIN      0x1F
#define DATA_BITS           0x20
#define FIVE_BAUD_MOD       0x21
#define BS_TX               0x22
#define STMIN_TX            0x23
#define T3_MAX              0x24
#define ISO15765_WFT_MAX    0x25

/* ---- Tx flags / Rx status ---------------------------------------------- */

#define ISO15765_FRAME_PAD  0x00000040
#define ISO15765_ADDR_TYPE  0x00000080
#define CAN_29BIT_ID        0x00000100
#define WAIT_P3_MIN_ONLY    0x00000200
#define SW_CAN_HV_TX        0x00000400
#define SCI_MODE            0x00400000
#define SCI_TX_VOLTAGE      0x00800000
#define TX_MSG_TYPE         0x00000001

#define TX_DONE                 0x00000008
#define ISO15765_PADDING_ERROR  0x00000010
#define RX_BREAK                0x00000004
#define START_OF_MESSAGE        0x00000002

/* ---- Entry points ------------------------------------------------------ */

long J2534_API PassThruOpen(const void *pName, unsigned long *pDeviceID);
long J2534_API PassThruClose(unsigned long DeviceID);
long J2534_API PassThruConnect(unsigned long DeviceID, unsigned long ProtocolID,
                               unsigned long Flags, unsigned long BaudRate,
                               unsigned long *pChannelID);
long J2534_API PassThruDisconnect(unsigned long ChannelID);
long J2534_API PassThruReadMsgs(unsigned long ChannelID, PASSTHRU_MSG *pMsg,
                                unsigned long *pNumMsgs, unsigned long Timeout);
long J2534_API PassThruWriteMsgs(unsigned long ChannelID, PASSTHRU_MSG *pMsg,
                                 unsigned long *pNumMsgs, unsigned long Timeout);
long J2534_API PassThruStartPeriodicMsg(unsigned long ChannelID, const PASSTHRU_MSG *pMsg,
                                        unsigned long *pMsgID, unsigned long TimeInterval);
long J2534_API PassThruStopPeriodicMsg(unsigned long ChannelID, unsigned long MsgID);
long J2534_API PassThruStartMsgFilter(unsigned long ChannelID, unsigned long FilterType,
                                      const PASSTHRU_MSG *pMaskMsg,
                                      const PASSTHRU_MSG *pPatternMsg,
                                      const PASSTHRU_MSG *pFlowControlMsg,
                                      unsigned long *pFilterID);
long J2534_API PassThruStopMsgFilter(unsigned long ChannelID, unsigned long FilterID);
long J2534_API PassThruSetProgrammingVoltage(unsigned long DeviceID, unsigned long PinNumber,
                                             unsigned long Voltage);
long J2534_API PassThruReadVersion(unsigned long DeviceID, char *pFirmwareVersion,
                                   char *pDllVersion, char *pApiVersion);
long J2534_API PassThruGetLastError(char *pErrorDescription);
long J2534_API PassThruIoctl(unsigned long ChannelID, unsigned long IoctlID,
                             const void *pInput, void *pOutput);

#ifdef __cplusplus
}
#endif

#endif /* VOLVO_J2534_H */
