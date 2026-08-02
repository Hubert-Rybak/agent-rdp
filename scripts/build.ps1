#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Builds agent-rdp (Windows-to-Windows edition) natively on Windows.

.DESCRIPTION
    Bootstraps vcpkg (if VCPKG_ROOT is not already set), installs FreeRDP
    (with the "client" feature, giving us wfreerdp.exe + dev headers/libs)
    via the vcpkg.json manifest, configures and builds this repo's
    CMakeLists.txt (the client-side plugin DLL + the target-side bridge
    EXE), and stages everything an operator needs into ./artifacts:
      - agent-rdp-bridge.exe   (staged over the RDP session, never copied to
                                the remote host's disk -- see README)
      - agent-rdp-client.dll   (the FreeRDP DVC plugin)
      - wfreerdp.exe + its runtime DLLs, copied alongside so
        src/launcher/agent_rdp.py can find everything via --wfreerdp /
        --plugin-dir defaults without extra setup.

    Requires: PowerShell 7+, git, a C++ toolchain (Visual Studio Build
    Tools with the "Desktop development with C++" workload, or mingw-w64),
    and CMake/Ninja on PATH.

.PARAMETER Triplet
    vcpkg triplet to build against. Defaults to the repository's custom
    x64-windows-agent-rdp triplet, which enables the wfreerdp Windows client
    disabled by the upstream vcpkg port.

.PARAMETER Configuration
    CMake build configuration. Defaults to Release.
#>
param(
    [string]$Triplet = "x64-windows-agent-rdp",
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Find-VcpkgRoot {
    if ($env:VCPKG_ROOT -and (Test-Path $env:VCPKG_ROOT)) {
        return $env:VCPKG_ROOT
    }
    $local = Join-Path $RepoRoot ".vcpkg"
    if (-not (Test-Path $local)) {
        Write-Host "[agent-rdp] Cloning vcpkg into $local ..."
        # Output of unassigned native-command calls inside a function leaks
        # into the function's return value in PowerShell -- pipe to Out-Null
        # so $vcpkgRoot below ends up as the plain path string, not a mix of
        # git's/the bootstrapper's console output plus the path.
        git clone --depth 1 https://github.com/microsoft/vcpkg.git $local | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "git clone of vcpkg failed" }
    }
    $bootstrap = Join-Path $local "bootstrap-vcpkg.bat"
    if (-not (Test-Path (Join-Path $local "vcpkg.exe"))) {
        Write-Host "[agent-rdp] Bootstrapping vcpkg ..."
        & $bootstrap -disableMetrics | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "vcpkg bootstrap failed" }
    }
    return $local
}

$vcpkgRoot = Find-VcpkgRoot
$vcpkgExe = Join-Path $vcpkgRoot "vcpkg.exe"
$toolchainFile = Join-Path $vcpkgRoot "scripts\buildsystems\vcpkg.cmake"

Write-Host "[agent-rdp] Installing dependencies via vcpkg (triplet=$Triplet) ..."
& $vcpkgExe install "--triplet=$Triplet" "--overlay-triplets=$RepoRoot\triplets" `
    "--x-manifest-root=$RepoRoot" "--x-install-root=$RepoRoot\vcpkg_installed"
if ($LASTEXITCODE -ne 0) { throw "vcpkg install failed" }

$installedDir = Join-Path $RepoRoot "vcpkg_installed\$Triplet"
$pkgConfigDirs = Get-ChildItem -Path $installedDir -Recurse -Directory -Filter "pkgconfig" -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty FullName
if (-not $pkgConfigDirs) {
    throw "Could not locate a pkgconfig directory under $installedDir -- did the freerdp/pkgconf install succeed?"
}
$env:PKG_CONFIG_PATH = ($pkgConfigDirs -join ";")
Write-Host "[agent-rdp] PKG_CONFIG_PATH = $($env:PKG_CONFIG_PATH)"

$buildDir = Join-Path $RepoRoot "build"
Write-Host "[agent-rdp] Configuring CMake ..."
cmake -S $RepoRoot -B $buildDir `
    "-DCMAKE_TOOLCHAIN_FILE=$toolchainFile" `
    "-DVCPKG_TARGET_TRIPLET=$Triplet" `
    "-DVCPKG_OVERLAY_TRIPLETS=$RepoRoot\triplets" `
    "-DCMAKE_BUILD_TYPE=$Configuration"
if ($LASTEXITCODE -ne 0) { throw "CMake configure failed" }

Write-Host "[agent-rdp] Building ..."
cmake --build $buildDir --config $Configuration
if ($LASTEXITCODE -ne 0) { throw "CMake build failed" }

$artifacts = Join-Path $RepoRoot "artifacts"
if (Test-Path $artifacts) { Remove-Item -Path $artifacts -Recurse -Force }
New-Item -ItemType Directory -Path $artifacts | Out-Null

Write-Host "[agent-rdp] Staging build outputs into $artifacts ..."
Get-ChildItem -Path $buildDir -Recurse -Include "agent-rdp-bridge.exe", "agent-rdp-client.dll" -ErrorAction SilentlyContinue |
    Copy-Item -Destination $artifacts -Force

Write-Host "[agent-rdp] Locating wfreerdp.exe from the vcpkg install ..."
$wfreerdp = Get-ChildItem -Path $installedDir -Recurse -Filter "wfreerdp.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($wfreerdp) {
    Copy-Item -Path $wfreerdp.FullName -Destination $artifacts -Force
    # vcpkg commonly installs wfreerdp under tools/freerdp while placing its
    # FreeRDP, WinPR, OpenSSL, and other runtime DLLs under bin.
    @($wfreerdp.DirectoryName, (Join-Path $installedDir "bin")) |
        Select-Object -Unique |
        ForEach-Object {
            Get-ChildItem -Path $_ -Filter "*.dll" -File -ErrorAction SilentlyContinue |
                Copy-Item -Destination $artifacts -Force
        }
    Write-Host "[agent-rdp] Staged wfreerdp.exe -> $artifacts"
} else {
    throw "wfreerdp.exe not found under $installedDir; cannot produce a runnable application"
}

$requiredArtifacts = @("agent-rdp-bridge.exe", "agent-rdp-client.dll", "wfreerdp.exe")
foreach ($requiredArtifact in $requiredArtifacts) {
    if (-not (Test-Path (Join-Path $artifacts $requiredArtifact) -PathType Leaf)) {
        throw "Required artifact was not staged: $requiredArtifact"
    }
}
if (-not (Get-ChildItem -Path $artifacts -Filter "*.dll" -File | Where-Object Name -ne "agent-rdp-client.dll")) {
    throw "No FreeRDP/runtime DLLs were staged; cannot produce a runnable application"
}

Write-Host ""
Write-Host "[agent-rdp] Build complete. Artifacts in: $artifacts"
Write-Host "[agent-rdp] IMPORTANT: agent-rdp-client.dll must be discoverable by wfreerdp.exe's DVC addin loader."
Write-Host "           If wfreerdp does not pick it up from $artifacts automatically, consult your FreeRDP"
Write-Host "           build's addin search path (commonly alongside the exe) and copy it there too."
