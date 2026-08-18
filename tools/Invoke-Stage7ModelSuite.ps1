[CmdletBinding()]
param(
    [string]$Database = (Join-Path $PSScriptRoot "..\data\curated\consumer-research.db"),
    [string]$Python = $env:CODEX_PYTHON
)

$ErrorActionPreference = "Stop"
if (-not $Python) {
    $Python = "python"
}

$engine = Join-Path $PSScriptRoot "consumer_model_engine.py"
$modelRoot = Join-Path $PSScriptRoot "..\data\models\stage7"

& $Python $engine --db $Database init
if ($LASTEXITCODE -ne 0) {
    throw "Stage 7 initialization failed with exit code $LASTEXITCODE"
}

$packages = Get-ChildItem -LiteralPath $modelRoot -Filter "*.v1.json" |
    Where-Object { $_.Name -ne "stage7-model-suite.manifest.v1.json" } |
    Sort-Object Name

foreach ($package in $packages) {
    & $Python $engine --db $Database run --package $package.FullName
    if ($LASTEXITCODE -ne 0) {
        throw "Stage 7 model failed: $($package.Name)"
    }
}

& $Python $engine --db $Database status
if ($LASTEXITCODE -ne 0) {
    throw "Stage 7 status check failed with exit code $LASTEXITCODE"
}
