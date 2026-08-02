#define _WIN32_WINNT 0x0A00
#include <windows.h>
#include <wtsapi32.h>

#include <atomic>
#include <cstdint>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "common/protocol.hpp"

#ifndef WTS_CHANNEL_OPTION_DYNAMIC
#define WTS_CHANNEL_OPTION_DYNAMIC 0x00000001
#endif

#ifndef PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE
#define PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE 0x00020016
#endif

typedef HANDLE HPCON;
typedef HRESULT(WINAPI *CreatePseudoConsoleFn)(COORD, HANDLE, HANDLE, DWORD, HPCON *);
typedef void(WINAPI *ClosePseudoConsoleFn)(HPCON);
typedef HRESULT(WINAPI *ResizePseudoConsoleFn)(HPCON, COORD);

struct ConptyApi
{
  CreatePseudoConsoleFn create = nullptr;
  ClosePseudoConsoleFn close = nullptr;
  ResizePseudoConsoleFn resize = nullptr;
};

struct WriteGuard
{
  std::mutex mu;
};

static bool load_conpty_api(ConptyApi &api)
{
  HMODULE kernel = GetModuleHandleW(L"kernel32.dll");
  if (!kernel)
  {
    return false;
  }
  api.create = reinterpret_cast<CreatePseudoConsoleFn>(GetProcAddress(kernel, "CreatePseudoConsole"));
  api.close = reinterpret_cast<ClosePseudoConsoleFn>(GetProcAddress(kernel, "ClosePseudoConsole"));
  api.resize = reinterpret_cast<ResizePseudoConsoleFn>(GetProcAddress(kernel, "ResizePseudoConsole"));
  return api.create && api.close && api.resize;
}

static bool write_all(HANDLE h, const uint8_t *data, size_t size)
{
  size_t total = 0;
  while (total < size)
  {
    ULONG written = 0;
    if (!WTSVirtualChannelWrite(h, const_cast<LPSTR>(reinterpret_cast<const char *>(data + total)),
                                static_cast<ULONG>(size - total), &written))
    {
      return false;
    }
    if (written == 0)
    {
      return false;
    }
    total += written;
  }
  return true;
}

static bool send_frame(HANDLE channel, WriteGuard &guard, uint8_t type, const void *payload, uint32_t size)
{
  uint8_t hdr[5];
  hdr[0] = type;
  hdr[1] = static_cast<uint8_t>(size & 0xFF);
  hdr[2] = static_cast<uint8_t>((size >> 8) & 0xFF);
  hdr[3] = static_cast<uint8_t>((size >> 16) & 0xFF);
  hdr[4] = static_cast<uint8_t>((size >> 24) & 0xFF);

  std::lock_guard<std::mutex> lock(guard.mu);
  if (!write_all(channel, hdr, sizeof(hdr)))
  {
    return false;
  }
  if (size && payload)
  {
    return write_all(channel, reinterpret_cast<const uint8_t *>(payload), size);
  }
  return true;
}

static bool send_text(HANDLE channel, WriteGuard &guard, uint8_t type, const std::string &text)
{
  return send_frame(channel, guard, type, text.data(), static_cast<uint32_t>(text.size()));
}

static bool is_channel_gone_error(DWORD err)
{
  switch (err)
  {
  case ERROR_INVALID_HANDLE:
  case ERROR_BROKEN_PIPE:
  case ERROR_NO_DATA:
  case ERROR_OPERATION_ABORTED:
  case ERROR_GEN_FAILURE:
  case ERROR_DEVICE_NOT_CONNECTED:
    return true;
  default:
    return false;
  }
}

struct FrameParser
{
  std::vector<uint8_t> buf;

