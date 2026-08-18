[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$domain = Get-Content -Raw -Encoding UTF8 (Join-Path $projectRoot 'specs\consumer-domain-model.v1.json') | ConvertFrom-Json
$metrics = Get-Content -Raw -Encoding UTF8 (Join-Path $projectRoot 'specs\consumer-metric-dictionary.v1.json') | ConvertFrom-Json
$evidence = Get-Content -Raw -Encoding UTF8 (Join-Path $projectRoot 'specs\research-evidence-standard.v1.json') | ConvertFrom-Json

$failures = New-Object 'System.Collections.Generic.List[string]'
function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { $script:failures.Add($Message) }
}

$sectors = @($domain.taxonomy | ForEach-Object { $_.children })
$subcategories = @($sectors | ForEach-Object { $_.children })
Assert-True ($sectors.Count -eq 11) "Expected 11 sectors; got $($sectors.Count)."
Assert-True ($subcategories.Count -eq 73) "Expected 73 subcategories; got $($subcategories.Count)."
Assert-True (@($subcategories.code | Sort-Object -Unique).Count -eq 73) 'Subcategory codes are not unique.'
Assert-True (@($subcategories | Where-Object { $_.code -notmatch '^CR\.[SDV]\.[A-Z]{2}\.[A-Z]{2}$' }).Count -eq 0) 'Subcategory code format failed.'
Assert-True (@($subcategories | Where-Object { $_.level -ne 3 -or -not $_.valid_from -or -not $_.status }).Count -eq 0) 'Subcategory required fields missing.'

$allMetrics = @($metrics.common_metrics) + @($metrics.sector_metric_packs | ForEach-Object { $_.metrics })
$professional = @($metrics.sector_metric_packs | ForEach-Object { $_.metrics })
$requiredMetricFields = @($metrics.required_fields)
Assert-True ($professional.Count -eq 64) "Expected 64 professional metrics; got $($professional.Count)."
Assert-True (@($allMetrics.metric_id | Sort-Object -Unique).Count -eq $allMetrics.Count) 'Metric IDs are not unique.'
foreach ($metric in $allMetrics) {
    foreach ($field in $requiredMetricFields) {
        Assert-True ($metric.PSObject.Properties.Name -contains $field -and $null -ne $metric.$field -and [string]$metric.$field -ne '') "Metric $($metric.metric_id) missing $field."
    }
}
$requiredFinancialInputs = @('CR.CO.REVENUE','CR.CO.OPERATING_COST','CR.CO.SELLING_EXPENSE','CR.CO.CFO_NET','CR.CO.PARENT_NET_PROFIT')
foreach ($id in $requiredFinancialInputs) {
    Assert-True (@($allMetrics.metric_id) -contains $id) "Financial input metric $id is not registered."
}

Assert-True (@($evidence.p0_templates).Count -eq 5) 'Expected 5 P0 templates.'
Assert-True (@($evidence.p0_templates | Where-Object {
    @($_.required_input_fields).Count -eq 0 -or @($_.required_output_fields).Count -eq 0 -or @($_.evidence_requirements).Count -eq 0 -or @($_.validation_rules).Count -eq 0
}).Count -eq 0) 'One or more P0 templates lack executable schema fields.'
Assert-True ($evidence.point_in_time_control.cutoff_format -match 'ISO-8601') 'Point-in-time cutoff format is missing.'
Assert-True (@($evidence.connector_result_contract.deterministic_post_filters).Count -ge 5) 'Deterministic connector filters are incomplete.'
Assert-True ($evidence.comparative_conclusion_policy.default_without_rule -ne $null) 'Comparison conclusion policy missing.'

$summary = [ordered]@{
    passed = $failures.Count -eq 0
    versions = [ordered]@{ domain=$domain.spec_version; metrics=$metrics.spec_version; evidence=$evidence.spec_version }
    counts = [ordered]@{
        sectors=$sectors.Count
        subcategories=$subcategories.Count
        common_metrics=@($metrics.common_metrics).Count
        professional_metrics=$professional.Count
        p0_templates=@($evidence.p0_templates).Count
        evidence_required_fields=@($evidence.evidence_required_fields).Count
    }
    failures = @($failures)
}
$summary | ConvertTo-Json -Depth 10
if ($failures.Count -gt 0) { exit 1 }

