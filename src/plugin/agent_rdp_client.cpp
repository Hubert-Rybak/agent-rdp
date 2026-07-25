// FreeRDP Windows client plugin for agent-rdp.
//
// Loaded by wfreerdp.exe (FreeRDP's Windows client) as a Dynamic Virtual
// Channel (DVC) handler. Bridges the "agent-rdp" DVC to a local TCP loopback
// socket that the Python launcher listens on -- the launcher is the process
// that actually terminates the connection to the user's terminal/agent
// caller, this plugin is just the byte pipe between the RDP session and it.
//
// This is a straight port of the original Linux plugin (which used an
// AF_UNIX socket + pthreads); the FreeRDP/WinPR channel API itself
// (IWTSVirtualChannelCallback, DVCPluginEntry) is fully cross-platform and
// required no changes -- only the local IPC transport and threading primitive
// were swapped for Windows equivalents (Winsock TCP loopback, std::thread).

// winsock2.h must come before windows.h (pulled in transitively by the
// FreeRDP/WinPR headers below) to avoid the winsock.h/winsock2.h conflict.
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>

#include <freerdp/client/channels.h>
#include <freerdp/channels/log.h>
#include <freerdp/dvc.h>

#include <winpr/crt.h>
#include <winpr/stream.h>

#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <inttypes.h>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "common/protocol.hpp"

#pragma comment(lib, "ws2_32.lib")

#define TAG CHANNELS_TAG("agent-rdp.client")

namespace
{

  struct AGENT_RDP_PLUGIN
  {
    GENERIC_DYNVC_PLUGIN base;
  };

  struct AGENT_RDP_CHANNEL_CALLBACK
  {
    GENERIC_CHANNEL_CALLBACK generic;
    SOCKET sock;
    int running;
    int reader_started;
    std::thread *reader;
  };

  void ensure_winsock_initialized()
  {
    static std::once_flag once;
    std::call_once(once, []()
                  {
      WSADATA wsa_data;
      WSAStartup(MAKEWORD(2, 2), &wsa_data); });
  }

  bool resolve_socket_endpoint(std::string &host, uint16_t &port)
  {
    const char *env = std::getenv("AGENT_RDP_SOCKET");
    if (!env || env[0] == '\0')
    {
      return false;
    }

    const std::string value(env);
    const size_t sep = value.rfind(':');
    if (sep == std::string::npos || sep == value.size() - 1)
    {
      return false;
    }

    host = value.substr(0, sep);
    if (host.empty())
    {
      host = agent_rdp::kDefaultSocketHost;
    }

    const std::string port_str = value.substr(sep + 1);
    const int parsed = std::atoi(port_str.c_str());
    if (parsed <= 0 || parsed > 65535)
    {
      return false;
    }
    port = static_cast<uint16_t>(parsed);
    return true;
  }

  SOCKET connect_tcp_socket(const std::string &host, uint16_t port)
  {
    ensure_winsock_initialized();

    char port_str[8];
    std::snprintf(port_str, sizeof(port_str), "%u", static_cast<unsigned>(port));

    ADDRINFOA hints{};
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_protocol = IPPROTO_TCP;

    ADDRINFOA *result = nullptr;
    if (getaddrinfo(host.c_str(), port_str, &hints, &result) != 0 || !result)
    {
      WLog_ERR(TAG, "getaddrinfo(%s:%s) failed", host.c_str(), port_str);
      return INVALID_SOCKET;
    }

    constexpr int kAttempts = 100;
    SOCKET sock = INVALID_SOCKET;
    for (int i = 0; i < kAttempts; ++i)
    {
      sock = ::socket(result->ai_family, result->ai_socktype, result->ai_protocol);
      if (sock == INVALID_SOCKET)
      {
        WLog_ERR(TAG, "socket() failed: %d", WSAGetLastError());
        break;
      }

      if (::connect(sock, result->ai_addr, static_cast<int>(result->ai_addrlen)) == 0)
      {
        freeaddrinfo(result);
        return sock;
      }

      const int err = WSAGetLastError();
      ::closesocket(sock);
      sock = INVALID_SOCKET;

      if (err == WSAECONNREFUSED || err == WSAETIMEDOUT)
      {
        Sleep(200);
        continue;
      }

      WLog_ERR(TAG, "connect(%s:%s) failed: %d", host.c_str(), port_str, err);
      break;
    }

    freeaddrinfo(result);
    return INVALID_SOCKET;
  }

