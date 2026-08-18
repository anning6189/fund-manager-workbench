[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$registryPath = Join-Path $projectRoot 'specs\connectors\source-registry.v1.json'
$contractPath = Join-Path $projectRoot 'specs\connectors\unified-connector-contract.v1.json'
$officialPath = Join-Path $projectRoot 'specs\connectors\official-source-adapters.v1.json'
$gildataPath = Join-Path $projectRoot 'specs\connectors\gildata-capability-adapters.v1.json'
$financialPath = Join-Path $projectRoot 'specs\connectors\gildata-financial-research-adapter.v1.json'
$researchEvidencePath = Join-Path $projectRoot 'specs\research-evidence-standard.v1.json'
$runtimePolicyPath = Join-Path $projectRoot 'specs\connectors\research-runtime-policy.v2.json'

$registry = Get-Content -Raw -Encoding UTF8 $registryPath | ConvertFrom-Json
$contract = Get-Content -Raw -Encoding UTF8 $contractPath | ConvertFrom-Json
$official = Get-Content -Raw -Encoding UTF8 $officialPath | ConvertFrom-Json
$gildata = Get-Content -Raw -Encoding UTF8 $gildataPath | ConvertFrom-Json
$financial = Get-Content -Raw -Encoding UTF8 $financialPath | ConvertFrom-Json
$evidence = Get-Content -Raw -Encoding UTF8 $researchEvidencePath | ConvertFrom-Json
$runtimePolicy = Get-Content -Raw -Encoding UTF8 $runtimePolicyPath | ConvertFrom-Json

$failures = New-Object 'System.Collections.Generic.List[string]'
$warnings = New-Object 'System.Collections.Generic.List[string]'

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { $script:failures.Add($Message) }
}

$requiredSourceFields = @($registry.required_source_fields)
$sources = @($registry.sources)
$sourceIds = @($sources.source_id)
Assert-True ($sources.Count -ge 18) "Expected at least 18 registered sources; got $($sources.Count)."
Assert-True (@($sourceIds | Sort-Object -Unique).Count -eq $sourceIds.Count) 'Source IDs are not unique.'

foreach ($source in $sources) {
    foreach ($field in $requiredSourceFields) {
        $present = $source.PSObject.Properties.Name -contains $field
        Assert-True $present "Source $($source.source_id) missing field $field."
        if ($present -and $null -eq $source.$field) { Assert-True $false "Source $($source.source_id) has null field $field." }
    }
    if ($source.status -like 'live_connected*') {
        Assert-True ($source.probe.status -like 'success*') "Live source $($source.source_id) lacks successful probe."
    }
    if ($source.source_family -like 'official*') {
        Assert-True ($source.evidence_tier -eq 'A') "Official source $($source.source_id) is not evidence tier A."
        Assert-True (@($source.allowed_hosts).Count -gt 0) "Official source $($source.source_id) lacks allowed_hosts."
        foreach ($allowedHostName in @($source.allowed_hosts)) {
            Assert-True ($allowedHostName -notmatch '^\*') "Official source $($source.source_id) uses wildcard host $allowedHostName."
        }
    }
}

$expectedGildataAdapters = @(
    'CR.CONNECTOR.GILDATA.FINANCIAL_RESEARCH', 'CR.CONNECTOR.GILDATA.ANNOUNCEMENT',
    'CR.CONNECTOR.GILDATA.MACRO_INDUSTRY', 'CR.CONNECTOR.GILDATA.RESEARCH',
    'CR.CONNECTOR.GILDATA.NEWS', 'CR.CONNECTOR.GILDATA.ENTERPRISE',
    'CR.CONNECTOR.GILDATA.STOCK_UNIVERSE'
)
$actualGildataAdapters = @($gildata.capabilities.adapter_id)
foreach ($adapterId in $expectedGildataAdapters) {
    Assert-True ($actualGildataAdapters -contains $adapterId) "Missing Gildata adapter $adapterId."
    Assert-True (@($sources | Where-Object adapter_id -eq $adapterId).Count -eq 1) "Source registry missing or duplicates adapter $adapterId."
}
Assert-True (@($gildata.capabilities).Count -eq 7) 'Expected seven Gildata capability adapters.'
Assert-True ($gildata.common_rules.client_side_projection_required -eq $true) 'Gildata client-side field projection must be required.'

