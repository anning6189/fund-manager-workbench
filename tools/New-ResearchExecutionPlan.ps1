[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$InputPath,
    [string]$OutputPath,
    [string]$PolicyPath
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($PolicyPath)) { $PolicyPath = Join-Path $projectRoot 'specs\connectors\research-runtime-policy.v2.json' }
$policy = Get-Content -Raw -Encoding UTF8 $PolicyPath | ConvertFrom-Json
$request = Get-Content -Raw -Encoding UTF8 $InputPath | ConvertFrom-Json
$probe = Get-Content -Raw -Encoding UTF8 (Join-Path $projectRoot 'tests\stage-5-gildata-live-probe.v1.json') | ConvertFrom-Json
$latency = @{}
foreach ($p in @($probe.probes)) { $latency[[string]$p.tool] = [double]$p.latency_seconds }
$latency['MarketData'] = if ($latency.ContainsKey('FinQuery')) { $latency['FinQuery'] } else { 12.0 }
$latency['OfficialDocument'] = 10.0
$latency['OfficialMacro'] = 5.0
$latency['Compose'] = 8.0

$tasks = New-Object 'System.Collections.Generic.List[object]'
function Add-Task([string]$Id,[string]$Lane,[string]$Tool,[int]$Priority,[string[]]$DependsOn,[bool]$CacheHit,[string]$Partition,[string[]]$Projection,[bool]$Required) {
    $seconds = if ($CacheHit) { 0.05 } elseif ($latency.ContainsKey($Tool)) { [double]$latency[$Tool] } else { 10.0 }
    $script:tasks.Add([pscustomobject]@{
        task_id=$Id; lane=$Lane; tool=$Tool; priority=$Priority; depends_on=$DependsOn; cache_hit=$CacheHit;
        estimated_seconds=$seconds; partition=$Partition; field_projection=$Projection; required_for_core=$Required
    })
}

$cache = $request.cache_state
$entityIds = @($request.entities | ForEach-Object { [string]$_.entity_id })
$securityIds = @($request.entities | ForEach-Object { [string]$_.security_id })
Add-Task 'entity.resolve' 'core' 'EntityCache' 100 @() ([bool]$cache.entity_resolution) ('entities=' + ($entityIds -join ',')) @('entity_id','security_id','legal_identifier','group_tree') $true

foreach ($entity in @($request.entities)) {
    $entityId = [string]$entity.entity_id
    $securityId = [string]$entity.security_id
    Add-Task "financial.$entityId" 'core' 'FinQuery' 95 @('entity.resolve') ([bool]$cache.historical_financials) "entity=$entityId;periods<=2;metrics<=6" @('revenue','gross_profit','parent_net_profit','cfo_net','inventory','overseas_revenue') $true
    Add-Task "announcement.$entityId" 'core' 'AnnouncementData' 85 @('entity.resolve') ([bool]$cache.announcement_index) "issuer=$securityId;days<=100;types<=3" @('title','published_at','document_type','official_url','announcement_id') $true
    Add-Task "research.$entityId" 'enrichment' 'FinancialResearchReport' 35 @('entity.resolve') ([bool]$cache.research_metadata) "company=$entityId;days<=120;max_reports=3" @('title','publisher','published_at','original_url','locator','view_summary','risk_summary') $false
    Add-Task "risk.$entityId" 'enrichment' 'IcEnterpriseDataQuery' 40 @('entity.resolve') ([bool]$cache.enterprise_risk) "legal_entity=$entityId;event_types<=4" @('legal_identifier','group_relationship','event_role','amount','status','official_locator') $false
}
Add-Task 'market.range' 'core' 'MarketData' 90 @('entity.resolve') ([bool]$cache.completed_market_bars) ('securities=' + ($securityIds -join ',') + ';N_plus_baseline') @('date','previous_close','open','high','low','close','volume','turnover') $true
Add-Task 'macro.official' 'core' 'OfficialMacro' 80 @() ([bool]$cache.macro_releases) 'indicators<=4;periods<=4' @('indicator_code','period','value','unit','release_date','official_url') $true

