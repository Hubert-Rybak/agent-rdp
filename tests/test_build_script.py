from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_cmake_configure_receives_custom_triplet_overlay():
    """CMake's vcpkg manifest install must be able to resolve the repo triplet."""
    build_script = (REPO_ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")

    assert '"-DVCPKG_OVERLAY_TRIPLETS=$RepoRoot\\triplets"' in build_script