  template <typename Fn>
  void feed(const uint8_t *data, size_t size, Fn fn)
  {
    buf.insert(buf.end(), data, data + size);
    while (buf.size() >= 5)
    {
      const uint8_t type = buf[0];
      const uint32_t len = static_cast<uint32_t>(buf[1]) |
                           (static_cast<uint32_t>(buf[2]) << 8) |
                           (static_cast<uint32_t>(buf[3]) << 16) |
                           (static_cast<uint32_t>(buf[4]) << 24);
      if (buf.size() < (5u + len))
      {
        return;
      }
      std::vector<uint8_t> payload;
      if (len)
      {
        payload.assign(buf.begin() + 5, buf.begin() + 5 + len);
      }
      buf.erase(buf.begin(), buf.begin() + 5 + len);
      fn(type, payload);
    }
  }
};

struct Args
{
  std::string channel = "agent-rdp";
  std::wstring child = L"powershell";
  std::wstring command_file;
  std::wstring powershell_execution_policy;
  short cols = 120;
  short rows = 40;
  bool valid = true;
};

static bool set_powershell_execution_policy(Args &args, const std::wstring &value)
{
  if (_wcsicmp(value.c_str(), L"default") == 0)
  {
    args.powershell_execution_policy.clear();
    return true;
  }

  const wchar_t *allowed[] = {L"AllSigned", L"RemoteSigned", L"Restricted", L"Unrestricted", L"Bypass"};
  for (const wchar_t *policy : allowed)
  {
    if (_wcsicmp(value.c_str(), policy) == 0)
    {
      args.powershell_execution_policy = policy;
      return true;
    }
  }
  return false;
}

static Args parse_args(int argc, wchar_t **argv)
{
  Args args;
  for (int i = 1; i < argc; ++i)
  {
    const std::wstring a = argv[i];
    if (a == L"--channel" && i + 1 < argc)
    {
      std::wstring v = argv[++i];
      args.channel.assign(v.begin(), v.end());
    }
    else if (a == L"--child" && i + 1 < argc)
    {
      args.child = argv[++i];
    }
    else if (a == L"--command-file" && i + 1 < argc)
    {
      args.command_file = argv[++i];
    }
    else if (a == L"--powershell-execution-policy")
    {
      if (i + 1 >= argc)
      {
        args.valid = false;
      }
      else
      {
        args.valid = set_powershell_execution_policy(args, argv[++i]) && args.valid;
      }
    }
    else if (a == L"--cols" && i + 1 < argc)
    {
      args.cols = static_cast<short>(_wtoi(argv[++i]));
    }
    else if (a == L"--rows" && i + 1 < argc)
    {
      args.rows = static_cast<short>(_wtoi(argv[++i]));
    }
  }
  if (args.cols <= 0)
    args.cols = 120;
  if (args.rows <= 0)
    args.rows = 40;
  if (!args.powershell_execution_policy.empty() &&
      (_wcsicmp(args.child.c_str(), L"powershell") != 0 || args.command_file.empty()))
  {
    args.valid = false;
  }
  return args;
}

static std::wstring quote_win32_arg(const std::wstring &value)
{
  std::wstring quoted = L"\"";
  for (const wchar_t ch : value)
  {
    if (ch == L'"')
    {
      quoted += L'\\';
    }
    quoted += ch;
  }
  quoted += L"\"";
  return quoted;
}

static std::wstring build_command_line(const Args &args)
{
  if (_wcsicmp(args.child.c_str(), L"cmd") == 0)
  {
    if (!args.command_file.empty())
    {
      return L"\"C:\\Windows\\System32\\cmd.exe\" /Q /D /C " + quote_win32_arg(args.command_file);
    }
    return L"\"C:\\Windows\\System32\\cmd.exe\" /Q /K";
  }

  if (!args.command_file.empty())
  {
    std::wstring command =
        L"\"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe\" "
        L"-NoLogo -NoProfile -NonInteractive";
    if (!args.powershell_execution_policy.empty())
    {
      command += L" -ExecutionPolicy " + args.powershell_execution_policy;
    }
    command += L" -File " + quote_win32_arg(args.command_file);
    return command;
  }

  return L"\"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe\" "
         L"-NoLogo -NoProfile";
}

