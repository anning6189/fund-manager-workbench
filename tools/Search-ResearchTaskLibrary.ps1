param(
  [string]$Query = '',
  [string]$SectorCode = '',
  [string]$Category = '',
  [string]$TemplateId = '',
  [string]$UserId = 'anonymous',
  [string]$Role = 'public_fund_manager',
  [string]$Database = (Join-Path $PSScriptRoot '..\data\curated\consumer-research.db')
)

$ErrorActionPreference = 'Stop'
$Python = 'C:\Users\chi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$Arguments = @((Join-Path $PSScriptRoot 'consumer_task_library.py'), '--db', $Database, 'search', '--query', $Query, '--role', $Role, '--user-id', $UserId)
if ($SectorCode) { $Arguments += @('--sector-code', $SectorCode) }
if ($Category) { $Arguments += @('--category', $Category) }
if ($TemplateId) { $Arguments += @('--template-id', $TemplateId) }
& $Python @Arguments

