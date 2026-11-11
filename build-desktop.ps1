#Requires -Version 7.0
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    foreach ($tool in @('conda')) {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
            throw "$tool is missing from PATH."
        }
    }
    foreach ($dll in @('roc.dll', 'libc++.dll', 'libunwind.dll')) {
        if (-not (Test-Path -LiteralPath "build/Release/$dll")) {
            throw "Missing build/Release/$dll. Run build.ps1 first."
        }
    }
    conda run --no-capture-output --prefix ./.conda python -m PyInstaller --noconfirm --clean RocSender.spec
    if ($LASTEXITCODE -ne 0) { throw 'Desktop packaging failed.' }
    Copy-Item LICENSE dist/RocSender/LICENSE -Force
    Copy-Item DESKTOP.md dist/RocSender/使用说明.md -Force
    Copy-Item patches/roc-windows.patch dist/RocSender/roc-windows.patch -Force
    Compress-Archive -Path dist/RocSender -DestinationPath dist/RocSender-Windows-x64.zip -Force
    Write-Host "Ready: $PSScriptRoot/dist/RocSender/RocSender.exe"
    Write-Host "Portable archive: $PSScriptRoot/dist/RocSender-Windows-x64.zip"
} finally {
    Pop-Location
}
