#pragma once

#include <cstdint>

namespace rdp2exec
{

    inline constexpr const char *kChannelName = "rdp2exec";

    // Default client-side IPC endpoint (TCP loopback). The launcher always
    // overrides this via the RDP2EXEC_SOCKET env var ("host:port"), but a
    // default keeps the plugin usable stand-alone during development.
    inline constexpr const char *kDefaultSocketHost = "127.0.0.1";
    inline constexpr uint16_t kDefaultSocketPort = 0; // 0 = must be supplied by launcher

    namespace frame
    {
        inline constexpr uint8_t kInput = 0x01;
        inline constexpr uint8_t kResize = 0x02;
        inline constexpr uint8_t kClose = 0x03;

        inline constexpr uint8_t kReady = 0x81;
        inline constexpr uint8_t kOutput = 0x82;
        inline constexpr uint8_t kExit = 0x83;
        inline constexpr uint8_t kError = 0x84;

        // Stderr bytes, used only by the non-PTY "pipe mode" command path
        // (interactive ConPTY mode merges stdout/stderr into a single
        // stream, as a real terminal would, and never emits this frame).
        inline constexpr uint8_t kOutputErr = 0x85;
    } // namespace frame

} // namespace rdp2exec
