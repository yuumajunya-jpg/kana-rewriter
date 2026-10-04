param(
    [string]$Compiler = "",
    [ValidatePattern('^[A-Za-z0-9_-]+\.exe$')]
    [string]$OutputName = "kana-editor-worker.exe",
    [switch]$SkipTests
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$output = Join-Path $root "build\native"
$executable = Join-Path $output $OutputName
New-Item -ItemType Directory -Force -Path $output | Out-Null
if ($Compiler) {
    # llvm-mingw/MinGW clang++; no system installation or PATH changes.
    & $Compiler -std=c++17 -O2 -Wall -Wextra -Wpedantic -DUNICODE -D_UNICODE -DNOMINMAX -DWIN32_LEAN_AND_MEAN `
        (Join-Path $root "native\worker.cpp") -o $executable `
        -lole32 -loleaut32 -luuid -luser32 -limm32 -static
    if ($LASTEXITCODE -ne 0) { throw "Native compilation failed" }
} else {
    # Run from a VS Developer PowerShell with C++ tools and CMake installed.
    & cmake -S (Join-Path $root "native") -B $output
    if ($LASTEXITCODE -ne 0) { throw "CMake configuration failed" }
    & cmake --build $output --config Release
    if ($LASTEXITCODE -ne 0) { throw "Native compilation failed" }
    $release = Join-Path $output "Release\kana-editor-worker.exe"
    if (Test-Path -LiteralPath $release) {
        Copy-Item -LiteralPath $release -Destination $executable
    } elseif ($OutputName -ne "kana-editor-worker.exe") {
        Copy-Item -LiteralPath (Join-Path $output "kana-editor-worker.exe") -Destination $executable
    }
}
if (-not $SkipTests) {
    & $executable --self-test
    if ($LASTEXITCODE -ne 0) { throw "Native self-test failed" }
}
Write-Output "Built: $executable"
