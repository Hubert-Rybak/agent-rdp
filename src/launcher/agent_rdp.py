#!/usr/bin/env python3
"""Windows-to-Windows launcher: drives a Windows RDP target's cmd/PowerShell
from a Windows client host, via wfreerdp.exe + the agent-rdp DVC plugin.

Bootstraps execution using RDP's native Alternate Shell (Initial Program)
feature -- no GUI automation, no visible-desktop keystroke injection. The
remote-side bridge executable is run directly off the FreeRDP-redirected
client drive (a \\\\tsclient\\... UNC path); it is never copied onto the
target host's local disk.
"""
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
from __future__ import annotations

import argparse
import concurrent.futures
import ctypes
import getpass
import json
import os
import queue
import shlex
import socket
import struct
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from dataclasses import dataclass
from pathlib import Path

if os.name != "nt":
    print("agent-rdp: this launcher targets Windows clients only.", file=sys.stderr)
else:
    import msvcrt


def set_binary_mode():
    """Windows file descriptors default to text mode, where the CRT rewrites
    \\n to \\r\\n on write and treats byte 0x1A (Ctrl-Z) as EOF on read.
    Both would corrupt raw terminal pass-through (already-CRLF remote output,
    or a literal Ctrl-Z keystroke sent by the user), so stdin/stdout/stderr
    are switched to binary mode before any frame bytes flow through them."""
    if os.name != "nt":
        return
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            msvcrt.setmode(stream.fileno(), os.O_BINARY)
        except (OSError, ValueError):
            pass

def _default_artifacts_dir() -> Path:
    """Where agent-rdp-client.dll / agent-rdp-bridge.exe / wfreerdp.exe are
    expected to live by default.

    In a source checkout that's <repo>/artifacts (populated by
    scripts/build.ps1). When frozen into a standalone exe (PyInstaller, as
    used for the winget package), __file__ no longer reflects the source
    tree -- sys.executable's own directory is the install directory, and
    the winget zip lays everything out flat alongside agent-rdp.exe there.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2] / "artifacts"


DEFAULT_ARTIFACTS_DIR = _default_artifacts_dir()
DEFAULT_PLUGIN_DIR = os.environ.get("AGENT_RDP_PLUGIN_DIR", str(DEFAULT_ARTIFACTS_DIR))
DEFAULT_PLUGIN_NAME = "agent-rdp-client.dll"
# Prefer a wfreerdp.exe staged alongside our own binaries (source-tree
# artifacts/, or the flat winget/PyInstaller install layout) over relying on
# PATH, but still fall back to PATH resolution if one isn't found there.
_bundled_wfreerdp = DEFAULT_ARTIFACTS_DIR / "wfreerdp.exe"
DEFAULT_WFREERDP = str(_bundled_wfreerdp) if _bundled_wfreerdp.exists() else "wfreerdp.exe"
DEFAULT_DRIVE_NAME = "r2e"
DEFAULT_HELPER_EXE = str(DEFAULT_ARTIFACTS_DIR / "agent-rdp-bridge.exe")
POWERSHELL_EXECUTION_POLICIES = (
    "default",
    "allsigned",
    "remotesigned",
    "restricted",
    "unrestricted",
    "bypass",
)

FRAME_INPUT = 0x01
FRAME_RESIZE = 0x02
FRAME_CLOSE = 0x03
FRAME_READY = 0x81
FRAME_OUTPUT = 0x82
FRAME_EXIT = 0x83
FRAME_ERROR = 0x84
FRAME_OUTPUT_ERR = 0x85


# ---------------------------------------------------------------------------
# Stable error vocabulary (follow-up #7).
#
# The remote-side bridge reports failures two ways: free-text kError frames
# (see src/windows/agent_rdp_bridge.cpp) and numeric process exit codes. Raw
# text and numbers are awkward for an agent to branch on, so we fold them into
# a small, closed set of slugs. `error_detail` always carries the original
# text/number, so nothing is lost for a human debugging.
# ---------------------------------------------------------------------------
ERR_CONPTY_UNAVAILABLE = "conpty_unavailable"
ERR_PIPE_SETUP_FAILED = "pipe_setup_failed"
ERR_PROCESS_SETUP_FAILED = "process_setup_failed"
ERR_PROCESS_SPAWN_FAILED = "process_spawn_failed"
ERR_REMOTE = "remote_error"
ERR_CONNECT_TIMEOUT = "connect_timeout"
ERR_CHANNEL_DROPPED = "channel_dropped"
ERR_LOCAL_SETUP = "local_setup_error"
ERR_AUTH_REQUIRED = "auth_required"

# Matched by substring against the bridge's kError text, most-specific first.
_ERROR_TEXT_RULES: list[tuple[str, str]] = [
    ("ConPTY API unavailable", ERR_CONPTY_UNAVAILABLE),
    ("CreatePseudoConsole", ERR_CONPTY_UNAVAILABLE),
    ("CreatePipe(", ERR_PIPE_SETUP_FAILED),
    ("HeapAlloc(", ERR_PROCESS_SETUP_FAILED),
    ("InitializeProcThreadAttributeList", ERR_PROCESS_SETUP_FAILED),
    ("UpdateProcThreadAttribute", ERR_PROCESS_SETUP_FAILED),
    ("CreateProcessW", ERR_PROCESS_SPAWN_FAILED),
]


def classify_error(text: str) -> str:
    """Map a bridge kError text to a stable slug an agent can branch on.

    Unknown text falls through to ERR_REMOTE (the raw text is preserved by the
    caller in `error_detail`), so a future bridge message never crashes a
    consumer -- it just reads as a generic remote error until we add a rule.
    """
    for needle, slug in _ERROR_TEXT_RULES:
        if needle in text:
            return slug
    return ERR_REMOTE


@dataclass
class CommandResult:
    """Structured outcome of a single-command run, shared by the JSON path
    (#2), the error vocabulary (#7), and the multi-target aggregator (#6)."""

    target: str = ""
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    error_detail: str | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None or self.exit_code != 0

    def process_exit_code(self) -> int:
        """Exit code to return to the OS: the child's own code, but never 0
        when an error was reported (setup failures send a kError with no exit
        frame, leaving exit_code at its 0 default)."""
        if self.error is not None and self.exit_code == 0:
            return 1
        return self.exit_code

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
            "error_detail": self.error_detail,
        }


