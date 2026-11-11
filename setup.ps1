param(
    # Skip the Python environment; fetch and patch only what build.ps1 needs.
    [switch]$NativeOnly
)

$ErrorActionPreference = 'Stop'

function Assert-LastExitCode([string]$Operation) {
    if ($LASTEXITCODE -ne 0) { throw "$Operation failed with exit code $LASTEXITCODE" }
}

function Get-PinnedRepository([string]$Uri, [string]$Directory, [string]$Commit) {
    if (-not (Test-Path "$Directory/.git")) {
        git clone $Uri $Directory
        Assert-LastExitCode "Cloning $Uri"
        git -C $Directory checkout --quiet --detach $Commit
        Assert-LastExitCode "Checking out $Commit"
    }
    $actual = git -C $Directory rev-parse HEAD
    Assert-LastExitCode "Reading $Directory revision"
    if ($actual -ne $Commit) {
        throw "$Directory is at $actual; expected $Commit. Resolve the checkout explicitly."
    }
}

function Get-VerifiedArchive([string]$Uri, [string]$File, [string]$Sha256) {
    if (-not (Test-Path $File)) {
        Invoke-WebRequest -Uri $Uri -OutFile $File
    }
    $actual = (Get-FileHash $File -Algorithm SHA256).Hash
    if ($actual -ne $Sha256) {
        throw "$File SHA-256 is $actual; expected $Sha256"
    }
}

if (-not $NativeOnly -and -not (Test-Path .conda/python.exe)) {
    conda env create --prefix ./.conda --file environment.yml
    Assert-LastExitCode 'Creating the Conda environment'
}

New-Item -ItemType Directory -Force vendor, tools | Out-Null
Get-PinnedRepository 'https://github.com/roc-streaming/roc-toolkit.git' `
    'vendor/roc-toolkit' '60db91cbba666972845d190d981bc0d093298699'
Get-PinnedRepository 'https://github.com/libuv/libuv.git' `
    'vendor/libuv' '5152db2cbfeb5582e9c27c5ea1dba2cd9e10759b'
Get-PinnedRepository 'https://github.com/roc-streaming/openfec.git' `
    'vendor/openfec' 'e549f3b8ba54a7d6e2aaefdd1cdd4c73ef32000c'

git -C vendor/roc-toolkit apply --check ../../patches/roc-windows.patch 2>$null
if ($LASTEXITCODE -eq 0) {
    git -C vendor/roc-toolkit apply ../../patches/roc-windows.patch
    Assert-LastExitCode 'Applying the Roc Windows patch'
} else {
    git -C vendor/roc-toolkit apply --reverse --check ../../patches/roc-windows.patch 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'The Roc checkout is neither clean nor already patched; inspect it before continuing.'
    }
}

$llvmVersion = '20260908'
$llvmArchive = 'tools/llvm-mingw.zip'
Get-VerifiedArchive `
    "https://github.com/mstorsjo/llvm-mingw/releases/download/$llvmVersion/llvm-mingw-$llvmVersion-ucrt-x86_64.zip" `
    $llvmArchive '1BCF74D06B724AEECAA6412CA85F5B26FB1DA770E7CDCEFA9263C9C5C3AD34B6'
if (-not (Test-Path "tools/llvm-mingw-$llvmVersion-ucrt-x86_64/bin/clang.exe")) {
    Expand-Archive -LiteralPath $llvmArchive -DestinationPath tools -Force
}

$ragelArchive = 'tools/ragel-6.10.tar.gz'
Get-VerifiedArchive 'https://www.colm.net/files/ragel/ragel-6.10.tar.gz' `
    $ragelArchive '5F156EDB65D20B856D638DD9EE2DFB43285914D9AA2B6EC779DAC0270CD56C3F'
if (-not (Test-Path tools/ragel-6.10/ragel/rlscan.cpp)) {
    tar -xf $ragelArchive -C tools
    Assert-LastExitCode 'Extracting Ragel'
}

Write-Host 'Setup complete. Run ./build.ps1.'
