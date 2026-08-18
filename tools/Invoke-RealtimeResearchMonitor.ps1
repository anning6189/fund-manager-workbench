param(
  [string]$CutoffTimestamp = '',
  [ValidateSet('scheduled','manual','event')]
  [string]$Mode = 'scheduled',
  [string]$Database = (Join-Path $PSScriptRoot '..\data\curated\consumer-research.db')
)

$ErrorActionPreference = 'Stop'
$Python = 'C:\Users\chi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (-not $CutoffTimestamp) {
  $CutoffTimestamp = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')
}
& $Python (Join-Path $PSScriptRoot 'consumer_realtime_monitor.py') --db $Database run --cutoff $CutoffTimestamp --mode $Mode

