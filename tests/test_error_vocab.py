"""Follow-up #7: every bridge kError string maps to a stable slug, and unknown
text falls through to the generic remote_error catch-all."""
import pytest


# The exact kError strings emitted by src/windows/agent_rdp_bridge.cpp, paired
# with the slug an agent should see. Keep this in sync with the bridge.
BRIDGE_MESSAGES = {
    "ConPTY API unavailable on this Windows build/session": "conpty_unavailable",
    "CreatePseudoConsole failed": "conpty_unavailable",
    "CreatePipe(input) failed": "pipe_setup_failed",
    "CreatePipe(output) failed": "pipe_setup_failed",
    "CreatePipe(stdin) failed": "pipe_setup_failed",
    "CreatePipe(stdout) failed": "pipe_setup_failed",
    "CreatePipe(stderr) failed": "pipe_setup_failed",
    "HeapAlloc(attr_list) failed": "process_setup_failed",
    "InitializeProcThreadAttributeList failed": "process_setup_failed",
    "UpdateProcThreadAttribute(PSEUDOCONSOLE) failed": "process_setup_failed",
    "CreateProcessW child failed": "process_spawn_failed",
}


@pytest.mark.parametrize("text,slug", BRIDGE_MESSAGES.items())
def test_known_messages_map_to_slug(r2e, text, slug):
    assert r2e.classify_error(text) == slug


def test_unknown_message_is_catch_all(r2e):
    assert r2e.classify_error("something we have never seen") == r2e.ERR_REMOTE


def test_all_slugs_are_from_a_closed_set(r2e):
    known = {
        r2e.ERR_CONPTY_UNAVAILABLE,
        r2e.ERR_PIPE_SETUP_FAILED,
        r2e.ERR_PROCESS_SETUP_FAILED,
        r2e.ERR_PROCESS_SPAWN_FAILED,
    }
    assert set(BRIDGE_MESSAGES.values()) <= known
