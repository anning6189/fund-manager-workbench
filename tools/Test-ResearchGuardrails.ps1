[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$InputPath,
    [string]$PolicyPath
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($PolicyPath)) {
    $PolicyPath = Join-Path $projectRoot 'specs\connectors\research-runtime-policy.v2.json'
}
$policy = Get-Content -Raw -Encoding UTF8 $PolicyPath | ConvertFrom-Json
$payload = Get-Content -Raw -Encoding UTF8 $InputPath | ConvertFrom-Json
$issues = New-Object 'System.Collections.Generic.List[object]'
$quarantine = New-Object 'System.Collections.Generic.List[object]'

function Add-Issue([string]$Severity, [string]$Code, [string]$Path, [string]$Message) {
    $script:issues.Add([pscustomobject]@{ severity=$Severity; code=$Code; path=$Path; message=$Message })
}
function Add-Quarantine([string]$Kind, [string]$Id, [string]$Reason) {
    $script:quarantine.Add([pscustomobject]@{ kind=$Kind; id=$Id; reason=$Reason })
}
function Test-GuardHasValue([object]$Object, [string]$Name) {
    if ($null -eq $Object -or $Object.PSObject.Properties.Name -notcontains $Name) { return $false }
    $value = $Object.$Name
    if ($null -eq $value) { return $false }
    if ($value -is [string] -and [string]::IsNullOrWhiteSpace($value)) { return $false }
    return $true
}

# 1. Requested date must match returned market dates.
$mode = [string]$payload.query.date_alignment_mode
$marketRows = [object[]]$payload.PSObject.Properties['market_observations'].Value
if ($mode -eq 'exact_date') {
    $requested = [string]$payload.query.requested_date
    foreach ($row in $marketRows) {
        if ([string]$row.as_of_date -ne $requested) {
            Add-Issue 'error' 'requested_date_mismatch' "market_observations.$($row.observation_id).as_of_date" "请求日期为$requested，返回日期为$($row.as_of_date)。"
            Add-Quarantine 'market_observation' ([string]$row.observation_id) 'requested_date_mismatch'
        }
    }
} elseif ($mode -eq 'date_range') {
    $start = [DateTime]::Parse([string]$payload.query.date_start)
    $end = [DateTime]::Parse([string]$payload.query.date_end)
    foreach ($row in $marketRows) {
        $date = [DateTime]::Parse([string]$row.as_of_date)
        if ($date -lt $start -or $date -gt $end) {
            Add-Issue 'error' 'returned_date_outside_range' "market_observations.$($row.observation_id).as_of_date" '行情记录超出请求区间。'
            Add-Quarantine 'market_observation' ([string]$row.observation_id) 'returned_date_outside_range'
        }
    }
}

# 2. Every normalized financial value must be reproducible from raw value and unit.
$normalizedRows = [object[]]$payload.PSObject.Properties['normalized_observations'].Value
foreach ($row in $normalizedRows) {
    foreach ($field in @($policy.correctness_guards.unit_and_scale.raw_fields_required)) {
        if (-not (Test-GuardHasValue $row $field)) {
            Add-Issue 'error' 'unit_lineage_incomplete' "normalized_observations.$($row.observation_id).$field" '标准化数值缺少原值、原单位或换算因子。'
        }
    }
    if ((Test-GuardHasValue $row 'raw_value') -and (Test-GuardHasValue $row 'normalization_factor') -and (Test-GuardHasValue $row 'normalized_value')) {
        $expected = [double]$row.raw_value * [double]$row.normalization_factor
        $actual = [double]$row.normalized_value
        $denominator = [Math]::Max([Math]::Abs($expected), 1e-12)
        if ([Math]::Abs($actual - $expected) / $denominator -gt [double]$policy.correctness_guards.unit_and_scale.recompute_tolerance_ratio) {
            Add-Issue 'error' 'normalization_result_mismatch' "normalized_observations.$($row.observation_id).normalized_value" "单位换算不可复算；expected=$expected actual=$actual。"
            Add-Quarantine 'normalized_observation' ([string]$row.observation_id) 'normalization_result_mismatch'
        }
    }
}

# 3. Material cross-source conflicts and common 10x/100x errors are blocked.
$groups = @($normalizedRows | Group-Object -Property canonical_key)
for ($groupIndex = 0; $groupIndex -lt $groups.Count; $groupIndex++) {
    $rows = @($groups[$groupIndex].Group | ForEach-Object { $_ })
    if ($rows.Count -lt 2) { continue }
    $officialRow = $null
    foreach ($candidate in $rows) { if ([string]$candidate.evidence_tier -eq 'A') { $officialRow = $candidate; break } }
    if ($null -eq $officialRow) { continue }
    $base = [double]$officialRow.normalized_value
    foreach ($row in $rows) {
        if ([string]$row.observation_id -eq [string]$officialRow.observation_id) { continue }
        $value = [double]$row.normalized_value
        $difference = [Math]::Abs($value - $base) / [Math]::Max([Math]::Abs($base), 1e-12)
        if ($difference -gt [double]$policy.correctness_guards.unit_and_scale.material_cross_source_difference_ratio) {
            $ratio = [Math]::Max([Math]::Abs($value), [Math]::Abs($base)) / [Math]::Max([Math]::Min([Math]::Abs($value), [Math]::Abs($base)), 1e-12)
            $nearestPower = [Math]::Pow(10, [Math]::Round([Math]::Log10($ratio)))
            $powerTen = [Math]::Abs($ratio - $nearestPower) / $nearestPower -le [double]$policy.correctness_guards.unit_and_scale.power_of_ten_tolerance_ratio
            $code = if ($powerTen) { 'power_of_ten_scale_conflict' } else { 'material_cross_source_conflict' }
            Add-Issue 'error' $code "normalized_observations.$($row.observation_id)" "与A级官方值存在重大冲突；official=$base candidate=$value ratio=$ratio。"
            Add-Quarantine 'normalized_observation' ([string]$row.observation_id) $code
        }
    }
}

