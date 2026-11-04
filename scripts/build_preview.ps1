param([string]$Compiler = "")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $Compiler) {
    $Compiler = Join-Path $root "build/toolchain/llvm-mingw-20260922-ucrt-x86_64/bin/x86_64-w64-mingw32-clang++.exe"
}
$output = Join-Path $root "build/tsf-preview"
New-Item -ItemType Directory -Force -Path $output | Out-Null
$name = "kana-tsf-preview-{0}.dll" -f [DateTime]::UtcNow.ToString("yyyyMMddHHmmssfff")
if (Test-Path -LiteralPath $Compiler) {
    & $Compiler -std=c++17 -O2 -Wall -Wextra -DKANA_LIVE_PREVIEW -shared (Join-Path $root "native/tsf_probe.cpp") -o (Join-Path $output $name) -lole32 -loleaut32 -luuid -luser32 -static
    if ($LASTEXITCODE -ne 0) { throw "Preview TSF build failed" }
} else {
    if (-not (Get-Command cmake -ErrorAction SilentlyContinue)) { throw "Use a 64-bit C++ compiler or a Visual Studio C++ developer shell" }
    $cmakeOutput = Join-Path $root "build/tsf-cmake"
    & cmake -S (Join-Path $root "native") -B $cmakeOutput -A x64
    if ($LASTEXITCODE -ne 0) { throw "TSF CMake configuration failed" }
    & cmake --build $cmakeOutput --config Release --target kana-tsf-preview
    if ($LASTEXITCODE -ne 0) { throw "Preview TSF build failed" }
    Copy-Item -LiteralPath (Join-Path $cmakeOutput "Release/kana-tsf-preview.dll") -Destination (Join-Path $output $name)
}
@{dll=$name; source_sha256=(Get-FileHash -LiteralPath (Join-Path $root 'native/tsf_probe.cpp') -Algorithm SHA256).Hash.ToLowerInvariant()} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $output "latest.json") -Encoding utf8
Write-Output (Join-Path $output $name)
