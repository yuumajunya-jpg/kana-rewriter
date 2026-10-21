param([string]$Compiler = "")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$output = Join-Path $root "build\tsf-edit-probe"
New-Item -ItemType Directory -Force -Path $output | Out-Null
if (-not $Compiler) {
    $Compiler = Join-Path $root "build\toolchain\llvm-mingw-20260922-ucrt-x86_64\bin\x86_64-w64-mingw32-clang++.exe"
}
$dllName = "kana-tsf-edit-{0}.dll" -f [DateTime]::UtcNow.ToString("yyyyMMddHHmmssfff")
if (-not (Test-Path -LiteralPath $Compiler)) {
    if (-not (Get-Command cmake -ErrorAction SilentlyContinue)) { throw "Use a 64-bit C++ compiler or a Visual Studio C++ developer shell" }
    $cmakeOutput = Join-Path $root "build\tsf-cmake"
    & cmake -S (Join-Path $root "native") -B $cmakeOutput -A x64
    if ($LASTEXITCODE -ne 0) { throw "TSF CMake configuration failed" }
    & cmake --build $cmakeOutput --config Release --target kana-tsf-edit tsf-probe-fixture
    if ($LASTEXITCODE -ne 0) { throw "TSF editor build failed" }
    Copy-Item -LiteralPath (Join-Path $cmakeOutput "Release\kana-tsf-edit.dll") -Destination (Join-Path $output $dllName)
    Copy-Item -LiteralPath (Join-Path $cmakeOutput "Release\tsf-probe-fixture.exe") -Destination (Join-Path $output "tsf-probe-fixture.exe")
} else {
& $Compiler -std=c++17 -O2 -Wall -Wextra -shared (Join-Path $root "native\tsf_edit_probe.cpp") `
    -o (Join-Path $output $dllName) -lole32 -loleaut32 -luuid -luser32 -static
if ($LASTEXITCODE -ne 0) { throw "Experimental TSF editor build failed" }
& $Compiler -std=c++17 -O2 -Wall -Wextra (Join-Path $root "native\tsf_probe_fixture.cpp") `
    -o (Join-Path $output "tsf-probe-fixture.exe") -lole32 -loleaut32 -luuid -luser32 -static
if ($LASTEXITCODE -ne 0) { throw "Experimental TSF fixture build failed" }
}
@{ dll = $dllName; source_sha256 = (Get-FileHash -LiteralPath (Join-Path $root "native\tsf_edit_probe.cpp") -Algorithm SHA256).Hash.ToLowerInvariant() } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $output "latest.json") -Encoding utf8
Write-Output (Join-Path $output $dllName)