$expectedOfficialAdapters = @(
    'CR.CONNECTOR.OFFICIAL.HTTP_DOCUMENT', 'CR.CONNECTOR.OFFICIAL.STATISTICS',
    'CR.CONNECTOR.OFFICIAL.TRADE_STATISTICS', 'CR.CONNECTOR.OFFICIAL.POLICY'
)
foreach ($adapterId in $expectedOfficialAdapters) {
    Assert-True (@($official.adapters.adapter_id) -contains $adapterId) "Missing official adapter $adapterId."
}
Assert-True ($official.common_security.https_only -eq $true) 'Official connectors must use HTTPS only.'
Assert-True ($official.common_security.allowed_host_required -eq $true) 'Official connectors must require allowed hosts.'
Assert-True ($official.common_security.private_ip_and_localhost_blocked -eq $true) 'Official connectors must block private IP and localhost.'

foreach ($field in @($contract.request_schema.required)) {
    Assert-True (-not [string]::IsNullOrWhiteSpace([string]$field)) 'Unified request schema contains blank required field.'
}
foreach ($field in @($contract.response_schema.connector_metadata_required)) {
    Assert-True (@($evidence.connector_result_contract.response_required_fields) -contains $field -or $field -in @('adapter_id','adapter_version','accepted_record_count','discarded_record_count','retry_count','source_ids')) "Connector metadata field $field is not covered by evidence or audit contract."
}
$requiredPipelineSteps = @('validate_requested_date_alignment','validate_unit_scale_lineage','resolve_group_entity_tree','apply_license_gate','apply_point_in_time_gate','apply_scope_whitelists','bind_evidence','run_research_input_validator')
foreach ($step in $requiredPipelineSteps) {
    Assert-True (@($contract.pipeline) -contains $step) "Unified connector pipeline missing $step."
}
Assert-True ($runtimePolicy.execution_algorithm.name -eq 'evidence_first_parallel_dag_v2') 'Runtime v2 parallel DAG policy is missing.'
Assert-True ($runtimePolicy.retry_policy.max_retries -eq 1) 'Runtime v2 must allow at most one retry.'
Assert-True ($runtimePolicy.retry_policy.same_query_retry -eq 'forbidden') 'Runtime v2 must forbid repeating the same broad query.'
Assert-True ([double]$runtimePolicy.sla.minimum_modeled_improvement_vs_serial -ge 0.5) 'Runtime v2 performance acceptance threshold is too low.'

$forbiddenStrings = @((('to' + 'ken') + '='), ('ed82c6584' + 'c824d9ba18aeee99d852317'))
$scanFiles = Get-ChildItem -Path $projectRoot -Recurse -File | Where-Object { $_.Extension -in @('.json','.md','.ps1','.tsx') }
foreach ($needle in $forbiddenStrings) {
    $hits = @($scanFiles | Where-Object { $_.FullName -ne $PSCommandPath } | Select-String -SimpleMatch -Pattern $needle -ErrorAction SilentlyContinue)
    Assert-True ($hits.Count -eq 0) "Secret-like string '$needle' found in workspace."
}

$licensed = @($sources | Where-Object { $_.source_family -like 'licensed*' })
$licensePending = @($licensed | Where-Object { $_.license_status -match 'pending' })
foreach ($source in $licensePending) {
    if ($source.status -ne 'live_connected_license_gate') {
        $warnings.Add("License-pending source $($source.source_id) should remain behind license gate.")
    }
}

$live = @($sources | Where-Object status -like 'live_connected*')
$officialReady = @($sources | Where-Object status -eq 'endpoint_verified_connector_ready')
$families = @($sources.source_family | Sort-Object -Unique)
$result = [ordered]@{
    suite_id = 'CR.TEST.STAGE5.SOURCES.001'
    run_at = [DateTimeOffset]::Now.ToString('o')
    passed = $failures.Count -eq 0
    counts = [ordered]@{
        registered_sources = $sources.Count
        source_families = $families.Count
        live_gildata_capabilities = $live.Count
        official_endpoints_ready = $officialReady.Count
        registered_pending_access = @($registry.registered_pending_access).Count
        license_pending_live_sources = $licensePending.Count
        unified_pipeline_steps = @($contract.pipeline).Count
    }
    failures = $failures.ToArray()
    warnings = $warnings.ToArray()
}
$result | ConvertTo-Json -Depth 12
if ($failures.Count -gt 0) { exit 1 }
