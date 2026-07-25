"""Follow-up #4: resolve_password precedence -- explicit -P beats RDP_PASSWORD
beats the Windows Credential Manager beats an interactive prompt. The Win32
credential reader is monkeypatched so this runs on any OS."""
from types import SimpleNamespace

import pytest


def _args(**over):
    base = dict(password="", credential_target="")
    base.update(over)
    return SimpleNamespace(**base)


def test_explicit_password_wins(r2e, monkeypatch):
    monkeypatch.setenv("RDP_PASSWORD", "from-env")
    monkeypatch.setattr(r2e, "read_windows_credential",
                        lambda t: (_ for _ in ()).throw(AssertionError("should not read cred")))
    assert r2e.resolve_password(_args(password="explicit", credential_target="X")) == "explicit"


def test_env_beats_credential_manager(r2e, monkeypatch):
    monkeypatch.setenv("RDP_PASSWORD", "from-env")
    monkeypatch.setattr(r2e, "read_windows_credential",
                        lambda t: (_ for _ in ()).throw(AssertionError("should not read cred")))
    assert r2e.resolve_password(_args(credential_target="X")) == "from-env"


def test_credential_manager_used_when_no_password_or_env(r2e, monkeypatch):
    monkeypatch.delenv("RDP_PASSWORD", raising=False)
    monkeypatch.setattr(r2e, "read_windows_credential", lambda t: ("user", "secret"))
    assert r2e.resolve_password(_args(credential_target="my-target")) == "secret"


def test_missing_everything_raises(r2e, monkeypatch):
    monkeypatch.delenv("RDP_PASSWORD", raising=False)
    # stdin is not a tty under pytest, so no prompt path is taken.
    with pytest.raises(RuntimeError):
        r2e.resolve_password(_args())
