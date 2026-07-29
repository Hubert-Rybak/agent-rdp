---
name: agent-rdp
description: >-
  Run cmd/PowerShell commands on a remote Windows host over RDP using the
  agent-rdp launcher, without opening extra ports or writing files to the
  target's disk. Use this whenever the user wants to execute a command,
  script, or check on a remote Windows machine over RDP — e.g. "run whoami on
  the RDP box", "check the Spooler service on WIN-SERVER01", "restart IIS on
  those three hosts", "grab ipconfig from user@10.0.0.5" — and whenever you
  need to programmatically shell into a Windows target from a Windows host.
  Trigger even when the user names RDP, wfreerdp, a Windows hostname, or a
  remote command without saying "agent-rdp" by name.
---

# agent-rdp

`agent-rdp` is a Windows-to-Windows tool that runs `cmd`/PowerShell commands
on a remote Windows host over an RDP connection. It needs no extra management
port and writes nothing to the target's local disk — the payload runs off a
client-redirected drive and command output flows back over an RDP virtual
channel. This skill covers how to drive it as a programmatic tool.

## When to reach for this

Use `agent-rdp` for one-shot command execution against a Windows target: run a
command, capture its output and exit code, move on. This is the mode built for
agents and scripts. Reach for the interactive shell only when a human wants to
sit at a live prompt.

