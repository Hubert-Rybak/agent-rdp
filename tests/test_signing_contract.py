import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SIGNING_SCRIPT = REPO_ROOT / "scripts" / "sign-artifacts.ps1"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DEPENDABOT_CONFIG = REPO_ROOT / ".github" / "dependabot.yml"


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
    assert "$existingCertificateThumbprints -notcontains $thumbprint" in source
    assert "[switch]$SkipTimestamp" in source
    assert "if (-not $SkipTimestamp)" in source
    assert "[int]$SignToolTimeoutSeconds = 120" in source
    assert "function Invoke-SignTool" in source
    assert "$process.Kill($true)" in source
    assert "$process.WaitForExit(10000)" in source
    assert "$process.WaitForExit()" not in source
    assert "$startInfo.RedirectStandardOutput = $true" in source
    assert "$startInfo.RedirectStandardError = $true" in source
    assert "ReadToEndAsync()" in source
    assert "signtool stdout" in source
    assert "signtool stderr" in source
    assert "TimeStamperCertificate" in source
    assert "SignerCertificate.Thumbprint" in source
    assert "SkipTimestamp signing already selected the certificate by thumbprint" in source
    assert "-DeleteKey" in source
    assert "EphemeralKeySet" in source
    assert "already exists in Cert:\\CurrentUser\\My" in source
    assert "changed while the PFX was being inspected" in source
    assert "System.Threading.Mutex" in source
    assert "$expectedCertificateThumbprints" in source
    assert "$cleanupErrors" in source
    assert "function Write-SigningProgress" in source
    assert "::notice title=agent-rdp signing helper::" in source
    assert "function Test-CodeSigningCertificate" in source
    assert "EnhancedKeyUsageList" not in source
    assert '"2.5.29.37"' in source
    assert "$decodedEnhancedKeyUsage.EnhancedKeyUsages" in source
    assert "$importedCertificate.Dispose()" in source
    assert source.index("foreach ($importedCertificate in $importedCertificates)") < source.index(
        "foreach ($thumbprint in $expectedCertificateThumbprints)"
    )


def test_sign_tool_discovery_does_not_recursively_scan_the_windows_sdk():
    source = SIGNING_SCRIPT.read_text(encoding="utf-8")
    find_start = source.index("function Find-SignTool")
    find_end = source.index("if (-not (Test-Path $CertificatePath")
    find_function = source[find_start:find_end]

    assert "-Recurse" not in find_function
    assert '"*\\x64\\signtool.exe"' in find_function


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
    release_signing_step = workflow[sign:package].lower()
    assert "-skiptimestamp" not in release_signing_step


def test_release_workflow_always_removes_temporary_certificate():
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    cleanup = workflow.index("- name: Remove temporary signing certificate")
    cleanup_step = workflow[cleanup : cleanup + 600]
    assert "if: always()" in cleanup_step
    assert 'Join-Path $env:RUNNER_TEMP "agent-rdp-signing.pfx"' in cleanup_step


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


def test_release_signing_is_bounded_and_runtime_cache_is_avoided():
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    signing_start = workflow.index("- name: Sign Windows artifacts")
    cleanup_start = workflow.index("- name: Remove temporary signing certificate")
    signing_step = workflow[signing_start:cleanup_start]

    assert "timeout-minutes: 10" in signing_step
    assert "actions/cache@" not in workflow
    assert "cancel-in-progress: false" in workflow
    assert "timeout-minutes: 60" in workflow


def test_ci_smoke_tests_authenticode_signing_helper():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "- name: Smoke-test Authenticode signing helper" in workflow
    assert "timeout-minutes: 5" in workflow
    assert "New-SelfSignedCertificate" not in workflow
    assert "CertificateRequest" in workflow
    assert "CreateSelfSigned" in workflow
    assert "X509ContentType]::Pfx" in workflow
    assert "X509EnhancedKeyUsageExtension" in workflow
    assert 'Oid]::new("1.3.6.1.5.5.7.3.3")' in workflow
    assert "Import-Certificate" not in workflow
    assert "certutil.exe" not in workflow
    assert "StoreName]::TrustedPeople" not in workflow
    assert "StoreName]::TrustedPublisher" in workflow
    assert "$trustedPublisherStore.Add($trustedCertificate)" in workflow
    assert "$cleanupStore.Remove($trustedPublisherCertificate)" in workflow
    assert "./scripts/sign-artifacts.ps1" in workflow
    assert "-SkipTimestamp" in workflow
    assert "-SignToolTimeoutSeconds 60" in workflow
    assert "Get-AuthenticodeSignature" not in workflow
    assert "$certificate.Dispose()" in workflow
    assert "$rsa.Dispose()" in workflow
    assert "Signing helper left its imported certificate" in workflow
    assert "name: agent-rdp-windows-build-unsigned" in workflow
    assert "retention-days: 7" in workflow
    assert "if-no-files-found: error" in workflow