static constexpr size_t kChannelPduLength = 8;

// Reads WTS virtual channel PDUs and yields decoded agent-rdp frames via `fn`.
// Shared by both the ConPTY interactive path and the pipe-mode command path.
template <typename Fn>
static bool pump_channel_once(HANDLE channel, std::vector<uint8_t> &rx, FrameParser &parser, Fn fn, bool &channel_gone)
{
  channel_gone = false;
  ULONG read = 0;
  if (!WTSVirtualChannelRead(channel, 200, reinterpret_cast<LPSTR>(rx.data()), static_cast<ULONG>(rx.size()), &read))
  {
    const DWORD err = GetLastError();
    if (is_channel_gone_error(err))
    {
      channel_gone = true;
    }
    return false;
  }
  if (read < kChannelPduLength)
  {
    return false;
  }

  const uint8_t *pdu_payload = rx.data() + kChannelPduLength;
  const size_t pdu_payload_size = static_cast<size_t>(read) - kChannelPduLength;
  if (pdu_payload_size == 0)
  {
    return false;
  }

  parser.feed(pdu_payload, pdu_payload_size, fn);
  return true;
}

// ---------------------------------------------------------------------------
// ConPTY-backed interactive shell mode (no --command-file): a real pseudo
// console is created and stdin/stdout/stderr of the child are merged into a
// single terminal-like stream, matching normal shell behavior.
// ---------------------------------------------------------------------------
static int run_conpty_mode(HANDLE channel, WriteGuard &write_guard, const Args &args)
{
  ConptyApi conpty{};
  if (!load_conpty_api(conpty))
  {
    send_text(channel, write_guard, agent_rdp::frame::kError,
              "ConPTY API unavailable on this Windows build/session");
    return 20;
  }

  SECURITY_ATTRIBUTES sa{};
  sa.nLength = sizeof(sa);
  sa.bInheritHandle = TRUE;

  HANDLE pty_in_read = nullptr, pty_in_write = nullptr;
  HANDLE pty_out_read = nullptr, pty_out_write = nullptr;
  if (!CreatePipe(&pty_in_read, &pty_in_write, &sa, 0))
  {
    send_text(channel, write_guard, agent_rdp::frame::kError, "CreatePipe(input) failed");
    return 21;
  }
  if (!CreatePipe(&pty_out_read, &pty_out_write, &sa, 0))
  {
    send_text(channel, write_guard, agent_rdp::frame::kError, "CreatePipe(output) failed");
    CloseHandle(pty_in_read);
    CloseHandle(pty_in_write);
    return 22;
  }

  COORD size{args.cols, args.rows};
  HPCON hpc = nullptr;
  HRESULT hr = conpty.create(size, pty_in_read, pty_out_write, 0, &hpc);
  CloseHandle(pty_in_read);
  CloseHandle(pty_out_write);
  if (FAILED(hr))
  {
    send_text(channel, write_guard, agent_rdp::frame::kError, "CreatePseudoConsole failed");
    CloseHandle(pty_in_write);
    CloseHandle(pty_out_read);
    return 23;
  }

  SIZE_T attr_size = 0;
  InitializeProcThreadAttributeList(nullptr, 1, 0, &attr_size);
  auto *attr_list = reinterpret_cast<PPROC_THREAD_ATTRIBUTE_LIST>(HeapAlloc(GetProcessHeap(), 0, attr_size));
  if (!attr_list)
  {
    send_text(channel, write_guard, agent_rdp::frame::kError, "HeapAlloc(attr_list) failed");
    conpty.close(hpc);
    CloseHandle(pty_in_write);
    CloseHandle(pty_out_read);
    return 24;
  }
  if (!InitializeProcThreadAttributeList(attr_list, 1, 0, &attr_size))
  {
    send_text(channel, write_guard, agent_rdp::frame::kError, "InitializeProcThreadAttributeList failed");
    HeapFree(GetProcessHeap(), 0, attr_list);
    conpty.close(hpc);
    CloseHandle(pty_in_write);
    CloseHandle(pty_out_read);
    return 25;
  }
  if (!UpdateProcThreadAttribute(attr_list, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, hpc, sizeof(hpc), nullptr,
                                 nullptr))
  {
    send_text(channel, write_guard, agent_rdp::frame::kError, "UpdateProcThreadAttribute(PSEUDOCONSOLE) failed");
    DeleteProcThreadAttributeList(attr_list);
    HeapFree(GetProcessHeap(), 0, attr_list);
    conpty.close(hpc);
    CloseHandle(pty_in_write);
    CloseHandle(pty_out_read);
    return 26;
  }

  STARTUPINFOEXW si{};
  si.StartupInfo.cb = sizeof(si);
  si.lpAttributeList = attr_list;
  PROCESS_INFORMATION pi{};

  std::wstring cmdline = build_command_line(args);
  std::vector<wchar_t> cmd_mut(cmdline.begin(), cmdline.end());
  cmd_mut.push_back(L'\0');

  if (!CreateProcessW(nullptr, cmd_mut.data(), nullptr, nullptr, FALSE,
                      EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT, nullptr, nullptr,
                      &si.StartupInfo, &pi))
  {
    send_text(channel, write_guard, agent_rdp::frame::kError, "CreateProcessW child failed");
    DeleteProcThreadAttributeList(attr_list);
    HeapFree(GetProcessHeap(), 0, attr_list);
    conpty.close(hpc);
    CloseHandle(pty_in_write);
    CloseHandle(pty_out_read);
    return 27;
  }

  CloseHandle(pi.hThread);
  send_frame(channel, write_guard, agent_rdp::frame::kReady, nullptr, 0);

  std::atomic<bool> running{true};
  std::thread out_thread([&]()
                         {
    std::vector<uint8_t> out(8192);
    while (running.load()) {
      DWORD n = 0;
      if (!ReadFile(pty_out_read, out.data(), static_cast<DWORD>(out.size()), &n, nullptr)) {
        break;
      }
      if (n == 0) {
        break;
      }
      if (!send_frame(channel, write_guard, agent_rdp::frame::kOutput, out.data(), static_cast<uint32_t>(n))) {
        running.store(false);
        TerminateProcess(pi.hProcess, 0);
        break;
      }
    } });

  FrameParser parser;
  std::vector<uint8_t> rx(8192);
  while (running.load())
  {
    const DWORD wait = WaitForSingleObject(pi.hProcess, 0);
    if (wait == WAIT_OBJECT_0)
    {
      break;
    }

    bool channel_gone = false;
    pump_channel_once(channel, rx, parser, [&](uint8_t type, const std::vector<uint8_t> &payload)
                {
      if (type == agent_rdp::frame::kInput) {
        if (!payload.empty()) {
          DWORD written = 0;
          if (!WriteFile(pty_in_write, payload.data(), static_cast<DWORD>(payload.size()), &written, nullptr)) {
            running.store(false);
            TerminateProcess(pi.hProcess, 0);
          }
        }
      } else if (type == agent_rdp::frame::kResize) {
        if (payload.size() >= 4) {
          const short cols = static_cast<short>(payload[0] | (payload[1] << 8));
          const short rows = static_cast<short>(payload[2] | (payload[3] << 8));
          COORD new_size{static_cast<SHORT>(cols > 0 ? cols : 120),
                         static_cast<SHORT>(rows > 0 ? rows : 40)};
          conpty.resize(hpc, new_size);
        }
      } else if (type == agent_rdp::frame::kClose) {
        running.store(false);
        TerminateProcess(pi.hProcess, 0);
      } }, channel_gone);
    if (channel_gone)
    {
      running.store(false);
      TerminateProcess(pi.hProcess, 0);
      break;
    }
  }

  running.store(false);
  if (WaitForSingleObject(pi.hProcess, 0) == WAIT_TIMEOUT)
  {
    TerminateProcess(pi.hProcess, 0);
  }
  CloseHandle(pty_in_write);
  CloseHandle(pty_out_read);
  WaitForSingleObject(pi.hProcess, 3000);
  DWORD exit_code = 0;
  GetExitCodeProcess(pi.hProcess, &exit_code);
  send_frame(channel, write_guard, agent_rdp::frame::kExit, &exit_code, sizeof(exit_code));

  if (out_thread.joinable())
  {
    out_thread.join();
  }

  CloseHandle(pi.hProcess);
  DeleteProcThreadAttributeList(attr_list);
  HeapFree(GetProcessHeap(), 0, attr_list);
  conpty.close(hpc);
  return static_cast<int>(exit_code);
}

