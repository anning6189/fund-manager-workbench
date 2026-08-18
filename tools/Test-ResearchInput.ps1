[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$InputPath,
    [string]$SpecRoot,
    [switch]$FailOnWarning
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($SpecRoot)) {
    $SpecRoot = Join-Path (Split-Path -Parent $PSScriptRoot) 'specs'
}

function Get-PropertyNames([object]$Object) {
    if ($null -eq $Object) { return @() }
    return @($Object.PSObject.Properties.Name)
}

function Test-HasValue([object]$Object, [string]$Name) {
    if ($null -eq $Object) { return $false }
    if ((Get-PropertyNames $Object) -notcontains $Name) { return $false }
    $value = $Object.$Name
    if ($null -eq $value) { return $false }
    if ($value -is [string] -and [string]::IsNullOrWhiteSpace($value)) { return $false }
    if ($value -is [System.Collections.ICollection] -and $value.Count -eq 0) { return $false }
    return $true
}

function Add-Issue {
    param(
        [System.Collections.Generic.List[object]]$List,
        [ValidateSet('error','warning')][string]$Severity,
        [string]$Code,
        [string]$Path,
        [string]$Message
    )
    $List.Add([pscustomobject]@{ severity=$Severity; code=$Code; path=$Path; message=$Message })
}

function Convert-ToTimestamp {
    param([object]$Value, [string]$Path, [System.Collections.Generic.List[object]]$Issues)
    if ($null -eq $Value) { return $null }
    if ([string]$Value -notmatch '(Z|[+-]\d{2}:\d{2})$') {
        Add-Issue $Issues 'error' 'timestamp_offset_missing' $Path '时间戳必须显式包含Z或UTC偏移。'
        return $null
    }
    try {
        return [DateTimeOffset]::Parse([string]$Value, [System.Globalization.CultureInfo]::InvariantCulture)
    } catch {
        Add-Issue $Issues 'error' 'invalid_timestamp' $Path '时间必须是带明确UTC偏移的ISO-8601时间戳。'
        return $null
    }
}

$metricSpec = Get-Content -Raw -Encoding UTF8 (Join-Path $SpecRoot 'consumer-metric-dictionary.v1.json') | ConvertFrom-Json
$evidenceSpec = Get-Content -Raw -Encoding UTF8 (Join-Path $SpecRoot 'research-evidence-standard.v1.json') | ConvertFrom-Json
$input = Get-Content -Raw -Encoding UTF8 $InputPath | ConvertFrom-Json
$issues = New-Object 'System.Collections.Generic.List[object]'
$discarded = New-Object 'System.Collections.Generic.List[object]'
$acceptedEvidenceIds = New-Object 'System.Collections.Generic.HashSet[string]'
$acceptedObservationIds = New-Object 'System.Collections.Generic.HashSet[string]'
$observationById = @{}

$requiredQueryFields = @($evidenceSpec.connector_result_contract.query_required_fields)
foreach ($field in $requiredQueryFields) {
    if (-not (Test-HasValue $input.query $field)) {
        Add-Issue $issues 'error' 'missing_query_field' "query.$field" '缺少连接器查询契约必填字段。'
    }
}

foreach ($field in @($evidenceSpec.connector_result_contract.response_required_fields)) {
    if (-not (Test-HasValue $input.connector_metadata $field)) {
        Add-Issue $issues 'error' 'missing_connector_metadata' "connector_metadata.$field" '缺少连接器响应契约必填字段。'
    }
}
if ((Test-HasValue $input.connector_metadata 'truncated') -and [bool]$input.connector_metadata.truncated) {
    Add-Issue $issues 'error' 'connector_result_truncated' 'connector_metadata.truncated' '连接器结果已截断，必须缩小查询分片后重试，禁止直接作答。'
}
if (Test-HasValue $input.connector_metadata 'retrieved_at') {
    [void](Convert-ToTimestamp $input.connector_metadata.retrieved_at 'connector_metadata.retrieved_at' $issues)
}

$cutoff = $null
if (Test-HasValue $input.query 'cutoff_timestamp') {
    $cutoff = Convert-ToTimestamp $input.query.cutoff_timestamp 'query.cutoff_timestamp' $issues
}

$allowedEntities = @($input.query.allowed_entity_ids)
$allowedSecurities = @($input.query.allowed_security_ids)
$allowedMetrics = @($input.query.allowed_metric_ids)
$allowedPeriods = @($input.query.allowed_periods)
$allowedStatementScopes = @($input.query.allowed_statement_scopes)
$metricById = @{}
foreach ($metric in @($metricSpec.common_metrics) + @($metricSpec.sector_metric_packs | ForEach-Object { $_.metrics })) {
    $metricById[$metric.metric_id] = $metric
}

