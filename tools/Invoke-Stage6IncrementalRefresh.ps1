[CmdletBinding()]
param(
    [string]$DatabasePath = "",
    [string]$InboxPath = ""
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = 'C:\Users\chi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "Bundled Python runtime not found: $python" }
if ([string]::IsNullOrWhiteSpace($DatabasePath)) { $DatabasePath = Join-Path $projectRoot 'data\curated\consumer-research.db' }
if ([string]::IsNullOrWhiteSpace($InboxPath)) { $InboxPath = Join-Path $projectRoot 'data\normalized\inbox' }
$runtime = Join-Path $PSScriptRoot 'consumer_knowledge_store.py'

New-Item -ItemType Directory -Force -Path $InboxPath | Out-Null
$results = New-Object 'System.Collections.Generic.List[object]'
Get-ChildItem -LiteralPath $InboxPath -Filter '*.json' -File | Sort-Object Name | ForEach-Object {
    $package = Get-Content -Raw -Encoding UTF8 $_.FullName | ConvertFrom-Json
    $sourceId = [string]$package.source_id
    $licenseGated = $sourceId.StartsWith('CR.SRC.GILDATA.', [StringComparison]::OrdinalIgnoreCase)
    $target = if ($licenseGated) { 'raw' } else { 'curated' }
    $output = & $python $runtime --db $DatabasePath ingest --package $_.FullName --target $target 2>&1 | Out-String
    $exitCode = $LASTEXITCODE
    $results.Add([pscustomobject]@{ package=$_.Name; target=$target; exit_code=$exitCode; output=$output.Trim() })
}
$plan = & $python $runtime --db $DatabasePath refresh-plan | Out-String
$freshness = & $python $runtime --db $DatabasePath freshness | Out-String
[ordered]@{
    run_at = [DateTimeOffset]::Now.ToString('o')
    inbox = $InboxPath
    packages_processed = $results.Count
    results = $results
    next_refresh_plan = ($plan | ConvertFrom-Json)
    freshness = ($freshness | ConvertFrom-Json)
} | ConvertTo-Json -Depth 20
