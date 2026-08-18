param(
  [string]$Database = (Join-Path $PSScriptRoot '..\data\curated\consumer-research.db')
)

$ErrorActionPreference = 'Stop'
$Python = 'C:\Users\chi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $Python (Join-Path $PSScriptRoot 'consumer_task_library.py') --db $Database init