for ($i = 0; $i -lt @($input.evidence).Count; $i++) {
    $ev = @($input.evidence)[$i]
    $path = "evidence[$i]"
    $missing = @($evidenceSpec.evidence_required_fields | Where-Object { -not (Test-HasValue $ev $_) })
    if ($missing.Count -gt 0) {
        Add-Issue $issues 'error' 'evidence_incomplete' $path ("缺少证据字段：" + ($missing -join ', '))
        continue
    }
    $published = Convert-ToTimestamp $ev.published_at "$path.published_at" $issues
    $available = Convert-ToTimestamp $ev.available_at "$path.available_at" $issues
    $retrieved = Convert-ToTimestamp $ev.retrieved_at "$path.retrieved_at" $issues
    if ($null -eq $published -or $null -eq $available -or $null -eq $retrieved) { continue }
    if ($null -ne $cutoff -and ($published -ge $cutoff -or $available -ge $cutoff)) {
        Add-Issue $issues 'error' 'future_information_leakage' $path '证据的发布时间或可得时间不早于截止时点。'
        $discarded.Add([pscustomobject]@{ kind='evidence'; id=$ev.evidence_id; reason='future_information_leakage' })
        continue
    }
    if ([string]::IsNullOrWhiteSpace([string]$ev.locator) -or $ev.locator -eq 'unknown') {
        Add-Issue $issues 'error' 'unlocatable_citation' "$path.locator" '证据必须定位到页码、段落、表格、公告记录或等价位置。'
        continue
    }
    if ([string]$ev.content_hash -notmatch '^sha256:[0-9a-fA-F]{64}$') {
        Add-Issue $issues 'error' 'invalid_content_hash' "$path.content_hash" '内容哈希必须使用sha256:加64位十六进制格式。'
        continue
    }
    if ([string]$ev.license_tag -match 'unknown|to_confirm|unlicensed') {
        Add-Issue $issues 'error' 'license_unknown_for_required_use' "$path.license_tag" '许可状态不明确，证据不得进入正式研究答案。'
        continue
    }
    [void]$acceptedEvidenceIds.Add([string]$ev.evidence_id)
}

for ($i = 0; $i -lt @($input.observations).Count; $i++) {
    $obs = @($input.observations)[$i]
    $path = "observations[$i]"
    $missing = @($metricSpec.observation_schema.required_fields | Where-Object { -not (Test-HasValue $obs $_) })
    if ($missing.Count -gt 0) {
        Add-Issue $issues 'error' 'observation_incomplete' $path ("缺少观测字段：" + ($missing -join ', '))
        continue
    }
    if ($allowedEntities.Count -gt 0 -and $allowedEntities -notcontains $obs.entity_id) {
        Add-Issue $issues 'warning' 'out_of_scope_entity' "$path.entity_id" '实体不在查询白名单，已丢弃。'
        $discarded.Add([pscustomobject]@{ kind='observation'; id=$obs.observation_id; reason='out_of_scope_entity' })
        continue
    }
    if ((Test-HasValue $obs 'security_id') -and $allowedSecurities.Count -gt 0 -and $allowedSecurities -notcontains $obs.security_id) {
        Add-Issue $issues 'warning' 'out_of_scope_security' "$path.security_id" '证券不在查询白名单，已丢弃。'
        $discarded.Add([pscustomobject]@{ kind='observation'; id=$obs.observation_id; reason='out_of_scope_security' })
        continue
    }
    if ($allowedMetrics.Count -gt 0 -and $allowedMetrics -notcontains $obs.metric_id) {
        Add-Issue $issues 'warning' 'out_of_scope_metric' "$path.metric_id" '指标不在查询白名单，已丢弃。'
        $discarded.Add([pscustomobject]@{ kind='observation'; id=$obs.observation_id; reason='out_of_scope_metric' })
        continue
    }
    if (-not $metricById.ContainsKey([string]$obs.metric_id)) {
        Add-Issue $issues 'error' 'unknown_metric' "$path.metric_id" '指标未在指标字典注册。'
        continue
    }
    $periodKey = "$($obs.period_start)/$($obs.period_end)"
    if ($allowedPeriods.Count -gt 0 -and $allowedPeriods -notcontains $periodKey) {
        Add-Issue $issues 'warning' 'out_of_scope_period' $path '期间不在查询白名单，已丢弃。'
        $discarded.Add([pscustomobject]@{ kind='observation'; id=$obs.observation_id; reason='out_of_scope_period' })
        continue
    }
    if ([string]$obs.metric_id -like 'CR.CO.*') {
        foreach ($field in @($metricSpec.global_rules.financial_statement_policy.required_fields)) {
            if (-not (Test-HasValue $obs $field)) {
                Add-Issue $issues 'error' 'financial_scope_incomplete' "$path.$field" '财务指标缺少报表口径元数据。'
            }
        }
        if ($allowedStatementScopes.Count -gt 0 -and $allowedStatementScopes -notcontains $obs.statement_scope) {
            Add-Issue $issues 'warning' 'out_of_scope_statement' "$path.statement_scope" '报表类型不在查询白名单，已丢弃。'
            $discarded.Add([pscustomobject]@{ kind='observation'; id=$obs.observation_id; reason='out_of_scope_statement' })
            continue
        }
    }
    $published = Convert-ToTimestamp $obs.published_at "$path.published_at" $issues
    $available = Convert-ToTimestamp $obs.available_at "$path.available_at" $issues
    if ($null -eq $published -or $null -eq $available) { continue }
    if ($null -ne $cutoff -and ($published -ge $cutoff -or $available -ge $cutoff)) {
        Add-Issue $issues 'error' 'future_information_leakage' $path '观测值的发布时间或可得时间不早于截止时点。'
        $discarded.Add([pscustomobject]@{ kind='observation'; id=$obs.observation_id; reason='future_information_leakage' })
        continue
    }
    if (-not $acceptedEvidenceIds.Contains([string]$obs.evidence_id)) {
        Add-Issue $issues 'error' 'evidence_not_accepted' "$path.evidence_id" '观测值绑定的证据未通过准入。'
        continue
    }
    [void]$acceptedObservationIds.Add([string]$obs.observation_id)
    $observationById[[string]$obs.observation_id] = $obs
}

