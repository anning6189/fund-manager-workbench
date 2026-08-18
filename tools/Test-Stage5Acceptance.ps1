[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$failures = New-Object 'System.Collections.Generic.List[string]'
function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { $script:failures.Add($Message) }
}

$sourceTestText = & (Join-Path $PSScriptRoot 'Test-Stage5Sources.ps1') | Out-String
$sourceTest = $sourceTestText | ConvertFrom-Json
$regressionText = & (Join-Path $PSScriptRoot 'Test-Stage34Regression.ps1') | Out-String
$regression = $regressionText | ConvertFrom-Json
$registry = Get-Content -Raw -Encoding UTF8 (Join-Path $projectRoot 'specs\connectors\source-registry.v1.json') | ConvertFrom-Json
$gildataProbe = Get-Content -Raw -Encoding UTF8 (Join-Path $projectRoot 'tests\stage-5-gildata-live-probe.v1.json') | ConvertFrom-Json
$officialProbe = Get-Content -Raw -Encoding UTF8 (Join-Path $projectRoot 'tests\stage-5-official-endpoint-probe.v1.json') | ConvertFrom-Json
$runtimeV2Text = & (Join-Path $PSScriptRoot 'Test-ResearchRuntimeV2.ps1') | Out-String
$runtimeV2 = $runtimeV2Text | ConvertFrom-Json

Assert-True ($sourceTest.passed -eq $true) 'Stage 5 source specification suite failed.'
Assert-True ($regression.passed -eq $true) 'Stage 3/4 regression suite failed after source integration.'
Assert-True ($gildataProbe.summary.capabilities_tested -eq 7) 'Not all seven Gildata capabilities were probed.'
Assert-True ($gildataProbe.summary.transport_success -eq 7) 'One or more Gildata transport probes failed.'
Assert-True ($gildataProbe.summary.post_filter_required -eq 7) 'Gildata precision risks were not fully recorded.'
Assert-True ($officialProbe.summary.official_sources_tested -eq 13) 'Not all official sources were probed.'
Assert-True ($officialProbe.summary.usable_access_path -eq 13) 'One or more official sources lack a usable access path.'
Assert-True ($officialProbe.summary.document_download_hash_pass -eq 1) 'Official document download/hash loop did not pass.'
Assert-True ($officialProbe.document_ingestion_probe.content_hash -match '^sha256:[0-9a-f]{64}$') 'Official document SHA-256 is invalid.'
Assert-True (@($registry.registered_pending_access).Count -eq 3) 'Expected three explicitly pending procurement categories.'
Assert-True (@($registry.sources | Where-Object license_status -match 'pending').Count -eq 7) 'Expected seven Gildata capabilities behind license gates.'
Assert-True ($runtimeV2.passed -eq $true) 'Research runtime v2 correctness/performance suite failed.'

$result = [ordered]@{
    suite_id = 'CR.TEST.STAGE5.ACCEPTANCE.001'
    run_at = [DateTimeOffset]::Now.ToString('o')
    passed = $failures.Count -eq 0
    decision = if ($failures.Count -eq 0) { 'accept_core_data_access_layer_with_declared_external_gates' } else { 'reject' }
    counts = [ordered]@{
        registered_sources = @($registry.sources).Count
        source_families = @($registry.sources.source_family | Sort-Object -Unique).Count
        gildata_transport_success = $gildataProbe.summary.transport_success
        official_usable_access_paths = $officialProbe.summary.usable_access_path
        commercial_categories_pending_procurement = @($registry.registered_pending_access).Count
        stage34_regression_passed = $regression.passed_count
        stage34_regression_total = $regression.total
        runtime_v2_guardrails_passed = @($runtimeV2.guardrails.PSObject.Properties | Where-Object Value -eq $true).Count
        runtime_v2_modeled_full_seconds = $runtimeV2.performance_model.optimized_full
        runtime_v2_modeled_improvement_ratio = $runtimeV2.performance_model.improvement_ratio
    }
    required_runtime_gates = @(
        'Gildata is restricted to connectivity and internal technical validation until license terms are confirmed.',
        'Every Gildata result requires exact entity, metric, period, and field projection filters.',
        'Runtime v2 enforces date alignment, unit lineage, verified risk entities, bounded split retries, cache-safe reads, and nonblocking enrichment.',
        'Material facts prefer official sources with locators and SHA-256.',
        'Use NBS release pages and licensed structured-source cross-checks when the interactive page blocks program access.',
        'Fund holdings, portfolio exposure, and position inference are prohibited.'
    )
    failures = $failures.ToArray()
}
$json = $result | ConvertTo-Json -Depth 12
$outputPath = Join-Path $projectRoot 'tests\stage-5-acceptance-report.v1.json'
[IO.File]::WriteAllText($outputPath, $json + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
$json
if ($failures.Count -gt 0) { exit 1 }
