# Migration notes: rdp2exec → Windows-to-Windows

Status/handoff doc for the port of [blacknon/rdp2exec](https://github.com/blacknon/rdp2exec) from a Linux client driving a Windows RDP target, to a native Windows client driving a Windows RDP target, purpose-built for an AI agent to invoke as a command-execution tool.

## Summary

Upstream `rdp2exec` bootstraps command execution by simulating `Win+R` and typing into the visible Run dialog over X11/`xdotool`, and briefly copies its helper executable onto the target's `%TEMP%` before deleting it. This fork:

- replaces that GUI-automation bootstrap with RDP's native **Alternate Shell** (Initial Program) protocol feature (`/shell:`, `/shell-dir:`) — no GUI automation, no visible-desktop dependency, works headlessly;
- runs the helper executable **directly off the FreeRDP-redirected client drive** instead of copying it to the target's local disk at all, so the target's filesystem is never touched by the tool itself;
- ports the client side (launcher + FreeRDP plugin) from Linux/X11 to native Windows;
- adds a non-PTY "pipe mode" to the remote-side bridge so single-command execution returns clean, separated stdout/stderr and an exit code — built for a program (an AI agent) to parse, rather than a human terminal.

## What changed, file by file

| File | Change |
|---|---|
| `src/common/protocol.hpp` | Added `kOutputErr` (0x85) frame type for separated stderr in pipe mode. |
| `src/windows/rdp2exec_bridge.cpp` | Kept the existing ConPTY-backed interactive shell path unchanged. Added `run_pipe_mode`: plain stdin/stdout/stderr pipes (no pseudo console) when `--command-file` is given, streaming stdout/stderr as separate frames plus a final exit-code frame. |
| `src/plugin/rdp2exec_client.cpp` | Ported from an `AF_UNIX` socket + `pthread` (Linux `.so`) to a Winsock TCP loopback socket + `std::thread` (Windows `.dll`). The FreeRDP/WinPR `IWTSVirtualChannelCallback`/`DVCPluginEntry` plugin API itself needed no changes — it's already cross-platform. |
| `src/launcher/rdp2exec.py` | Ported from POSIX `termios`/`SIGWINCH`/Unix sockets to Win32 console raw mode (via `ctypes`), a resize-polling thread (Windows has no `SIGWINCH`), and a TCP loopback IPC server. Removed `xdotool`/window-title-wait/Run-dialog-injection entirely; added `build_alternate_shell` to construct the AlternateShell bootstrap command line. Added binary-mode fd handling (`msvcrt.setmode`) to avoid Windows' text-mode CRLF translation and Ctrl-Z-as-EOF corrupting raw terminal bytes. |
| `CMakeLists.txt`, `vcpkg.json`, `scripts/build.ps1` | Native Windows build (CMake + vcpkg, reusing the same pkg-config-based FreeRDP discovery pattern as upstream's Linux build) replacing the Docker/X11/`xdotool`/mingw cross-compile setup. |
| `README.md`, `LICENSE` | Rewritten usage/architecture docs for the Windows-to-Windows flow; MIT license retaining upstream's copyright notice. |

## Known limitations / unverified assumptions

This was implemented in a Linux sandbox with no Windows toolchain, no FreeRDP Windows SDK, and no Windows RDP target available, so verification was necessarily limited:

- **Never compiled.** Only static checks were possible: Python syntax (`py_compile`), C++ brace/paren balance, and manual review. No MSVC/mingw+vcpkg build was run.
- **Never run against a real Windows RDP target.** The core new mechanism — AlternateShell launching a retry-poll `cmd.exe` one-liner that waits for the redirected drive to mount, then executes the bridge straight off the UNC path — is a logically sound design (grounded in FreeRDP's documented `/shell:`/`/shell-dir:` options), but its real-world timing (how long device redirection actually lags behind alternate-shell process start) is unverified.
- **`wfreerdp.exe`'s DVC plugin addin search path is a best guess.** `build.ps1` stages `rdp2exec-client.dll` next to `wfreerdp.exe`, matching the common "same directory as the client" convention, but this wasn't empirically confirmed against a real FreeRDP Windows build.
- **AlternateShell field length wasn't tested against a real RDP server.** The bootstrap command is kept short and space-free by construction (short drive name, fixed filenames) to stay safely under the historical ~512-char constraint, but no real server round-trip has validated this.
- **vcpkg install layout details** (exact path to `wfreerdp.exe` under `vcpkg_installed/`) are discovered defensively at build time (`Get-ChildItem -Recurse`) rather than hardcoded, precisely because the exact layout couldn't be confirmed from this environment.

## Possible follow-ups

1. **Build and smoke-test on a real Windows host against a real Windows RDP target.** This is the highest-priority next step — everything above is a plausible-but-unverified assumption until exercised for real. Expect to iterate on the drive-mount retry timing and the plugin addin path.
2. **Structured output mode.** Add a `--json` flag to the launcher that emits `{"exit_code": ..., "stdout": "...", "stderr": "..."}` as a single object, instead of (or in addition to) streaming raw bytes — even easier for an agent/tool-call layer to consume than today's separated-stream approach.
3. **MCP server wrapper.** Wrap `rdp2exec.py` as an MCP tool so an agent calls it directly (structured args/results) instead of shelling out to a subprocess and parsing stdout/stderr/exit code.
4. **Credential handling.** The RDP password currently flows through `-P`/`RDP_PASSWORD`/an interactive prompt and is piped to `wfreerdp` via stdin (`/from-stdin:force`) — reasonable, but consider Windows Credential Manager integration or a secrets-manager hook for unattended agent use.
5. **CI.** There is no automated build/test today (deliberately, since this environment can't run one) — a Windows-hosted GitHub Actions workflow that runs `scripts/build.ps1` on every push would close the biggest verification gap.
6. **Concurrency.** The current design is single-target, single-session per launcher invocation. If an agent needs to fan out across multiple RDP targets in parallel, the launcher would need session isolation (it already uses per-invocation ephemeral temp dirs and OS-assigned loopback ports, which is a reasonable starting point).
7. **Error surface for agents.** Consider mapping the bridge's numeric failure codes (20–33, see `rdp2exec_bridge.cpp`) and `kError` frame text to a small stable vocabulary an agent can branch on (e.g. "ConPTY unavailable" vs. "process spawn failed" vs. "channel dropped"), rather than leaving them as raw exit codes/free-text.
