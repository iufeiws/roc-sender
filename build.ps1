$ErrorActionPreference = 'Stop'

$toolchain = Get-ChildItem "$PSScriptRoot/tools" -Directory -Filter 'llvm-mingw-*-ucrt-x86_64' |
    Sort-Object Name -Descending | Select-Object -First 1
if (-not $toolchain) {
    throw 'LLVM-MinGW is missing. Run setup.ps1 first.'
}

$env:PATH = "$($toolchain.FullName)/bin;$PSScriptRoot/.conda/Scripts;$env:PATH"
cmake -S $PSScriptRoot -B "$PSScriptRoot/build" -G Ninja `
    -DCMAKE_BUILD_TYPE=Release `
    -DCMAKE_C_COMPILER=clang `
    -DCMAKE_CXX_COMPILER=clang++
if ($LASTEXITCODE -ne 0) { throw "CMake configure failed with exit code $LASTEXITCODE" }
cmake --build "$PSScriptRoot/build" --config Release
if ($LASTEXITCODE -ne 0) { throw "CMake build failed with exit code $LASTEXITCODE" }
