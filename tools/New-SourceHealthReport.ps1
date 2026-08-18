[CmdletBinding()]
param(
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $projectRoot 'tests\stage-5-source-health-report.v1.json'
}
$registry = Get-Content -Raw -Encoding UTF8 (Join-Path $projectRoot 'specs\connectors\source-registry.v1.json') | ConvertFrom-Json
$sources = @($registry.sources)
$statusCounts = @{}
foreach ($status in @($registry.status_values)) { $statusCounts[$status] = @($sources | Where-Object status -eq $status).Count }
$familyRows = @($sources | Group-Object source_family | Sort-Object Name | ForEach-Object {
    [ordered]@{ source_family=$_.Name; count=$_.Count; live_or_ready=@($_.Group | Where-Object { $_.status -like 'live_connected*' -or $_.status -eq 'endpoint_verified_connector_ready' }).Count }
})
$probeIssues = @($sources | Where-Object { $_.probe.status -match 'over_retrieval|low_precision|extra_|registration_may|probe_required' } | ForEach-Object {
    [ordered]@{ source_id=$_.source_id; status=$_.probe.status; required_action=$_.quality_gates }
})
$report = [ordered]@{
    report_id = 'CR.REPORT.STAGE5.SOURCE_HEALTH.001'
    generated_at = [DateTimeOffset]::Now.ToString('o')
    registered_sources = $sources.Count
    status_counts = $statusCounts
    source_families = $familyRows
    license_pending_sources = @($sources | Where-Object license_status -match 'pending' | Select-Object source_id,name,status,license_status)
    probe_issues = $probeIssues
    registered_pending_access = @($registry.registered_pending_access)
    production_policy = [ordered]@{
        official_sources = 'ready_for_runtime_fetch_and_hash'
        gildata = 'live_connected_but_license_and_post_filter_gated'
        commercial_channel_and_alternative_data = 'not_connected_pending_procurement_and_compliance'
    }
}
$json = $report | ConvertTo-Json -Depth 20
[IO.File]::WriteAllText($OutputPath, $json + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
$json

