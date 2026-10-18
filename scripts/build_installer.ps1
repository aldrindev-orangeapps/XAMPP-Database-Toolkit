param(
    [ValidatePattern('^[0-9A-Za-z][0-9A-Za-z._-]*$')]
    [string]$Version = "0.0.0-local",
    [switch]$SkipAppBuild
)

$ErrorActionPreference = "Stop"
$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$applicationExe = Join-Path $repositoryRoot "dist\XAMPP Database Toolkit.exe"
$installerScript = Join-Path $repositoryRoot "installer\XAMPP Database Toolkit.nsi"
$outputDirectory = Join-Path $repositoryRoot "dist"

if (-not $SkipAppBuild) {
    Push-Location $repositoryRoot
    try {
        python -m PyInstaller --noconfirm ".\XAMPP Database Toolkit.spec"
        if ($LASTEXITCODE -ne 0) {
            throw "PyInstaller failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }
}

if (-not (Test-Path -LiteralPath $applicationExe -PathType Leaf)) {
    throw "Expected application executable was not found: $applicationExe"
}

$compilerCandidates = @(
    (Get-Command makensis.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1),
    (Join-Path $env:ProgramFiles "NSIS\makensis.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "NSIS\makensis.exe")
) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) }
$compiler = $compilerCandidates | Select-Object -First 1
if (-not $compiler) {
    throw "NSIS was not found. Install it with: winget install --id NSIS.NSIS -e"
}

New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
$numericParts = @([regex]::Matches($Version, '\d+') | ForEach-Object { [int]$_.Value } | Select-Object -First 4)
while ($numericParts.Count -lt 4) {
    $numericParts += 0
}
if ($numericParts | Where-Object { $_ -gt 65535 }) {
    throw "Each numeric version component must be between 0 and 65535."
}
$numericVersion = $numericParts -join "."
Push-Location $repositoryRoot
try {
    & $compiler /V2 /WX "/DAPP_VERSION=$Version" "/DAPP_NUMERIC_VERSION=$numericVersion" "/DSOURCE_EXE=$applicationExe" "/DOUTPUT_DIR=$outputDirectory" $installerScript
    if ($LASTEXITCODE -ne 0) {
        throw "NSIS failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}

$installerExe = Join-Path $outputDirectory "XAMPP-Database-Toolkit-$Version-Setup.exe"
if (-not (Test-Path -LiteralPath $installerExe -PathType Leaf)) {
    throw "Expected installer was not found: $installerExe"
}
$hash = Get-FileHash -LiteralPath $installerExe -Algorithm SHA256
[pscustomobject]@{
    Path = $installerExe
    Bytes = (Get-Item -LiteralPath $installerExe).Length
    SHA256 = $hash.Hash
} | Format-List