for ($i = 0; $i -lt @($input.calculations).Count; $i++) {
    $calc = @($input.calculations)[$i]
    $path = "calculations[$i]"
    foreach ($field in @('calculation_id','metric_id','formula','input_observation_ids','result','unit')) {
        if (-not (Test-HasValue $calc $field)) {
            Add-Issue $issues 'error' 'calculation_incomplete' "$path.$field" '计算记录缺少必填字段。'
        }
    }
    foreach ($id in @($calc.input_observation_ids)) {
        if (-not $acceptedObservationIds.Contains([string]$id)) {
            Add-Issue $issues 'error' 'calculation_input_not_accepted' "$path.input_observation_ids" "计算输入 $id 未通过准入。"
        }
    }
    $inputsAccepted = @($calc.input_observation_ids | Where-Object { -not $acceptedObservationIds.Contains([string]$_) }).Count -eq 0
    if ($inputsAccepted -and (Test-HasValue $calc 'result')) {
        $values = @($calc.input_observation_ids | ForEach-Object { [double]$observationById[[string]$_].value })
        $expected = $null
        switch ([string]$calc.metric_id) {
            'CR.CO.REVENUE_GROWTH_YOY' { if ($values.Count -eq 2 -and $values[1] -ne 0) { $expected = $values[0] / $values[1] - 1 } }
            'CR.CO.GROSS_MARGIN' { if ($values.Count -eq 2 -and $values[0] -ne 0) { $expected = ($values[0] - $values[1]) / $values[0] } }
            'CR.CO.SELLING_EXPENSE_RATIO' { if ($values.Count -eq 2 -and $values[1] -ne 0) { $expected = $values[0] / $values[1] } }
            'CR.CO.CFO_TO_NET_PROFIT' { if ($values.Count -eq 2 -and $values[1] -gt 0) { $expected = $values[0] / $values[1] } }
        }
        if ($null -ne $expected) {
            $tolerance = if (Test-HasValue $calc 'tolerance') { [double]$calc.tolerance } else { 0.000001 }
            if ([Math]::Abs([double]$calc.result - $expected) -gt $tolerance) {
                Add-Issue $issues 'error' 'calculation_result_mismatch' "$path.result" "计算结果与输入复算不一致；expected=$expected，actual=$($calc.result)，tolerance=$tolerance。"
            }
        }
    }
}

if (Test-HasValue $input 'comparison') {
    if (-not (Test-HasValue $input.comparison 'decision_rule') -and [string]$input.comparison.conclusion_type -eq 'single_winner') {
        Add-Issue $issues 'error' 'single_winner_without_decision_rule' 'comparison' '无明确决策规则、权重和期间时，不得输出单一赢家。'
    }
    foreach ($field in @('dimension_results','supporting_evidence','counter_evidence','comparability_limits','cannot_conclude','pm_judgment_required')) {
        if (-not (Test-HasValue $input.comparison $field)) {
            Add-Issue $issues 'error' 'comparison_section_missing' "comparison.$field" '公司比较缺少有限结论政策要求的部分。'
        }
    }
}

$errors = @($issues | Where-Object severity -eq 'error')
$warnings = @($issues | Where-Object severity -eq 'warning')
$passed = $errors.Count -eq 0 -and (-not $FailOnWarning -or $warnings.Count -eq 0)
$result = [ordered]@{
    validator_version = '1.0.0'
    input_path = (Resolve-Path $InputPath).Path
    passed = $passed
    summary = [ordered]@{
        accepted_evidence = $acceptedEvidenceIds.Count
        accepted_observations = $acceptedObservationIds.Count
        discarded_records = $discarded.Count
        errors = $errors.Count
        warnings = $warnings.Count
    }
    issues = $issues.ToArray()
    discarded = $discarded.ToArray()
}
$result | ConvertTo-Json -Depth 12
if (-not $passed) { exit 1 }
