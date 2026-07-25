# agent-rdp

[![CI](https://github.com/Hubert-Rybak/agent-rdp/actions/workflows/ci.yml/badge.svg)](https://github.com/Hubert-Rybak/agent-rdp/actions/workflows/ci.yml)

A Windows-to-Windows fork of [blacknon/rdp2exec](https://github.com/blacknon/rdp2exec), retargeted so an AI agent (or any script) running on a **Windows** host can execute `cmd`/PowerShell commands on a remote **Windows** RDP target — without opening any extra management port, and **without ever writing a file to the target's local disk**.

## What changed vs. upstream rdp2exec

Upstream `rdp2exec` is a Linux client (FreeRDP/`xfreerdp` + `xdotool`/X11) driving a Windows RDP target. This fork retargets the client side to Windows and removes the GUI-automation bootstrap entirely:

| | Upstream (Linux → Windows) | This fork (Windows → Windows) |
|---|---|---|
| Client | `xfreerdp` on Linux, X11 + `xdotool` | `wfreerdp.exe` (FreeRDP's native Windows client) |
| Bootstrap | Simulates `Win+R`, types a command into the visible Run dialog | RDP's native **Alternate Shell** (`/shell:`, `/shell-dir:`) — no GUI automation, no visible desktop interaction, works headlessly |
| Remote persistence | Copies the helper `.exe` to the target's `%TEMP%`, runs it, deletes it | Helper `.exe` runs **directly off the client-redirected drive** (`\\tsclient\...`); nothing is ever written to the target's local disk |
| Local IPC (client ↔ plugin) | Unix domain socket | TCP loopback socket (Windows has no `AF_UNIX` here) |
| Command output | Always via ConPTY (terminal control sequences mixed into output) | Interactive shell still uses ConPTY; single-command mode uses plain pipes with **separate stdout/stderr and exit code** — easy for a program to parse |

The Windows target-side bridge (`rdp2exec_bridge.exe`) was already native Windows C++ in upstream and needed no port — only a new non-PTY execution mode was added to it.

## Requirements

- A Windows host to run the client (`rdp2exec`) from.
- A Windows target with RDP enabled and a user account you can authenticate as.

Building from source has its own toolchain requirements — see [Build](#build).

## Install

### Pre-built release

Every tag push (`v*.*.*`) triggers [`.github/workflows/release.yml`](.github/workflows/release.yml), which builds `rdp2exec-client.dll` and `rdp2exec_bridge.exe`, bundles `wfreerdp.exe`, freezes the launcher into a standalone `rdp2exec.exe` (via PyInstaller — no separate Python install required), zips it all together, and publishes it to [Releases](https://github.com/Hubert-Rybak/agent-rdp/releases) with a `.sha256.txt` checksum. Download the zip for a release, extract it anywhere, and run `rdp2exec.exe` from that folder.

### winget

A local winget manifest lives under [`winget/manifests/h/HubertRybak/AgentRdp/`](winget/manifests/h/HubertRybak/AgentRdp/), laid out in the same directory convention the [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs) community repo uses (`InstallerType: zip` + `NestedInstallerType: portable`, so `winget` puts an `rdp2exec` command on your `PATH`). Install straight from it, without waiting on a public submission:

```powershell
winget install --manifest winget/manifests/h/HubertRybak/AgentRdp/0.1.0
```

Notes:
- The manifest's `InstallerUrl`/`InstallerSha256` must match a real published release asset — after cutting a release, update `winget/manifests/.../<version>/HubertRybak.AgentRdp.installer.yaml` with the version, URL, and SHA256 the release workflow prints (or add a new version folder for the new release).
- This has **not** been submitted to the public `microsoft/winget-pkgs` repository — that's a separate manual PR to an external repo, up to whoever maintains this fork to do when they're ready. Today this manifest gets you `winget install --manifest <path>` against a local checkout, not a plain `winget install agent-rdp` from a fresh machine.
- Not yet validated end-to-end against a real `winget install` — treat it as a starting point to verify, not a guarantee.

### From source

See [Build](#build).

## Usage

> Installed via winget or a Release zip? Use `rdp2exec.exe` in place of `python src/launcher/rdp2exec.py` in every example below — same arguments, same behavior, just a standalone exe instead of a source-tree script.

```powershell
# Login shell: PowerShell (default)
python src/launcher/rdp2exec.py user@hostname

# Login shell: CMD
python src/launcher/rdp2exec.py user@hostname cmd

# Single command, clean separated stdout/stderr + exit code -- the mode an AI agent should use
python src/launcher/rdp2exec.py user@hostname powershell Get-Process

python src/launcher/rdp2exec.py user@hostname cmd ipconfig /all

# Non-default port
python src/launcher/rdp2exec.py -p 3390 user@hostname

# Password via argument (or set RDP_PASSWORD)
python src/launcher/rdp2exec.py -P 'secret' user@hostname powershell

# Structured JSON result (single object) -- easiest for a tool layer to parse
python src/launcher/rdp2exec.py --json user@hostname powershell Get-Service Spooler

# Fan one command out across several targets, in parallel (JSON array result)
python src/launcher/rdp2exec.py user@h1,user@h2,user@h3 cmd hostname

# Unattended: read the password from a stored Windows Credential Manager entry
python src/launcher/rdp2exec.py --credential-target my-rdp-box user@hostname cmd whoami
```

`command...` triggers single-command mode (non-interactive, plain-pipe I/O). Omit it to get an interactive ConPTY-backed shell.

### Using this as an AI agent tool

For programmatic/agent use, invoke with a single command and no interactive shell:

```powershell
rdp2exec.exe user@host powershell Get-Service -Name Spooler
```

- stdout and stderr arrive as separate byte streams (no ANSI/terminal control sequences mixed in, since single-command mode bypasses ConPTY).
- The process's exit code is the remote command's exit code.
- Wrap this invocation as a tool call that shells out to it, captures stdout/stderr, and returns them structured — or just use `--json` (below) and parse one object.

#### `--json`: one structured object

`--json` (single-command mode only) buffers the run and prints a single JSON object instead of streaming raw bytes:

```json
{"target": "user@host", "exit_code": 0, "stdout": "...", "stderr": "...", "error": null, "error_detail": null}
```

- `exit_code` is the remote command's exit code (the process also exits with it; when an `error` is set but no exit code was reported, the process exits non-zero).
- `error` is `null` on success, or one of the stable slugs below; `error_detail` carries the original bridge text/number for humans.
- Note: `--json` holds the full output in memory — intended for command results, not for arbitrarily large streams.

#### Stable error vocabulary

Rather than making an agent branch on raw bridge text or numeric codes, `error` uses a small closed set (`error_detail` preserves the original):

| `error` slug | Meaning |
|---|---|
| `conpty_unavailable` | The target couldn't create a pseudo console (ConPTY missing/failed). |
| `pipe_setup_failed` | A stdin/stdout/stderr pipe couldn't be created on the target. |
| `process_setup_failed` | Process/attribute-list setup failed before spawning the command. |
| `process_spawn_failed` | `CreateProcessW` for the command itself failed on the target. |
| `remote_error` | Some other remote error (see `error_detail`). |
| `connect_timeout` | The remote bridge never connected back within `--accept-timeout`. |
| `channel_dropped` | The channel closed before an exit code was reported. |
| `local_setup_error` | A local prerequisite (plugin DLL / bridge exe) was missing. |
| `auth_required` | No password could be resolved for an unattended run. |

#### Multiple targets

Pass a comma-separated list as the target (`user@h1,user@h2`) or `--targets-file <path>` (one `user@host` per line, `#` comments allowed) to run the same command across many hosts in parallel. Multi-target mode requires a command (no interactive shell), runs up to `--max-parallel` (default 4) at once — each with its own isolated session — and always prints a **JSON array** of the per-target result objects (in input order). The process exits `0` only if every target succeeded.

#### Credentials for unattended use

Beyond `-P`/`RDP_PASSWORD`/interactive prompt, the password can come from the **Windows Credential Manager**:

```powershell
# Store once (uses the target username), then run unattended later with no -P:
rdp2exec.exe --credential-target my-rdp-box --save-credential -P 'secret' user@host cmd whoami
rdp2exec.exe --credential-target my-rdp-box user@host cmd whoami
```

Resolution order: `-P/--password` → `RDP_PASSWORD` → `--credential-target` (Credential Manager) → interactive prompt.

## Architecture

```
[Windows host running the AI agent]                    [Windows RDP target]
  rdp2exec.py (launcher)                                  rdp2exec_bridge.exe
    - spawns wfreerdp.exe                                   (runs directly from
    - /dvc:rdp2exec  /drive:r2e,<local temp dir>              \\tsclient\r2e\...,
    - /shell:"cmd.exe /d /c ..."                               never copied to
    - /shell-dir:\\tsclient\r2e                                the target's disk)
    - TCP loopback <-> DVC bytes
        |
        | DVC "rdp2exec" (framed protocol, src/common/protocol.hpp)
        v
  rdp2exec-client.dll (FreeRDP plugin)
    - Dynamic Virtual Channel handler
    - bridges DVC bytes <-> TCP loopback
```

1. **Launcher** (`src/launcher/rdp2exec.py`) spawns `wfreerdp.exe` with device redirection (`/drive:`) pointing at a local temp directory containing the bridge executable (and, for single-command mode, a small generated `.ps1`/`.cmd` script), a Dynamic Virtual Channel (`/dvc:rdp2exec`), and an **Alternate Shell** command line (`/shell:`) that waits for the redirected drive to mount and then runs the bridge straight off it.
2. **FreeRDP client plugin** (`src/plugin/rdp2exec_client.cpp`, built as `rdp2exec-client.dll`) opens the `rdp2exec` DVC and relays bytes to/from a TCP loopback socket the launcher listens on.
3. **Windows bridge** (`src/windows/rdp2exec_bridge.cpp`) runs on the target, opens the DVC server-side (`WTSVirtualChannelOpenEx`), and either:
   - creates a ConPTY-backed interactive shell (no `--command-file`), or
   - runs a single command through plain stdin/stdout/stderr pipes (`--command-file` given), streaming stdout and stderr back as **separate** frames plus a final exit-code frame — the mode used for agent/single-command invocations.

### Zero remote disk writes

The only thing that ever touches the target host's local filesystem is whatever the *command you run* does. The tool itself does not:
- copy the bridge executable to the target (it runs directly from the FreeRDP-redirected `\\tsclient\...` UNC path),
- write any wrapper `.bat`/`.cmd` file to the target (the retry/bootstrap logic is passed inline via `/shell:`, not staged as a file),
- leave the per-command PowerShell/CMD script on the target (it's likewise read straight from the redirected drive).

Everything staged for a session lives in an ephemeral temp directory on the **client** (the machine running `rdp2exec.py`), for the lifetime of that RDP connection.

## Build

Prerequisites:
- A C++ toolchain: Visual Studio Build Tools ("Desktop development with C++") or mingw-w64, plus CMake and Ninja.
- [vcpkg](https://github.com/microsoft/vcpkg) — `scripts/build.ps1` bootstraps a local copy automatically if `VCPKG_ROOT` isn't set.
- Python 3.10+, to run `src/launcher/rdp2exec.py` from source (not needed if you're only using the packaged `rdp2exec.exe`).

```powershell
./scripts/build.ps1
```

This bootstraps vcpkg, installs FreeRDP (`client` feature) via `vcpkg.json`, builds `rdp2exec-client.dll` and `rdp2exec_bridge.exe` via `CMakeLists.txt`, and stages everything into `./artifacts` alongside a copy of `wfreerdp.exe` and its runtime DLLs.

> **Note:** FreeRDP's Windows client loads Dynamic Virtual Channel plugins from an addin search path whose exact layout can vary by FreeRDP version/build. `build.ps1` stages `rdp2exec-client.dll` next to `wfreerdp.exe` in `./artifacts`, which covers the common "same directory as the client" convention — if your `wfreerdp.exe` doesn't pick it up from there, check your build's addin directory and copy the DLL there too.

## Security / detection note

Carried over from upstream: the server-side helper process, RDP-based command bridging, and remote process execution used here can resemble malware behavior to antivirus/EDR products, even though no files are persisted on the target. This tool is intended for legitimate administrative, testing, and research use against systems you're authorized to manage.

## Further reading

[`MIGRATION_NOTES.md`](MIGRATION_NOTES.md) has a file-by-file breakdown of the port from upstream, the assumptions made without a Windows toolchain/RDP target to verify against, and a longer list of possible follow-ups.

## License

MIT, same as upstream [blacknon/rdp2exec](https://github.com/blacknon/rdp2exec).