# Official documents are read only on cache miss or after a material conflict. The plan records the conditional branch instead of eagerly fetching every PDF.
$financialAndAnnouncementIds = @($tasks | Where-Object { $_.task_id -like 'financial.*' -or $_.task_id -like 'announcement.*' } | ForEach-Object task_id)
Add-Task 'official.conflict_verification' 'core' 'OfficialDocument' 70 $financialAndAnnouncementIds ([bool]$cache.official_page_extracts) 'conditional=material_conflict_or_missing_core_locator' @('source_url','content_hash','page_locator','table_extract') $true
$composeDependencies = @($financialAndAnnouncementIds) + @('market.range','macro.official','official.conflict_verification')
Add-Task 'compose.core' 'core' 'Compose' 10 $composeDependencies $false 'validated_claim_graph_only' @('facts','calculations','inferences','gaps','evidence_index') $true

# Bounded list scheduling: cached tasks consume no vendor slot; noncritical tasks never delay compose.core.
$max = [int]$policy.execution_algorithm.global_max_in_flight
$remaining = @($tasks | ForEach-Object { $_ })
$waves = New-Object 'System.Collections.Generic.List[object]'
$completed = New-Object 'System.Collections.Generic.HashSet[string]'
$waveNumber = 0
while ($remaining.Count -gt 0) {
    $ready = @($remaining | Where-Object {
        $deps = @($_.depends_on)
        @($deps | Where-Object { -not $completed.Contains([string]$_) }).Count -eq 0
    } | Sort-Object @{Expression='priority';Descending=$true}, task_id)
    if ($ready.Count -eq 0) { throw 'Execution plan contains a dependency cycle.' }
    $batch = @($ready | Select-Object -First $max)
    $waveNumber++
    $duration = (@($batch.estimated_seconds | Measure-Object -Maximum).Maximum)
    $waves.Add([pscustomobject]@{ wave=$waveNumber; estimated_seconds=[Math]::Round([double]$duration,2); task_ids=@($batch.task_id) })
    foreach ($task in $batch) { [void]$completed.Add([string]$task.task_id) }
    $batchIds = @($batch.task_id)
    $remaining = @($remaining | Where-Object { $batchIds -notcontains $_.task_id })
}

$coreIds = @($tasks | Where-Object required_for_core | ForEach-Object task_id)
$modeledCore = 0.0
foreach ($wave in @($waves | ForEach-Object { $_ })) {
    $waveTaskIds = @($wave.task_ids)
    $coreTasksInWave = @($tasks | Where-Object { $coreIds -contains $_.task_id -and $waveTaskIds -contains $_.task_id })
    if ($coreTasksInWave.Count -gt 0) {
        $modeledCore += [double](@($coreTasksInWave.estimated_seconds | Measure-Object -Maximum).Maximum)
    }
}
$modeledFull = [double](@($waves.estimated_seconds | Measure-Object -Sum).Sum)
$serial = [double](@($tasks | Where-Object { -not $_.cache_hit -and $_.tool -ne 'Compose' } | Measure-Object estimated_seconds -Sum).Sum) + $latency['Compose']
$improvement = if ($serial -gt 0) { 1 - $modeledFull / $serial } else { 0 }

$result = [ordered]@{
    plan_id = "CR.PLAN.$($request.request_id)"
    policy_version = $policy.spec_version
    cutoff_timestamp = $request.cutoff_timestamp
    max_in_flight = $max
    tasks = $tasks.ToArray()
    waves = $waves.ToArray()
    modeled_latency_seconds = [ordered]@{
        legacy_serial = [Math]::Round($serial,2)
        optimized_core = [Math]::Round($modeledCore,2)
        optimized_full = [Math]::Round($modeledFull,2)
        improvement_ratio = [Math]::Round($improvement,4)
        basis = 'stage-5 measured connector probe latency; deterministic critical-path model, not a live SLA measurement'
    }
    runtime_rules = [ordered]@{
        progressive_core_answer = $true
        enrichment_may_timeout_without_blocking = $true
        official_fetch_is_conflict_driven = $true
        same_broad_query_retry_forbidden = $true
    }
}
$json = $result | ConvertTo-Json -Depth 15
if (-not [string]::IsNullOrWhiteSpace($OutputPath)) {
    [IO.File]::WriteAllText($OutputPath, $json + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
}
$json
