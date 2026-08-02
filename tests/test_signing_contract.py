from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SIGNING_SCRIPT = REPO_ROOT / "scripts" / "sign-artifacts.ps1"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def test_signing_script_uses_sha256_rfc3161_and_environment_password():
    assert SIGNING_SCRIPT.exists(), "release signing script is missing"
    source = SIGNING_SCRIPT.read_text(encoding="utf-8")

    assert 'GetEnvironmentVariable($CertificatePasswordEnvironmentVariable)' in source
    assert '"/fd", "SHA256"' in source
    assert '"/tr", $TimestampUrl' in source
    assert '"/td", "SHA256"' in source
    assert 'Get-ChildItem -Path $ArtifactsDirectory -Recurse -File' in source
    assert '"*.exe", "*.dll"' in source
    assert "$existingCertificateThumbprints" in source
    assert "$existingCertificateThumbprints -notcontains $certificate.Thumbprint" in source


def test_release_workflow_pins_pyinstaller_version():
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "pip install pyinstaller==6.21.0" in workflow


def test_release_workflow_signs_after_freezing_and_before_packaging():
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "WINDOWS_SIGNING_CERTIFICATE_BASE64" in workflow
    assert "WINDOWS_SIGNING_CERTIFICATE_PASSWORD" in workflow
    freeze = workflow.index("- name: Freeze launcher with PyInstaller")
    sign = workflow.index("- name: Sign Windows artifacts")
    package = workflow.index("- name: Package release zip")
    assert freeze < sign < package


def test_release_workflow_always_removes_temporary_certificate():
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    cleanup = workflow.index("- name: Remove temporary signing certificate")
    cleanup_step = workflow[cleanup : cleanup + 600]
    assert "if: always()" in cleanup_step
    assert 'Join-Path $env:RUNNER_TEMP "agent-rdp-signing.pfx"' in cleanup_step


def test_ci_smoke_tests_authenticode_signing_helper():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "- name: Smoke-test Authenticode signing helper" in workflow
    assert "New-SelfSignedCertificate" in workflow
    assert "./scripts/sign-artifacts.ps1" in workflow
    assert "if-no-files-found: error" in workflow