def debug_print(enabled: bool, *parts, **kwargs):
    if enabled:
        print(*parts, **kwargs)


def write_text(path: Path, text: str):
    path.write_text(text, encoding="utf-8", newline="\r\n")


def ensure_plugin(plugin_dir: str, plugin_name: str) -> Path:
    path = Path(plugin_dir) / plugin_name
    if not path.exists():
        raise FileNotFoundError(f"plugin not found: {path}")
    return path


def ensure_helper(helper_path: str) -> Path:
    path = Path(helper_path)
    if not path.exists():
        raise FileNotFoundError(f"Windows helper not found: {path}")
    return path


# ---------------------------------------------------------------------------
# Windows Credential Manager integration (follow-up #4).
#
# For unattended agent use, a password can be stored once in the Windows
# Credential Manager (a generic credential keyed by an arbitrary target name)
# and read back on later runs without ever passing -P or setting RDP_PASSWORD.
# Implemented via ctypes against advapi32 (CredReadW/CredWriteW/CredFree); the
# whole module stays importable on non-Windows (these functions just raise).
# ---------------------------------------------------------------------------
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", ctypes.c_uint64),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


def _require_windows(feature: str):
    if os.name != "nt":
        raise RuntimeError(f"{feature} requires Windows (Credential Manager is a Win32 feature).")


def read_windows_credential(target_name: str) -> tuple[str | None, str]:
    """Read a generic credential from Windows Credential Manager.

    Returns (username_or_None, password). Raises RuntimeError if the target is
    not found or on any Win32 error. The credential blob is stored as UTF-16LE
    (the convention CredWriteW below uses)."""
    _require_windows("--credential-target")
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    cred_ptr = ctypes.POINTER(_CREDENTIAL)()
    ok = advapi32.CredReadW(ctypes.c_wchar_p(target_name), CRED_TYPE_GENERIC, 0,
                            ctypes.byref(cred_ptr))
    if not ok:
        err = ctypes.get_last_error()
        raise RuntimeError(f"CredReadW failed for target {target_name!r} (error {err}).")
    try:
        cred = cred_ptr.contents
        size = cred.CredentialBlobSize
        blob = ctypes.string_at(cred.CredentialBlob, size) if size else b""
        password = blob.decode("utf-16-le", errors="replace")
        username = cred.UserName or None
        return username, password
    finally:
        advapi32.CredFree(cred_ptr)


def write_windows_credential(target_name: str, username: str, password: str) -> None:
    """Store a generic credential in Windows Credential Manager under
    target_name, so later runs can resolve the password unattended."""
    _require_windows("--save-credential")
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    blob = password.encode("utf-16-le")
    blob_buf = (ctypes.c_byte * len(blob)).from_buffer_copy(blob) if blob else None
    cred = _CREDENTIAL()
    cred.Flags = 0
    cred.Type = CRED_TYPE_GENERIC
    cred.TargetName = target_name
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(blob_buf, ctypes.POINTER(ctypes.c_byte)) if blob_buf else None
    cred.Persist = CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = username or None
    ok = advapi32.CredWriteW(ctypes.byref(cred), 0)
    if not ok:
        err = ctypes.get_last_error()
        raise RuntimeError(f"CredWriteW failed for target {target_name!r} (error {err}).")


