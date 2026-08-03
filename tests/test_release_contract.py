import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DEPENDABOT_CONFIG = REPO_ROOT / ".github" / "dependabot.yml"


def test_repository_has_no_legacy_certificate_pipeline_or_documentation():
    removed_script = REPO_ROOT / "scripts" / ("sign" + "-artifacts.ps1")
    assert not removed_script.exists()

    forbidden_fragments = (
        "Authenti" + "code",
        "sign" + "-artifacts.ps1",
        "WINDOWS_" + "SIGNING_",
        "Sign" + "Tool",
        "Code " + "Signing",
        "signing " + "certificate",
        "RFC " + "3161",
        ".p" + "fx",
    )
    text_suffixes = {".md", ".py", ".ps1", ".yml", ".yaml", ".toml", ".json", ".txt"}
    ignored_roots = {".git", ".venv", ".pytest_cache", "build", "dist"}
    violations = []
    for path in REPO_ROOT.rglob("*"):
        relative = path.relative_to(REPO_ROOT)
        if (
            not path.is_file()
            or relative.parts[0] in ignored_roots
            or path.suffix.lower() not in text_suffixes
        ):
            continue
        text = path.read_text(encoding="utf-8")
        for fragment in forbidden_fragments:
            if fragment.lower() in text.lower():
                violations.append(f"{relative}: {fragment}")

    assert violations == []


def test_workflows_build_and_package_without_repository_secrets():
    release = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    ci = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "secrets." not in release
    assert "secrets." not in ci
    release_build = release.index("- name: Build native components")
    freeze = release.index("- name: Freeze launcher with PyInstaller")
    package = release.index("- name: Package release zip")
    publish = release.index("- name: Create GitHub Release")
    assert release_build < freeze < package < publish

    ci_build = ci.index("- name: Build agent-rdp-client.dll + agent-rdp-bridge.exe")
    upload = ci.index("- name: Upload build artifacts")
    assert ci_build < upload


def test_release_workflow_pins_pyinstaller_version():
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "pip install pyinstaller==6.21.0" in workflow


def _powershell_run_blocks(path: Path):
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks = []
    for index, line in enumerate(lines):
        if line.strip() != "run: |":
            continue
        indentation = len(line) - len(line.lstrip())
        block = []
        for candidate in lines[index + 1 :]:
            if candidate.strip() and len(candidate) - len(candidate.lstrip()) <= indentation:
                break
            block.append(candidate)
        blocks.append("\n".join(block))
    return blocks


def test_github_actions_are_pinned_and_checkout_does_not_persist_credentials():
    for workflow_path in (CI_WORKFLOW, RELEASE_WORKFLOW):
        workflow = workflow_path.read_text(encoding="utf-8")
        action_references = re.findall(
            r"^\s*(?:-\s+)?uses:\s+(?!\./)[^@\s]+@([^\s#]+)", workflow, re.MULTILINE
        )

        assert action_references
        assert all(re.fullmatch(r"[0-9a-f]{40}", reference) for reference in action_references)
        assert workflow.count("persist-credentials: false") >= workflow.count("actions/checkout@")


def test_pinned_github_actions_have_dependabot_maintenance():
    config = DEPENDABOT_CONFIG.read_text(encoding="utf-8")

    assert 'package-ecosystem: "github-actions"' in config
    assert 'directory: "/"' in config
    assert 'interval: "weekly"' in config


def test_ci_is_least_privilege_deduplicated_and_concurrent_runs_are_bounded():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    python_job = workflow[
        workflow.index("  python-check:") : workflow.index("  windows-build:")
    ]

    assert "permissions:\n  contents: read" in workflow
    assert 'branches: ["master"]' in workflow
    assert "cancel-in-progress: true" in workflow
    assert "timeout-minutes: 5" in python_job
    assert "timeout-minutes: 45" in workflow


def test_release_powershell_avoids_direct_github_template_interpolation():
    for run_block in _powershell_run_blocks(RELEASE_WORKFLOW):
        assert "${{" not in run_block


def test_release_checkout_failures_are_checked_immediately():
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "function Set-ReleaseCheckout" in workflow
    assert "git checkout --detach $Ref" in workflow
    assert 'throw "Could not checkout release target' in workflow


def test_release_is_bounded_without_runtime_dependency_cache():
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "actions/cache@" not in workflow
    assert "cancel-in-progress: false" in workflow
    assert "timeout-minutes: 60" in workflow


def test_ci_uploads_a_bounded_windows_build_artifact():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "name: agent-rdp-windows-build" in workflow
    assert "retention-days: 7" in workflow
    assert "if-no-files-found: error" in workflow
