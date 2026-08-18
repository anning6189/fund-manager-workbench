param(
  [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
  [string]$AsOfDate = '2026-08-12',
  [string]$Database = (Join-Path $PSScriptRoot '..\data\curated\consumer-research.db')
)

$ErrorActionPreference = 'Stop'
$Python = 'C:\Users\chi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$Engine = Join-Path $PSScriptRoot 'full_consumer_coverage.py'
$Project = Resolve-Path (Join-Path $PSScriptRoot '..')
$Main = Join-Path $Project 'data\raw\licensed\gildata\a-share-consumer-universe-2026-08-12.json'
$Culture = Join-Path $Project 'data\raw\licensed\gildata\a-share-culture-education-universe-2026-08-12.json'

$UniverseJson = & $Python $Engine --db $Database build-universe --snapshot $Main --snapshot $Culture --as-of-date $AsOfDate
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$Universe = $UniverseJson | ConvertFrom-Json
& $Python $Engine --db $Database coverage-matrix --universe-id $Universe.universe_snapshot_id --as-of-date $AsOfDate
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Python $Engine --db $Database generate-tasks --cutoff "${AsOfDate}T23:59:59+08:00"

