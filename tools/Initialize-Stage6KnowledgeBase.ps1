[CmdletBinding()]
param(
    [string]$DatabasePath = "",
    [switch]$SkipSeed
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = 'C:\Users\chi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "Bundled Python runtime not found: $python" }
if ([string]::IsNullOrWhiteSpace($DatabasePath)) {
    $DatabasePath = Join-Path $projectRoot 'data\curated\consumer-research.db'
}
$runtime = Join-Path $PSScriptRoot 'consumer_knowledge_store.py'
$seed = Join-Path $projectRoot 'data\seed\stage6-consumer-core-seed.v1.json'

& $python $runtime --db $DatabasePath init
if ($LASTEXITCODE -ne 0) { throw 'Knowledge store initialization failed.' }
if (-not $SkipSeed) {
    & $python $runtime --db $DatabasePath ingest --package $seed
    if ($LASTEXITCODE -ne 0) { throw 'Stage 6 seed ingestion failed.' }
}
& $python $runtime --db $DatabasePath status
if ($LASTEXITCODE -ne 0) { throw 'Knowledge store status check failed.' }
