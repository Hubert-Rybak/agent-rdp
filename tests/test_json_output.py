"""Follow-up #2/#7: drive run_command_session over a real socketpair fed with
synthetic bridge frames, and assert the resulting CommandResult / JSON shape.
This exercises the frame loop end-to-end without needing a Windows target."""
import json
import socket
import struct

import pytest


def _feed(r2e, frames):
    """Write `frames` (a list of (type, payload)) into one end of a socketpair,
    close it, then run run_command_session on the other end and return the
    CommandResult."""
    a, b = socket.socketpair()
    try:
        for ftype, payload in frames:
            r2e.send_frame(b, ftype, payload)
        b.shutdown(socket.SHUT_WR)
        result = r2e.run_command_session(a, target="u@h", stream=False)
    finally:
        a.close()
        b.close()
    return result


def test_clean_run_collects_streams_and_exit(r2e):
    result = _feed(r2e, [
        (r2e.FRAME_READY, b""),
        (r2e.FRAME_OUTPUT, b"hello "),
        (r2e.FRAME_OUTPUT, b"world"),
        (r2e.FRAME_OUTPUT_ERR, b"a warning"),
        (r2e.FRAME_EXIT, struct.pack("<I", 7)),
    ])
    assert result.target == "u@h"
    assert result.stdout == "hello world"
    assert result.stderr == "a warning"
    assert result.exit_code == 7
    assert result.error is None
    assert result.process_exit_code() == 7

    # JSON round-trips to the documented shape.
    obj = json.loads(json.dumps(result.to_dict()))
    assert obj == {
        "target": "u@h", "exit_code": 7, "stdout": "hello world",
        "stderr": "a warning", "error": None, "error_detail": None,
    }


def test_error_frame_sets_slug_and_nonzero_exit(r2e):
    result = _feed(r2e, [
        (r2e.FRAME_ERROR, b"CreateProcessW child failed"),
        # No FRAME_EXIT: setup failures close the channel without one.
    ])
    assert result.error == r2e.ERR_PROCESS_SPAWN_FAILED
    assert result.error_detail == "CreateProcessW child failed"
    assert result.exit_code == 0
    assert result.process_exit_code() == 1  # never report success on error


def test_channel_dropped_without_exit(r2e):
    result = _feed(r2e, [(r2e.FRAME_READY, b"")])
    assert result.error == r2e.ERR_CHANNEL_DROPPED
    assert result.process_exit_code() == 1


def test_invalid_utf8_is_replaced_not_fatal(r2e):
    result = _feed(r2e, [
        (r2e.FRAME_OUTPUT, b"\xff\xfe bad bytes"),
        (r2e.FRAME_EXIT, struct.pack("<I", 0)),
    ])
    assert "bad bytes" in result.stdout  # decoded with errors="replace"
    json.dumps(result.to_dict())  # must be JSON-serializable
