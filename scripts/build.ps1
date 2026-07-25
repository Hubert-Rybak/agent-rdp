#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Builds rdp2exec (Windows-to-Windows edition) natively on Windows.

.DESCRIPTION
    Bootstraps vcpkg (if VCPKG_ROOT is not already set), installs FreeRDP
    (with the "client" feature, giving us wfreerdp.exe + dev headers/libs)
    via the vcpkg.json manifest, configures and builds this repo's
    CMakeLists.txt (the client-side plugin DLL + the target-side bridge
    EXE), and stages everything an operator needs into ./artifacts:
      - rdp2exec_bridge.exe   (staged over the RDP session, never copied to
                                the remote host's disk -- see README)
      - rdp2exec-client.dll   (the FreeRDP DVC plugin)
      - wfreerdp.exe + its runtime DLLs, copied alongside so
        src/launcher/rdp2exec.py can find everything via --wfreerdp /
        --plugin-dir defaults without extra setup.

    Requires: PowerShell 7+, git, a C++ toolchain (Visual Studio Build
    Tools with the "Desktop development with C++" workload, or mingw-w64),
    and CMake/Ninja on PATH.

.PARAMETER Triplet
    vcpkg triplet to build against. Defaults to x64-windows (MSVC, dynamic
    CRT). Use x64-mingw-dynamic if building with mingw-w64 instead.

.PARAMETER Configuration
    CMake build configuration. Defaults to Release.
#>
param(
    [string]$Triplet = "x64-windows",
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
        Write-Host "[rdp2exec] Cloning vcpkg into $local ..."
        # Output of unassigned native-command calls inside a function leaks
        # into the function's return value in PowerShell -- pipe to Out-Null
        # so $vcpkgRoot below ends up as the plain path string, not a mix of
        # git's/the bootstrapper's console output plus the path.
        git clone --depth 1 https://github.com/microsoft/vcpkg.git $local | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "git clone of vcpkg failed" }
    }
    $bootstrap = Join-Path $local "bootstrap-vcpkg.bat"
    if (-not (Test-Path (Join-Path $local "vcpkg.exe"))) {
        Write-Host "[rdp2exec] Bootstrapping vcpkg ..."
        & $bootstrap -disableMetrics | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "vcpkg bootstrap failed" }
    }
    return $local
}

$vcpkgRoot = Find-VcpkgRoot
$vcpkgExe = Join-Path $vcpkgRoot "vcpkg.exe"
$toolchainFile = Join-Path $vcpkgRoot "scripts\buildsystems\vcpkg.cmake"

Write-Host "[rdp2exec] Installing dependencies via vcpkg (triplet=$Triplet) ..."
& $vcpkgExe install "--triplet=$Triplet" "--x-manifest-root=$RepoRoot" "--x-install-root=$RepoRoot\vcpkg_installed"
if ($LASTEXITCODE -ne 0) { throw "vcpkg install failed" }

$installedDir = Join-Path $RepoRoot "vcpkg_installed\$Triplet"
$pkgConfigDirs = Get-ChildItem -Path $installedDir -Recurse -Directory -Filter "pkgconfig" -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty FullName
if (-not $pkgConfigDirs) {
    throw "Could not locate a pkgconfig directory under $installedDir -- did the freerdp/pkgconf install succeed?"
}
$env:PKG_CONFIG_PATH = ($pkgConfigDirs -join ";")
Write-Host "[rdp2exec] PKG_CONFIG_PATH = $($env:PKG_CONFIG_PATH)"

$buildDir = Join-Path $RepoRoot "build"
Write-Host "[rdp2exec] Configuring CMake ..."
cmake -S $RepoRoot -B $buildDir `
    "-DCMAKE_TOOLCHAIN_FILE=$toolchainFile" `
    "-DVCPKG_TARGET_TRIPLET=$Triplet" `
    "-DCMAKE_BUILD_TYPE=$Configuration"
if ($LASTEXITCODE -ne 0) { throw "CMake configure failed" }

Write-Host "[rdp2exec] Building ..."
cmake --build $buildDir --config $Configuration
if ($LASTEXITCODE -ne 0) { throw "CMake build failed" }

$artifacts = Join-Path $RepoRoot "artifacts"
New-Item -ItemType Directory -Force -Path $artifacts | Out-Null

Write-Host "[rdp2exec] Staging build outputs into $artifacts ..."
Get-ChildItem -Path $buildDir -Recurse -Include "rdp2exec_bridge.exe", "rdp2exec-client.dll" -ErrorAction SilentlyContinue |
    Copy-Item -Destination $artifacts -Force

Write-Host "[rdp2exec] Locating wfreerdp.exe from the vcpkg install ..."
$wfreerdp = Get-ChildItem -Path $installedDir -Recurse -Filter "wfreerdp.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($wfreerdp) {
    Copy-Item -Path $wfreerdp.FullName -Destination $artifacts -Force
    # Pull in DLLs from the same directory (FreeRDP/WinPR/OpenSSL/etc runtime deps).
    Get-ChildItem -Path $wfreerdp.DirectoryName -Filter "*.dll" -ErrorAction SilentlyContinue |
        Copy-Item -Destination $artifacts -Force
    Write-Host "[rdp2exec] Staged wfreerdp.exe -> $artifacts"
} else {
    Write-Warning ("wfreerdp.exe not found under $installedDir. The 'client' feature may not have produced a " + `
        "standalone client binary in this FreeRDP version/triplet -- locate it manually and pass --wfreerdp " + `
        "to src/launcher/rdp2exec.py, or check the vcpkg port's install layout.")
}

Write-Host ""
Write-Host "[rdp2exec] Build complete. Artifacts in: $artifacts"
Write-Host "[rdp2exec] IMPORTANT: rdp2exec-client.dll must be discoverable by wfreerdp.exe's DVC addin loader."
Write-Host "           If wfreerdp does not pick it up from $artifacts automatically, consult your FreeRDP"
Write-Host "           build's addin search path (commonly alongside the exe) and copy it there too."
