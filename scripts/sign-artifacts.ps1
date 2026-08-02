#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Authenticode-signs every staged Windows executable and DLL.

.DESCRIPTION
    Imports a PFX into the ephemeral CurrentUser certificate store, signs all
    PE artifacts with SHA-256 and an RFC 3161 timestamp, verifies every
    signature, and removes all imported certificates in a finally block.
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
    [string]$TimestampUrl = "http://timestamp.digicert.com"
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
        $candidate = Get-ChildItem -Path $kitsRoot -Recurse -File -Filter "signtool.exe" -ErrorAction SilentlyContinue |
            Where-Object { $_.Directory.Name -eq "x64" } |
            Sort-Object -Property FullName -Descending |
            Select-Object -First 1
        if ($candidate) {
            return $candidate.FullName
        }
    }

    throw "signtool.exe was not found on PATH or in the Windows 10 SDK"
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
$securePassword = ConvertTo-SecureString $password -AsPlainText -Force
$password = $null
$importedCertificates = @()
$existingCertificateThumbprints = @(
    Get-ChildItem -Path "Cert:\CurrentUser\My" | ForEach-Object { $_.Thumbprint }
)

try {
    $importParameters = @{
        FilePath          = $CertificatePath
        CertStoreLocation = "Cert:\CurrentUser\My"
        Password          = $securePassword
        Exportable        = $false
    }
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
            "/fd", "SHA256",
            "/tr", $TimestampUrl,
            "/td", "SHA256",
            "/d", "agent-rdp",
            "/du", "https://github.com/Hubert-Rybak/agent-rdp",
            $file.FullName
        )
        & $signTool @signArguments
        if ($LASTEXITCODE -ne 0) {
            throw "signtool failed to sign $($file.FullName)"
        }

        & $signTool "verify" "/pa" "/all" $file.FullName
        if ($LASTEXITCODE -ne 0) {
            throw "signtool could not verify $($file.FullName)"
        }
    }

    Write-Host "[agent-rdp] Signed and verified $($peFiles.Count) PE artifact(s)."
}
finally {
    foreach ($certificate in $importedCertificates) {
        $storePath = "Cert:\CurrentUser\My\$($certificate.Thumbprint)"
        if (
            $existingCertificateThumbprints -notcontains $certificate.Thumbprint -and
            (Test-Path $storePath)
        ) {
            Remove-Item -Path $storePath -Force
        }
    }
    if ($securePassword) {
        $securePassword.Dispose()
    }
}
