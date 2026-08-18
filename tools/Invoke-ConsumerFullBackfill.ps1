param(
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
  [string]$CutoffDate,
  [string]$Database = (Join-Path $PSScriptRoot '..\data\curated\consumer-research.db')
)

$ErrorActionPreference = 'Stop'
$Python = 'C:\Users\chi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $Python (Join-Path $PSScriptRoot 'consumer_data_production.py') --db $Database plan --cutoff-date $CutoffDate