  void socket_reader_thread(AGENT_RDP_CHANNEL_CALLBACK *cb)
  {
    std::vector<BYTE> buffer(8192);

    while (cb && cb->running)
    {
      const int rc = ::recv(cb->sock, reinterpret_cast<char *>(buffer.data()),
                            static_cast<int>(buffer.size()), 0);
      if (rc == 0)
      {
        break;
      }
      if (rc < 0)
      {
        const int err = WSAGetLastError();
        if (err == WSAEINTR)
        {
          continue;
        }
        WLog_ERR(TAG, "recv(local socket) failed: %d", err);
        break;
      }

      const UINT status = cb->generic.channel->Write(cb->generic.channel, static_cast<UINT32>(rc),
                                                     buffer.data(), nullptr);
      if (status != CHANNEL_RC_OK)
      {
        WLog_ERR(TAG, "channel->Write failed: %" PRIu32, status);
        break;
      }
    }
  }

  UINT agent_rdp_on_data_received(IWTSVirtualChannelCallback *pChannelCallback, wStream *data)
  {
    auto *cb = reinterpret_cast<AGENT_RDP_CHANNEL_CALLBACK *>(pChannelCallback);
    if (!cb || cb->sock == INVALID_SOCKET || !data)
    {
      return CHANNEL_RC_OK;
    }

    const BYTE *pBuffer = static_cast<const BYTE *>(Stream_Pointer(data));
    const UINT32 cbSize = Stream_GetRemainingLength(data);

    if (!pBuffer || cbSize == 0)
    {
      return CHANNEL_RC_OK;
    }

    UINT32 total = 0;
    while (total < cbSize)
    {
      const int rc = ::send(cb->sock, reinterpret_cast<const char *>(pBuffer + total),
                            static_cast<int>(cbSize - total), 0);
      if (rc <= 0)
      {
        WLog_ERR(TAG, "send(local socket) failed: %d", WSAGetLastError());
        return CHANNEL_RC_OK;
      }
      total += static_cast<UINT32>(rc);
    }

    return CHANNEL_RC_OK;
  }

  UINT agent_rdp_on_open(IWTSVirtualChannelCallback *pChannelCallback)
  {
    auto *cb = reinterpret_cast<AGENT_RDP_CHANNEL_CALLBACK *>(pChannelCallback);
    if (!cb)
    {
      return ERROR_INVALID_DATA;
    }

    std::string host;
    uint16_t port = 0;
    if (!resolve_socket_endpoint(host, port))
    {
      WLog_ERR(TAG, "AGENT_RDP_SOCKET not set or malformed (expected host:port)");
      return ERROR_BAD_ARGUMENTS;
    }

    cb->sock = connect_tcp_socket(host, port);
    if (cb->sock == INVALID_SOCKET)
    {
      WLog_ERR(TAG, "failed to connect to launcher socket: %s:%u", host.c_str(), port);
      return ERROR_OPEN_FAILED;
    }

    cb->running = 1;
    cb->reader_started = 0;
    cb->reader = new (std::nothrow) std::thread(socket_reader_thread, cb);
    if (!cb->reader)
    {
      WLog_ERR(TAG, "failed to start reader thread");
      ::closesocket(cb->sock);
      cb->sock = INVALID_SOCKET;
      cb->running = 0;
      return ERROR_INTERNAL_ERROR;
    }

    cb->reader_started = 1;
    WLog_INFO(TAG, "agent-rdp DVC opened; socket=%s:%u", host.c_str(), port);
    return CHANNEL_RC_OK;
  }

  UINT agent_rdp_on_close(IWTSVirtualChannelCallback *pChannelCallback)
  {
    auto *cb = reinterpret_cast<AGENT_RDP_CHANNEL_CALLBACK *>(pChannelCallback);
    if (!cb)
    {
      return CHANNEL_RC_OK;
    }

    cb->running = 0;
    if (cb->sock != INVALID_SOCKET)
    {
      ::shutdown(cb->sock, SD_BOTH);
      ::closesocket(cb->sock);
      cb->sock = INVALID_SOCKET;
    }

    if (cb->reader_started && cb->reader)
    {
      if (cb->reader->joinable())
      {
        cb->reader->join();
      }
      delete cb->reader;
      cb->reader = nullptr;
    }

    std::free(cb);
    return CHANNEL_RC_OK;
  }

  static const IWTSVirtualChannelCallback agent_rdp_callbacks = {
      agent_rdp_on_data_received,
      agent_rdp_on_open,
      agent_rdp_on_close,
  };

} // namespace

extern "C" __declspec(dllexport) UINT DVCPluginEntry(IDRDYNVC_ENTRY_POINTS *pEntryPoints)
{
  return freerdp_generic_DVCPluginEntry(pEntryPoints, TAG, agent_rdp::kChannelName, sizeof(AGENT_RDP_PLUGIN),
                                        sizeof(AGENT_RDP_CHANNEL_CALLBACK), &agent_rdp_callbacks, nullptr,
                                        nullptr);
}