Prerequisite: `agent-rdp` runs **from a Windows host** and drives three native
binaries (`wfreerdp.exe`, `agent-rdp-client.dll`, `agent-rdp-bridge.exe`). If
these aren't present it fails fast before connecting — see
[When it can't run locally](#when-it-cant-run-locally).

## Invocation basics

The command form is:

```
agent-rdp [options] <target> [child] [command...]
```

- `<target>` — `user@host` (or a comma-separated list for
  [multiple targets](#running-across-many-targets)).
- `child` — `powershell` (default) or `cmd`. Pick the shell the command is
  written for.
- `command...` — everything after the shell is the command to run. **Supplying
  a command is what selects single-command mode** (non-interactive, clean
  separated stdout/stderr + real exit code). Omit it and you get an
  interactive terminal instead, which a program can't parse.

If `agent-rdp` isn't on `PATH` (i.e. you're in a source checkout rather than a
winget/release install), substitute `python src/launcher/agent_rdp.py` for
`agent-rdp` in every example below — identical arguments and behavior.

### The two golden rules for agent use

1. **Always pass a command.** No trailing command = interactive ConPTY shell =
   merged output with terminal escape codes and no exit code. That's for
   humans, not tools.
2. **Prefer `--json`.** It buffers the run and prints one object you can parse,
   instead of streaming raw bytes you have to reassemble.

```powershell
agent-rdp --json user@host powershell Get-Service -Name Spooler
```

## Parsing results with `--json`

`--json` (single-command mode only) prints exactly one object:

```json
{"target": "user@host", "exit_code": 0, "stdout": "...", "stderr": "...", "error": null, "error_detail": null}
```

- `exit_code` is the remote command's own exit code. The launcher process also
  exits with it, so you can branch on either the JSON field or `$?`.
- `error` is `null` on success, or one of the stable slugs in the
  [error vocabulary](#error-vocabulary) below. `error_detail` keeps the raw
  bridge text/number for logging.
- `--json` holds the whole output in memory — great for command results, not
  for arbitrarily large streams. For huge output, drop `--json` and consume the
  separate stdout/stderr byte streams directly.

Decision flow for reading a result:
1. `error` is non-null → the run failed to execute cleanly; branch on the slug.
2. `error` is null → the command ran; judge success by `exit_code` and the two
   streams like any local process.

## Error vocabulary

`error` is a small closed set so you never have to pattern-match raw bridge
text. Branch on these; surface `error_detail` to a human when useful.

| slug | Meaning | Typical response |
|---|---|---|
| `conpty_unavailable` | Target couldn't create a pseudo console. | Retry; check target health. |
| `pipe_setup_failed` | A stdin/stdout/stderr pipe couldn't be created on the target. | Retry; report if persistent. |
| `process_setup_failed` | Process/attribute-list setup failed before spawning. | Retry; report if persistent. |
| `process_spawn_failed` | `CreateProcessW` for your command failed on the target. | Check the command/shell is valid on the target. |
| `remote_error` | Some other remote error. | Read `error_detail`. |
| `connect_timeout` | Remote bridge never connected back in time. | Raise `--accept-timeout`; check RDP reachability/creds. |
| `channel_dropped` | Channel closed before an exit code arrived. | Retry; the session died mid-run. |
| `local_setup_error` | A local binary (plugin DLL / bridge exe) was missing. | See [When it can't run locally](#when-it-cant-run-locally). |
| `auth_required` | No password could be resolved for an unattended run. | Provide a password — see [Passwords](#passwords-and-unattended-use). |

## Passwords and unattended use

Resolution order: `-P/--password` → `RDP_PASSWORD` env var →
`--credential-target` (Windows Credential Manager) → interactive prompt.

For unattended/agent runs there is **no human to answer a prompt**, so a
password must resolve from one of the first three sources or the run fails with
`auth_required`. Prefer not to put secrets on the command line where they land
in process listings and shell history:

```powershell
# Best for automation: pass the secret via the environment, not argv
$env:RDP_PASSWORD = '...'; agent-rdp --json user@host cmd whoami

# Or store once in Windows Credential Manager, then run unattended with no -P:
agent-rdp --credential-target my-rdp-box --save-credential -P 'secret' user@host cmd whoami
agent-rdp --credential-target my-rdp-box user@host cmd whoami
```

Other connection options: `-p/--port` (default 3389), `-d/--domain`.

## Running across many targets

Fan one command out across hosts in parallel by passing a comma-separated
target or `--targets-file`:

```powershell
agent-rdp user@h1,user@h2,user@h3 cmd hostname
agent-rdp --targets-file hosts.txt --max-parallel 8 powershell "Get-Service W32Time"
```

- Multi-target mode **requires a command** (no interactive shell) and **always
  emits a JSON array** of per-target result objects, in input order — you don't
  need `--json` for the array, but each element has the same shape as the
  single `--json` object.
- Runs up to `--max-parallel` at once (default 4), each in its own isolated
  session.
- The process exits `0` only if **every** target succeeded, so check per-target
  `exit_code`/`error` rather than trusting the overall exit alone.
- `--targets-file` is one `user@host` per line; blank lines and `#` comments
  are ignored.

## Quoting the command

Everything after the shell name is the command. The launcher generates a
per-shell script with correct quoting and propagates the remote exit code
(`$LASTEXITCODE` for PowerShell, `%ERRORLEVEL%` for cmd) back out — so you
don't hand-escape for the target. Your job is just to get the command past your
**local** shell intact. When a command contains spaces, quotes, or shell
metacharacters, wrap the whole thing so your local shell passes it as intended:

```powershell
agent-rdp --json user@host powershell "Get-ChildItem 'C:\Program Files' | Measure-Object"
```

Match the shell to the syntax: use `powershell` for cmdlets/`.ps1` idioms,
`cmd` for classic `ipconfig`/`dir`/batch syntax.

## When it can't run locally

`agent-rdp` needs a Windows host plus three native binaries. If you get
`local_setup_error` (or the launcher refuses to start), the engine binaries
aren't discoverable. They live in the repo's `artifacts/` after a build
(`scripts/build.ps1`), or ship inside a winget/release install. Point the
launcher at them explicitly if they're outside the tree:

- `--plugin-dir` / `AGENT_RDP_PLUGIN_DIR` → directory holding `agent-rdp-client.dll`
- `--helper-exe` / `AGENT_RDP_HELPER_EXE` → path to `agent-rdp-bridge.exe`
- `--wfreerdp` / `WFREERDP` → path to `wfreerdp.exe`

If you're not on Windows at all, this tool cannot run — say so plainly rather
than fabricating output.

## Debugging a stuck connection

- `connect_timeout` on a target you believe is reachable: the redirected drive
  may be slow to mount at logon. Raise `--drive-poll-timeout` (remote retry
  window, default 20s) and/or `--accept-timeout` (default 60s).
- Add `--debug` to surface launcher diagnostics when a run misbehaves.

## Quick reference

| Goal | Command |
|---|---|
| One command, parseable | `agent-rdp --json user@host powershell <cmd>` |
| Classic cmd command | `agent-rdp --json user@host cmd ipconfig /all` |
| Many hosts in parallel | `agent-rdp user@h1,user@h2 cmd hostname` (JSON array) |
| Hosts from a file | `agent-rdp --targets-file hosts.txt cmd hostname` |
| Unattended password | `RDP_PASSWORD` env var or `--credential-target <name>` |
| Interactive shell (human) | `agent-rdp user@host` (no command) |

For how the transport actually works (Alternate Shell bootstrap, DVC↔loopback
bridging, the framed wire protocol), see the repo `README.md`.
