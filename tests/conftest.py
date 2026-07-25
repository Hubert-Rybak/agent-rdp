"""Shared fixtures: import the launcher module by path so its pure-Python
helpers (error vocabulary, JSON result assembly, multi-target aggregation,
credential resolution order) can be unit-tested on any OS -- the Windows-only
bits are import-guarded, so this loads fine on the Linux CI runner too."""
import importlib.util
import sys
from pathlib import Path

import pytest

_LAUNCHER = Path(__file__).resolve().parents[1] / "src" / "launcher" / "agent_rdp.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("agent_rdp_launcher", _LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass string-annotation resolution can find it.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def r2e():
    return _load_module()
