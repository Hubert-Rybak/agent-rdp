#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Authenticode-signs every staged Windows executable and DLL.

.DESCRIPTION
    Imports a PFX into the ephemeral CurrentUser certificate store, signs all
    PE artifacts with SHA-256 and an RFC 3161 timestamp by default, verifies
    every signature, and removes all imported certificates in a finally block.
    The test-only SkipTimestamp switch omits the external timestamp request for
    isolated CI smoke tests; production release signing does not use it.
    The PFX password is read from an environment variable and is never accepted
    as a command-line argument.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$CertificatePath,

    [string]$ArtifactsDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) "artifacts"),

    [string]$CertificatePasswordEnvironmentVariable = "WINDOWS_SIGNING_CERTIFICATE_PASSWORD",

    [ValidatePattern("^https?://")]
    [string]$TimestampUrl = "http://timestamp.digicert.com",

    # Intended only for isolated CI smoke tests; release signing must remain timestamped.
    [switch]$SkipTimestamp,

    [ValidateRange(1, 3600)]
    [int]$SignToolTimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Find-SignTool {
    $command = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $kitsRoot = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    if (Test-Path $kitsRoot) {
        $signToolPattern = Join-Path $kitsRoot "*\x64\signtool.exe"
        $candidate = Get-ChildItem -Path $signToolPattern -File -ErrorAction SilentlyContinue |
            Sort-Object -Property FullName -Descending |
            Select-Object -First 1
        if ($candidate) {
            return $candidate.FullName
        }
    }

    throw "signtool.exe was not found on PATH or in the Windows 10 SDK"
}

function Invoke-SignTool {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SignToolPath,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [Parameter(Mandatory = $true)]
        [int]$TimeoutSeconds,

        [Parameter(Mandatory = $true)]
        [string]$Operation
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $SignToolPath
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    foreach ($argument in $Arguments) {
        [void]$startInfo.ArgumentList.Add($argument)
    }

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw "signtool could not start while attempting to $Operation"
        }
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            try {
                $process.Kill($true)
            }
            catch {
                throw "signtool timed out and its process tree could not be terminated while attempting to $Operation`: $($_.Exception.Message)"
            }
            if (-not $process.WaitForExit(10000)) {
                throw "signtool timed out and did not terminate within 10 additional seconds while attempting to $Operation"
            }
            throw "signtool timed out after $TimeoutSeconds second(s) while attempting to $Operation"
        }
        if ($process.ExitCode -ne 0) {
            throw "signtool exited with code $($process.ExitCode) while attempting to $Operation"
        }
    }
    finally {
        $process.Dispose()
    }
}

if (-not (Test-Path $CertificatePath -PathType Leaf)) {
    throw "Signing certificate does not exist: $CertificatePath"
}
if (-not (Test-Path $ArtifactsDirectory -PathType Container)) {
    throw "Artifacts directory does not exist: $ArtifactsDirectory"
}

$password = [Environment]::GetEnvironmentVariable($CertificatePasswordEnvironmentVariable)
if ([string]::IsNullOrWhiteSpace($password)) {
    throw "Signing certificate password environment variable is empty: $CertificatePasswordEnvironmentVariable"
}

$peFiles = @(
    Get-ChildItem -Path $ArtifactsDirectory -Recurse -File -Include "*.exe", "*.dll" |
        Sort-Object -Property FullName
)
if ($peFiles.Count -eq 0) {
    throw "No EXE or DLL artifacts were found under $ArtifactsDirectory"
}

$signTool = Find-SignTool
$securePassword = $null
$importedCertificates = @()
$inspectedCertificates = [System.Security.Cryptography.X509Certificates.X509Certificate2Collection]::new()
$expectedCertificateThumbprints = @()
$existingCertificateThumbprints = @()
$operationError = $null
$cleanupErrors = [System.Collections.Generic.List[string]]::new()
$storeMutex = $null
$mutexHeld = $false
$importAttempted = $false

