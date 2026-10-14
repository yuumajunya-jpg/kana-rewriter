param([string]$Compiler = "")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$output = Join-Path $root "build\tsf-probe"
New-Item -ItemType Directory -Force -Path $output | Out-Null
if (-not $Compiler) {
    $Compiler = Join-Path $root "build\toolchain\llvm-mingw-20260922-ucrt-x86_64\bin\x86_64-w64-mingw32-clang++.exe"
}
$dllName = "kana-tsf-probe-{0}.dll" -f [DateTime]::UtcNow.ToString("yyyyMMddHHmmssfff")
if (-not (Test-Path -LiteralPath $Compiler)) {
    if (-not (Get-Command cmake -ErrorAction SilentlyContinue)) {
        throw "64-bit C++ compiler required. Use -Compiler <MinGW/llvm-mingw clang++.exe>, or run from a Visual Studio C++ developer shell with CMake."
    }
    $cmakeOutput = Join-Path $root "build\tsf-cmake"
    & cmake -S (Join-Path $root "native") -B $cmakeOutput -A x64
    if ($LASTEXITCODE -ne 0) { throw "TSF CMake configuration failed" }
    & cmake --build $cmakeOutput --config Release --target kana-tsf-monitor tsf-probe-fixture
    if ($LASTEXITCODE -ne 0) { throw "TSF CMake build failed" }
    Copy-Item -LiteralPath (Join-Path $cmakeOutput "Release\kana-tsf-monitor.dll") -Destination (Join-Path $output $dllName)
    Copy-Item -LiteralPath (Join-Path $cmakeOutput "Release\tsf-probe-fixture.exe") -Destination (Join-Path $output "tsf-probe-fixture.exe")
} else {
    & $Compiler -std=c++17 -O2 -Wall -Wextra -shared (Join-Path $root "native\tsf_probe.cpp") `
        -o (Join-Path $output $dllName) -lole32 -loleaut32 -luuid -luser32 -static
    if ($LASTEXITCODE -ne 0) { throw "TSF probe build failed" }
    & $Compiler -std=c++17 -O2 -Wall -Wextra (Join-Path $root "native\tsf_probe_fixture.cpp") `
        -o (Join-Path $output "tsf-probe-fixture.exe") -lole32 -loleaut32 -luuid -luser32 -static
    if ($LASTEXITCODE -ne 0) { throw "TSF probe fixture build failed" }
}
# A target keeps its observed DLL mapped until process exit. Publish a fresh
# filename so rebuilding does not require closing the user's applications.
@{
    dll = $dllName
    source_sha256 = (Get-FileHash -LiteralPath (Join-Path $root "native\tsf_probe.cpp") -Algorithm SHA256).Hash.ToLowerInvariant()
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $output "latest.json") -Encoding utf8
Write-Output (Join-Path $output $dllName)
