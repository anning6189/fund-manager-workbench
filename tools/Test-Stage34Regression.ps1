[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$validator = Join-Path $PSScriptRoot 'Test-ResearchInput.ps1'
$validFixture = Join-Path $projectRoot 'tests\fixtures\company-comparison-valid.v1.json'
$futureFixture = Join-Path $projectRoot 'tests\fixtures\company-comparison-future-leak.v1.json'
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("consumer-research-stage34-" + [guid]::NewGuid().ToString('N'))
[void](New-Item -ItemType Directory -Path $tempRoot)
$results = New-Object 'System.Collections.Generic.List[object]'

function Invoke-Validator([string]$Path) {
    $output = & powershell -NoProfile -ExecutionPolicy Bypass -File $validator -InputPath $Path 2>&1
    $exitCode = $LASTEXITCODE
    $text = ($output | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
    [pscustomobject]@{ exit_code=$exitCode; payload=($text | ConvertFrom-Json) }
}

function Add-Result([string]$Name, [bool]$Passed, [string]$Expected, [string]$Actual) {
    $results.Add([pscustomobject]@{ name=$Name; passed=$Passed; expected=$Expected; actual=$Actual })
}

function Write-TempFixture([string]$Name, [object]$Value) {
    $path = Join-Path $tempRoot $Name
    $json = $Value | ConvertTo-Json -Depth 40
    [IO.File]::WriteAllText($path, $json, (New-Object Text.UTF8Encoding($false)))
    return $path
}

try {
    $specOutput = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'Test-Stage34Specs.ps1') 2>&1
    $specExit = $LASTEXITCODE
    $specText = ($specOutput | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
    $specPayload = $specText | ConvertFrom-Json
    Add-Result '规格完整性' ($specExit -eq 0 -and $specPayload.passed) 'pass' "exit=$specExit, passed=$($specPayload.passed)"

    $valid = Invoke-Validator $validFixture
    Add-Result '合格历史比较用例' ($valid.exit_code -eq 0 -and $valid.payload.passed -and $valid.payload.summary.errors -eq 0) 'pass with 0 errors' "exit=$($valid.exit_code), errors=$($valid.payload.summary.errors)"

    $future = Invoke-Validator $futureFixture
    $futureCodes = @($future.payload.issues.code)
    Add-Result '未来信息穿越阻断' ($future.exit_code -ne 0 -and $futureCodes -contains 'future_information_leakage') 'hard block future_information_leakage' ($futureCodes -join ',')

    $base = Get-Content -Raw -Encoding UTF8 $validFixture | ConvertFrom-Json
    $base.connector_metadata.truncated = $true
    $truncated = Invoke-Validator (Write-TempFixture 'truncated.json' $base)
    $truncatedCodes = @($truncated.payload.issues.code)
    Add-Result '截断响应阻断' ($truncated.exit_code -ne 0 -and $truncatedCodes -contains 'connector_result_truncated') 'hard block connector_result_truncated' ($truncatedCodes -join ',')

    $base = Get-Content -Raw -Encoding UTF8 $validFixture | ConvertFrom-Json
    $base.comparison.conclusion_type = 'single_winner'
    $base.comparison.decision_rule = $null
    $winner = Invoke-Validator (Write-TempFixture 'single-winner.json' $base)
    $winnerCodes = @($winner.payload.issues.code)
    Add-Result '无规则单一赢家阻断' ($winner.exit_code -ne 0 -and $winnerCodes -contains 'single_winner_without_decision_rule') 'hard block single_winner_without_decision_rule' ($winnerCodes -join ',')

    $base = Get-Content -Raw -Encoding UTF8 $validFixture | ConvertFrom-Json
    $extra = $base.observations[0].PSObject.Copy()
    $extra.observation_id = 'obs:out-of-scope'
    $extra.entity_id = 'cr:legal_entity:out-of-scope'
    $base.observations += $extra
    $scope = Invoke-Validator (Write-TempFixture 'out-of-scope.json' $base)
    $scopeCodes = @($scope.payload.issues.code)
    Add-Result '越界实体后过滤' ($scope.exit_code -eq 0 -and $scopeCodes -contains 'out_of_scope_entity' -and $scope.payload.summary.discarded_records -eq 1) 'discard and log, answer remains valid' "exit=$($scope.exit_code), discarded=$($scope.payload.summary.discarded_records), codes=$($scopeCodes -join ',')"

    $base = Get-Content -Raw -Encoding UTF8 $validFixture | ConvertFrom-Json
    $base.calculations[0].result = 0.5
    $calc = Invoke-Validator (Write-TempFixture 'bad-calculation.json' $base)
    $calcCodes = @($calc.payload.issues.code)
    Add-Result '错误计算阻断' ($calc.exit_code -ne 0 -and $calcCodes -contains 'calculation_result_mismatch') 'hard block calculation_result_mismatch' ($calcCodes -join ',')

    $base = Get-Content -Raw -Encoding UTF8 $validFixture | ConvertFrom-Json
    $base.evidence[0].locator = 'unknown'
    $locator = Invoke-Validator (Write-TempFixture 'bad-locator.json' $base)
    $locatorCodes = @($locator.payload.issues.code)
    Add-Result '不可定位证据阻断' ($locator.exit_code -ne 0 -and $locatorCodes -contains 'unlocatable_citation') 'hard block unlocatable_citation' ($locatorCodes -join ',')

    $failed = @($results | Where-Object { -not $_.passed })
    $summary = [ordered]@{
        suite_id = 'CR.REGRESSION.S3S4.001'
        run_at = [DateTimeOffset]::Now.ToString('o')
        passed = $failed.Count -eq 0
        total = $results.Count
        passed_count = @($results | Where-Object passed).Count
        failed_count = $failed.Count
        results = $results.ToArray()
    }
    $summary | ConvertTo-Json -Depth 12
    if ($failed.Count -gt 0) { exit 1 }
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        $resolvedTemp = [IO.Path]::GetFullPath($tempRoot)
        $allowedPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
        if ($resolvedTemp.StartsWith($allowedPrefix, [StringComparison]::OrdinalIgnoreCase) -and (Split-Path -Leaf $resolvedTemp) -like 'consumer-research-stage34-*') {
            Remove-Item -LiteralPath $resolvedTemp -Recurse -Force
        }
    }
}