try {
    $currentUserSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $mutexName = "Local\agent-rdp-signing-$($currentUserSid.Replace('-', '_'))"
    $storeMutex = [System.Threading.Mutex]::new($false, $mutexName)
    try {
        $mutexHeld = $storeMutex.WaitOne(30000)
    }
    catch [System.Threading.AbandonedMutexException] {
        # An abandoned mutex is granted to this thread; continue while recording ownership.
        $mutexHeld = $true
    }
    if (-not $mutexHeld) {
        throw "Timed out waiting for exclusive access to the CurrentUser certificate store"
    }

    $securePassword = ConvertTo-SecureString $password -AsPlainText -Force
    $existingCertificateThumbprints = @(
        Get-ChildItem -Path "Cert:\CurrentUser\My" | ForEach-Object { $_.Thumbprint }
    )

    $ephemeralKeySet = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet
    $inspectedCertificates.Import($CertificatePath, $password, $ephemeralKeySet)
    $expectedCertificateThumbprints = @(
        $inspectedCertificates |
            ForEach-Object { $_.Thumbprint } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
            Sort-Object -Unique
    )
    $overlappingThumbprints = @(
        $expectedCertificateThumbprints |
            Where-Object { $existingCertificateThumbprints -contains $_ }
    )
    if ($overlappingThumbprints.Count -gt 0) {
        throw "A certificate from the PFX already exists in Cert:\CurrentUser\My; refusing to mutate pre-existing certificate state"
    }

    $lateOverlappingThumbprints = @(
        Get-ChildItem -Path "Cert:\CurrentUser\My" |
            Where-Object { $expectedCertificateThumbprints -contains $_.Thumbprint } |
            ForEach-Object { $_.Thumbprint }
    )
    if ($lateOverlappingThumbprints.Count -gt 0) {
        throw "The CurrentUser certificate store changed while the PFX was being inspected; refusing the import"
    }
    $password = $null

    $importParameters = @{
        FilePath          = $CertificatePath
        CertStoreLocation = "Cert:\CurrentUser\My"
        Password          = $securePassword
        Exportable        = $false
    }
    $importAttempted = $true
    $importedCertificates = @(Import-PfxCertificate @importParameters)

    $codeSigningOid = "1.3.6.1.5.5.7.3.3"
    $signingCertificates = @(
        $importedCertificates | Where-Object {
            $_.HasPrivateKey -and
            ($_.EnhancedKeyUsageList | Where-Object { $_.ObjectId.Value -eq $codeSigningOid })
        }
    )
    if ($signingCertificates.Count -ne 1) {
        throw "The PFX must contain exactly one private-key certificate with the Code Signing EKU"
    }

    $certificate = $signingCertificates[0]
    foreach ($file in $peFiles) {
        Write-Host "[agent-rdp] Authenticode signing $($file.Name)"
        $signArguments = @(
            "sign",
            "/sha1", $certificate.Thumbprint,
            "/s", "My",
            "/fd", "SHA256"
        )
        if (-not $SkipTimestamp) {
            $signArguments += @(
                "/tr", $TimestampUrl,
                "/td", "SHA256"
            )
        }
        $signArguments += @(
            "/d", "agent-rdp",
            "/du", "https://github.com/Hubert-Rybak/agent-rdp",
            $file.FullName
        )
        Invoke-SignTool `
            -SignToolPath $signTool `
            -Arguments $signArguments `
            -TimeoutSeconds $SignToolTimeoutSeconds `
            -Operation "sign $($file.FullName)"

        $verifyArguments = @("verify", "/pa", "/all", $file.FullName)
        Invoke-SignTool `
            -SignToolPath $signTool `
            -Arguments $verifyArguments `
            -TimeoutSeconds $SignToolTimeoutSeconds `
            -Operation "verify $($file.FullName)"

        if ($SkipTimestamp) {
            # SkipTimestamp signing already selected the certificate by thumbprint and SignTool verification succeeded.
            # Avoid WinVerifyTrust revocation lookups for an ephemeral self-signed CI certificate.
            continue
        }

        $signature = Get-AuthenticodeSignature -FilePath $file.FullName
        if ($signature.Status -ne "Valid") {
            throw "Authenticode status for $($file.FullName) is $($signature.Status), not Valid"
        }
        if (
            -not $signature.SignerCertificate -or
            $signature.SignerCertificate.Thumbprint -ne $certificate.Thumbprint
        ) {
            throw "Authenticode signer thumbprint does not match the imported code-signing certificate for $($file.FullName)"
        }
        if (-not $SkipTimestamp -and -not $signature.TimeStamperCertificate) {
            throw "Authenticode signature for $($file.FullName) does not contain an RFC 3161 timestamp"
        }
    }
}
catch {
    $operationError = $_
}
finally {
    $password = $null
    try {
        foreach ($inspectedCertificate in $inspectedCertificates) {
            try {
                $inspectedCertificate.Dispose()
            }
            catch {
                [void]$cleanupErrors.Add("Could not dispose an inspected certificate: $($_.Exception.Message)")
            }
        }

        foreach ($importedCertificate in $importedCertificates) {
            $importedThumbprint = "<unknown>"
            try {
                $importedThumbprint = $importedCertificate.Thumbprint
                $importedCertificate.Dispose()
            }
            catch {
                [void]$cleanupErrors.Add("Could not dispose imported certificate $importedThumbprint`: $($_.Exception.Message)")
            }
        }

        foreach ($thumbprint in $expectedCertificateThumbprints) {
            if ($importAttempted -and $existingCertificateThumbprints -notcontains $thumbprint) {
                try {
                    $storePath = "Cert:\CurrentUser\My\$thumbprint"
                    if (Test-Path $storePath) {
                        Remove-Item -Path $storePath -DeleteKey -Force
                    }
                }
                catch {
                    [void]$cleanupErrors.Add("Could not remove imported certificate $thumbprint and its private key: $($_.Exception.Message)")
                }
            }
        }
    }
    catch {
        [void]$cleanupErrors.Add("Unexpected certificate cleanup failure: $($_.Exception.Message)")
    }
    finally {
        if ($securePassword) {
            try {
                $securePassword.Dispose()
            }
            catch {
                [void]$cleanupErrors.Add("Could not dispose the signing password: $($_.Exception.Message)")
            }
        }
        if ($mutexHeld -and $storeMutex) {
            try {
                $storeMutex.ReleaseMutex()
            }
            catch {
                [void]$cleanupErrors.Add("Could not release the certificate-store mutex: $($_.Exception.Message)")
            }
        }
        if ($storeMutex) {
            try {
                $storeMutex.Dispose()
            }
            catch {
                [void]$cleanupErrors.Add("Could not dispose the certificate-store mutex: $($_.Exception.Message)")
            }
        }
    }
}

if ($operationError) {
    if ($cleanupErrors.Count -gt 0) {
        throw "Signing failed: $($operationError.Exception.Message). Cleanup also failed: $($cleanupErrors -join '; ')"
    }
    throw $operationError
}
if ($cleanupErrors.Count -gt 0) {
    throw "Signing completed, but cleanup failed: $($cleanupErrors -join '; ')"
}

Write-Host "[agent-rdp] Signed and verified $($peFiles.Count) PE artifact(s)."