// ---------------------------------------------------------------------------
// Pipe mode (--command-file present): a single non-interactive command runs
// via plain anonymous pipes (no pseudo console), so stdout and stderr stay
// separate and free of terminal control sequences -- easy for a caller (an
// AI agent, a script) to parse. This is the primary path for single-command
// "agent" invocations.
// ---------------------------------------------------------------------------
struct PipeEnds
{
  HANDLE read = nullptr;
  HANDLE write = nullptr;
};

static bool create_inheritable_pipe(PipeEnds &ends, bool child_is_reader)
{
  SECURITY_ATTRIBUTES sa{};
  sa.nLength = sizeof(sa);
  sa.bInheritHandle = TRUE;
  if (!CreatePipe(&ends.read, &ends.write, &sa, 0))
  {
    return false;
  }
  HANDLE &parent_side = child_is_reader ? ends.write : ends.read;
  return SetHandleInformation(parent_side, HANDLE_FLAG_INHERIT, 0) != 0;
}

static void close_if_valid(HANDLE &h)
{
  if (h)
  {
    CloseHandle(h);
    h = nullptr;
  }
}

static int run_pipe_mode(HANDLE channel, WriteGuard &write_guard, const Args &args)
{
  PipeEnds stdin_pipe, stdout_pipe, stderr_pipe;
  if (!create_inheritable_pipe(stdin_pipe, /*child_is_reader=*/true))
  {
    send_text(channel, write_guard, agent_rdp::frame::kError, "CreatePipe(stdin) failed");
    return 30;
  }
  if (!create_inheritable_pipe(stdout_pipe, /*child_is_reader=*/false))
  {
    send_text(channel, write_guard, agent_rdp::frame::kError, "CreatePipe(stdout) failed");
    close_if_valid(stdin_pipe.read);
    close_if_valid(stdin_pipe.write);
    return 31;
  }
  if (!create_inheritable_pipe(stderr_pipe, /*child_is_reader=*/false))
  {
    send_text(channel, write_guard, agent_rdp::frame::kError, "CreatePipe(stderr) failed");
    close_if_valid(stdin_pipe.read);
    close_if_valid(stdin_pipe.write);
    close_if_valid(stdout_pipe.read);
    close_if_valid(stdout_pipe.write);
    return 32;
  }

  STARTUPINFOW si{};
  si.cb = sizeof(si);
  si.dwFlags = STARTF_USESTDHANDLES;
  si.hStdInput = stdin_pipe.read;
  si.hStdOutput = stdout_pipe.write;
  si.hStdError = stderr_pipe.write;
  PROCESS_INFORMATION pi{};

  std::wstring cmdline = build_command_line(args);
  std::vector<wchar_t> cmd_mut(cmdline.begin(), cmdline.end());
  cmd_mut.push_back(L'\0');

  const BOOL created = CreateProcessW(nullptr, cmd_mut.data(), nullptr, nullptr, TRUE,
                                      CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi);

  // Parent no longer needs the child-side handles regardless of outcome.
  close_if_valid(stdin_pipe.read);
  close_if_valid(stdout_pipe.write);
  close_if_valid(stderr_pipe.write);

  if (!created)
  {
    send_text(channel, write_guard, agent_rdp::frame::kError, "CreateProcessW child failed");
    close_if_valid(stdin_pipe.write);
    close_if_valid(stdout_pipe.read);
    close_if_valid(stderr_pipe.read);
    return 33;
  }

  CloseHandle(pi.hThread);
  send_frame(channel, write_guard, agent_rdp::frame::kReady, nullptr, 0);

  std::atomic<bool> running{true};

  auto reader = [&](HANDLE pipe, uint8_t frame_type)
  {
    std::vector<uint8_t> out(8192);
    while (running.load())
    {
      DWORD n = 0;
      if (!ReadFile(pipe, out.data(), static_cast<DWORD>(out.size()), &n, nullptr) || n == 0)
      {
        break;
      }
      if (!send_frame(channel, write_guard, frame_type, out.data(), static_cast<uint32_t>(n)))
      {
        running.store(false);
        TerminateProcess(pi.hProcess, 0);
        break;
      }
    }
  };

  std::thread stdout_thread(reader, stdout_pipe.read, agent_rdp::frame::kOutput);
  std::thread stderr_thread(reader, stderr_pipe.read, agent_rdp::frame::kOutputErr);

  FrameParser parser;
  std::vector<uint8_t> rx(8192);
  while (running.load())
  {
    if (WaitForSingleObject(pi.hProcess, 0) == WAIT_OBJECT_0)
    {
      break;
    }

    bool channel_gone = false;
    pump_channel_once(channel, rx, parser, [&](uint8_t type, const std::vector<uint8_t> &payload)
                {
      if (type == agent_rdp::frame::kInput) {
        if (!payload.empty() && stdin_pipe.write) {
          DWORD written = 0;
          if (!WriteFile(stdin_pipe.write, payload.data(), static_cast<DWORD>(payload.size()), &written, nullptr)) {
            close_if_valid(stdin_pipe.write);
          }
        }
      } else if (type == agent_rdp::frame::kClose) {
        running.store(false);
        TerminateProcess(pi.hProcess, 0);
      }
      // kResize is meaningless without a pseudo console; ignored here.
                }, channel_gone);
    if (channel_gone)
    {
      running.store(false);
      TerminateProcess(pi.hProcess, 0);
      break;
    }
  }

  running.store(false);
  close_if_valid(stdin_pipe.write);
  if (WaitForSingleObject(pi.hProcess, 0) == WAIT_TIMEOUT)
  {
    TerminateProcess(pi.hProcess, 0);
  }
  WaitForSingleObject(pi.hProcess, 3000);
  DWORD exit_code = 0;
  GetExitCodeProcess(pi.hProcess, &exit_code);

  if (stdout_thread.joinable())
    stdout_thread.join();
  if (stderr_thread.joinable())
    stderr_thread.join();

  close_if_valid(stdout_pipe.read);
  close_if_valid(stderr_pipe.read);

  send_frame(channel, write_guard, agent_rdp::frame::kExit, &exit_code, sizeof(exit_code));

  CloseHandle(pi.hProcess);
  return static_cast<int>(exit_code);
}

int wmain(int argc, wchar_t **argv)
{
  Args args = parse_args(argc, argv);
  if (!args.valid)
  {
    return ERROR_INVALID_PARAMETER;
  }

  HANDLE channel = WTSVirtualChannelOpenEx(WTS_CURRENT_SESSION, const_cast<LPSTR>(args.channel.c_str()),
                                           WTS_CHANNEL_OPTION_DYNAMIC);
  if (!channel)
  {
    return static_cast<int>(GetLastError());
  }

  WriteGuard write_guard;
  const int result = args.command_file.empty() ? run_conpty_mode(channel, write_guard, args)
                                                : run_pipe_mode(channel, write_guard, args);

  WTSVirtualChannelClose(channel);
  return result;
}
