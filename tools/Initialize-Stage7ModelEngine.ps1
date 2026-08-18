[CmdletBinding()]
param(
    [string]$Database = (Join-Path $PSScriptRoot "..\data\curated\consumer-research.db")
)

$ErrorActionPreference = "Stop"
$python = $env:CODEX_PYTHON
if (-not $python) {
    $python = "python"
}

& $python (Join-Path $PSScriptRoot "consumer_model_engine.py") --db $Database init
if ($LASTEXITCODE -ne 0) {
    throw "Stage 7 model-engine initialization failed with exit code $LASTEXITCODE"
}
