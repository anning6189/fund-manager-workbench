[CmdletBinding(DefaultParameterSetName = "Run")]
param(
    [Parameter(ParameterSetName = "Run")]
    [string]$Request = "",

    [Parameter(Mandatory = $true, ParameterSetName = "Resume")]
    [string]$ResumeRunId,

    [string]$Database = "",
    [string]$OutputRoot = ""
)

$ErrorActionPreference = "Stop"
if (-not $Request) {
    $Request = Join-Path $PSScriptRoot "..\tests\fixtures\stage8-research-workflow-request.v1.json"
}
if (-not $Database) {
    $Database = Join-Path $PSScriptRoot "..\data\curated\consumer-research.db"
}
if (-not $OutputRoot) {
    $OutputRoot = Join-Path $PSScriptRoot "..\data\workflows\stage8"
}
$python = $env:CODEX_PYTHON
if (-not $python) {
    $python = "python"
}
$runtime = Join-Path $PSScriptRoot "consumer_workflow_engine.py"

if ($PSCmdlet.ParameterSetName -eq "Resume") {
    & $python $runtime --db $Database resume --run-id $ResumeRunId
}
else {
    & $python $runtime --db $Database run --request $Request --output-root $OutputRoot
}

if ($LASTEXITCODE -ne 0) {
    throw "Stage 8 workflow command failed with exit code $LASTEXITCODE"
}
