[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$fixtureRoot = Join-Path $projectRoot 'tests\fixtures'
$policyPath = Join-Path $projectRoot 'specs\connectors\research-runtime-policy.v2.json'
$failures = New-Object 'System.Collections.Generic.List[string]'
function Assert-True([bool]$Condition,[string]$Message) { if (-not $Condition) { $script:failures.Add($Message) } }
function Invoke-Guard([string]$FileName) {
    $path = Join-Path $fixtureRoot $FileName
    $text = & (Join-Path $PSScriptRoot 'Test-ResearchGuardrails.ps1') -InputPath $path 2>&1 | Out-String
    $exitCode = $LASTEXITCODE
    return [pscustomobject]@{ exit_code=$exitCode; result=($text | ConvertFrom-Json) }
}

$policy = Get-Content -Raw -Encoding UTF8 $policyPath | ConvertFrom-Json
$valid = Invoke-Guard 'research-guardrails-valid.v2.json'
$dateBad = Invoke-Guard 'research-guardrails-date-mismatch.v2.json'
$scaleBad = Invoke-Guard 'research-guardrails-scale-conflict.v2.json'
$riskBad = Invoke-Guard 'research-guardrails-risk-entity.v2.json'
$reportOnly = Invoke-Guard 'research-guardrails-unlocatable-report.v2.json'

Assert-True ([bool]$valid.result.passed) 'Valid guardrail fixture did not pass.'
Assert-True (@($dateBad.result.issues.code) -contains 'requested_date_mismatch') 'Wrong-year market result was not blocked.'
Assert-True (@($scaleBad.result.issues.code) -contains 'power_of_ten_scale_conflict') '10x unit error was not blocked.'
Assert-True (@($riskBad.result.issues.code) -contains 'risk_entity_unverified') 'Name-only risk entity was not blocked.'
Assert-True ([bool]$reportOnly.result.passed -and @($reportOnly.result.issues.code) -contains 'research_report_downgraded') 'Unlocatable research report was not downgraded to nonblocking view.'

$requestPath = Join-Path $fixtureRoot 'research-execution-request.v2.json'
$planText = & (Join-Path $PSScriptRoot 'New-ResearchExecutionPlan.ps1') -InputPath $requestPath | Out-String
$plan = $planText | ConvertFrom-Json
$taskIds = @($plan.tasks.task_id)
Assert-True ($taskIds -contains 'financial.cr:issuer:popmart' -and $taskIds -contains 'financial.cr:issuer:miniso') 'Financial queries were not partitioned per entity.'
Assert-True (@($plan.tasks | Where-Object { $_.lane -eq 'enrichment' -and $_.required_for_core }).Count -eq 0) 'Enrichment task incorrectly blocks core answer.'
Assert-True ($plan.runtime_rules.progressive_core_answer -eq $true) 'Progressive core answer is not enabled.'
Assert-True ([double]$plan.modeled_latency_seconds.improvement_ratio -ge [double]$policy.sla.minimum_modeled_improvement_vs_serial) 'Modeled latency improvement is below policy threshold.'
Assert-True ([double]$plan.modeled_latency_seconds.optimized_full -le [double]$policy.sla.full_answer_cold_seconds) 'Optimized full-answer modeled latency exceeds SLA.'
Assert-True ($policy.retry_policy.max_retries -eq 1 -and $policy.retry_policy.same_query_retry -eq 'forbidden') 'Bounded split retry policy is not enforced.'
$expectedPresentationOrder = @(
    'research_header',
    'executive_summary',
    'industry_context',
    'comparison_dashboard',
    'company_deep_dives',
    'market_and_events',
    'risks_and_counter_evidence',
    'final_conclusion_and_monitoring',
    'evidence_index',
    'runtime_appendix'
)
Assert-True ((@($policy.output_contract.presentation_order) -join '|') -eq ($expectedPresentationOrder -join '|')) 'Fund-manager output presentation order regressed.'
Assert-True ($policy.output_contract.runtime_appendix_policy.must_be_after_research_answer -eq $true) 'Runtime appendix can interrupt the research answer.'
Assert-True ($policy.output_contract.runtime_appendix_policy.transport_success_separate_from_data_completeness -eq $true) 'Transport success is not separated from field completeness.'
Assert-True (@($policy.output_contract.formatting_rules).Count -ge 8) 'Output formatting contract is incomplete.'

$result = [ordered]@{
    suite_id = 'CR.TEST.RESEARCH.RUNTIME.V2'
    run_at = [DateTimeOffset]::Now.ToString('o')
    passed = $failures.Count -eq 0
    guardrails = [ordered]@{
        valid_passed = [bool]$valid.result.passed
        wrong_year_blocked = @($dateBad.result.issues.code) -contains 'requested_date_mismatch'
        ten_x_scale_blocked = @($scaleBad.result.issues.code) -contains 'power_of_ten_scale_conflict'
        risk_name_match_blocked = @($riskBad.result.issues.code) -contains 'risk_entity_unverified'
        unlocatable_report_downgraded = @($reportOnly.result.issues.code) -contains 'research_report_downgraded'
    }
    performance_model = $plan.modeled_latency_seconds
    task_count = @($plan.tasks).Count
    wave_count = @($plan.waves).Count
    output_contract = [ordered]@{
        contract_id = $policy.output_contract.contract_id
        presentation_order_locked = ((@($policy.output_contract.presentation_order) -join '|') -eq ($expectedPresentationOrder -join '|'))
        runtime_appendix_last = [bool]$policy.output_contract.runtime_appendix_policy.must_be_after_research_answer
    }
    failures = $failures.ToArray()
}
$json = $result | ConvertTo-Json -Depth 15
$outputPath = Join-Path $projectRoot 'tests\research-runtime-v2-report.json'
[IO.File]::WriteAllText($outputPath, $json + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
$json
if ($failures.Count -gt 0) { exit 1 }