# 4. Risk events need verified group membership, identifier, actor role and materiality.
$allowedRelationships = @($policy.correctness_guards.entity_resolution.allowed_group_relationships)
$riskRows = [object[]]$payload.PSObject.Properties['risk_events'].Value
for ($riskIndex = 0; $riskIndex -lt $riskRows.Count; $riskIndex++) {
    $event = $riskRows[$riskIndex]
    foreach ($field in @($policy.correctness_guards.entity_resolution.risk_event_required)) {
        if (-not (Test-GuardHasValue $event $field)) {
            Add-Issue 'error' 'risk_entity_lineage_incomplete' "risk_events.$($event.event_id).$field" '企业风险事件缺少实体树、角色或重大性字段。'
        }
    }
    $resolutionMethod = [string]$event.resolution_method
    $groupRelationship = [string]$event.group_relationship
    if (($resolutionMethod -eq 'name_match_only') -or (-not ($allowedRelationships -contains $groupRelationship))) {
        Add-Issue 'error' 'risk_entity_unverified' "risk_events.$($event.event_id)" '仅名称命中或集团关系未验证，不得进入上市公司核心风险结论。'
        Add-Quarantine 'risk_event' ([string]$event.event_id) 'risk_entity_unverified'
    }
}

# 5. Core facts require original evidence; reports without originals are enrichment only.
$claimRows = [object[]]$payload.PSObject.Properties['claims'].Value
$documentRows = [object[]]$payload.PSObject.Properties['documents'].Value
for ($claimIndex = 0; $claimIndex -lt $claimRows.Count; $claimIndex++) {
    $claim = $claimRows[$claimIndex]
    $matchedDocument = $null
    foreach ($candidateDocument in $documentRows) {
        if ([string]$candidateDocument.document_id -eq [string]$claim.document_id) { $matchedDocument = $candidateDocument; break }
    }
    if ($null -eq $matchedDocument) {
        Add-Issue 'error' 'claim_document_missing' "claims.$($claim.claim_id)" '主张没有绑定文档。'
        continue
    }
    $hasStableId = (Test-GuardHasValue $matchedDocument 'source_url') -or (Test-GuardHasValue $matchedDocument 'stable_document_id')
    $hasLocator = (Test-GuardHasValue $matchedDocument 'locator') -and [string]$matchedDocument.locator -ne 'unknown'
    if ([string]$claim.importance -eq 'core' -and [string]$claim.content_label -match '^FACT_' -and (-not $hasStableId -or -not $hasLocator)) {
        Add-Issue 'error' 'core_fact_unlocatable' "claims.$($claim.claim_id)" '核心事实缺少稳定原文标识或定位。'
        Add-Quarantine 'claim' ([string]$claim.claim_id) 'core_fact_unlocatable'
    } elseif ([string]$matchedDocument.source_type -eq 'research_report' -and (-not $hasStableId -or -not $hasLocator)) {
        Add-Issue 'warning' 'research_report_downgraded' "documents.$($matchedDocument.document_id)" '研报缺少稳定原文，只能作为非阻塞第三方观点。'
    }
}

# 6. N-session return requires a baseline before the N returned sessions and is recomputed.
$marketCalculationRows = [object[]]$payload.PSObject.Properties['market_calculations'].Value
foreach ($calc in $marketCalculationRows) {
    foreach ($field in @('window_size','observation_count','baseline_date','baseline_close','end_date','end_close','result')) {
        if (-not (Test-GuardHasValue $calc $field)) { Add-Issue 'error' 'market_return_lineage_incomplete' "market_calculations.$($calc.calculation_id).$field" 'N日收益率缺少交易日数量、基线或终值。' }
    }
    if ((Test-GuardHasValue $calc 'window_size') -and (Test-GuardHasValue $calc 'observation_count') -and [int]$calc.window_size -ne [int]$calc.observation_count) {
        Add-Issue 'error' 'market_window_count_mismatch' "market_calculations.$($calc.calculation_id).observation_count" '行情序列数量与N日窗口不一致。'
    }
    if ((Test-GuardHasValue $calc 'baseline_close') -and (Test-GuardHasValue $calc 'end_close') -and (Test-GuardHasValue $calc 'result')) {
        $expected = [double]$calc.end_close / [double]$calc.baseline_close - 1
        if ([Math]::Abs($expected - [double]$calc.result) -gt 0.000001) {
            Add-Issue 'error' 'market_return_result_mismatch' "market_calculations.$($calc.calculation_id).result" "N日收益率复算不一致；expected=$expected actual=$($calc.result)。"
        }
    }
}

$errors = @($issues | Where-Object severity -eq 'error')
$result = [ordered]@{
    suite_id = 'CR.TEST.RESEARCH.GUARDRAILS.V2'
    validator_version = '2.0.0'
    input_path = (Resolve-Path $InputPath).Path
    passed = $errors.Count -eq 0
    summary = [ordered]@{
        errors=$errors.Count; warnings=@($issues | Where-Object severity -eq 'warning').Count; quarantined=$quarantine.Count;
        market_observations=$marketRows.Count; normalized_observations=$normalizedRows.Count;
        risk_events=@($payload.risk_events).Count; documents=@($payload.documents).Count; claims=@($payload.claims).Count
    }
    issues = $issues.ToArray()
    quarantine = $quarantine.ToArray()
}
$result | ConvertTo-Json -Depth 12
if ($errors.Count -gt 0) { exit 1 }