def resolve_password(args) -> str:
    if args.password:
        return args.password
    if os.environ.get("RDP_PASSWORD"):
        # Kept explicit even though argparse also defaults --password from this,
        # so the precedence is obvious and stable regardless of how -P was set.
        return os.environ["RDP_PASSWORD"]
    if getattr(args, "credential_target", ""):
        _, password = read_windows_credential(args.credential_target)
        if password:
            return password
    if sys.stdin.isatty() and sys.stderr.isatty():
        return getpass.getpass("RDP password: ")
    raise RuntimeError(
        "RDP password is required. Provide -P/--password, set RDP_PASSWORD, "
        "use --credential-target with a stored Windows credential, or run from "
        "an interactive terminal for prompt input."
    )


def parse_target(value: str) -> tuple[str, str]:
    username, sep, host = value.rpartition("@")
    if not sep or not username or not host:
        raise argparse.ArgumentTypeError(
            "target must be specified as user@hostname"
        )
    return username, host


def get_terminal_size() -> tuple[int, int]:
    try:
        cols, rows = os.get_terminal_size(sys.stdin.fileno())
        return cols, rows
    except OSError:
        return 120, 40


def quote_powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def quote_cmd_token(value: str) -> str:
    operators = {"&", "&&", "||", "|", ">", ">>", "<", "2>", "2>>", "1>", "1>>"}
    if value in operators:
        return value
    if any(ch.isspace() or ch in '&|<>^()"%' for ch in value):
        return '"' + value.replace('"', '""') + '"'
    return value


def build_command_script(child: str, command: list[str]) -> tuple[str, str]:
    if not command:
        raise ValueError("command must not be empty")

    if child == "powershell":
        invocation = " ".join(quote_powershell_literal(part) for part in command)
        script = textwrap.dedent(
            f"""
            $ErrorActionPreference = 'Stop'
            & {invocation}
            if ($null -ne $LASTEXITCODE) {{
                exit $LASTEXITCODE
            }}
            exit 0
            """
        ).strip() + "\r\n"
        return "agent-rdp-command.ps1", script

    if child == "cmd":
        invocation = " ".join(quote_cmd_token(part) for part in command)
        script = textwrap.dedent(
            f"""
            @echo off
            setlocal enableextensions
            {invocation}
            exit /b %ERRORLEVEL%
            """
        ).strip() + "\r\n"
        return "agent-rdp-command.cmd", script

    raise ValueError("child must be powershell or cmd")


def build_alternate_shell(drive_name: str, child: str, cols: int, rows: int, command_file: str,
                          poll_attempts: int, powershell_execution_policy: str = "default") -> str:
    """Build the RDP AlternateShell (Initial Program) command line.

    Polls for the FreeRDP-redirected client drive to be mounted (device
    redirection can lag a moment behind session logon), then runs the bridge
    executable directly from that UNC path -- it is never copied onto the
    target's local disk. Drive name and staged filenames are kept short and
    space-free by construction so no inner quoting is needed, keeping this
    well under the RDP protocol's AlternateShell field size expectations.
    """
    child = child.lower().strip()
    if child not in {"powershell", "cmd"}:
        raise ValueError("child must be powershell or cmd")
    powershell_execution_policy = powershell_execution_policy.lower().strip()
    if powershell_execution_policy not in POWERSHELL_EXECUTION_POLICIES:
        raise ValueError("invalid PowerShell execution policy")
    if child != "powershell" and powershell_execution_policy != "default":
        raise ValueError("PowerShell execution policy only applies to the powershell child")
    if not command_file and powershell_execution_policy != "default":
        raise ValueError("PowerShell execution policy override requires a one-shot PowerShell command")

    exe_unc = rf"\\tsclient\{drive_name}\agent-rdp-bridge.exe"
    bridge_args = f"--channel agent-rdp --child {child} --cols {cols} --rows {rows}"
    if command_file:
        bridge_args += f" --command-file {command_file}"
        if powershell_execution_policy != "default":
            bridge_args += f" --powershell-execution-policy {powershell_execution_policy}"

    poll_attempts = max(1, poll_attempts)
    inner = (
        f"for /l %i in (1,1,{poll_attempts}) do "
        f"(if exist {exe_unc} ({exe_unc} {bridge_args} & exit /b) "
        f"else (ping -n 2 127.0.0.1>nul))"
    )
    return f'cmd.exe /d /c "{inner}"'


