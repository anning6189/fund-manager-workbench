[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonCandidates = @(
    (Join-Path $ProjectRoot 'runtime\python\python.exe'),
    'C:\Users\chi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe',
    (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1)
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
if (-not $PythonCandidates) { throw 'Python packaging runtime was not found.' }

$Python = $PythonCandidates[0]
& $Python -m PyInstaller --version | Out-Null
$Spec = Join-Path $ProjectRoot 'apps\fund-manager-workbench\consumer-research-workbench.spec'
$Dist = Join-Path $ProjectRoot 'apps\fund-manager-workbench\dist'
$Build = Join-Path $ProjectRoot 'apps\fund-manager-workbench\build-pyinstaller'
& $Python -m PyInstaller --noconfirm --clean --distpath $Dist --workpath $Build $Spec
if ($LASTEXITCODE -ne 0) { throw 'Workbench executable build failed.' }

$InnoCandidates = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
    'C:\Program Files\Inno Setup 6\ISCC.exe'
) | Where-Object { Test-Path -LiteralPath $_ }
if ($InnoCandidates) {
    & $InnoCandidates[0] (Join-Path $ProjectRoot 'apps\fund-manager-workbench\installer\ConsumerResearchWorkbench.iss')
    if ($LASTEXITCODE -ne 0) { throw 'Windows installer build failed.' }
} else {
    Write-Warning 'Inno Setup is not installed. The standalone executable was built; the installer script is ready.'
}
