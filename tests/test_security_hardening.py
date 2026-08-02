import re
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_SOURCE = REPO_ROOT / "src" / "windows" / "agent_rdp_bridge.cpp"


def test_certificate_validation_is_enabled_by_default(r2e):
    args = r2e.parser().parse_args(["user@example", "cmd", "whoami"])

    assert args.cert_ignore is False


def test_certificate_validation_override_is_explicit(r2e):
    args = r2e.parser().parse_args(
        ["--insecure-cert-ignore", "user@example", "cmd", "whoami"]
    )

    assert args.cert_ignore is True


def test_insecure_certificate_override_emits_a_warning(r2e, capsys):
    args = r2e.parser().parse_args(
        ["--insecure-cert-ignore", "user@example", "cmd", "whoami"]
    )

    r2e.warn_security_overrides(args)

    assert "disables RDP server certificate validation" in capsys.readouterr().err


def _wfreerdp_args(cert_ignore: bool):
    return SimpleNamespace(
        debug=False,
        helper_exe_path=Path("agent-rdp-bridge.exe"),
        child="cmd",
        drive_name="r2e",
        drive_poll_timeout=20.0,
        command=["whoami"],
        powershell_execution_policy="default",
        wfreerdp="wfreerdp.exe",
        port=3389,
        enable_clipboard=False,
        domain="",
        cert_ignore=cert_ignore,
    )


def test_wfreerdp_validates_certificates_unless_explicitly_overridden(r2e, monkeypatch, tmp_path):
    monkeypatch.setattr(r2e, "get_terminal_size", lambda: (120, 40))
    monkeypatch.setattr(
        r2e,
        "prepare_drive_share",
        lambda *args, **kwargs: (tmp_path, "cmd.exe /d /c exit 0"),
    )

    secure_command = r2e.build_wfreerdp_command(
        _wfreerdp_args(cert_ignore=False), tmp_path, "user", "host"
    )
    insecure_command = r2e.build_wfreerdp_command(
        _wfreerdp_args(cert_ignore=True), tmp_path, "user", "host"
    )

    assert "/cert:ignore" not in secure_command
    assert "/cert:ignore" in insecure_command


def test_powershell_execution_policy_defaults_to_target_policy(r2e):
    args = r2e.parser().parse_args(["user@example", "powershell", "Get-Date"])

    assert args.powershell_execution_policy == "default"


def test_environment_cannot_silently_enable_execution_policy_bypass(r2e, monkeypatch):
    monkeypatch.setenv("AGENT_RDP_POWERSHELL_EXECUTION_POLICY", "bypass")

    args = r2e.parser().parse_args(["user@example", "powershell", "Get-Date"])

    assert args.powershell_execution_policy == "default"


def test_interactive_policy_override_is_rejected_before_connection_side_effects(r2e):
    p = r2e.parser()
    args = p.parse_args(
        ["--powershell-execution-policy", "bypass", "user@example", "powershell"]
    )

    with pytest.raises(SystemExit) as exc_info:
        r2e.validate_args(args, p, multi=False)

    assert exc_info.value.code == 2


def test_default_alternate_shell_does_not_override_powershell_policy(r2e):
    shell = r2e.build_alternate_shell(
        "r2e",
        "powershell",
        120,
        40,
        r"\\tsclient\r2e\agent-rdp-command.ps1",
        20,
        powershell_execution_policy="default",
    )

    assert "--powershell-execution-policy" not in shell


def test_bypass_requires_an_explicit_launcher_option(r2e):
    args = r2e.parser().parse_args(
        [
            "--powershell-execution-policy",
            "bypass",
            "user@example",
            "powershell",
            "Get-Date",
        ]
    )
    shell = r2e.build_alternate_shell(
        "r2e",
        "powershell",
        120,
        40,
        r"\\tsclient\r2e\agent-rdp-command.ps1",
        20,
        powershell_execution_policy=args.powershell_execution_policy,
    )

    assert "--powershell-execution-policy bypass" in shell


def test_explicit_bypass_emits_a_policy_warning(r2e, capsys):
    args = r2e.parser().parse_args(
        [
            "--powershell-execution-policy",
            "bypass",
            "user@example",
            "powershell",
            "Get-Date",
        ]
    )

    r2e.warn_security_overrides(args)

    assert "overrides the target PowerShell execution policy" in capsys.readouterr().err


def test_execution_policy_override_requires_a_one_shot_command(r2e):
    with pytest.raises(ValueError, match="one-shot PowerShell command"):
        r2e.build_alternate_shell(
            "r2e",
            "powershell",
            120,
            40,
            "",
            20,
            powershell_execution_policy="bypass",
        )


def test_bridge_uses_noninteractive_powershell_without_hardcoded_bypass():
    source = BRIDGE_SOURCE.read_text(encoding="utf-8")
    assert "-ExecutionPolicy Bypass" not in source
    assert "-NoLogo -NoProfile -NonInteractive" in source
    assert "set_powershell_execution_policy" in source
    assert "args.powershell_execution_policy.empty()" in source
    assert "args.command_file.empty()" in source


def test_launcher_and_bridge_execution_policy_allowlists_stay_in_sync(r2e):
    source = BRIDGE_SOURCE.read_text(encoding="utf-8")
    function_start = source.index("static bool set_powershell_execution_policy")
    function_end = source.index("static Args parse_args")
    bridge_policies = {
        value.lower()
        for value in re.findall(r'L"([A-Za-z]+)"', source[function_start:function_end])
    }

    assert bridge_policies == set(r2e.POWERSHELL_EXECUTION_POLICIES)