def prepare_drive_share(base_dir: Path, helper_exe: Path, child: str, drive_name: str, cols: int, rows: int,
                        poll_timeout: float, command: list[str] | None = None,
                        powershell_execution_policy: str = "default") -> tuple[Path, str]:
    """Stage the bridge exe and any per-command script into a client-side
    temp directory that gets redirected to the target over RDP. Nothing here
    touches the target's disk -- both source and destination are local to
    this (client) machine; the target only ever sees these files through the
    virtual \\\\tsclient\\... drive for the lifetime of the RDP session.
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    staged_exe = base_dir / "agent-rdp-bridge.exe"
    staged_exe.write_bytes(helper_exe.read_bytes())

    command_file = ""
    if command:
        command_name, command_text = build_command_script(child, command)
        staged_command = base_dir / command_name
        write_text(staged_command, command_text)
        command_file = rf"\\tsclient\{drive_name}\{command_name}"

    poll_attempts = max(1, int(poll_timeout))
    alternate_shell = build_alternate_shell(
        drive_name,
        child,
        cols,
        rows,
        command_file,
        poll_attempts,
        powershell_execution_policy=powershell_execution_policy,
    )
    return base_dir, alternate_shell


class LoopbackSocketServer:
    """TCP loopback IPC endpoint between the launcher and the FreeRDP client
    plugin DLL, replacing the Unix domain socket used on Linux (Windows has
    no AF_UNIX support in the toolchain this plugin targets)."""

    def __init__(self, host: str = "127.0.0.1"):
        self.host = host
        self.port = 0
        self.server: socket.socket | None = None

    def __enter__(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind((self.host, 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.server:
            self.server.close()

    def accept(self, timeout: float):
        self.server.settimeout(timeout)
        conn, _ = self.server.accept()
        conn.setblocking(False)
        return conn

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"


class FrameParser:
    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes):
        self.buf.extend(data)
        frames = []
        while len(self.buf) >= 5:
            frame_type = self.buf[0]
            length = struct.unpack_from("<I", self.buf, 1)[0]
            if len(self.buf) < 5 + length:
                break
            payload = bytes(self.buf[5:5 + length])
            del self.buf[:5 + length]
            frames.append((frame_type, payload))
        return frames


def send_frame(conn: socket.socket, frame_type: int, payload: bytes = b""):
    conn.sendall(bytes([frame_type]) + struct.pack("<I", len(payload)) + payload)


def build_wfreerdp_command(args, share_dir: Path, username: str, host: str):
    freerdp_log_level = "INFO" if args.debug else "OFF"
    cols, rows = get_terminal_size()
    _, alternate_shell = prepare_drive_share(
        share_dir, args.helper_exe_path, args.child, args.drive_name, cols, rows,
        poll_timeout=args.drive_poll_timeout, command=args.command or None,
        powershell_execution_policy=args.powershell_execution_policy,
    )
    cmd = [
        args.wfreerdp,
        f"/v:{host}",
        f"/port:{args.port}",
        f"/u:{username}",
        "/from-stdin:force",
        "/dvc:agent-rdp",
        f"/drive:{args.drive_name},{share_dir}",
        f"/shell:{alternate_shell}",
        f"/shell-dir:\\\\tsclient\\{args.drive_name}",
        f"/log-level:{freerdp_log_level}",
        "/size:1280x900",
    ]
    if args.enable_clipboard:
        cmd.append("+clipboard")
    if args.domain:
        cmd.append(f"/d:{args.domain}")
    if args.cert_ignore:
        cmd.append("/cert:ignore")
    return cmd


class RawTerminal:
    """Puts the local Windows console into raw byte-passthrough mode using
    the Virtual Terminal console modes, the Windows analogue of POSIX
    termios raw mode. Restores the original mode on exit."""

    STD_INPUT_HANDLE = -10
    STD_OUTPUT_HANDLE = -11
    ENABLE_PROCESSED_INPUT = 0x0001
    ENABLE_LINE_INPUT = 0x0002
    ENABLE_ECHO_INPUT = 0x0004
    ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

    def __init__(self):
        self.is_tty = sys.stdin.isatty()
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if self.is_tty else None
        self.stdin_handle = None
        self.stdout_handle = None
        self.old_in_mode = ctypes.c_uint32(0)
        self.old_out_mode = ctypes.c_uint32(0)

    def __enter__(self):
        if not self.is_tty:
            return self
        self.stdin_handle = self.kernel32.GetStdHandle(self.STD_INPUT_HANDLE)
        self.stdout_handle = self.kernel32.GetStdHandle(self.STD_OUTPUT_HANDLE)
        self.kernel32.GetConsoleMode(self.stdin_handle, ctypes.byref(self.old_in_mode))
        self.kernel32.GetConsoleMode(self.stdout_handle, ctypes.byref(self.old_out_mode))

        new_in_mode = self.old_in_mode.value
        new_in_mode &= ~(self.ENABLE_ECHO_INPUT | self.ENABLE_LINE_INPUT | self.ENABLE_PROCESSED_INPUT)
        new_in_mode |= self.ENABLE_VIRTUAL_TERMINAL_INPUT
        self.kernel32.SetConsoleMode(self.stdin_handle, new_in_mode)

        new_out_mode = self.old_out_mode.value | self.ENABLE_VIRTUAL_TERMINAL_PROCESSING
        self.kernel32.SetConsoleMode(self.stdout_handle, new_out_mode)
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self.is_tty:
            return
        if self.stdin_handle:
            self.kernel32.SetConsoleMode(self.stdin_handle, self.old_in_mode.value)
        if self.stdout_handle:
            self.kernel32.SetConsoleMode(self.stdout_handle, self.old_out_mode.value)


def stdin_reader_thread(q: "queue.Queue[bytes | None]", stop: threading.Event):
    """Windows' select() only supports sockets, not console/file handles, so
    stdin is drained on a dedicated blocking-read thread instead of polled
    with select() the way the POSIX launcher did."""
    fd = sys.stdin.fileno()
    while not stop.is_set():
        try:
            data = os.read(fd, 1024)
        except OSError:
            break
        if not data:
            break
        q.put(data)
    q.put(None)


def resize_watcher_thread(conn: socket.socket, stop: threading.Event, interval: float = 0.25):
    """Windows has no SIGWINCH; poll the console size instead."""
    last = get_terminal_size()
    while not stop.is_set():
        time.sleep(interval)
        if stop.is_set():
            return
        current = get_terminal_size()
        if current != last:
            last = current
            cols, rows = current
            try:
                send_frame(conn, FRAME_RESIZE, struct.pack("<HH", cols, rows))
            except OSError:
                stop.set()
                return


def interactive_bridge(conn: socket.socket, debug: bool = False):
    stop = threading.Event()
    parser = FrameParser()
    cols, rows = get_terminal_size()
    exit_code = 0

    resize_thread = threading.Thread(target=resize_watcher_thread, args=(conn, stop), daemon=True)
    resize_thread.start()

    def recv_loop():
        nonlocal exit_code
        while not stop.is_set():
            try:
                data = conn.recv(8192)
            except (BlockingIOError, InterruptedError):
                time.sleep(0.02)
                continue
            except OSError:
                stop.set()
                return
            if not data:
                stop.set()
                return
            for frame_type, payload in parser.feed(data):
                if frame_type == FRAME_READY:
                    debug_print(debug, "\r\n[agent-rdp] ConPTY bridge ready. Type `exit` to close. Ctrl-] detaches local client.\r\n", file=sys.stderr, flush=True)
                    try:
                        send_frame(conn, FRAME_INPUT, b"\r")
                    except OSError:
                        stop.set()
                        return
                elif frame_type in (FRAME_OUTPUT, FRAME_OUTPUT_ERR):
                    os.write(sys.stdout.fileno(), payload)
                elif frame_type == FRAME_ERROR:
                    text = payload.decode("utf-8", errors="replace")
                    debug_print(debug, f"\r\n[agent-rdp] remote error: {text}\r", file=sys.stderr)
                elif frame_type == FRAME_EXIT:
                    exit_code = struct.unpack("<I", payload[:4])[0] if len(payload) >= 4 else 0
                    debug_print(debug, f"\r\n[agent-rdp] remote exited with code {exit_code}\r", file=sys.stderr)
                    stop.set()
                    return
                else:
                    debug_print(debug, f"\r\n[agent-rdp] unknown frame type {frame_type} len={len(payload)}\r", file=sys.stderr)

    t = threading.Thread(target=recv_loop, daemon=True)
    t.start()

    input_q: "queue.Queue[bytes | None]" = queue.Queue()
    input_thread = threading.Thread(target=stdin_reader_thread, args=(input_q, stop), daemon=True)
    input_thread.start()

    with RawTerminal():
        send_frame(conn, FRAME_RESIZE, struct.pack("<HH", cols, rows))
        try:
            while not stop.is_set():
                try:
                    data = input_q.get(timeout=0.1)
                except queue.Empty:
                    continue
                if data is None:
                    stop.set()
                    break
                if data == b"\x1d":
                    try:
                        send_frame(conn, FRAME_CLOSE)
                    except OSError:
                        pass
                    time.sleep(1.0)
                    stop.set()
                    break
                try:
                    send_frame(conn, FRAME_INPUT, data)
                except OSError:
                    stop.set()
                    break
        finally:
            try:
                send_frame(conn, FRAME_CLOSE)
            except OSError:
                pass
            stop.set()
            t.join(timeout=3)
            resize_thread.join(timeout=1)
    return exit_code


def run_command_session(conn: socket.socket, *, target: str = "", stream: bool = True,
                        debug: bool = False) -> CommandResult:
    """Drive a single non-interactive command over the channel and collect its
    outcome into a CommandResult.

    stdout/stderr bytes are always accumulated (so --json / multi-target can
    return them). When `stream` is True (the default single-target, non-JSON
    path) the bytes are also written live to the real stdout/stderr fds,
    preserving the original streaming behavior exactly. The last kError text
    is folded into a stable `error` slug via classify_error(); a channel that
    closes before an exit frame is reported as ERR_CHANNEL_DROPPED.

    Note: JSON/multi-target consumers hold the full output in memory. That is
    fine for command results, but not intended for arbitrarily large streams.
    """
    parser = FrameParser()
    cols, rows = get_terminal_size()
    conn.setblocking(True)
    conn.settimeout(0.2)
    send_frame(conn, FRAME_RESIZE, struct.pack("<HH", cols, rows))

    stdout_buf = bytearray()
    stderr_buf = bytearray()
    result = CommandResult(target=target)
    got_exit = False

    while True:
        try:
            data = conn.recv(8192)
        except socket.timeout:
            continue
        if not data:
            break
        for frame_type, payload in parser.feed(data):
            if frame_type == FRAME_READY:
                continue
            if frame_type == FRAME_OUTPUT:
                stdout_buf.extend(payload)
                if stream:
                    os.write(sys.stdout.fileno(), payload)
            elif frame_type == FRAME_OUTPUT_ERR:
                stderr_buf.extend(payload)
                if stream:
                    os.write(sys.stderr.fileno(), payload)
            elif frame_type == FRAME_ERROR:
                text = payload.decode("utf-8", errors="replace")
                result.error = classify_error(text)
                result.error_detail = text
                debug_print(debug, f"\n[agent-rdp] remote error: {text}", file=sys.stderr)
            elif frame_type == FRAME_EXIT:
                result.exit_code = struct.unpack("<I", payload[:4])[0] if len(payload) >= 4 else 0
                got_exit = True
                debug_print(debug, f"\n[agent-rdp] remote exited with code {result.exit_code}", file=sys.stderr)
                break
            else:
                debug_print(debug, f"\n[agent-rdp] unknown frame type {frame_type} len={len(payload)}", file=sys.stderr)
        if got_exit:
            break

    result.stdout = stdout_buf.decode("utf-8", errors="replace")
    result.stderr = stderr_buf.decode("utf-8", errors="replace")
    if not got_exit and result.error is None:
        # Channel closed before the bridge reported an exit code.
        result.error = ERR_CHANNEL_DROPPED
        result.error_detail = "channel closed before exit frame"
    return result


class ProcessLogger:
    def __init__(self, enabled: bool, prefix: str = "", limit: int = 200):
        self.enabled = enabled
        self.prefix = prefix
        self.limit = limit
        self.lines = []
        self._lock = threading.Lock()
        self._thread = None

    def start(self, pipe):
        if pipe is None:
            return
        def _reader():
            try:
                for raw in iter(pipe.readline, b""):
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    with self._lock:
                        self.lines.append(line)
                        if len(self.lines) > self.limit:
                            self.lines = self.lines[-self.limit:]
                    if self.enabled:
                        if self.prefix:
                            print(f"{self.prefix}{line}", file=sys.stderr)
                        else:
                            print(line, file=sys.stderr)
            finally:
                try:
                    pipe.close()
                except Exception:
                    pass
        self._thread = threading.Thread(target=_reader, daemon=True)
        self._thread.start()

    def recent(self):
        with self._lock:
            return list(self.lines)

    def join(self, timeout: float = 1.0):
        if self._thread is not None:
            self._thread.join(timeout=timeout)


class nullcontext:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, exc_type, exc, tb):
        return False


def do_connect(args, username: str, host: str, password: str, *, stream: bool = True):
    """Run one RDP session against (username, host). Returns a CommandResult in
    command mode or an int exit code in interactive mode.

    Takes username/host/password explicitly (rather than reading args.username
    etc.) so it is safe to invoke concurrently for multi-target fan-out (#6):
    each call gets its own temp share dir, OS-assigned loopback port, and
    wfreerdp subprocess -- nothing target-specific is shared through `args`.
    """
    target = f"{username}@{host}"
    with tempfile.TemporaryDirectory(prefix="agent-rdp-share-") if not args.share_dir else nullcontext(Path(args.share_dir)) as tmp:
        share_dir = Path(tmp) if isinstance(tmp, str) else tmp

        with LoopbackSocketServer() as server:
            env = dict(os.environ)
            env["AGENT_RDP_SOCKET"] = server.endpoint

            cmd = build_wfreerdp_command(args, share_dir, username, host)
            debug_print(args.debug, f"[agent-rdp] launching ({target}):", " ".join(shlex.quote(str(x)) for x in cmd), file=sys.stderr)

            popen_kwargs = {"env": env, "stdin": subprocess.PIPE}
            if args.debug:
                proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, **popen_kwargs)
                proc_logger = ProcessLogger(enabled=True, prefix="[wfreerdp] ")
                proc_logger.start(proc.stderr)
            else:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    **popen_kwargs,
                )
                proc_logger = ProcessLogger(enabled=False, prefix="[wfreerdp] ")
                proc_logger.start(proc.stderr)
            try:
                if proc.stdin is not None:
                    proc.stdin.write((password + "\n").encode("utf-8"))
                    proc.stdin.flush()
                    proc.stdin.close()
                try:
                    conn = server.accept(timeout=args.accept_timeout)
                except socket.timeout as exc:
                    raise TimeoutError(
                        f"timed out waiting for the remote bridge to connect (target {target})"
                    ) from exc
                try:
                    if args.command:
                        return run_command_session(conn, target=target, stream=stream, debug=args.debug)
                    return interactive_bridge(conn, debug=args.debug)
                finally:
                    conn.close()
            except Exception:
                if not args.debug and proc_logger is not None:
                    recent = [line for line in proc_logger.recent() if line.strip()]
                    if recent:
                        print(f"[agent-rdp] wfreerdp stderr (most recent, {target}):", file=sys.stderr)
                        for line in recent[-20:]:
                            print(f"[wfreerdp] {line}", file=sys.stderr)
                raise
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                if proc_logger is not None:
                    proc_logger.join(timeout=1.0)


def parse_targets_spec(spec: str) -> list[tuple[str, str]]:
    """Parse a --targets value into a list of (username, host).

    Accepts a comma-separated list of user@host, or `@path` to read one
    user@host per line from a file (blank lines and `#` comments ignored)."""
    entries: list[str] = []
    if spec.startswith("@"):
        path = Path(spec[1:])
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                entries.append(line)
    else:
        entries = [part.strip() for part in spec.split(",") if part.strip()]
    if not entries:
        raise argparse.ArgumentTypeError("--targets did not yield any user@host entries")
    return [parse_target(entry) for entry in entries]


def _exception_to_result(target: str, exc: Exception) -> CommandResult:
    """Fold a per-target failure into a CommandResult so one bad target never
    sinks a multi-target batch."""
    if isinstance(exc, TimeoutError):
        slug = ERR_CONNECT_TIMEOUT
    elif isinstance(exc, FileNotFoundError):
        slug = ERR_LOCAL_SETUP
    else:
        slug = ERR_CHANNEL_DROPPED
    return CommandResult(target=target, exit_code=1, error=slug, error_detail=str(exc))


def run_multi_target(args, targets: list[tuple[str, str]], password: str) -> int:
    """Fan a single command out across multiple targets in parallel (#6) and
    emit a JSON array of per-target CommandResults. Exit 0 iff every target
    exited 0, else 1."""
    results: dict[int, CommandResult] = {}
    max_workers = max(1, min(args.max_parallel, len(targets)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(do_connect, args, username, host, password, stream=False): (idx, f"{username}@{host}")
            for idx, (username, host) in enumerate(targets)
        }
        for fut in concurrent.futures.as_completed(futures):
            idx, target = futures[fut]
            try:
                results[idx] = fut.result()
            except Exception as exc:  # noqa: BLE001 - turned into a per-target result
                results[idx] = _exception_to_result(target, exc)

    ordered = [results[i] for i in range(len(targets))]
    print(json.dumps([r.to_dict() for r in ordered]))
    return 0 if not any(r.failed for r in ordered) else 1


def run_single_target(args, username: str, host: str, password: str) -> int:
    """Single-target run: stream output live (legacy behavior) unless --json,
    in which case buffer and emit one JSON object. Interactive mode ignores
    --json (guarded in main) and returns its own exit code."""
    result = do_connect(args, username, host, password, stream=not args.json)
    if not args.command:
        # Interactive mode returned a raw exit code.
        return int(result)
    if args.json:
        print(json.dumps(result.to_dict()))
    return result.process_exit_code()


def parser():
    p = argparse.ArgumentParser(description="Windows-to-Windows agent-rdp: run cmd/PowerShell on a remote Windows host over RDP")
    p.add_argument("target", nargs="?", default="",
                   help="Remote target as user@hostname. Pass a comma-separated list "
                        "(user@h1,user@h2) or use --targets-file to fan a command out across "
                        "multiple targets in parallel.")
    p.add_argument("child", nargs="?", choices=["powershell", "cmd"], default="powershell")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.add_argument(
        "--powershell-execution-policy",
        choices=POWERSHELL_EXECUTION_POLICIES,
        default="default",
        help="PowerShell execution policy for one-shot commands. The default respects the target's "
             "configured policy; 'bypass' is an explicit compatibility override and can increase "
             "antivirus/EDR scrutiny. Place this option before the target.",
    )
    p.add_argument("-p", "--port", type=int, default=int(os.environ.get("RDP_PORT", "3389")))
    p.add_argument("-P", "--password", default=os.environ.get("RDP_PASSWORD", ""))
    p.add_argument("-d", "--domain", default=os.environ.get("RDP_DOMAIN", ""))
    p.add_argument(
        "--insecure-cert-ignore",
        "--cert-ignore",
        dest="cert_ignore",
        action="store_true",
        default=False,
        help="INSECURE compatibility override: disable RDP server certificate validation and emit a warning.",
    )
    p.add_argument("--wfreerdp", default=os.environ.get("WFREERDP", DEFAULT_WFREERDP))
    p.add_argument("--plugin-dir", default=os.environ.get("AGENT_RDP_PLUGIN_DIR", DEFAULT_PLUGIN_DIR))
    p.add_argument("--plugin-name", default=os.environ.get("AGENT_RDP_PLUGIN_NAME", DEFAULT_PLUGIN_NAME))
    p.add_argument("--helper-exe", default=os.environ.get("AGENT_RDP_HELPER_EXE", DEFAULT_HELPER_EXE))
    p.add_argument("--accept-timeout", type=float, default=float(os.environ.get("AGENT_RDP_ACCEPT_TIMEOUT", "60.0")))
    p.add_argument("--drive-poll-timeout", type=float,
                   default=float(os.environ.get("AGENT_RDP_DRIVE_POLL_TIMEOUT", "20.0")),
                   help="Seconds the remote-side retry loop waits for the redirected drive to mount before giving up")
    p.add_argument("--drive-name", default=os.environ.get("AGENT_RDP_DRIVE_NAME", DEFAULT_DRIVE_NAME))
    p.add_argument("--share-dir", default=os.environ.get("AGENT_RDP_SHARE_DIR", ""))
    p.add_argument("--enable-clipboard", action="store_true", default=bool(int(os.environ.get("AGENT_RDP_ENABLE_CLIPBOARD", "0"))))
    p.add_argument("--debug", action="store_true", default=bool(int(os.environ.get("AGENT_RDP_DEBUG", "0"))))
    # Structured output (#2) / error vocabulary (#7)
    p.add_argument("-j", "--json", action="store_true",
                   default=bool(int(os.environ.get("AGENT_RDP_JSON", "0"))),
                   help="Emit a single JSON object {target, exit_code, stdout, stderr, error, "
                        "error_detail} instead of streaming raw output (single-command mode only). "
                        "Multi-target mode always emits a JSON array.")
    # Credential Manager (#4)
    p.add_argument("--credential-target", default=os.environ.get("AGENT_RDP_CREDENTIAL_TARGET", ""),
                   help="Read the RDP password from a Windows Credential Manager generic "
                        "credential stored under this target name (unattended use).")
    p.add_argument("--save-credential", action="store_true", default=False,
                   help="Store the resolved password in Windows Credential Manager under "
                        "--credential-target for later unattended runs, then continue.")
    # Multi-target concurrency (#6)
    p.add_argument("--targets-file", default=os.environ.get("AGENT_RDP_TARGETS_FILE", ""),
                   help="Path to a file with one user@host per line (blank lines and # comments "
                        "ignored); fans the command out across all of them in parallel.")
    p.add_argument("--max-parallel", type=int, default=int(os.environ.get("AGENT_RDP_MAX_PARALLEL", "4")),
                   help="Maximum number of targets to run concurrently in multi-target mode.")
    return p


def resolve_targets(args, p) -> list[tuple[str, str]]:
    """Resolve the requested target list from the positional / --targets-file,
    erroring via the parser on conflicts or empty input."""
    if args.targets_file and args.target:
        p.error("provide targets either positionally or via --targets-file, not both")
    try:
        if args.targets_file:
            return parse_targets_spec("@" + args.targets_file)
        if args.target:
            return parse_targets_spec(args.target)
    except (argparse.ArgumentTypeError, OSError) as exc:
        p.error(str(exc))
    p.error("a target is required: user@hostname (or a comma-separated list / --targets-file)")


def validate_args(args, p, *, multi: bool):
    """Reject incompatible option combinations before local checks, prompts, or connection attempts."""
    if not args.command:
        if args.powershell_execution_policy != "default":
            p.error("--powershell-execution-policy requires a one-shot PowerShell command")
        if multi:
            p.error("multi-target mode requires a command (the interactive shell is single-target only)")
        if args.json:
            p.error("--json requires a command (interactive mode streams a live terminal)")
    if args.child != "powershell" and args.powershell_execution_policy != "default":
        p.error("--powershell-execution-policy only applies to the powershell child")
    if multi and args.share_dir:
        p.error("--share-dir cannot be combined with multiple targets (each target needs its own share dir)")
    if args.save_credential and not args.credential_target:
        p.error("--save-credential requires --credential-target NAME")


def warn_security_overrides(args):
    if args.cert_ignore:
        print(
            "agent-rdp: WARNING: --insecure-cert-ignore disables RDP server certificate validation.",
            file=sys.stderr,
        )
    if args.powershell_execution_policy == "bypass":
        print(
            "agent-rdp: WARNING: --powershell-execution-policy bypass overrides the target PowerShell execution policy.",
            file=sys.stderr,
        )


def main():
    set_binary_mode()
    p = parser()
    args = p.parse_args()

    targets = resolve_targets(args, p)
    multi = len(targets) > 1
    validate_args(args, p, multi=multi)
    warn_security_overrides(args)

    # Validate local components once, before any (possibly concurrent) connect.
    ensure_plugin(args.plugin_dir, args.plugin_name)
    args.helper_exe_path = ensure_helper(args.helper_exe)

    password = resolve_password(args)
    if args.save_credential:
        write_windows_credential(args.credential_target, targets[0][0], password)

    if multi:
        raise SystemExit(run_multi_target(args, targets, password))
    username, host = targets[0]
    raise SystemExit(run_single_target(args, username, host, password) or 0)


if __name__ == "__main__":
    main()
