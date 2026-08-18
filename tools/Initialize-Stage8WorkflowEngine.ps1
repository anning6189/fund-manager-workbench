[CmdletBinding()]
param(
    [string]$Database = ""
)

$ErrorActionPreference = "Stop"
if (-not $Database) {
    $Database = Join-Path $PSScriptRoot "..\data\curated\consumer-research.db"
}
$python = $env:CODEX_PYTHON
if (-not $python) {
    $python = "python"
}

& $python (Join-Path $PSScriptRoot "consumer_workflow_engine.py") --db $Database init
if ($LASTEXITCODE -ne 0) {
    throw "Stage 8 workflow-engine initialization failed with exit code $LASTEXITCODE"
}
