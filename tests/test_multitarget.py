"""Follow-up #6: multi-target fan-out aggregates per-target CommandResults into
a JSON array (in input order), rolls the exit code up correctly, and turns a
per-target exception into a result instead of sinking the batch."""
import json
from types import SimpleNamespace

import pytest


def _args(**over):
    base = dict(max_parallel=4, command=["whoami"], share_dir="", json=True, debug=False)
    base.update(over)
    return SimpleNamespace(**base)


def test_results_are_ordered_and_rolled_up(r2e, monkeypatch, capsys):
    def fake_do_connect(args, username, host, password, *, stream=True):
        code = {"h1": 0, "h2": 5}[host]
        return r2e.CommandResult(target=f"{username}@{host}", exit_code=code,
                                 stdout=host, stderr="")
    monkeypatch.setattr(r2e, "do_connect", fake_do_connect)

    targets = [("u", "h1"), ("u", "h2")]
    rc = r2e.run_multi_target(_args(), targets, "pw")

    out = json.loads(capsys.readouterr().out)
    assert [r["target"] for r in out] == ["u@h1", "u@h2"]  # input order preserved
    assert out[0]["exit_code"] == 0 and out[1]["exit_code"] == 5
    assert rc == 1  # any non-zero target -> batch fails


def test_all_success_returns_zero(r2e, monkeypatch, capsys):
    monkeypatch.setattr(r2e, "do_connect",
                        lambda a, u, h, p, *, stream=True: r2e.CommandResult(target=f"{u}@{h}"))
    rc = r2e.run_multi_target(_args(), [("u", "h1"), ("u", "h2")], "pw")
    capsys.readouterr()
    assert rc == 0


def test_exception_becomes_a_result(r2e, monkeypatch, capsys):
    def boom(args, username, host, password, *, stream=True):
        if host == "h2":
            raise TimeoutError("no bridge")
        return r2e.CommandResult(target=f"{username}@{host}")
    monkeypatch.setattr(r2e, "do_connect", boom)

    rc = r2e.run_multi_target(_args(), [("u", "h1"), ("u", "h2")], "pw")
    out = json.loads(capsys.readouterr().out)
    assert out[1]["error"] == r2e.ERR_CONNECT_TIMEOUT
    assert out[1]["error_detail"] == "no bridge"
    assert rc == 1
