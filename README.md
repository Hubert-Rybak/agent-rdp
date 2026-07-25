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

### uv

The launcher (`src/launcher/rdp2exec.py`) is a single, **stdlib-only** Python script (3.10+, no third-party packages), so [`uv`](https://github.com/astral-sh/uv) is the quickest way to run it from a source checkout without managing a Python install yourself — `uv` provisions the interpreter and runs the script in one step:

```powershell
# Install uv (if you don't have it)
winget install astral-sh.uv

# From the repo root: uv fetches Python 3.10+ as needed and runs the launcher
uv run --python 3.10 src/launcher/rdp2exec.py user@host cmd whoami
```

There are no dependencies to install (`uv pip install` / a `pyproject.toml` aren't needed) — `uv run` just gives you a known-good Python for the script.

Note: `uv` only covers the **Python launcher**. The tool still needs the two native binaries it drives — `rdp2exec-client.dll` and `rdp2exec_bridge.exe` — plus `wfreerdp.exe`, which come from a [pre-built release](#pre-built-release) or a [source build](#from-source). Put them where the launcher can find them (the release/build layout already does this), then use `uv run` in place of a bare `python` for the launcher itself.

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

### End-to-end walkthrough

What actually happens between typing `rdp2exec.exe user@host cmd whoami` and getting output back:

1. **Resolve and validate (client).** The launcher parses the target(s), resolves the password (`-P` → `RDP_PASSWORD` → Credential Manager → interactive prompt), and confirms the two local binaries it needs exist: the FreeRDP plugin (`rdp2exec-client.dll`) and the bridge exe (`rdp2exec_bridge.exe`). Anything missing here fails fast as `local_setup_error` before a connection is attempted.
2. **Open a loopback listener (client).** `LoopbackSocketServer` binds `127.0.0.1:0` — the OS picks a free port — and starts listening for exactly one connection. The chosen `host:port` is exported as the `RDP2EXEC_SOCKET` environment variable, which is how the plugin (below) will find its way back to this launcher instance. A random OS-assigned port per run is what makes concurrent multi-target sessions safe — no two collide.
3. **Stage the payload into a redirected drive (client).** The bridge exe is copied into an ephemeral client-side temp dir, and — in single-command mode — a small `.ps1` or `.cmd` script wrapping your command is written next to it. That temp dir is handed to `wfreerdp.exe` as a redirected drive (`/drive:r2e,<tempdir>`), so the target will see its contents at `\\tsclient\r2e\...`. These files live on the **client's** disk; the target only ever reads them across the RDP device-redirection channel.
4. **Launch the RDP client with an Alternate Shell (client).** `wfreerdp.exe` is spawned with the target/credentials, the redirected drive, a Dynamic Virtual Channel registration (`/dvc:rdp2exec`), and an **Alternate Shell** command line (`/shell:` + `/shell-dir:`). The password is fed to `wfreerdp` over stdin (`/from-stdin:force`) rather than the command line. See [Bootstrapping without GUI automation](#bootstrapping-without-gui-automation) for what that shell command actually does.
5. **Session logon runs the bridge (target).** On logon, RDP runs the Alternate Shell command instead of the normal desktop shell. That command polls for the redirected drive to mount, then executes `rdp2exec_bridge.exe` straight off `\\tsclient\r2e\` — no copy to local disk.
6. **The bridge opens the channel from the inside (target).** The bridge calls `WTSVirtualChannelOpenEx(WTS_CURRENT_SESSION, "rdp2exec", …DYNAMIC)` to open the server end of the `rdp2exec` DVC from within the RDP session.
7. **The plugin bridges DVC ↔ loopback (client).** Inside `wfreerdp.exe`, FreeRDP loads `rdp2exec-client.dll` as the handler for the `rdp2exec` DVC. When the channel opens, the plugin reads `RDP2EXEC_SOCKET`, connects a TCP socket back to the launcher's loopback listener (retrying while the connection is refused), and from then on shuttles raw bytes both ways: DVC → socket, and socket → DVC.
8. **The launcher accepts the connection (client).** `server.accept(timeout=--accept-timeout)` (default 60s) unblocks the moment the plugin connects. Now there is a continuous byte pipe: **launcher ⇄ loopback socket ⇄ plugin ⇄ DVC ⇄ bridge**. Everything above this point was setup; everything below is framed protocol over that pipe.
9. **Run and stream (both ends).** The bridge spawns the child shell/command and relays its I/O back as [protocol frames](#the-framed-wire-protocol); the launcher decodes them, writing output to your stdout/stderr (or buffering it for `--json`). A final exit-code frame ends the run, and the launcher exits with that code.

### The framed wire protocol

The byte pipe carries a simple length-prefixed framing shared by both ends (`src/common/protocol.hpp`, and mirrored in the launcher's `FrameParser`/`send_frame`). Every frame is:

```
+--------+------------------------+-----------------+
| type   | length (uint32, LE)    | payload         |
| 1 byte | 4 bytes                | `length` bytes  |
+--------+------------------------+-----------------+
```

| Frame | Value | Direction | Meaning |
|---|---|---|---|
| `kInput` | `0x01` | launcher → bridge | stdin bytes (keystrokes, or piped input) for the child |
| `kResize` | `0x02` | launcher → bridge | new terminal size (`cols`, `rows` as two uint16 LE); resizes the ConPTY |
| `kClose` | `0x03` | launcher → bridge | detach/terminate request |
| `kReady` | `0x81` | bridge → launcher | child spawned, channel live (launcher nudges an interactive shell with a `\r` to draw its first prompt) |
| `kOutput` | `0x82` | bridge → launcher | stdout bytes (in ConPTY mode this is the merged terminal stream) |
| `kExit` | `0x83` | bridge → launcher | child exited; payload is its exit code (uint32 LE) |
| `kError` | `0x84` | bridge → launcher | a remote-side setup failure, as free text (folded into the [stable error slugs](#stable-error-vocabulary)) |
| `kOutputErr` | `0x85` | bridge → launcher | stderr bytes — **pipe mode only**; ConPTY mode never emits this |

One detail specific to the DVC transport: FreeRDP delivers DVC data to the bridge prefixed with an 8-byte channel PDU header, which the bridge strips (`kChannelPduLength`) before feeding the remainder to its frame parser. The launcher↔plugin loopback leg carries the raw frame bytes with no such header, because the plugin passes socket bytes through to the channel verbatim.

### Execution modes: ConPTY vs. pipe

The bridge chooses one of two modes based on whether it was given a `--command-file`, and the two behave deliberately differently:

**Interactive (ConPTY) mode** — no command supplied. The bridge creates a real Windows pseudo console (`CreatePseudoConsole`) and attaches the child shell to it via a process-thread attribute list. This gives a genuine terminal: stdout and stderr are **merged** into one stream carrying ANSI/VT control sequences, `kResize` frames retune the console dimensions, and the launcher puts the local console into raw VT pass-through mode so keystrokes flow through unmodified. This is what you get for an interactive shell session — good for a human, awkward for a program to parse.

**Pipe mode** — a `--command-file` is supplied (any single-command / agent invocation). The bridge wires the child up to three plain anonymous pipes with `CREATE_NO_WINDOW` and **no** pseudo console, so:

- stdout and stderr stay **separate** (`kOutput` vs. `kOutputErr`), each free of terminal control sequences;
- the child's real exit code comes back in the `kExit` frame;
- there's nothing to resize, so `kResize` is ignored.

That clean separation is exactly what makes single-command mode parseable, and it's the basis for `--json` (the launcher just buffers the two streams and the exit code into one object instead of streaming them live). The command itself is never passed as a raw string to be re-quoted on the target: the launcher generates a script file (`build_command_script`) with per-shell quoting — PowerShell single-quote literals, or `cmd` token quoting — that propagates `$LASTEXITCODE`/`%ERRORLEVEL%` back out as the process exit code.

### Bootstrapping without GUI automation

The one genuinely non-obvious mechanism is how the bridge gets launched on the target without any visible-desktop interaction. Upstream `rdp2exec` simulated `Win+R` and typed into the Run dialog over X11; this fork uses RDP's native **Alternate Shell** (a.k.a. Initial Program) feature instead. `build_alternate_shell` emits a command line like:

```bat
cmd.exe /d /c "for /l %i in (1,1,20) do (if exist \\tsclient\r2e\rdp2exec_bridge.exe (\\tsclient\r2e\rdp2exec_bridge.exe --channel rdp2exec --child cmd --cols 120 --rows 40 --command-file \\tsclient\r2e\rdp2exec-command.cmd & exit /b) else (ping -n 2 127.0.0.1>nul))"
```

- RDP runs this in place of the normal shell at logon — headless, no desktop automation, no keystroke injection.
- The `for /l` loop exists because **device redirection can lag a moment behind logon**: the `\\tsclient\` drive isn't guaranteed mounted the instant the shell starts. The loop retries `--drive-poll-timeout` times (default 20), sleeping ~1s each miss (`ping -n 2`), until the bridge exe appears, then runs it and exits.
- The drive name and staged filenames are kept short and space-free by construction so the whole command needs no inner quoting and stays well under RDP's historical AlternateShell field length limit.
- `/shell-dir:\\tsclient\r2e` sets the working directory to the redirected drive so the bridge starts there.

> **Note:** the timing of that drive-mount race (how long redirection actually lags) is one of the port's [unverified assumptions](MIGRATION_NOTES.md#known-limitations--unverified-assumptions) — the retry loop is the safety margin, and `--drive-poll-timeout` is the knob if a slow target needs longer.

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
